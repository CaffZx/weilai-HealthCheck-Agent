from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine, bindparam

from clients.mcp_client import ReusableMcpSessionClient
from core.operating_unit import OperatingUnitBinding
from integrations.pangolinfo_product import is_amazon_product_image_url
from integrations.product_image import MySqlProductImageService
from integrations.repositories.tables import patrol_fact_snapshot, patrol_product_image
from web.backend.deps import get_database_engine

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "artifacts/data-coverage/patrol-unit-enrichment.json"
PRODUCT_TOOL = "erp_listing_product_info"


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _latest_units(connection: sa.Connection, batch_ids: list[str]) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.text(
            """
            WITH candidates AS (
                SELECT j.shop_id,j.site_code,j.parent_asin,j.parent_seller_sku,
                       j.payload_json,j.result_ref,j.status,
                       ROW_NUMBER() OVER (
                           PARTITION BY j.shop_id,j.parent_asin,j.parent_seller_sku
                           ORDER BY j.created_at DESC,j.job_id DESC
                       ) AS rank_no
                FROM t_patrol_job j
                WHERE j.batch_id IN :batch_ids
            )
            SELECT * FROM candidates WHERE rank_no=1
            ORDER BY parent_asin,shop_id,parent_seller_sku
            """
        ).bindparams(bindparam("batch_ids", expanding=True)),
        {"batch_ids": batch_ids},
    ).mappings().all()
    units = []
    for row in rows:
        unit = dict(row)
        payload = _json(unit["payload_json"]) or {}
        unit["binding"] = payload.get("binding") or {}
        unit["children"] = []
        if unit["result_ref"]:
            snapshot = connection.execute(
                sa.select(patrol_fact_snapshot.c.normalized_summary_json).where(
                    patrol_fact_snapshot.c.run_id == unit["result_ref"]
                )
            ).scalar_one_or_none()
            normalized = _json(snapshot) or {}
            unit["children"] = list(
                (normalized.get("identity") or {}).get("children") or []
            )
        units.append(unit)
    return units


def _cached_image_ids(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(
            sa.select(patrol_product_image.c.operating_unit_id)
        ).scalars())


def _binding(unit: dict[str, Any]) -> OperatingUnitBinding:
    source = unit["binding"]
    return OperatingUnitBinding(
        shop_id=unit["shop_id"],
        shop_account=source.get("shop_account") or source.get("shopAccount") or "unknown",
        site_code=unit["site_code"],
        parent_asin=unit["parent_asin"],
        parent_seller_sku=unit["parent_seller_sku"],
    )


