from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.mysql import insert as mysql_insert

from core.operating_unit import OperatingUnitBinding
from integrations.repositories.tables import patrol_product_image


class MySqlProductImageService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def retain_latest(
        self,
        binding: OperatingUnitBinding,
        normalized: dict[str, Any],
        *,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        current = (observed_at or datetime.now(UTC)).replace(tzinfo=None)
        identity = dict(normalized.get("identity") or {})
        storefront = dict(identity.get("storefront") or {})
        image_url = str(storefront.get("main_image_url") or "").strip()
        source_tool = str(
            storefront.get("main_image_source")
            or "erp_listing_product_info.picUrl"
        ).strip()

        with self.engine.begin() as connection:
            if image_url:
                statement = mysql_insert(patrol_product_image).values(
                    operating_unit_id=binding.operating_unit_id,
                    shop_id=binding.shop_id,
                    parent_asin=binding.parent_asin,
                    parent_seller_sku=binding.parent_seller_sku,
                    image_url=image_url,
                    source_tool=source_tool,
                    source_fetched_at=current,
                    first_observed_at=current,
                    last_observed_at=current,
                )
                connection.execute(statement.on_duplicate_key_update(
                    image_url=statement.inserted.image_url,
                    source_tool=statement.inserted.source_tool,
                    source_fetched_at=statement.inserted.source_fetched_at,
                    last_observed_at=statement.inserted.last_observed_at,
                ))
            row = connection.execute(
                sa.select(patrol_product_image).where(
                    patrol_product_image.c.operating_unit_id
                    == binding.operating_unit_id
                )
            ).mappings().one_or_none()

        if row is None:
            return normalized
        if row["source_tool"] != "erp_listing_product_info.picUrl":
            return normalized
        if not image_url:
            storefront.update({
                "main_image_url": row["image_url"],
                "main_image_source": row["source_tool"],
                "main_image_fetched_at": row["source_fetched_at"].isoformat(),
                "main_image_cached": True,
            })
        else:
            storefront.update({
                "main_image_fetched_at": current.isoformat(),
                "main_image_cached": False,
            })
        result = dict(normalized)
        result["identity"] = {**identity, "storefront": storefront}
        return result
