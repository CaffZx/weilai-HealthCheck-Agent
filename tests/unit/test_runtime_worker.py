from __future__ import annotations

import asyncio

import pytest

from core.enums import JobStatus
from integrations.rollout import RolloutPolicy
from integrations.runtime_queue import ClaimedJob
from integrations.runtime_worker import MySqlRuntimeWorker


class FakeQueue:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.succeeded = []
        self.failed = []

    def claim(self, *, worker_id, allowed_shop_ids=None, allowed_batch_ids=None):
        if allowed_batch_ids is not None:
            for index, queued_job in enumerate(self.jobs):
                if queued_job.batch_id in allowed_batch_ids:
                    return self.jobs.pop(index)
            return None
        if allowed_shop_ids is not None:
            for index, queued_job in enumerate(self.jobs):
                shop_id = queued_job.payload.get("binding", {}).get("shop_id")
                if shop_id in allowed_shop_ids:
                    return self.jobs.pop(index)
            return None
        return self.jobs.pop(0) if self.jobs else None

    def succeed(self, job_id, *, result_ref):
        self.succeeded.append((job_id, result_ref))

    def fail(self, job_id, **kwargs):
        self.failed.append((job_id, kwargs))
        return JobStatus.DEAD if not kwargs["retryable"] else JobStatus.PENDING

    def reclaim_stale(self, *, older_than):
        return 0


def job(job_type="patrol.run", shop_id=101):
    return ClaimedJob(
        job_id="job-1",
        batch_id="batch-1",
        request_id="request-1",
        job_type=job_type,
        operating_unit_id="ou_0123456789abcdef01234567",
        payload={"binding": {"shop_id": shop_id}},
        retry_count=0,
        max_retry=3,
    )


async def test_worker_marks_success_with_result_ref():
    queue = FakeQueue([job()])

    async def handler(claimed):
        return "pr_0123456789abcdef01234567"

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={"patrol.run": handler},
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    result = await worker.process_once()
    assert result.status is JobStatus.SUCCEEDED
    assert queue.succeeded == [("job-1", "pr_0123456789abcdef01234567")]


async def test_worker_retries_handler_failure():
    queue = FakeQueue([job()])

    async def handler(claimed):
        raise RuntimeError("temporary failure")

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={"patrol.run": handler},
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    result = await worker.process_once()
    assert result.status is JobStatus.PENDING
    assert queue.failed[0][1]["retryable"] is True
    assert queue.failed[0][1]["error_code"] == "RUNTIMEERROR"


async def test_worker_times_out_handler_and_releases_claim_for_retry():
    queue = FakeQueue([job()])

    async def handler(claimed):
        del claimed
        await asyncio.sleep(1)
        return "never-reached"

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id="worker-timeout",
        handlers={"patrol.run": handler},
        rollout_policy=RolloutPolicy(stage="FULL"),
        handler_timeout_seconds=0.01,
    )

    result = await worker.process_once()

    assert result.status is JobStatus.PENDING
    assert queue.failed[0][1] == {
        "error_code": "WORKER_HANDLER_TIMEOUT",
        "error_message": "patrol handler exceeded configured timeout",
        "retryable": True,
    }


async def test_worker_releases_claim_when_cancelled():
    queue = FakeQueue([job()])

    async def handler(claimed):
        raise asyncio.CancelledError

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={"patrol.run": handler},
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.process_once()

    assert queue.failed[0][1] == {
        "error_code": "WORKER_CANCELLED",
        "error_message": "worker cancelled while processing claimed job",
        "retryable": True,
    }


async def test_worker_rejects_unknown_job_type_without_retry():
    queue = FakeQueue([job("unknown")])
    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id="worker-1",
        handlers={},
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    result = await worker.process_once()
    assert result.status is JobStatus.DEAD
    assert queue.failed[0][1]["retryable"] is False
    assert queue.failed[0][1]["error_code"] == "UNKNOWN_JOB_TYPE"


async def test_internal_rollout_worker_does_not_claim_or_call_handler():
    queue = FakeQueue([job()])
    handler_called = False

    async def handler(claimed):
        nonlocal handler_called
        handler_called = True
        return "pr_0123456789abcdef01234567"

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id="worker-internal",
        handlers={"patrol.run": handler},
        rollout_policy=RolloutPolicy(stage="INTERNAL_ONLY"),
    )

    result = await worker.process_once()

    assert result.processed is False
    assert len(queue.jobs) == 1
    assert handler_called is False


async def test_canary_worker_skips_jobs_outside_shop_allowlist():
    queue = FakeQueue([job(shop_id=101)])

    async def handler(claimed):
        raise AssertionError("non-allowlisted job must not be handled")

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id="worker-canary",
        handlers={"patrol.run": handler},
        rollout_policy=RolloutPolicy(stage="CANARY", allowlisted_shop_ids=[202]),
    )

    result = await worker.process_once()

    assert result.processed is False
    assert len(queue.jobs) == 1


async def test_worker_only_claims_explicit_batch():
    queue = FakeQueue([job()])

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id="worker-batch-scoped",
        handlers={"patrol.run": lambda claimed: None},
        rollout_policy=RolloutPolicy(stage="FULL"),
        allowed_batch_ids=frozenset({"batch-other"}),
    )

    result = await worker.process_once()

    assert result.processed is False
    assert len(queue.jobs) == 1
