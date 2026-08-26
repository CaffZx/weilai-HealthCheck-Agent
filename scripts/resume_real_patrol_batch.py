from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime
from uuid import uuid4

from core.enums import JobStatus
from integrations.rollout import RolloutPolicy
from integrations.runtime_worker import MySqlRuntimeWorker
from scripts.run_real_patrol_batch import _deliver_to_control_center, _evidence
from web.backend.deps import (
    get_runtime_orchestrator,
    get_runtime_queue,
    load_settings,
)


async def resume(batch_id: str, concurrency: int) -> int:
    queue = get_runtime_queue()
    batch = queue.get_batch(batch_id)
    if batch is None:
        raise RuntimeError("batch does not exist")
    count = int(batch["total_count"])
    settings = load_settings()
    worker_settings = settings.get("worker", {}) or {}
    job_timeout = float(worker_settings.get("job_timeout_seconds", 900))
    batch_timeout = float(worker_settings.get("batch_timeout_seconds", 10800))
    deadline = datetime.now(UTC).timestamp() + batch_timeout
    token = uuid4().hex[:12]

    async def patrol_handler(job):
        envelope = await get_runtime_orchestrator().run_job(job, as_of=date.today())
        return envelope.run.run_id

    async def drain(worker_number: int) -> None:
        worker = MySqlRuntimeWorker(
            queue=queue,
            worker_id=f"resume-real-batch:{token}:{worker_number}",
            handlers={"patrol.run": patrol_handler},
            rollout_policy=RolloutPolicy(stage=str(settings["rollout"]["stage"])),
            allowed_batch_ids=frozenset({batch_id}),
            handler_timeout_seconds=job_timeout,
        )
        while datetime.now(UTC).timestamp() < deadline:
            current = queue.get_batch(batch_id)
            if current is None:
                raise RuntimeError("batch disappeared during execution")
            terminal = int(current["succeeded_count"]) + int(current["failed_count"])
            if terminal == count:
                return
            result = await worker.process_once()
            if not result.processed:
                await asyncio.sleep(2)
        raise TimeoutError("resumed batch exceeded configured timeout")

    workers = [asyncio.create_task(drain(index)) for index in range(concurrency)]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise

    evidence = _evidence(batch_id)
    succeeded = [item for item in evidence if item.job_status == JobStatus.SUCCEEDED.value]
    if not succeeded:
        raise RuntimeError(f"resumed batch has no successful jobs: {batch_id}")
    outbox_ids = await _deliver_to_control_center(batch_id, count)
    print(
        f"resumed batch={batch_id} succeeded={len(succeeded)}/{count} "
        f"control_center_delivered={len(outbox_ids)}",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume one existing real patrol batch")
    parser.add_argument("batch_id")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 8:
        parser.error("--concurrency must be between 1 and 8")
    return asyncio.run(resume(args.batch_id, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
