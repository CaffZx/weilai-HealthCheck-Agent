from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from core.enums import JobStatus
from integrations.rollout import RolloutPolicy
from integrations.runtime_queue import ClaimedJob, MySqlRuntimeQueue

JobHandler = Callable[[ClaimedJob], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class WorkerResult:
    processed: bool
    job_id: str | None = None
    status: JobStatus | None = None


class MySqlRuntimeWorker:
    def __init__(
        self,
        *,
        queue: MySqlRuntimeQueue,
        worker_id: str,
        handlers: dict[str, JobHandler],
        stale_after: timedelta = timedelta(minutes=30),
        rollout_policy: RolloutPolicy | None = None,
        allowed_batch_ids: frozenset[str] | None = None,
        handler_timeout_seconds: float | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        self.queue = queue
        self.worker_id = worker_id
        self.handlers = handlers
        self.stale_after = stale_after
        self.rollout_policy = rollout_policy or RolloutPolicy()
        self.allowed_batch_ids = allowed_batch_ids
        if handler_timeout_seconds is not None and handler_timeout_seconds <= 0:
            raise ValueError("handler_timeout_seconds must be positive")
        self.handler_timeout_seconds = handler_timeout_seconds

    async def process_once(self) -> WorkerResult:
        if not self.rollout_policy.patrol_execution_enabled:
            return WorkerResult(processed=False)
        job = self.queue.claim(
            worker_id=self.worker_id,
            allowed_shop_ids=self.rollout_policy.allowed_shop_ids,
            allowed_batch_ids=self.allowed_batch_ids,
        )
        if job is None:
            return WorkerResult(processed=False)
        handler = self.handlers.get(job.job_type)
        if handler is None:
            status = self.queue.fail(
                job.job_id,
                error_code="UNKNOWN_JOB_TYPE",
                error_message=f"no handler registered for {job.job_type}",
                retryable=False,
            )
            return WorkerResult(processed=True, job_id=job.job_id, status=status)
        try:
            if self.handler_timeout_seconds is None:
                result_ref = await handler(job)
            else:
                async with asyncio.timeout(self.handler_timeout_seconds):
                    result_ref = await handler(job)
            if not result_ref:
                raise ValueError("job handler returned an empty result_ref")
        except asyncio.CancelledError:
            self.queue.fail(
                job.job_id,
                error_code="WORKER_CANCELLED",
                error_message="worker cancelled while processing claimed job",
                retryable=True,
            )
            raise
        except TimeoutError:
            status = self.queue.fail(
                job.job_id,
                error_code="WORKER_HANDLER_TIMEOUT",
                error_message="patrol handler exceeded configured timeout",
                retryable=True,
            )
            return WorkerResult(processed=True, job_id=job.job_id, status=status)
        except Exception as exc:
            retryable = bool(getattr(exc, "retryable", True))
            error_code = str(getattr(exc, "code", type(exc).__name__)).upper()
            status = self.queue.fail(
                job.job_id,
                error_code=error_code,
                error_message=str(exc),
                retryable=retryable,
            )
            return WorkerResult(processed=True, job_id=job.job_id, status=status)
        self.queue.succeed(job.job_id, result_ref=result_ref)
        return WorkerResult(
            processed=True,
            job_id=job.job_id,
            status=JobStatus.SUCCEEDED,
        )

    def reclaim_stale(self) -> int:
        return self.queue.reclaim_stale(older_than=self.stale_after)

    async def drain(self) -> int:
        count = 0
        while True:
            result = await self.process_once()
            if not result.processed:
                return count
            count += 1

    async def run_forever(
        self,
        *,
        shutdown: asyncio.Event,
        poll_interval: float = 2.0,
        reclaim_every: int = 60,
    ) -> None:
        polls = 0
        while not shutdown.is_set():
            result = await self.process_once()
            polls += 1
            if polls >= reclaim_every:
                self.reclaim_stale()
                polls = 0
            if not result.processed:
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)
                except TimeoutError:
                    pass
