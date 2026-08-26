from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import text

from core.enums import TriggerType
from core.operating_unit import OperatingUnitBinding
from integrations.rollout import RolloutPolicy
from integrations.runtime_worker import MySqlRuntimeWorker
from scripts.run_real_patrol_batch import _evidence, _require_safe_runtime
from web.backend.deps import (
    get_database_engine,
    get_runtime_orchestrator,
    get_runtime_queue,
)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _incomplete_bindings(source_batch_id: str) -> list[OperatingUnitBinding]:
    with get_database_engine().connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT j.shop_id,j.site_code,j.parent_asin,j.parent_seller_sku,
                       j.shop_account_ref,j.payload_json,j.status AS job_status,
                       r.status AS run_status
                FROM t_patrol_job j
                LEFT JOIN t_patrol_run r ON r.run_id=j.result_ref
                WHERE j.batch_id=:batch_id
                  AND (j.status<>'SUCCEEDED' OR r.status='BLOCKED')
                ORDER BY j.parent_asin,j.shop_id,j.parent_seller_sku
                """
            ),
            {"batch_id": source_batch_id},
        ).mappings().all()
    bindings = []
    for row in rows:
        payload = _json(row["payload_json"]) or {}
        bindings.append(
            OperatingUnitBinding(
                shop_id=row["shop_id"],
                site_code=row["site_code"],
                parent_asin=row["parent_asin"],
                parent_seller_sku=row["parent_seller_sku"],
                shop_account=row["shop_account_ref"],
            )
        )
    return bindings


async def run(args: argparse.Namespace) -> int:
    _require_safe_runtime()
    bindings = _incomplete_bindings(args.source_batch_id)
    if not bindings:
        print("source batch has no failed or blocked operating units")
        return 0
    if len(bindings) > args.max_units:
        raise RuntimeError(
            f"refusing to rerun {len(bindings)} units; --max-units={args.max_units}"
        )

    token = uuid4().hex[:12]
    queue = get_runtime_queue()
    batch_id, created = queue.create_batch(
        trigger_type=TriggerType.MANUAL,
        business_date=date.today(),
        units=bindings,
        rule_bundle_version="real-incomplete-rerun-v1",
        idempotency_key=f"RERUN_INCOMPLETE:{args.source_batch_id}:{token}",
        scope={
            "kind": "REAL_INCOMPLETE_RERUN",
            "source_batch_id": args.source_batch_id,
            "request_id": f"rerun-incomplete-{token}",
            "trace_id": f"rerun-incomplete-trace-{token}",
            "initialization_ready": False,
            "external_delivery": "SUPPRESSED",
            "selection_source": "SOURCE_BATCH_FAILED_OR_BLOCKED",
            "operating_unit_ids": [binding.operating_unit_id for binding in bindings],
        },
    )
    if not created:
        raise RuntimeError("rerun batch unexpectedly already existed")
    print(f"created batch_id={batch_id} jobs={len(bindings)}")

    async def patrol_handler(job):
        envelope = await get_runtime_orchestrator().run_job(job, as_of=date.today())
        return envelope.run.run_id

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id=f"rerun-incomplete:{token}",
        handlers={"patrol.run": patrol_handler},
        rollout_policy=RolloutPolicy(
            stage="CANARY",
            allowlisted_shop_ids=sorted({binding.shop_id for binding in bindings}),
        ),
        allowed_batch_ids=frozenset({batch_id}),
    )
    deadline = datetime.now(UTC).timestamp() + args.timeout_seconds
    while True:
        batch = queue.get_batch(batch_id)
        if batch is None:
            raise RuntimeError("rerun batch disappeared")
        terminal = int(batch["succeeded_count"]) + int(batch["failed_count"])
        print(
            f"progress batch={batch_id} terminal={terminal}/{len(bindings)} "
            f"pending={batch['pending_count']} running={batch['running_count']} "
            f"succeeded={batch['succeeded_count']} failed={batch['failed_count']}"
        )
        if terminal == len(bindings):
            break
        if datetime.now(UTC).timestamp() >= deadline:
            raise TimeoutError("incomplete-unit rerun exceeded configured timeout")
        result = await worker.process_once()
        if not result.processed:
            await asyncio.sleep(2)

    evidence = _evidence(batch_id)
    for item in evidence:
        print(
            f"result asin={item.parent_asin} shopId={item.shop_id} "
            f"job={item.job_status} run={item.run_status or '-'} "
            f"facts={item.raw_fact_success_count}/{item.raw_fact_count} "
            f"error={item.error_code or '-'}"
        )
    usable = sum(
        item.job_status == "SUCCEEDED" and item.run_status != "BLOCKED"
        for item in evidence
    )
    print(f"summary batch_id={batch_id} usable={usable}/{len(bindings)}")
    return 0 if usable == len(bindings) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Rerun failed or blocked real patrol units")
    parser.add_argument("--source-batch-id", required=True)
    parser.add_argument("--max-units", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
