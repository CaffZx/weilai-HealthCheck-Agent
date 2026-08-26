"""把 erp_amazon_listing_query_page 抓到的负责人 userId 直接回填到经营单元目录，不过配置表。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, "/opt/weilai-HealthCheck-Agent-v2.0")

from clients.mcp_client import ReusableMcpSessionClient
from integrations.mcp_operating_units import McpOperatingUnitProvider
from integrations.database import create_database_engine
import sqlalchemy as sa
from integrations.repositories.tables import patrol_operating_unit_catalog


async def run(*, apply: bool) -> dict:
    token = os.environ["MCP_API_KEY"]
    engine = create_database_engine()
    client = ReusableMcpSessionClient(
        os.environ.get("AZLISTING_GATEWAY", "http://mcp-gateway.example.com/mcp"),
        token,
        timeout_seconds=60,
    )
    async with client:
        provider = McpOperatingUnitProvider(client)
        records = await provider.list_all_active_listing_records()

    print(f"listing records fetched: {len(records)}")

    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                patrol_operating_unit_catalog.c.operating_unit_id,
                patrol_operating_unit_catalog.c.shop_id,
                patrol_operating_unit_catalog.c.parent_seller_sku,
                patrol_operating_unit_catalog.c.owner_user_ids_json,
            )
        ).mappings().all()

    by_key: dict[tuple[int, str], str] = {}
    current_owners: dict[str, set[int]] = {}
    for row in rows:
        key = (int(row["shop_id"]), str(row["parent_seller_sku"] or "").strip())
        by_key.setdefault(key, row["operating_unit_id"])
        current_owners[row["operating_unit_id"]] = {
            int(v) for v in (row["owner_user_ids_json"] or []) if str(v).isdigit() and int(v) > 0
        }

    owner_by_unit: dict[str, set[int]] = {}
    skipped_no_key = 0
    for rec in records:
        shop_id = rec.get("shop_id")
        sku = str(rec.get("seller_sku") or "").strip()
        user_id = rec.get("user_id")
        if user_id is None or user_id <= 0:
            continue
        if shop_id is None or not sku:
            continue
        unit_id = by_key.get((int(shop_id), sku))
        if unit_id is None:
            skipped_no_key += 1
            continue
        owner_by_unit.setdefault(unit_id, set()).add(int(user_id))

    print(f"units with owner from listing: {len(owner_by_unit)} skipped_unmatched={skipped_no_key}")

    if not apply:
        return {"records": len(records), "units_with_owner": len(owner_by_unit), "skipped": skipped_no_key, "applied": False}

    updated = 0
    with engine.begin() as connection:
        for unit_id, owners in owner_by_unit.items():
            merged = sorted(current_owners.get(unit_id, set()) | owners)
            if merged == sorted(current_owners.get(unit_id, set())):
                continue
            connection.execute(
                sa.update(patrol_operating_unit_catalog)
                .where(patrol_operating_unit_catalog.c.operating_unit_id == unit_id)
                .values(owner_user_ids_json=merged)
            )
            updated += 1
    print(f"updated catalog rows: {updated}")
    return {"records": len(records), "units_with_owner": len(owner_by_unit), "skipped": skipped_no_key, "updated": updated, "applied": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill owners from listing MCP into catalog")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(apply=args.apply))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
