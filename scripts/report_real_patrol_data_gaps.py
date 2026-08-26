from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import bindparam

from core.contracts import canonical_json
from integrations.repositories.tables import (
    patrol_fact_snapshot,
    patrol_raw_fact,
    patrol_signal,
    patrol_signal_occurrence,
)
from scripts.submit_real_patrol_to_control_center import (
    UnitNotSubmittable,
    _build_unit,
    _latest_complete_daily_metric,
    _load_enrichment,
)
from web.backend.deps import get_database_engine

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "artifacts/data-coverage"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _jobs(connection: sa.Connection, batch_ids: list[str]) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.text(
            """
            WITH candidates AS (
                SELECT j.job_id,j.batch_id,j.shop_id,j.site_code,j.parent_asin,
                       j.parent_seller_sku,j.payload_json,j.status,j.result_ref,
                       j.last_error_code,j.last_error_message,b.scope_json,
                       ROW_NUMBER() OVER (
                           PARTITION BY j.shop_id,j.parent_asin,j.parent_seller_sku
                           ORDER BY j.created_at DESC,j.job_id DESC
                       ) AS rank_no
                FROM t_patrol_job j
                JOIN t_patrol_batch b ON b.batch_id=j.batch_id
                WHERE j.batch_id IN :batch_ids
            )
            SELECT * FROM candidates WHERE rank_no=1
            ORDER BY parent_asin,shop_id,parent_seller_sku
            """
        ).bindparams(bindparam("batch_ids", expanding=True)),
        {"batch_ids": batch_ids},
    ).mappings().all()
    return [dict(row) for row in rows]


def _raw_facts(connection: sa.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.select(
            patrol_raw_fact.c.fact_key,
            patrol_raw_fact.c.tool_name,
            patrol_raw_fact.c.status,
            patrol_raw_fact.c.row_count,
            patrol_raw_fact.c.warning,
            patrol_raw_fact.c.error_code,
            patrol_raw_fact.c.error_message,
        )
        .where(patrol_raw_fact.c.run_id == run_id)
        .order_by(patrol_raw_fact.c.fact_key)
    ).mappings().all()
    return [dict(row) for row in rows]


def _snapshot(connection: sa.Connection, run_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        sa.select(patrol_fact_snapshot).where(patrol_fact_snapshot.c.run_id == run_id)
    ).mappings().one_or_none()
    if row is None:
        return None
    result = dict(row)
    for field in (
        "source_refs_json",
        "data_gaps_json",
        "normalized_summary_json",
        "raw_reference_json",
    ):
        result[field] = _json(result[field]) if result[field] is not None else None
    return result


def _signals(connection: sa.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.select(patrol_signal)
        .join(
            patrol_signal_occurrence,
            sa.and_(
                patrol_signal_occurrence.c.signal_id == patrol_signal.c.signal_id,
                patrol_signal_occurrence.c.run_id == run_id,
                patrol_signal_occurrence.c.occurred_at == patrol_signal.c.last_detected_at,
                patrol_signal_occurrence.c.occurrence_type.in_(
                    ("DETECTED", "DATA_GAP", "RECURRED")
                ),
            ),
        )
        .where(patrol_signal.c.last_scan_run_id == run_id)
        .order_by(patrol_signal.c.signal_type, patrol_signal.c.issue_code)
    ).mappings().all()
    signals = []
    for row in rows:
        signal = dict(row)
        signal["signal_payload_json"] = _json(signal["signal_payload_json"])
        signals.append(signal)
    return signals


def _field_status(value: Any) -> str:
    return "AVAILABLE" if value is not None else "MISSING"


