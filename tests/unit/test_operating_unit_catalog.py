from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from integrations.operating_unit_catalog import OperatingUnitCatalogService
from integrations.repositories.tables import metadata, patrol_operating_unit_catalog


class Provider:
    def __init__(self, units):
        self.units = units
        self.calls = 0

    async def list_all_active_units(self):
        self.calls += 1
        return self.units


def test_catalog_refreshes_once_and_reads_frozen_identity(binding):
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    provider = Provider([binding])
    service = OperatingUnitCatalogService(engine, provider)

    import asyncio

    first = asyncio.run(service.list_all_active_units())
    second = asyncio.run(service.list_all_active_units())

    assert provider.calls == 1
    assert first == second == [binding]
    assert service.is_fresh(now=datetime.now(UTC) + timedelta(days=6))
    assert not service.is_fresh(now=datetime.now(UTC) + timedelta(days=8))


def test_catalog_rejects_duplicate_provider_units(binding):
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    provider = Provider([binding, binding])
    service = OperatingUnitCatalogService(engine, provider)

    import asyncio

    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(service.refresh())


def test_catalog_replaces_removed_units_on_refresh(binding):
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    replacement = binding.with_owner_user_ids((42,))
    provider = Provider([binding])
    service = OperatingUnitCatalogService(engine, provider)
    import asyncio

    asyncio.run(service.refresh())
    provider.units = [replacement]
    asyncio.run(service.refresh())

    assert service.list_current_units() == [replacement]


def test_catalog_refresh_keeps_owner_backfilled_by_patrol(binding):
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    provider = Provider([binding])
    service = OperatingUnitCatalogService(engine, provider)
    import asyncio

    asyncio.run(service.refresh())
    with engine.begin() as connection:
        connection.execute(
            sa.update(patrol_operating_unit_catalog)
            .where(
                patrol_operating_unit_catalog.c.operating_unit_id
                == binding.operating_unit_id
            )
            .values(owner_user_ids_json=[42])
        )
    provider.units = [binding.with_owner_user_ids((43,))]
    asyncio.run(service.refresh())

    assert service.list_current_units()[0].owner_user_ids == (42, 43)


def test_catalog_uses_previous_complete_snapshot_when_refresh_fails(binding):
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    provider = Provider([binding])
    service = OperatingUnitCatalogService(engine, provider)
    import asyncio

    asyncio.run(service.refresh())
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE t_patrol_operating_unit_catalog "
                "SET fetched_at='2020-01-01 00:00:00'"
            )
        )

    async def fail():
        raise TimeoutError("AZ directory unavailable")

    provider.list_all_active_units = fail
    result = asyncio.run(service.get_or_refresh())

    assert result.stale is True
    assert result.unit_count == 1
    assert service.list_current_units() == [binding]
