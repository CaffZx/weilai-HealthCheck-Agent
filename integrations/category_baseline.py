from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.mysql import insert as mysql_insert

from core.operating_unit import OperatingUnitBinding
from integrations.repositories.tables import patrol_category_baseline


class MySqlCategoryBaselineService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def observe_and_resolve(
        self,
        binding: OperatingUnitBinding,
        normalized: dict[str, Any],
        *,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        current = (observed_at or datetime.now(UTC)).replace(tzinfo=None)
        children = list((normalized.get("identity") or {}).get("children") or [])
        observations = [
            {
                "operating_unit_id": binding.operating_unit_id,
                "child_asin": child["child_asin"],
                "shop_id": binding.shop_id,
                "parent_asin": binding.parent_asin,
                "parent_seller_sku": binding.parent_seller_sku,
                "category_id": (child.get("frontend") or {}).get("category_id"),
                "category_path": (child.get("frontend") or {}).get("category_name"),
                "source_tool": "erp_listing_price_promotion_analysis",
                "source_snapshot_id": None,
                "status": "PENDING_CONFIRMATION",
                "first_observed_at": current,
                "last_observed_at": current,
                "confirmed_by": None,
                "confirmed_at": None,
            }
            for child in children
            if child.get("child_asin")
            and (child.get("frontend") or {}).get("category_name")
        ]
        with self.engine.begin() as connection:
            for observation in observations:
                statement = mysql_insert(patrol_category_baseline).values(**observation)
                connection.execute(
                    statement.on_duplicate_key_update(
                        last_observed_at=statement.inserted.last_observed_at
                    )
                )
            rows = connection.execute(
                sa.select(patrol_category_baseline).where(
                    patrol_category_baseline.c.operating_unit_id
                    == binding.operating_unit_id
                )
            ).mappings().all()
        baselines = {row["child_asin"]: dict(row) for row in rows}
        enriched_children = []
        for child in children:
            baseline = baselines.get(child.get("child_asin"))
            enriched_children.append({
                **child,
                "category_baseline": (
                    {
                        "category_id": baseline["category_id"],
                        "category_path": baseline["category_path"],
                        "status": baseline["status"],
                        "source_tool": baseline["source_tool"],
                    }
                    if baseline
                    else None
                ),
            })
        result = dict(normalized)
        result["identity"] = {
            **(normalized.get("identity") or {}),
            "children": enriched_children,
        }
        return result

    def confirm(
        self,
        operating_unit_id: str,
        child_asin: str,
        *,
        confirmed_by: str,
        confirmed_at: datetime | None = None,
    ) -> None:
        if not confirmed_by.strip():
            raise ValueError("confirmed_by is required")
        current = (confirmed_at or datetime.now(UTC)).replace(tzinfo=None)
        with self.engine.begin() as connection:
            updated = connection.execute(
                sa.update(patrol_category_baseline)
                .where(
                    patrol_category_baseline.c.operating_unit_id == operating_unit_id,
                    patrol_category_baseline.c.child_asin == child_asin.strip().upper(),
                    patrol_category_baseline.c.status == "PENDING_CONFIRMATION",
                )
                .values(
                    status="CONFIRMED",
                    confirmed_by=confirmed_by.strip(),
                    confirmed_at=current,
                )
            )
        if updated.rowcount != 1:
            raise ValueError("pending category baseline was not found")