def _child_candidates(unit: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for child in unit["children"]:
        asin = str(child.get("child_asin") or "").strip().upper()
        sku = str(child.get("seller_sku") or "").strip()
        if asin and sku:
            rows.append((asin, sku))
    return list(dict.fromkeys(rows))


def _principal_id(row: dict[str, Any]) -> int | None:
    value = (
        row.get("principalUserId")
        or row.get("ASIN_PRINCIPAL_USER_ID")
        or row.get("asinPrincipalUserId")
    )
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _product_arguments(binding: OperatingUnitBinding) -> dict[str, str]:
    return {
        "paramsJson": json.dumps(
            {
                "shopAccount": binding.shop_account,
                "parentAsin": binding.parent_asin,
                "parentSellerSku": binding.parent_seller_sku,
                "qryFiveBulletPoint": True,
            },
            ensure_ascii=False,
        ),
    }


def _main_image(rows: list[dict[str, Any]]) -> str | None:
    preferred = [row for row in rows if str(row.get("flowInlet") or "").strip() == "是"]
    for row in [*preferred, *rows]:
        value = row.get("picUrl")
        if is_amazon_product_image_url(value):
            return str(value).strip()
    return None


async def _retry_database_write(operation: Any, *, attempts: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            operation()
        except (sa.exc.DBAPIError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(attempt * 2)
        else:
            return
    assert last_error is not None
    raise last_error


async def run(
    batch_ids: list[str],
    *,
    apply: bool,
    limit: int | None,
    interval_seconds: float,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    token = _required_environment("MCP_API_KEY")
    engine = get_database_engine()
    with engine.connect() as connection:
        units = _latest_units(connection, batch_ids)
    if limit is not None:
        units = units[:limit]

    product_client = ReusableMcpSessionClient(
        os.environ.get("AZLISTING_GATEWAY", "http://mcp-gateway.example.com/mcp"),
        token,
        timeout_seconds=90,
        allowed_tools=frozenset({PRODUCT_TOOL}),
    )
    image_service = MySqlProductImageService(engine)
    cached_image_ids = _cached_image_ids(engine)
    unit_reports: list[dict[str, Any]] = []
    try:
        async with product_client:
            for index, unit in enumerate(units, start=1):
                binding = _binding(unit)
                candidates = _child_candidates(unit)
                report = {
                    "shopId": binding.shop_id,
                    "parentAsin": binding.parent_asin,
                    "parentSellerSku": binding.parent_seller_sku,
                    "childCount": len(candidates),
                    "image": "MISSING",
                    "principalUserIds": [],
                    "errors": [],
                }
                try:
                    result = await product_client.call_tool(
                        PRODUCT_TOOL,
                        _product_arguments(binding),
                    )
                    product_rows = result.data
                except Exception as exc:
                    report["errors"].append(f"product:{type(exc).__name__}")
                    product_rows = []

                image_url = _main_image(product_rows)
                if image_url:
                    report["image"] = "AVAILABLE"
                    if apply:
                        await _retry_database_write(
                            lambda current_binding=binding, current_url=image_url: image_service.retain_latest(
                                current_binding,
                                {"identity": {"storefront": {
                                    "main_image_url": current_url,
                                    "main_image_source": "erp_listing_product_info.picUrl",
                                }}},
                            )
                        )
                        cached_image_ids.add(binding.operating_unit_id)
                elif binding.operating_unit_id in cached_image_ids:
                    report["image"] = "CACHED"

                principal_ids: set[int] = set()
                for row in product_rows:
                    if (principal_id := _principal_id(row)) is not None:
                        principal_ids.add(principal_id)
                report["principalUserIds"] = sorted(principal_ids)
                unit_reports.append(report)
                if checkpoint_path is not None:
                    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                    checkpoint_path.write_text(
                        json.dumps(
                            {
                                "sourceBatchIds": batch_ids,
                                "applied": apply,
                                "complete": False,
                                "unitCount": len(unit_reports),
                                "units": unit_reports,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                print(
                    f"[{index}/{len(units)}] {binding.parent_asin} "
                    f"image={report['image']} owners={report['principalUserIds']}",
                    flush=True,
                )
                await asyncio.sleep(interval_seconds)
    finally:
        engine.dispose()

    owner_counts = Counter(
        "NONE" if not row["principalUserIds"] else
        "SINGLE" if len(row["principalUserIds"]) == 1 else "MULTIPLE"
        for row in unit_reports
    )
    return {
        "sourceBatchIds": batch_ids,
        "applied": apply,
        "unitCount": len(unit_reports),
        "imageAvailableCount": sum(
            row["image"] in {"AVAILABLE", "CACHED"} for row in unit_reports
        ),
        "ownerCoverage": dict(owner_counts),
        "userDirectoryCount": len({
            user_id
            for row in unit_reports
            for user_id in row["principalUserIds"]
        }),
        "units": unit_reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="补齐真实巡检流量入口主图并统计负责人覆盖")
    parser.add_argument("--batch-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--interval-seconds", type=float, default=0.8)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = asyncio.run(run(
        args.batch_id,
        apply=args.apply,
        limit=args.limit,
        interval_seconds=max(0, args.interval_seconds),
        checkpoint_path=args.output,
    ))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({**result, "complete": True}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in result.items() if key != "units"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
