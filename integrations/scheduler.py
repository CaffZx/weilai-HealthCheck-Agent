from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.mysql import insert as mysql_insert

from core.enums import SignalState, TriggerType
from core.operating_unit import OperatingUnitBinding
from integrations.repositories.patrol import _utc_naive
from integrations.repositories.tables import patrol_scheduler_lock, patrol_signal
from integrations.rollout import RolloutPolicy
from integrations.runtime_queue import MySqlRuntimeQueue, daily_idempotency_key, scope_hash

ACTIVE_SIGNAL_STATES = {
    SignalState.NEW.value,
    SignalState.PENDING_CONFIRMATION.value,
    SignalState.IN_PROGRESS.value,
    SignalState.OBSERVING.value,
    SignalState.LONG_TERM_FOLLOW_UP.value,
    SignalState.AWAITING_RESCAN.value,
}

class OperatingUnitProviderPort(Protocol):
    async def list_all_active_units(self) -> list[OperatingUnitBinding]: ...


class SchedulerLease:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def acquire(
        self,
        *,
        lock_name: str,
        holder_id: str,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(UTC)
        expires_at = current + ttl
        if ttl <= timedelta(0):
            raise ValueError("scheduler lease ttl must be positive")
        with self.engine.begin() as connection:
            inserted = connection.execute(
                mysql_insert(patrol_scheduler_lock)
                .values(
                    lock_name=lock_name,
                    holder_id=holder_id,
                    acquired_at=_utc_naive(current),
                    renewed_at=_utc_naive(current),
                    expires_at=_utc_naive(expires_at),
                    version=1,
                )
                .prefix_with("IGNORE")
            )
            if inserted.rowcount == 1:
                return True
            existing = connection.execute(
                sa.select(patrol_scheduler_lock)
                .where(patrol_scheduler_lock.c.lock_name == lock_name)
                .with_for_update()
            ).mappings().one_or_none()
            if existing is None:
                return False
            expired = existing["expires_at"] <= _utc_naive(current)
            if existing["holder_id"] != holder_id and not expired:
                return False
            connection.execute(
                sa.update(patrol_scheduler_lock)
                .where(
                    patrol_scheduler_lock.c.lock_name == lock_name,
                    patrol_scheduler_lock.c.version == existing["version"],
                )
                .values(
                    holder_id=holder_id,
                    acquired_at=(
                        _utc_naive(current) if expired else existing["acquired_at"]
                    ),
                    renewed_at=_utc_naive(current),
                    expires_at=_utc_naive(expires_at),
                    version=existing["version"] + 1,
                )
            )
            return True

    def release(self, *, lock_name: str, holder_id: str) -> bool:
        with self.engine.begin() as connection:
            deleted = connection.execute(
                sa.delete(patrol_scheduler_lock).where(
                    patrol_scheduler_lock.c.lock_name == lock_name,
                    patrol_scheduler_lock.c.holder_id == holder_id,
                )
            )
            return deleted.rowcount == 1

    def renew(
        self,
        *,
        lock_name: str,
        holder_id: str,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(UTC)
        if ttl <= timedelta(0):
            raise ValueError("scheduler lease ttl must be positive")
        with self.engine.begin() as connection:
            renewed = connection.execute(
                sa.update(patrol_scheduler_lock)
                .where(
                    patrol_scheduler_lock.c.lock_name == lock_name,
                    patrol_scheduler_lock.c.holder_id == holder_id,
                    patrol_scheduler_lock.c.expires_at > _utc_naive(current),
                )
                .values(
                    renewed_at=_utc_naive(current),
                    expires_at=_utc_naive(current + ttl),
                    version=patrol_scheduler_lock.c.version + 1,
                )
            )
            return renewed.rowcount == 1


class PatrolScheduler:
    def __init__(
        self,
        *,
        queue: MySqlRuntimeQueue,
        unit_provider: OperatingUnitProviderPort,
        lease: SchedulerLease,
        holder_id: str,
        rule_bundle_version: str,
        lease_ttl: timedelta = timedelta(minutes=10),
        lease_heartbeat: timedelta | None = None,
        rollout_policy: RolloutPolicy | None = None,
    ) -> None:
        self.queue = queue
        self.unit_provider = unit_provider
        self.lease = lease
        self.holder_id = holder_id
        self.rule_bundle_version = rule_bundle_version
        self.lease_ttl = lease_ttl
        self.lease_heartbeat = lease_heartbeat or lease_ttl / 3
        if self.lease_heartbeat <= timedelta(0):
            raise ValueError("scheduler lease heartbeat must be positive")
        if self.lease_heartbeat >= self.lease_ttl:
            raise ValueError("scheduler lease heartbeat must be shorter than ttl")
        self.rollout_policy = rollout_policy or RolloutPolicy()

    async def _heartbeat(
        self,
        *,
        lock_name: str,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        interval = self.lease_heartbeat.total_seconds()
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    renewed = await asyncio.to_thread(
                        self.lease.renew,
                        lock_name=lock_name,
                        holder_id=self.holder_id,
                        ttl=self.lease_ttl,
                    )
                except Exception:
                    renewed = False
                if not renewed:
                    lost.set()
                    return

    async def _stop_heartbeat(
        self,
        *,
        lock_name: str,
        stop: asyncio.Event,
        task: asyncio.Task[None],
    ) -> None:
        stop.set()
        await task
        await asyncio.to_thread(
            self.lease.release,
            lock_name=lock_name,
            holder_id=self.holder_id,
        )

    async def create_daily_batch(self, *, business_date: date) -> tuple[str, bool] | None:
        if not self.rollout_policy.patrol_execution_enabled:
            return None
        lock_name = f"daily:{business_date.isoformat()}"
        if not self.lease.acquire(
            lock_name=lock_name,
            holder_id=self.holder_id,
            ttl=self.lease_ttl,
        ):
            return None
        stop = asyncio.Event()
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(lock_name=lock_name, stop=stop, lost=lost)
        )
        try:
            units = self.rollout_policy.filter_units(
                await self.unit_provider.list_all_active_units()
            )
            if lost.is_set() or not units:
                return None
            key = daily_idempotency_key(
                business_date, units, self.rule_bundle_version
            )
            return self.queue.create_batch(
                trigger_type=TriggerType.DAILY_SCHEDULE,
                business_date=business_date,
                units=units,
                rule_bundle_version=self.rule_bundle_version,
                idempotency_key=key,
                scope={"kind": "ALL_ACTIVE", "operating_unit_ids": sorted(
                    unit.operating_unit_id for unit in units
                )},
            )
        finally:
            await self._stop_heartbeat(
                lock_name=lock_name,
                stop=stop,
                task=heartbeat,
            )

    async def create_due_batch(
        self,
        *,
        business_date: date,
        now: datetime | None = None,
    ) -> tuple[str, bool] | None:
        if not self.rollout_policy.patrol_execution_enabled:
            return None
        current = now or datetime.now(UTC)
        bucket = current.astimezone(UTC).replace(
            minute=(current.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        lock_name = f"observation-due:{bucket.isoformat()}"
        if not self.lease.acquire(
            lock_name=lock_name,
            holder_id=self.holder_id,
            ttl=self.lease_ttl,
            now=current,
        ):
            return None
        stop = asyncio.Event()
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(lock_name=lock_name, stop=stop, lost=lost)
        )
        try:
            due_ids = self._due_operating_unit_ids(current)
            if lost.is_set() or not due_ids:
                return None
            active_units = self.rollout_policy.filter_units(
                await self.unit_provider.list_all_active_units()
            )
            if lost.is_set():
                return None
            units = [unit for unit in active_units if unit.operating_unit_id in due_ids]
            active_job_units = self.queue.active_operating_unit_ids(
                {unit.operating_unit_id for unit in units}
            )
            units = [
                unit for unit in units
                if unit.operating_unit_id not in active_job_units
            ]
            if not units:
                return None
            digest = scope_hash(units)
            bucket_token = hashlib.sha256(bucket.isoformat().encode("utf-8")).hexdigest()[:16]
            key = (
                f"OBSERVATION_DUE:{business_date.isoformat()}:"
                f"{bucket_token}:{digest}:{self.rule_bundle_version}"
            )
            return self.queue.create_batch(
                trigger_type=TriggerType.OBSERVATION_DUE,
                business_date=business_date,
                units=units,
                rule_bundle_version=self.rule_bundle_version,
                idempotency_key=key,
                scope={
                    "kind": "DUE_SIGNALS",
                    "due_operating_unit_ids": sorted(due_ids),
                    "scheduled_operating_unit_ids": sorted(
                        unit.operating_unit_id for unit in units
                    ),
                },
            )
        finally:
            await self._stop_heartbeat(
                lock_name=lock_name,
                stop=stop,
                task=heartbeat,
            )

    def _due_operating_unit_ids(self, now: datetime) -> set[str]:
        with self.queue.engine.connect() as connection:
            return set(
                connection.execute(
                    sa.select(patrol_signal.c.operating_unit_id)
                    .where(
                        patrol_signal.c.signal_state.in_(ACTIVE_SIGNAL_STATES),
                        patrol_signal.c.next_inspection_at.is_not(None),
                        patrol_signal.c.next_inspection_at <= _utc_naive(now),
                    )
                    .distinct()
                ).scalars()
            )
