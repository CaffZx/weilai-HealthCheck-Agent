from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import Engine

from core.operating_unit import OperatingUnitBinding
from integrations.mcp_operating_units import LIST_UNITS_TOOL
from integrations.repositories.tables import patrol_listing_catalog, patrol_operating_unit_catalog

logger = logging.getLogger(__name__)


class OperatingUnitProviderPort(Protocol):
    async def list_all_active_units(self) -> list[OperatingUnitBinding]: ...


@dataclass(frozen=True, slots=True)
class CatalogRefreshResult:
    catalog_id: str
    unit_count: int
    fetched_at: datetime
    source_record_count: int = 0
    unresolved_parent_count: int = 0
    stale: bool = False


class OperatingUnitCatalogService:
    def __init__(
        self,
        engine: Engine,
        provider: OperatingUnitProviderPort,
        *,
        refresh_interval: timedelta = timedelta(days=7),
    ) -> None:
        if refresh_interval <= timedelta(0):
            raise ValueError("refresh_interval must be positive")
        self.engine = engine
        self.provider = provider
        self.refresh_interval = refresh_interval

    def current_fetched_at(self) -> datetime | None:
        with self.engine.connect() as connection:
            value = connection.execute(
                sa.select(sa.func.max(patrol_operating_unit_catalog.c.fetched_at))
            ).scalar_one()
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        fetched_at = self.current_fetched_at()
        if fetched_at is None:
            return False
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current - fetched_at <= self.refresh_interval

    async def refresh(self) -> CatalogRefreshResult:
        raw_listing_method = getattr(self.provider, "list_all_active_listing_records", None)
        raw_records = await raw_listing_method() if raw_listing_method else None
        if raw_records is not None:
            units = self._units_from_raw_records(raw_records)
        else:
            units = await self.provider.list_all_active_units()
        by_id = {unit.operating_unit_id: unit for unit in units}
        if len(by_id) != len(units):
            raise ValueError("provider returned duplicate operating units")
        fetched_at = datetime.now(UTC)
        catalog_id = f"catalog_{uuid4().hex[:24]}"
        with self.engine.begin() as connection:
            previous_owner_ids: dict[str, set[int]] = {}
            previous_rows = connection.execute(
                sa.select(
                    patrol_operating_unit_catalog.c.operating_unit_id,
                    patrol_operating_unit_catalog.c.owner_user_ids_json,
                ).with_for_update()
            ).mappings()
            for previous in previous_rows:
                previous_owner_ids[previous["operating_unit_id"]] = {
                    int(value)
                    for value in (previous["owner_user_ids_json"] or [])
                    if str(value).isdigit() and int(value) > 0
                }
            rows = []
            for unit in by_id.values():
                inherited_owners = previous_owner_ids.get(unit.operating_unit_id, set())
                merged_unit = unit.with_owner_user_ids(
                    tuple(sorted(inherited_owners | set(unit.owner_user_ids)))
                )
                rows.append(self._row(catalog_id, merged_unit, fetched_at))
            if raw_records is not None:
                connection.execute(sa.delete(patrol_listing_catalog))
                listing_rows = [self._listing_row(record, fetched_at) for record in raw_records]
                if listing_rows:
                    connection.execute(sa.insert(patrol_listing_catalog), listing_rows)
            connection.execute(sa.delete(patrol_operating_unit_catalog))
            if rows:
                connection.execute(sa.insert(patrol_operating_unit_catalog), rows)
        unresolved = 0 if raw_records is None else sum(
            1 for record in raw_records
            if not record.get("parent_asin") or not record.get("parent_seller_sku")
        )
        return CatalogRefreshResult(
            catalog_id,
            len(rows),
            fetched_at,
            source_record_count=len(raw_records) if raw_records is not None else len(rows),
            unresolved_parent_count=unresolved,
        )

    @staticmethod
    def _units_from_raw_records(records: list[dict]) -> list[OperatingUnitBinding]:
        units: dict[str, OperatingUnitBinding] = {}
        for record in records:
            if not record.get("parent_asin") or not record.get("parent_seller_sku"):
                continue
            unit = OperatingUnitBinding(
                shop_id=record["shop_id"], site_code=record["site_code"],
                parent_asin=record["parent_asin"], parent_seller_sku=record["parent_seller_sku"],
                shop_account=record["shop_account"],
                owner_user_ids=((record["user_id"],) if record.get("user_id") else ()),
            )
            existing = units.get(unit.operating_unit_id)
            if existing is None:
                units[unit.operating_unit_id] = unit
            else:
                if (existing.shop_id, existing.site_code, existing.parent_asin,
                    existing.parent_seller_sku, existing.shop_account) != (
                    unit.shop_id, unit.site_code, unit.parent_asin,
                    unit.parent_seller_sku, unit.shop_account
                ):
                    raise ValueError("raw listing records map one operating unit inconsistently")
                units[unit.operating_unit_id] = existing.with_owner_user_ids(
                    tuple(sorted(set(existing.owner_user_ids) | set(unit.owner_user_ids)))
                )
        return sorted(units.values(), key=lambda unit: unit.operating_unit_id)

    @staticmethod
    def _listing_row(record: dict, fetched_at: datetime) -> dict:
        now = fetched_at.replace(tzinfo=None)
        return {
            "listing_record_id": record["listing_record_id"], "shop_id": record["shop_id"],
            "site_code": record["site_code"], "shop_account_ref": record["shop_account"],
            "asin": record["asin"], "seller_sku": record["seller_sku"],
            "parent_asin": record.get("parent_asin"), "parent_seller_sku": record.get("parent_seller_sku"),
            "user_id": record.get("user_id"), "status": record["status"],
            "source_tool": LIST_UNITS_TOOL, "fetched_at": now, "created_at": now, "updated_at": now,
        }

    async def get_or_refresh(self) -> CatalogRefreshResult:
        if not self.is_fresh():
            try:
                return await self.refresh()
            except Exception:
                stale = self._current_result(stale=True)
                if stale.unit_count == 0:
                    raise
                logger.exception(
                    "operating-unit catalog refresh failed; using previous complete snapshot"
                )
                return stale
        return self._current_result(stale=False)

    def _current_result(self, *, stale: bool) -> CatalogRefreshResult:
        fetched_at = self.current_fetched_at()
        if fetched_at is None:
            return CatalogRefreshResult(
                "current", 0, datetime.fromtimestamp(0, UTC), stale=stale
            )
        with self.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count()).select_from(patrol_operating_unit_catalog)
            ).scalar_one()
        with self.engine.connect() as connection:
            source_count = connection.execute(
                sa.select(sa.func.count()).select_from(patrol_listing_catalog)
            ).scalar_one()
            unresolved = connection.execute(
                sa.select(sa.func.count()).select_from(patrol_listing_catalog).where(
                    sa.or_(patrol_listing_catalog.c.parent_asin.is_(None),
                           patrol_listing_catalog.c.parent_seller_sku.is_(None))
                )
            ).scalar_one()
        return CatalogRefreshResult(
            "current", int(count), fetched_at,
            source_record_count=int(source_count), unresolved_parent_count=int(unresolved),
            stale=stale,
        )

    async def list_all_active_units(self) -> list[OperatingUnitBinding]:
        await self.get_or_refresh()
        return self.list_current_units()

    def list_current_units(self) -> list[OperatingUnitBinding]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(patrol_operating_unit_catalog).order_by(
                    patrol_operating_unit_catalog.c.operating_unit_id
                )
            ).mappings().all()
        return [
            OperatingUnitBinding(
                shop_id=row["shop_id"],
                site_code=row["site_code"],
                parent_asin=row["parent_asin"],
                parent_seller_sku=row["parent_seller_sku"],
                shop_account=row["shop_account_ref"],
                owner_user_ids=tuple(row["owner_user_ids_json"] or []),
            )
            for row in rows
        ]

    @staticmethod
    def _row(
        catalog_id: str,
        unit: OperatingUnitBinding,
        fetched_at: datetime,
    ) -> dict:
        del catalog_id
        return {
            "operating_unit_id": unit.operating_unit_id,
            "shop_id": unit.shop_id,
            "site_code": unit.site_code,
            "parent_asin": unit.parent_asin,
            "parent_seller_sku": unit.parent_seller_sku,
            "shop_account_ref": unit.shop_account,
            "owner_user_ids_json": list(unit.owner_user_ids),
            "source_tool": LIST_UNITS_TOOL,
            "fetched_at": fetched_at.replace(tzinfo=None),
            "created_at": fetched_at.replace(tzinfo=None),
            "updated_at": fetched_at.replace(tzinfo=None),
        }
