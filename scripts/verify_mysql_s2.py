from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from core.enums import BatchStatus, JobStatus, TriggerType
from core.operating_unit import OperatingUnitBinding
from integrations.database import create_database_engine
from integrations.runtime_queue import MySqlRuntimeQueue, daily_idempotency_key
from integrations.scheduler import SchedulerLease


def make_unit(index: int) -> OperatingUnitBinding:
    token = uuid4().hex[:8].upper()
    return OperatingUnitBinding(
        shop_id=900000000 + int(token, 16) + index,
        site_code="US",
        parent_asin=f"B0{token}",
        parent_seller_sku=f"VERIFY-{token}",
        shop_account=f"verify-shop-{token}",
    )


def job_row(engine, job_id: str) -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM t_patrol_job WHERE job_id=:job_id"),
                {"job_id": job_id},
            ).mappings().one()
        )


def cleanup(engine, batch_ids: set[str], lock_names: set[str]) -> None:
    with engine.begin() as connection:
        for batch_id in batch_ids:
            connection.execute(
                text("DELETE FROM t_patrol_job WHERE batch_id=:batch_id"),
                {"batch_id": batch_id},
            )
            connection.execute(
                text("DELETE FROM t_patrol_batch WHERE batch_id=:batch_id"),
                {"batch_id": batch_id},
            )
        for lock_name in lock_names:
            connection.execute(
                text("DELETE FROM t_patrol_scheduler_lock WHERE lock_name=:lock_name"),
                {"lock_name": lock_name},
            )


