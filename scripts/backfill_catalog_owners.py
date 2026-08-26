from __future__ import annotations

import argparse
import json

import sqlalchemy as sa

from integrations.repositories.tables import patrol_fact_snapshot, patrol_operating_unit_catalog
from web.backend.deps import get_database_engine


def _owner_ids(identity: object) -> list[int]:
    if not isinstance(identity, dict) or identity.get("owner_source") != "product_info":
        return []
    result: set[int] = set()
    for value in identity.get("owner_user_ids") or []:
        try:
            owner_id = int(value)
        except (TypeError, ValueError):
            continue
        if owner_id > 0:
            result.add(owner_id)
    return sorted(result)


def backfill(limit: int | None = None) -> tuple[int, int]:
    engine = get_database_engine()
    updated = 0
    with engine.begin() as connection:
        query = sa.select(
            patrol_fact_snapshot.c.operating_unit_id,
            patrol_fact_snapshot.c.normalized_summary_json,
        )
        if limit is not None:
            query = query.limit(limit)
        result = connection.execution_options(stream_results=True).execute(query).mappings()
        owners_by_unit: dict[str, set[int]] = {}
        for row in result:
            normalized = row["normalized_summary_json"]
            if isinstance(normalized, str):
                normalized = json.loads(normalized)
            identity = (normalized or {}).get("identity") if isinstance(normalized, dict) else None
            owner_ids = _owner_ids(identity)
            if owner_ids:
                owners_by_unit.setdefault(str(row["operating_unit_id"]), set()).update(owner_ids)
        for operating_unit_id, owner_ids in owners_by_unit.items():
            current = connection.execute(
                sa.select(patrol_operating_unit_catalog.c.owner_user_ids_json).where(
                    patrol_operating_unit_catalog.c.operating_unit_id == operating_unit_id
                ).with_for_update()
            ).scalar_one_or_none()
            if current is None:
                continue
            existing = {int(value) for value in (current or []) if str(value).isdigit() and int(value) > 0}
            merged = sorted(existing | owner_ids)
            if merged == sorted(existing):
                continue
            connection.execute(
                sa.update(patrol_operating_unit_catalog)
                .where(patrol_operating_unit_catalog.c.operating_unit_id == operating_unit_id)
                .values(owner_user_ids_json=merged)
            )
            updated += 1
    return len(owners_by_unit), updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill catalog owners from product_info snapshots")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    candidates, updated = backfill(args.limit)
    print(f"owner backfill candidates={candidates} updated={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