def _control_center_fields(
    job: dict[str, Any], snapshot: dict[str, Any], signals: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    normalized = snapshot["normalized_summary_json"] or {}
    sales = normalized.get("sales") or {}
    profit = normalized.get("profit") or {}
    inventory = normalized.get("inventory") or {}
    identity = normalized.get("identity") or {}
    daily_rows = list(sales.get("daily_rows") or [])
    daily = _latest_complete_daily_metric(
        daily_rows,
        before_date=snapshot["as_of"],
    ) or {}
    units = daily.get("units")
    unit_contribution = profit.get("unit_contribution")
    return {
        "listingRequired": {
            "shopId": _field_status(job.get("shop_id")),
            "siteCode": _field_status(job.get("site_code")),
            "parentAsin": _field_status(job.get("parent_asin")),
            "parentSellerSku": _field_status(job.get("parent_seller_sku")),
        },
        "listingOptional": {
            "productName": _field_status(identity.get("product_name")),
            "productPicUrl": _field_status(
                job.get("_cached_image_url")
                or (identity.get("storefront") or {}).get("main_image_url")
            ),
            "ownerUserId": _field_status(_single_owner(snapshot)),
            "businessModel": _field_status(
                ((profit.get("operating_tags") or {}).get("operating_mode"))
            ),
        },
        "tagsOptional": {
            "allFields": "MISSING_EXTERNAL_SOURCE",
        },
        "operatingMetricRequired": {
            "metricDate": "AVAILABLE" if daily else "MISSING",
            "salesAmount": _field_status(daily.get("revenue")),
            "contributionProfit": (
                "AVAILABLE"
                if units is not None and unit_contribution is not None
                else "MISSING"
            ),
            "adCost": _field_status(daily.get("ad_cost")),
            "canSaleNum": _field_status(inventory.get("fba_available")),
        },
        "operatingMetricOptional": {
            "salesQuantity": _field_status(units),
            "orderQuantity": _field_status(daily.get("orders")),
            "profitRate": _field_status(daily.get("gross_margin")),
            "adSalesAmount": _field_status(daily.get("ad_sales")),
            "adOrder": _field_status(daily.get("ad_orders")),
            "inboundInventory": _field_status(inventory.get("fba_inbound")),
            "reservedInventory": _field_status(inventory.get("fba_reserved")),
        },
        "proposalRequired": {
            "anomalies": (
                "AVAILABLE"
                if any(signal["signal_type"] == "ANOMALY" for signal in signals)
                else "MISSING_REAL_ANOMALY"
            ),
            "details": (
                "AVAILABLE_MANUAL_REVIEW"
                if any(signal["signal_type"] == "ANOMALY" for signal in signals)
                else "MISSING_REAL_ANOMALY"
            ),
        },
    }


def _single_owner(snapshot: dict[str, Any]) -> str | None:
    owners = list(
        ((snapshot.get("normalized_summary_json") or {}).get("identity") or {}).get(
            "owner_user_ids"
        ) or []
    )
    return str(owners[0]) if len(owners) == 1 else None


def _missing_paths(field_coverage: dict[str, dict[str, str]]) -> list[str]:
    return sorted(
        f"{section}.{field}"
        for section, fields in field_coverage.items()
        for field, status in fields.items()
        if status.startswith("MISSING")
    )


def build_report(batch_ids: str | list[str]) -> dict[str, Any]:
    source_batch_ids = [batch_ids] if isinstance(batch_ids, str) else batch_ids
    engine = get_database_engine()
    units: list[dict[str, Any]] = []
    with engine.connect() as connection:
        jobs = _jobs(connection, source_batch_ids)
        if not jobs:
            raise RuntimeError(f"batches not found: {','.join(source_batch_ids)}")
        for job in jobs:
            unit: dict[str, Any] = {
                "shopId": job["shop_id"],
                "siteCode": job["site_code"],
                "parentAsin": job["parent_asin"],
                "parentSellerSku": job["parent_seller_sku"],
                "jobStatus": job["status"],
                "runId": job["result_ref"],
                "rawFacts": [],
                "dataGaps": [],
                "inspectionPointGaps": [],
                "controlCenterFieldCoverage": {},
                "controlCenterMissingFields": [],
                "controlCenterSubmittable": False,
                "controlCenterBlockers": [],
            }
            run_id = job["result_ref"]
            if not run_id:
                unit["controlCenterBlockers"] = [
                    f"patrolRun: job is {job['status']} and has no persisted result"
                ]
                units.append(unit)
                continue
            raw_facts = _raw_facts(connection, run_id)
            snapshot = _snapshot(connection, run_id)
            signals = _signals(connection, run_id)
            unit["rawFacts"] = raw_facts
            if snapshot is None:
                unit["controlCenterBlockers"] = ["factSnapshot: missing"]
                units.append(unit)
                continue
            image_url = _load_enrichment(
                connection,
                snapshot["operating_unit_id"],
            )
            job["_cached_image_url"] = image_url
            gaps = list(snapshot["data_gaps_json"] or [])
            unit["snapshotId"] = snapshot["snapshot_id"]
            unit["factQualityStatus"] = snapshot["quality_status"]
            unit["factCompletenessScore"] = float(snapshot["completeness_score"])
            unit["dataGaps"] = gaps
            unit["inspectionPointGaps"] = [
                gap for gap in gaps if gap.get("code") == "INSPECTION_POINT_COVERAGE_LIMITED"
            ]
            coverage = _control_center_fields(job, snapshot, signals)
            unit["controlCenterFieldCoverage"] = coverage
            unit["controlCenterMissingFields"] = _missing_paths(coverage)
            try:
                _build_unit(job, snapshot, signals)
            except UnitNotSubmittable as exc:
                unit["controlCenterBlockers"] = exc.missing_fields
            else:
                unit["controlCenterSubmittable"] = True
            units.append(unit)

    job_statuses = Counter(unit["jobStatus"] for unit in units)
    tool_statuses: Counter[str] = Counter()
    gap_codes: Counter[str] = Counter()
    missing_fields: Counter[str] = Counter()
    for unit in units:
        for fact in unit["rawFacts"]:
            tool_statuses[f"{fact['tool_name']}:{fact['status']}"] += 1
        gap_codes.update(str(gap.get("code") or "UNKNOWN") for gap in unit["dataGaps"])
        missing_fields.update(unit["controlCenterMissingFields"])
    return {
        "generatedAt": datetime.now(UTC).isoformat(),
        "batchId": source_batch_ids[-1],
        "sourceBatchIds": source_batch_ids,
        "summary": {
            "unitCount": len(units),
            "jobStatuses": dict(sorted(job_statuses.items())),
            "completedUnitCount": sum(unit["runId"] is not None for unit in units),
            "controlCenterSubmittableCount": sum(
                unit["controlCenterSubmittable"] for unit in units
            ),
            "toolStatuses": dict(sorted(tool_statuses.items())),
            "dataGapCodes": dict(sorted(gap_codes.items())),
            "controlCenterMissingFields": dict(sorted(missing_fields.items())),
        },
        "units": units,
    }


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 真实巡检数据覆盖与中控 v2.0 对比",
        "",
        f"- 来源批次：`{', '.join(report['sourceBatchIds'])}`",
        f"- 生成时间：`{report['generatedAt']}`",
        f"- 经营单元：{summary['unitCount']}",
        f"- 已有真实运行结果：{summary['completedUnitCount']}",
        f"- 当前可按中控 v2.0 提交：{summary['controlCenterSubmittableCount']}",
        "",
        "## 工具状态",
        "",
    ]
    lines.extend(f"- `{key}`：{value}" for key, value in summary["toolStatuses"].items())
    lines.extend(["", "## 数据缺口", ""])
    lines.extend(
        f"- `{key}`：{value}" for key, value in summary["dataGapCodes"].items()
    )
    lines.extend(["", "## 中控字段缺口", ""])
    lines.extend(
        f"- `{key}`：{value} 个经营单元"
        for key, value in summary["controlCenterMissingFields"].items()
    )
    lines.extend(["", "## 逐经营单元", ""])
    for unit in report["units"]:
        lines.extend([
            f"### {unit['parentAsin']} / shopId={unit['shopId']}",
            "",
            f"- Job：`{unit['jobStatus']}`；Run：`{unit['runId'] or '-'}`",
            f"- 中控可提交：`{str(unit['controlCenterSubmittable']).lower()}`",
            "- 中控阻断："
            + ("；".join(unit["controlCenterBlockers"]) or "无"),
            "- 字段缺失："
            + ("；".join(unit["controlCenterMissingFields"]) or "无"),
            "",
        ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report real patrol facts, inspection gaps and control-center v2 coverage"
    )
    parser.add_argument("--batch-id", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    report = build_report(args.batch_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_name = args.batch_id[-1] if len(args.batch_id) == 1 else f"{args.batch_id[0]}-merged"
    json_path = args.output_dir / f"{output_name}.json"
    markdown_path = args.output_dir / f"{output_name}.md"
    json_path.write_text(canonical_json(report) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    print(canonical_json(report["summary"]))
    print(f"json={json_path}")
    print(f"markdown={markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
