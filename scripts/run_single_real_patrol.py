from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import text

from core.enums import TriggerType
from core.operating_unit import OperatingUnitBinding
from integrations.rollout import RolloutPolicy
from integrations.runtime_worker import MySqlRuntimeWorker
from scripts.run_real_patrol_batch import _evidence
from web.backend.deps import (
    get_database_engine,
    get_runtime_orchestrator,
    get_runtime_queue,
)


def _require_no_queued_work_for_shop(shop_id: int) -> None:
    with get_database_engine().connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT job_id,batch_id,parent_asin,status
                FROM t_patrol_job
                WHERE shop_id=:shop_id AND status IN ('PENDING','RUNNING')
                ORDER BY created_at,job_id
                """
            ),
            {"shop_id": shop_id},
        ).mappings().all()
    if rows:
        details = ", ".join(
            f"{row['job_id']}:{row['parent_asin']}:{row['status']}" for row in rows
        )
        raise RuntimeError(f"shop has active queued work; refusing ambiguous claim: {details}")


async def run(args: argparse.Namespace) -> int:
    binding = OperatingUnitBinding(
        shop_id=args.shop_id,
        shop_account=args.shop_account,
        site_code=args.site_code,
        parent_asin=args.parent_asin,
        parent_seller_sku=args.parent_seller_sku,
        owner_user_ids=(args.owner_user_id,),
    )
    token = uuid4().hex[:12]
    queue = get_runtime_queue()
    if args.resume_batch_id:
        batch_id = args.resume_batch_id
    else:
        _require_no_queued_work_for_shop(binding.shop_id)
        batch_id, created = queue.create_batch(
            trigger_type=TriggerType.MANUAL,
            business_date=date.today(),
            units=[binding],
            rule_bundle_version="single-real-patrol-v1",
            idempotency_key=f"SINGLE_REAL_PATROL:{date.today().isoformat()}:{token}",
            scope={
                "kind": "SINGLE_REAL_PATROL",
                "request_id": f"single-real-patrol-{token}",
                "trace_id": f"single-real-patrol-trace-{token}",
                "initialization_ready": True,
                "reuse_fact_run_id": args.reuse_fact_run_id,
                "external_delivery": "SUPPRESSED",
                "selection_source": "USER_CONFIRMED",
                "operating_mode": {
                    "code": args.operating_mode,
                    "name": args.operating_mode_name,
                    "source": "MANUAL_USER_OVERRIDE",
                },
                "owner": {
                    "user_id": args.owner_user_id,
                    "name": args.owner_name,
                    "source": "USER_CONFIRMED",
                },
                "operating_unit_ids": [binding.operating_unit_id],
                "parent_asins": [binding.parent_asin],
            },
        )
        if not created:
            raise RuntimeError("single patrol batch unexpectedly already existed")
    jobs = queue.jobs_for_batch(batch_id)
    if len(jobs) != 1:
        raise RuntimeError("single patrol batch must contain exactly one job")
    job_id = jobs[0]["job_id"]
    print(f"created batch_id={batch_id} job_id={job_id}")

    async def patrol_handler(job):
        if job.job_id != job_id:
            raise RuntimeError(f"worker claimed unexpected job: {job.job_id}")
        envelope = await get_runtime_orchestrator().run_job(job, as_of=date.today())
        return envelope.run.run_id

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id=f"single-real-patrol:{token}",
        handlers={"patrol.run": patrol_handler},
        rollout_policy=RolloutPolicy(
            stage="CANARY",
            allowlisted_shop_ids=[binding.shop_id],
        ),
    )
    deadline = datetime.now(UTC).timestamp() + args.timeout_seconds
    while True:
        result = await worker.process_once()
        batch = queue.get_batch(batch_id)
        if batch is None:
            raise RuntimeError("batch disappeared during execution")
        terminal = int(batch["succeeded_count"]) + int(batch["failed_count"])
        if result.processed:
            print(
                f"progress batch={batch_id} status={result.status.value if result.status else '-'} "
                f"retry_pending={batch['pending_count']}"
            )
        if terminal == 1:
            break
        if datetime.now(UTC).timestamp() >= deadline:
            raise TimeoutError("single real patrol exceeded configured timeout")
        if not result.processed:
            await asyncio.sleep(2)

    evidence = _evidence(batch_id)
    if len(evidence) != 1:
        raise RuntimeError("single patrol evidence count is not one")
    item = evidence[0]
    print(
        f"result asin={item.parent_asin} job={item.job_status} run_id={item.run_id or '-'} "
        f"run={item.run_status or '-'} facts={item.raw_fact_success_count}/{item.raw_fact_count} "
        f"snapshot={item.snapshot_count} occurrences={item.occurrence_count} "
        f"outbox={item.outbox_status or '-'} error={item.error_code or '-'}"
    )
    return 0 if item.job_status == "SUCCEEDED" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one user-confirmed real patrol")
    parser.add_argument("--shop-id", required=True, type=int)
    parser.add_argument("--shop-account", required=True)
    parser.add_argument("--site-code", required=True)
    parser.add_argument("--parent-asin", required=True)
    parser.add_argument("--parent-seller-sku", required=True)
    parser.add_argument("--owner-user-id", required=True, type=int)
    parser.add_argument("--owner-name", required=True)
    parser.add_argument("--operating-mode", default="TIME_BOXED_REPAIR")
    parser.add_argument("--operating-mode-name", default="限时修复")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--resume-batch-id")
    parser.add_argument("--reuse-fact-run-id")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
