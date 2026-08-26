from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from core.contracts import OperatingFactSnapshot, OperatingUnitRef, sha256_json
from core.enums import DataQualityStatus
from integrations.repositories.patrol import PatrolRepository
from integrations.repositories.tables import metadata, patrol_operating_unit_catalog


def _snapshot(binding, *, owner_ids, owner_source="product_info"):
    return OperatingFactSnapshot(
        snapshot_id="fs_0123456789abcdef01234567",
        operating_unit=OperatingUnitRef.derive(
            shop_id=binding.shop_id,
            site_code=binding.site_code,
            parent_asin=binding.parent_asin,
            parent_seller_sku=binding.parent_seller_sku,
        ),
        as_of_time=datetime.now(UTC),
        content_hash=sha256_json({"owner_ids": owner_ids}),
        quality_status=DataQualityStatus.COMPLETE,
        completeness_score=1,
        identity={"owner_user_ids": owner_ids, "owner_source": owner_source},
    )


def _insert_catalog(connection, binding, owner_ids):
    now = datetime.now(UTC).replace(tzinfo=None)
    connection.execute(
        sa.insert(patrol_operating_unit_catalog).values(
            operating_unit_id=binding.operating_unit_id,
            shop_id=binding.shop_id,
            site_code=binding.site_code,
            parent_asin=binding.parent_asin,
            parent_seller_sku=binding.parent_seller_sku,
            shop_account_ref=binding.shop_account,
            owner_user_ids_json=owner_ids,
            source_tool="erp_amazon_listing_query_page",
            fetched_at=now,
            created_at=now,
            updated_at=now,
        )
    )


def test_product_info_owner_is_merged_into_catalog(binding):
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        _insert_catalog(connection, binding, [35])
        repository = PatrolRepository(connection)

        repository._backfill_catalog_owner(_snapshot(binding, owner_ids=[42, 35]))
        repository._backfill_catalog_owner(_snapshot(binding, owner_ids=[42, 35]))

        owners = connection.execute(
            sa.select(patrol_operating_unit_catalog.c.owner_user_ids_json)
        ).scalar_one()
    assert owners == [35, 42]


def test_catalog_fallback_owner_is_not_rewritten(binding):
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        _insert_catalog(connection, binding, [35])
        PatrolRepository(connection)._backfill_catalog_owner(
            _snapshot(
                binding,
                owner_ids=[42],
                owner_source="operating_unit_catalog_fallback",
            )
        )
        owners = connection.execute(
            sa.select(patrol_operating_unit_catalog.c.owner_user_ids_json)
        ).scalar_one()
    assert owners == [35]