def main() -> None:
    engine = create_database_engine()
    queue = MySqlRuntimeQueue(
        engine,
        max_retry=2,
        base_backoff_seconds=1,
        job_insert_batch_size=1,
        idempotency_lock_timeout_seconds=30,
    )
    lease = SchedulerLease(engine)
    batch_ids: set[str] = set()
    lock_names: set[str] = set()
    now = datetime.now(UTC)
    try:
        units = [make_unit(1), make_unit(2)]
        key = daily_idempotency_key(date(2026, 7, 31), units, "verify-s2")

        def create_once():
            return queue.create_batch(
                trigger_type=TriggerType.DAILY_SCHEDULE,
                business_date=date(2026, 7, 31),
                units=units,
                rule_bundle_version="verify-s2",
                idempotency_key=key,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: create_once(), range(2)))
        assert len({item[0] for item in results}) == 1
        assert sorted(item[1] for item in results) == [False, True]
        batch_id = results[0][0]
        batch_ids.add(batch_id)
        claim_time = datetime.now(UTC) + timedelta(seconds=1)
        test_shop_ids = frozenset(unit.shop_id for unit in units)

        def claim(worker_id: str):
            return queue.claim(
                worker_id=worker_id,
                now=claim_time,
                allowed_shop_ids=test_shop_ids,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = list(executor.map(claim, ["verify-worker-1", "verify-worker-2"]))
        claimed = [job for job in jobs if job is not None]
        assert len({job.job_id for job in claimed}) == len(claimed)
        while len(claimed) < 2:
            next_job = queue.claim(
                worker_id=f"verify-worker-poll-{len(claimed)}",
                now=claim_time,
                allowed_shop_ids=test_shop_ids,
            )
            assert next_job is not None
            assert next_job.job_id not in {job.job_id for job in claimed}
            claimed.append(next_job)

        first, second = claimed
        queue.succeed(first.job_id, result_ref="pr_verify_success")
        status = queue.fail(
            second.job_id,
            error_code="VERIFY_RETRY",
            error_message="retry verification",
            retryable=True,
            now=claim_time + timedelta(seconds=1),
        )
        assert status is JobStatus.PENDING
        assert queue.claim(
            worker_id="too-early",
            now=claim_time + timedelta(seconds=1),
            allowed_shop_ids=test_shop_ids,
        ) is None
        retried = queue.claim(
            worker_id="retry-worker",
            now=claim_time + timedelta(seconds=3),
            allowed_shop_ids=test_shop_ids,
        )
        assert retried is not None and retried.job_id == second.job_id
        status = queue.fail(
            second.job_id,
            error_code="VERIFY_DEAD",
            error_message="max retry verification",
            retryable=True,
            now=claim_time + timedelta(seconds=4),
        )
        assert status is JobStatus.DEAD
        batch = queue.get_batch(batch_id)
        assert batch is not None
        assert batch["status"] == BatchStatus.PARTIAL_SUCCESS.value
        assert (batch["succeeded_count"], batch["failed_count"]) == (1, 1)

        allowlist_units = [make_unit(11), make_unit(12)]
        allowlist_batch, _ = queue.create_batch(
            trigger_type=TriggerType.MANUAL,
            business_date=date(2026, 7, 31),
            units=allowlist_units,
            rule_bundle_version="verify-s2",
            idempotency_key=f"VERIFY_ALLOWLIST:{uuid4().hex}",
        )
        batch_ids.add(allowlist_batch)
        allowed_claim = queue.claim(
            worker_id="allowlisted-worker",
            now=datetime.now(UTC) + timedelta(seconds=1),
            allowed_shop_ids=frozenset({allowlist_units[1].shop_id}),
        )
        assert allowed_claim is not None
        assert allowed_claim.payload["binding"]["shop_id"] == allowlist_units[1].shop_id
        blocked_job = next(
            job
            for job in queue.jobs_for_batch(allowlist_batch)
            if job["operating_unit_id"] == allowlist_units[0].operating_unit_id
        )
        assert blocked_job["status"] == JobStatus.PENDING.value
        queue.succeed(allowed_claim.job_id, result_ref="pr_verify_allowlisted")

        stale_unit = make_unit(3)
        stale_key = f"VERIFY_STALE:{uuid4().hex}"
        stale_batch, _ = queue.create_batch(
            trigger_type=TriggerType.MANUAL,
            business_date=date(2026, 7, 31),
            units=[stale_unit],
            rule_bundle_version="verify-s2",
            idempotency_key=stale_key,
        )
        batch_ids.add(stale_batch)
        stale_claim_time = datetime.now(UTC) + timedelta(seconds=1)
        stale_job = queue.claim(
            worker_id="crashed-worker",
            now=stale_claim_time,
            allowed_shop_ids=frozenset({stale_unit.shop_id}),
        )
        assert stale_job is not None
        assert queue.reclaim_stale(
            older_than=timedelta(minutes=5),
            now=stale_claim_time + timedelta(minutes=6),
            allowed_batch_ids=frozenset({stale_batch}),
        ) == 1
        assert job_row(engine, stale_job.job_id)["status"] == JobStatus.PENDING.value

        archive_units = [make_unit(4), make_unit(5)]
        archive_batch, _ = queue.create_batch(
            trigger_type=TriggerType.MANUAL,
            business_date=date(2026, 7, 31),
            units=archive_units,
            rule_bundle_version="verify-s2",
            idempotency_key=f"VERIFY_ARCHIVE:{uuid4().hex}",
        )
        batch_ids.add(archive_batch)
        completed_archive_job = queue.claim(
            worker_id="archive-success-verifier",
            allowed_shop_ids=frozenset({archive_units[0].shop_id}),
            allowed_batch_ids=frozenset({archive_batch}),
        )
        assert completed_archive_job is not None
        queue.succeed(completed_archive_job.job_id, result_ref="archive-success")
        assert queue.archive_batches(frozenset({archive_batch})) == {
            "batches": 1,
            "jobs": 1,
        }
        assert queue.get_batch(archive_batch)["status"] == BatchStatus.PARTIAL_SUCCESS.value
        archived_jobs = queue.jobs_for_batch(archive_batch)
        assert {job["status"] for job in archived_jobs} == {
            JobStatus.ARCHIVED.value,
            JobStatus.SUCCEEDED.value,
        }
        assert queue.claim(
            worker_id="archive-verifier",
            allowed_batch_ids=frozenset({archive_batch}),
        ) is None

        lock_name = f"verify-s2-{uuid4().hex}"
        lock_names.add(lock_name)
        assert lease.acquire(
            lock_name=lock_name,
            holder_id="holder-a",
            ttl=timedelta(seconds=10),
            now=now,
        )
        assert not lease.acquire(
            lock_name=lock_name,
            holder_id="holder-b",
            ttl=timedelta(seconds=10),
            now=now + timedelta(seconds=1),
        )
        assert lease.renew(
            lock_name=lock_name,
            holder_id="holder-a",
            ttl=timedelta(seconds=10),
            now=now + timedelta(seconds=2),
        )
        assert not lease.renew(
            lock_name=lock_name,
            holder_id="holder-b",
            ttl=timedelta(seconds=10),
            now=now + timedelta(seconds=3),
        )
        assert lease.acquire(
            lock_name=lock_name,
            holder_id="holder-b",
            ttl=timedelta(seconds=10),
            now=now + timedelta(seconds=13),
        )
        assert not lease.renew(
            lock_name=lock_name,
            holder_id="holder-b",
            ttl=timedelta(seconds=10),
            now=now + timedelta(seconds=24),
        )

        print(
            "S2 batch idempotency, claim, retry, reclaim, aggregate, "
            "and lease heartbeat verification passed"
        )
    finally:
        cleanup(engine, batch_ids, lock_names)
        engine.dispose()


if __name__ == "__main__":
    main()
