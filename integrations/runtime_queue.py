from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import Engine, text

from core.contracts import canonical_json
from core.enums import BatchStatus, JobStatus, TriggerType
from core.operating_unit import OperatingUnitBinding
from integrations.repositories.patrol import _utc_naive
from integrations.repositories.tables import patrol_batch, patrol_job

ACTIVE_JOB_STATUSES = {JobStatus.PENDING.value, JobStatus.RUNNING.value}


class RuntimeQueueConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: str
    batch_id: str
    request_id: str
    job_type: str
    operating_unit_id: str
    payload: dict[str, Any]
    retry_count: int
    max_retry: int


def utc_now() -> datetime:
    return datetime.now(UTC)


def scope_hash(units: Iterable[OperatingUnitBinding]) -> str:
    bindings = sorted(
        (
            unit.operating_unit_id,
            list(unit.owner_user_ids),
        )
        for unit in units
    )
    return hashlib.sha256(canonical_json(bindings).encode("utf-8")).hexdigest()


def daily_idempotency_key(
    business_date: date,
    units: Iterable[OperatingUnitBinding],
    rule_bundle_version: str,
) -> str:
    return f"DAILY_SCHEDULE:{business_date.isoformat()}:{scope_hash(units)}:{rule_bundle_version}"


class MySqlRuntimeQueue:
    def __init__(
        self,
        engine: Engine,
        *,
        max_retry: int = 3,
        base_backoff_seconds: int = 30,
        job_insert_batch_size: int = 50,
        idempotency_lock_timeout_seconds: int = 5,
    ) -> None:
        self.engine = engine
        if max_retry < 1:
            raise ValueError("max_retry must be at least 1")
        self.max_retry = max_retry
        self.base_backoff_seconds = base_backoff_seconds
        if job_insert_batch_size < 1:
            raise ValueError("job_insert_batch_size must be at least 1")
        self.job_insert_batch_size = job_insert_batch_size
        if idempotency_lock_timeout_seconds < 0:
            raise ValueError("idempotency_lock_timeout_seconds must not be negative")
        self.idempotency_lock_timeout_seconds = idempotency_lock_timeout_seconds

    def create_batch(
        self,
        *,
        trigger_type: TriggerType,
        business_date: date,
        units: list[OperatingUnitBinding],
        rule_bundle_version: str,
        idempotency_key: str,
        scope: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        unique_units = {unit.operating_unit_id: unit for unit in units}
        if len(unique_units) != len(units):
            raise ValueError("batch contains duplicate operating units")
        if not units:
            raise ValueError("batch must contain at least one operating unit")
        if len(idempotency_key) > 191:
            raise ValueError("idempotency_key exceeds 191 characters")

        batch_id = f"batch_{uuid4().hex[:24]}"
        now = utc_now()
        digest = scope_hash(units)
        effective_scope = scope or {"operating_unit_ids": sorted(unique_units)}
        connection = self.engine.connect()
        transaction = connection.begin()
        lock_names: list[str] = []
        try:
            idempotency_lock = (
                "patrol-idem:" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:49]
            )
            acquired = connection.execute(
                text("SELECT GET_LOCK(:lock_name, :timeout_seconds)"),
                {
                    "lock_name": idempotency_lock,
                    "timeout_seconds": self.idempotency_lock_timeout_seconds,
                },
            ).scalar_one()
            if acquired != 1:
                raise RuntimeQueueConflict("timed out waiting for batch idempotency lock")
            lock_names.append(idempotency_lock)
            existing = (
                connection.execute(
                    sa.select(
                        patrol_batch.c.batch_id,
                        patrol_batch.c.trigger_type,
                        patrol_batch.c.business_date,
                        patrol_batch.c.scope_json,
                        patrol_batch.c.scope_hash,
                        patrol_batch.c.rule_bundle_version,
                    ).where(patrol_batch.c.idempotency_key == idempotency_key)
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                expected = (
                    trigger_type.value,
                    business_date,
                    canonical_json(effective_scope),
                    digest,
                    rule_bundle_version,
                )
                actual = (
                    existing["trigger_type"],
                    existing["business_date"],
                    canonical_json(existing["scope_json"]),
                    existing["scope_hash"],
                    existing["rule_bundle_version"],
                )
                if actual != expected:
                    raise RuntimeQueueConflict("idempotency key was reused with different content")
                transaction.rollback()
                return existing["batch_id"], False

            for unit_id in sorted(unique_units):
                lock_name = (
                    "patrol-unit:" + hashlib.sha256(unit_id.encode("utf-8")).hexdigest()[:48]
                )
                acquired = connection.execute(
                    text("SELECT GET_LOCK(:lock_name, 0)"),
                    {"lock_name": lock_name},
                ).scalar_one()
                if acquired != 1:
                    raise RuntimeQueueConflict(
                        f"operating unit is being scheduled concurrently: {unit_id}"
                    )
                lock_names.append(lock_name)
            connection.execute(
                sa.insert(patrol_batch).values(
                    batch_id=batch_id,
                    idempotency_key=idempotency_key,
                    trigger_type=trigger_type.value,
                    business_date=business_date,
                    scope_json=effective_scope,
                    scope_hash=digest,
                    rule_bundle_version=rule_bundle_version,
                    status=BatchStatus.RUNNING.value,
                    total_count=len(units),
                    pending_count=len(units),
                    running_count=0,
                    succeeded_count=0,
                    failed_count=0,
                    started_at=_utc_naive(now),
                    finished_at=None,
                )
            )
            job_rows = []
            for unit in units:
                active = connection.execute(
                    sa.select(patrol_job.c.job_id).where(
                        patrol_job.c.operating_unit_id == unit.operating_unit_id,
                        patrol_job.c.status.in_(ACTIVE_JOB_STATUSES),
                    )
                ).scalar_one_or_none()
                if active is not None:
                    raise RuntimeQueueConflict(
                        f"operating unit already has active job: {unit.operating_unit_id}"
                    )
                job_id = f"job_{uuid4().hex[:24]}"
                request_id = (
                    "request_"
                    + hashlib.sha256(
                        f"{idempotency_key}\x1f{unit.operating_unit_id}".encode()
                    ).hexdigest()
                )
                job_rows.append(
                    {
                        "job_id": job_id,
                        "batch_id": batch_id,
                        "request_id": request_id,
                        "operating_unit_id": unit.operating_unit_id,
                        "shop_id": unit.shop_id,
                        "site_code": unit.site_code,
                        "parent_asin": unit.parent_asin,
                        "parent_seller_sku": unit.parent_seller_sku,
                        "shop_account_ref": unit.shop_account,
                        "job_type": "patrol.run",
                        "status": JobStatus.PENDING.value,
                        "payload_json": {
                            "binding": {
                                "shop_id": unit.shop_id,
                                "site_code": unit.site_code,
                                "parent_asin": unit.parent_asin,
                                "parent_seller_sku": unit.parent_seller_sku,
                                "shop_account": unit.shop_account,
                                "owner_user_ids": list(unit.owner_user_ids),
                            },
                            "trigger_type": trigger_type.value,
                            "request_id": request_id,
                            "client_request_id": effective_scope.get("request_id"),
                            "trace_id": effective_scope.get("trace_id"),
                            "initialization_ready": bool(
                                effective_scope.get("initialization_ready", True)
                            ),
                            "reuse_fact_run_id": effective_scope.get("reuse_fact_run_id"),
                            "batch_id": batch_id,
                        },
                        "retry_count": 0,
                        "max_retry": self.max_retry,
                        "next_retry_at": _utc_naive(now),
                    }
                )
            for offset in range(0, len(job_rows), self.job_insert_batch_size):
                connection.execute(
                    sa.insert(patrol_job),
                    job_rows[offset : offset + self.job_insert_batch_size],
                )
            transaction.commit()
        except Exception:
            if transaction.is_active:
                transaction.rollback()
            raise
        finally:
            for lock_name in reversed(lock_names):
                connection.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": lock_name},
                )
            connection.close()
        return batch_id, True

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        allowed_shop_ids: frozenset[int] | None = None,
        allowed_batch_ids: frozenset[str] | None = None,
    ) -> ClaimedJob | None:
        current = now or utc_now()
        with self.engine.begin() as connection:
            conditions = [
                patrol_job.c.status == JobStatus.PENDING.value,
                sa.or_(
                    patrol_job.c.next_retry_at.is_(None),
                    patrol_job.c.next_retry_at <= _utc_naive(current),
                ),
            ]
            if allowed_shop_ids is not None:
                if not allowed_shop_ids:
                    return None
                conditions.append(patrol_job.c.shop_id.in_(allowed_shop_ids))
            if allowed_batch_ids is not None:
                if not allowed_batch_ids:
                    return None
                conditions.append(patrol_job.c.batch_id.in_(allowed_batch_ids))
            row = (
                connection.execute(
                    sa.select(patrol_job)
                    .where(*conditions)
                    .order_by(patrol_job.c.created_at, patrol_job.c.job_id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            connection.execute(
                sa.update(patrol_job)
                .where(patrol_job.c.job_id == row["job_id"])
                .values(
                    status=JobStatus.RUNNING.value,
                    locked_by=worker_id,
                    locked_at=_utc_naive(current),
                )
            )
            connection.execute(
                sa.update(patrol_batch)
                .where(patrol_batch.c.batch_id == row["batch_id"])
                .values(
                    pending_count=patrol_batch.c.pending_count - 1,
                    running_count=patrol_batch.c.running_count + 1,
                )
            )
            return ClaimedJob(
                job_id=row["job_id"],
                batch_id=row["batch_id"],
                request_id=row["request_id"],
                job_type=row["job_type"],
                operating_unit_id=row["operating_unit_id"],
                payload=dict(row["payload_json"]),
                retry_count=row["retry_count"],
                max_retry=row["max_retry"],
            )

    def succeed(self, job_id: str, *, result_ref: str) -> None:
        with self.engine.begin() as connection:
            job = self._lock_running_job(connection, job_id)
            connection.execute(
                sa.update(patrol_job)
                .where(patrol_job.c.job_id == job_id)
                .values(
                    status=JobStatus.SUCCEEDED.value,
                    result_ref=result_ref,
                    locked_by=None,
                    locked_at=None,
                    last_error_code=None,
                    last_error_message=None,
                )
            )
            self._move_batch_count(connection, job["batch_id"], running=-1, succeeded=1)

    def fail(
        self,
        job_id: str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool = True,
        now: datetime | None = None,
    ) -> JobStatus:
        current = now or utc_now()
        with self.engine.begin() as connection:
            job = self._lock_running_job(connection, job_id)
            retry_count = int(job["retry_count"]) + 1
            terminal = not retryable or retry_count >= int(job["max_retry"])
            if terminal:
                status = JobStatus.DEAD
                next_retry_at = None
                batch_delta = {"running": -1, "failed": 1}
            else:
                status = JobStatus.PENDING
                delay = self.base_backoff_seconds * (2 ** (retry_count - 1))
                next_retry_at = current + timedelta(seconds=delay)
                batch_delta = {"running": -1, "pending": 1}
            connection.execute(
                sa.update(patrol_job)
                .where(patrol_job.c.job_id == job_id)
                .values(
                    status=status.value,
                    retry_count=retry_count,
                    next_retry_at=_utc_naive(next_retry_at),
                    locked_by=None,
                    locked_at=None,
                    last_error_code=error_code[:64],
                    last_error_message=error_message[:65535],
                )
            )
            self._move_batch_count(connection, job["batch_id"], **batch_delta)
            return status

    def reclaim_stale(
        self,
        *,
        older_than: timedelta,
        now: datetime | None = None,
        allowed_batch_ids: frozenset[str] | None = None,
    ) -> int:
        if allowed_batch_ids is not None and not allowed_batch_ids:
            return 0
        current = now or utc_now()
        cutoff = current - older_than
        reclaimed = 0
        while True:
            with self.engine.begin() as connection:
                conditions = [
                        patrol_job.c.status == JobStatus.RUNNING.value,
                        patrol_job.c.locked_at < _utc_naive(cutoff),
                ]
                if allowed_batch_ids is not None:
                    conditions.append(patrol_job.c.batch_id.in_(allowed_batch_ids))
                job_id = connection.execute(
                    sa.select(patrol_job.c.job_id)
                    .where(*conditions)
                    .order_by(patrol_job.c.locked_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if job_id is None:
                    return reclaimed
                job = self._lock_running_job(connection, job_id)
                retry_count = int(job["retry_count"]) + 1
                terminal = retry_count >= int(job["max_retry"])
                connection.execute(
                    sa.update(patrol_job)
                    .where(patrol_job.c.job_id == job_id)
                    .values(
                        status=(JobStatus.DEAD if terminal else JobStatus.PENDING).value,
                        retry_count=retry_count,
                        next_retry_at=None if terminal else _utc_naive(current),
                        locked_by=None,
                        locked_at=None,
                        last_error_code="WORKER_LEASE_EXPIRED",
                        last_error_message="stale RUNNING job reclaimed",
                    )
                )
                self._move_batch_count(
                    connection,
                    job["batch_id"],
                    running=-1,
                    **({"failed": 1} if terminal else {"pending": 1}),
                )
                reclaimed += 1

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(patrol_batch).where(patrol_batch.c.batch_id == batch_id)
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row is not None else None

    def archive_batches(self, batch_ids: frozenset[str]) -> dict[str, int]:
        if not batch_ids:
            return {"batches": 0, "jobs": 0}
        with self.engine.begin() as connection:
            batches = connection.execute(
                sa.select(patrol_batch.c.batch_id)
                .where(patrol_batch.c.batch_id.in_(batch_ids))
                .with_for_update()
            ).scalars().all()
            missing = batch_ids - frozenset(batches)
            if missing:
                raise KeyError(f"batches not found: {sorted(missing)}")
            job_rows = connection.execute(
                sa.select(patrol_job.c.job_id, patrol_job.c.status)
                .where(patrol_job.c.batch_id.in_(batch_ids))
                .with_for_update()
            ).all()
            allowed_statuses = {
                JobStatus.DEAD.value,
                JobStatus.PENDING.value,
                JobStatus.SUCCEEDED.value,
                JobStatus.ARCHIVED.value,
            }
            forbidden = sorted({
                status
                for _, status in job_rows
                if status not in allowed_statuses
            })
            if forbidden:
                raise RuntimeQueueConflict(
                    "archive requires terminal or unclaimed batches; "
                    f"found statuses: {forbidden}"
                )
            archived_jobs = connection.execute(
                sa.update(patrol_job)
                .where(
                    patrol_job.c.batch_id.in_(batch_ids),
                    patrol_job.c.status.in_(
                        {JobStatus.DEAD.value, JobStatus.PENDING.value}
                    ),
                )
                .values(
                    status=JobStatus.ARCHIVED.value,
                    next_retry_at=None,
                    locked_by=None,
                    locked_at=None,
                )
            ).rowcount
            archived_batches = 0
            for batch_id in batch_ids:
                counts = {
                    str(status): int(count)
                    for status, count in connection.execute(
                        sa.select(patrol_job.c.status, sa.func.count())
                        .where(patrol_job.c.batch_id == batch_id)
                        .group_by(patrol_job.c.status)
                    )
                }
                succeeded = int(counts.get(JobStatus.SUCCEEDED.value, 0))
                total = sum(int(value) for value in counts.values())
                batch_status = (
                    BatchStatus.SUCCEEDED
                    if succeeded == total
                    else BatchStatus.PARTIAL_SUCCESS
                    if succeeded
                    else BatchStatus.ARCHIVED
                )
                archived_batches += int(connection.execute(
                    sa.update(patrol_batch)
                    .where(patrol_batch.c.batch_id == batch_id)
                    .values(
                        status=batch_status.value,
                        pending_count=0,
                        running_count=0,
                        succeeded_count=succeeded,
                        failed_count=total - succeeded,
                        finished_at=sa.func.coalesce(
                            patrol_batch.c.finished_at,
                            _utc_naive(utc_now()),
                        ),
                    )
                ).rowcount or 0)
            return {
                "batches": archived_batches,
                "jobs": int(archived_jobs or 0),
            }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(sa.select(patrol_job).where(patrol_job.c.job_id == job_id))
                .mappings()
                .one_or_none()
            )
            return dict(row) if row is not None else None

    def jobs_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    sa.select(patrol_job)
                    .where(patrol_job.c.batch_id == batch_id)
                    .order_by(patrol_job.c.created_at, patrol_job.c.job_id)
                ).mappings()
            ]

    def stats(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return dict(
                connection.execute(
                    sa.select(patrol_job.c.status, sa.func.count()).group_by(patrol_job.c.status)
                )
            )

    def active_operating_unit_ids(self, operating_unit_ids: set[str]) -> set[str]:
        if not operating_unit_ids:
            return set()
        with self.engine.connect() as connection:
            return set(
                connection.execute(
                    sa.select(patrol_job.c.operating_unit_id)
                    .where(
                        patrol_job.c.operating_unit_id.in_(operating_unit_ids),
                        patrol_job.c.status.in_(ACTIVE_JOB_STATUSES),
                    )
                    .distinct()
                ).scalars()
            )

    def _lock_running_job(self, connection, job_id: str):
        row = (
            connection.execute(
                sa.select(patrol_job).where(patrol_job.c.job_id == job_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        if row["status"] != JobStatus.RUNNING.value:
            raise RuntimeQueueConflict(f"job is not RUNNING: {job_id}")
        return row

    def _move_batch_count(
        self,
        connection,
        batch_id: str,
        *,
        pending: int = 0,
        running: int = 0,
        succeeded: int = 0,
        failed: int = 0,
    ) -> None:
        batch = (
            connection.execute(
                sa.select(patrol_batch).where(patrol_batch.c.batch_id == batch_id).with_for_update()
            )
            .mappings()
            .one()
        )
        counts = {
            "pending_count": batch["pending_count"] + pending,
            "running_count": batch["running_count"] + running,
            "succeeded_count": batch["succeeded_count"] + succeeded,
            "failed_count": batch["failed_count"] + failed,
        }
        if min(counts.values()) < 0 or sum(counts.values()) > batch["total_count"]:
            raise RuntimeQueueConflict("invalid batch aggregate transition")
        terminal_count = counts["succeeded_count"] + counts["failed_count"]
        if terminal_count == batch["total_count"]:
            if counts["failed_count"] == 0:
                status = BatchStatus.SUCCEEDED
            elif counts["succeeded_count"] == 0:
                status = BatchStatus.FAILED
            else:
                status = BatchStatus.PARTIAL_SUCCESS
            finished_at = _utc_naive(utc_now())
        else:
            status = BatchStatus.RUNNING
            finished_at = None
        connection.execute(
            sa.update(patrol_batch)
            .where(patrol_batch.c.batch_id == batch_id)
            .values(**counts, status=status.value, finished_at=finished_at)
        )


class InMemoryRuntimeQueue:
    """与 MySQL 运行队列查询/入队语义一致的离线测试替身。"""

    def __init__(self, *, max_retry: int = 3) -> None:
        self.max_retry = max_retry
        self._batches_by_key: dict[str, tuple[tuple[Any, ...], str]] = {}
        self._batches: dict[str, dict[str, Any]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._batch_jobs: dict[str, list[str]] = {}

    def create_batch(
        self,
        *,
        trigger_type: TriggerType,
        business_date: date,
        units: list[OperatingUnitBinding],
        rule_bundle_version: str,
        idempotency_key: str,
        scope: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        unique_units = {unit.operating_unit_id: unit for unit in units}
        if len(unique_units) != len(units):
            raise ValueError("batch contains duplicate operating units")
        if not units:
            raise ValueError("batch must contain at least one operating unit")
        effective_scope = scope or {"operating_unit_ids": sorted(unique_units)}
        content = (
            trigger_type.value,
            business_date,
            scope_hash(units),
            rule_bundle_version,
            canonical_json(effective_scope),
        )
        existing = self._batches_by_key.get(idempotency_key)
        if existing is not None:
            existing_content, batch_id = existing
            if existing_content != content:
                raise RuntimeQueueConflict("idempotency key was reused with different content")
            return batch_id, False

        batch_id = f"batch_{uuid4().hex[:24]}"
        self._batches_by_key[idempotency_key] = (content, batch_id)
        self._batches[batch_id] = {
            "batch_id": batch_id,
            "trigger_type": trigger_type.value,
            "business_date": business_date,
            "status": BatchStatus.RUNNING.value,
            "total_count": len(units),
            "pending_count": len(units),
            "running_count": 0,
            "succeeded_count": 0,
            "failed_count": 0,
            "started_at": utc_now(),
            "finished_at": None,
        }
        self._batch_jobs[batch_id] = []
        for unit in units:
            job_id = f"job_{uuid4().hex[:24]}"
            request_id = (
                "request_"
                + hashlib.sha256(
                    f"{idempotency_key}\x1f{unit.operating_unit_id}".encode()
                ).hexdigest()
            )
            self._jobs[job_id] = {
                "job_id": job_id,
                "batch_id": batch_id,
                "request_id": request_id,
                "operating_unit_id": unit.operating_unit_id,
                "status": JobStatus.PENDING.value,
                "payload_json": {
                    "binding": {
                        "shop_id": unit.shop_id,
                        "site_code": unit.site_code,
                        "parent_asin": unit.parent_asin,
                        "parent_seller_sku": unit.parent_seller_sku,
                        "shop_account": unit.shop_account,
                        "owner_user_ids": list(unit.owner_user_ids),
                    },
                    "trigger_type": trigger_type.value,
                    "request_id": request_id,
                    "client_request_id": effective_scope.get("request_id"),
                    "trace_id": effective_scope.get("trace_id"),
                    "initialization_ready": bool(effective_scope.get("initialization_ready", True)),
                    "reuse_fact_run_id": effective_scope.get("reuse_fact_run_id"),
                    "batch_id": batch_id,
                },
                "retry_count": 0,
                "max_retry": self.max_retry,
                "last_error_message": None,
                "result_ref": None,
            }
            self._batch_jobs[batch_id].append(job_id)
        return batch_id, True

    def jobs_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        return [dict(self._jobs[job_id]) for job_id in self._batch_jobs.get(batch_id, [])]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        batch = self._batches.get(batch_id)
        return dict(batch) if batch is not None else None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return dict(job) if job is not None else None

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for job in self._jobs.values():
            status = str(job["status"])
            counts[status] = counts.get(status, 0) + 1
        return counts
