from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import bindparam, text

from core.enums import TriggerType
from core.operating_unit import OperatingUnitBinding
from facts.collector import TOOL_NAMES
from integrations.rollout import RolloutPolicy
from integrations.runtime_worker import MySqlRuntimeWorker
from scripts.run_real_patrol_batch import _evidence, _require_delivery_runtime
from web.backend.deps import (
    get_database_engine,
    get_runtime_orchestrator,
    get_runtime_queue,
)


def _source_units(
    batch_ids: list[str],
    count: int,
    parent_asins: frozenset[str],
) -> list[OperatingUnitBinding]:
    engine = get_database_engine()
    statement = text(
        """
        WITH ranked AS (
            SELECT j.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY j.parent_asin
                       ORDER BY j.created_at DESC,j.job_id DESC
                   ) AS rank_no
            FROM t_patrol_job j
            WHERE j.batch_id IN :batch_ids
              AND j.status='SUCCEEDED'
              AND j.result_ref IS NOT NULL
        )
        SELECT shop_id,site_code,parent_asin,parent_seller_sku,
               shop_account_ref,payload_json
        FROM ranked
        WHERE rank_no=1
        ORDER BY parent_asin
        """
    ).bindparams(bindparam("batch_ids", expanding=True))
    with engine.connect() as connection:
        active_jobs = connection.execute(
            text("SELECT COUNT(*) FROM t_patrol_job WHERE status IN ('PENDING','RUNNING')")
        ).scalar_one()
        if active_jobs:
            raise RuntimeError(f"runtime queue has active jobs: {active_jobs}")
        rows = connection.execute(statement, {"batch_ids": batch_ids}).mappings().all()
    if parent_asins:
        rows = [row for row in rows if str(row["parent_asin"]).upper() in parent_asins]
    if len(rows) != count:
        raise RuntimeError(
            f"source batches contain {len(rows)} unique successful ASINs; expected {count}"
        )
    units = []
    for row in rows:
        payload = row["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        units.append(
            OperatingUnitBinding(
                shop_id=row["shop_id"],
                site_code=row["site_code"],
                parent_asin=row["parent_asin"],
                parent_seller_sku=row["parent_seller_sku"],
                shop_account=row["shop_account_ref"],
            )
        )
    return units


def _resume_units(batch_id: str, count: int) -> list[OperatingUnitBinding]:
    engine = get_database_engine()
    with engine.connect() as connection:
        foreign_active_jobs = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM t_patrol_job
                WHERE status IN ('PENDING','RUNNING') AND batch_id<>:batch_id
                """
            ),
            {"batch_id": batch_id},
        ).scalar_one()
        rows = connection.execute(
            text(
                """
                SELECT shop_id,site_code,parent_asin,parent_seller_sku,
                       shop_account_ref,payload_json,status
                FROM t_patrol_job
                WHERE batch_id=:batch_id
                ORDER BY parent_asin,job_id
                """
            ),
            {"batch_id": batch_id},
        ).mappings().all()
    if foreign_active_jobs:
        raise RuntimeError(f"runtime queue has foreign active jobs: {foreign_active_jobs}")
    if len(rows) != count:
        raise RuntimeError(f"resume batch contains {len(rows)} jobs; expected {count}")
    invalid_statuses = sorted({row["status"] for row in rows} - {"PENDING", "SUCCEEDED"})
    if invalid_statuses:
        raise RuntimeError(f"resume batch has invalid job statuses: {invalid_statuses}")
    units = []
    for row in rows:
        payload = row["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        units.append(
            OperatingUnitBinding(
                shop_id=row["shop_id"],
                site_code=row["site_code"],
                parent_asin=row["parent_asin"],
                parent_seller_sku=row["parent_seller_sku"],
                shop_account=row["shop_account_ref"],
            )
        )
    return units


async def run(
    source_batch_ids: list[str],
    count: int,
    parent_asins: frozenset[str],
    resume_batch_id: str | None = None,
) -> int:
    _require_delivery_runtime()
    units = (
        _resume_units(resume_batch_id, count)
        if resume_batch_id
        else _source_units(source_batch_ids, count, parent_asins)
    )
    token = uuid4().hex[:12]
    queue = get_runtime_queue()
    if resume_batch_id:
        batch_id = resume_batch_id
        print(f"resuming recollect_batch_id={batch_id} jobs={count}")
    else:
        batch_id, created = queue.create_batch(
            trigger_type=TriggerType.MANUAL,
            business_date=date.today(),
            units=units,
            rule_bundle_version="real-20-asin-recollect-v2",
            idempotency_key=f"REAL_20_ASIN_RECOLLECT:{date.today().isoformat()}:{token}",
            scope={
                "kind": "REAL_20_ASIN_RECOLLECT",
                "request_id": f"real-20-asin-recollect-{token}",
                "trace_id": f"real-20-asin-recollect-trace-{token}",
                "initialization_ready": False,
                "external_delivery": "SUPPRESSED",
                "source_batch_ids": source_batch_ids,
                "parent_asins": [unit.parent_asin for unit in units],
                "operating_unit_ids": [unit.operating_unit_id for unit in units],
            },
        )
        if not created:
            raise RuntimeError("recollection batch unexpectedly already existed")
        print(f"created recollect_batch_id={batch_id} jobs={count}")

    async def patrol_handler(job):
        envelope = await get_runtime_orchestrator().run_job(job, as_of=date.today())
        return envelope.run.run_id

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id=f"real-20-recollect:{token}",
        handlers={"patrol.run": patrol_handler},
        rollout_policy=RolloutPolicy(
            stage="CANARY",
            allowlisted_shop_ids=sorted({unit.shop_id for unit in units}),
        ),
    )
    deadline = datetime.now(UTC).timestamp() + 7200
    while True:
        result = await worker.process_once()
        batch = queue.get_batch(batch_id)
        if batch is None:
            raise RuntimeError("recollection batch disappeared during execution")
        terminal = int(batch["succeeded_count"]) + int(batch["failed_count"])
        print(
            f"progress batch={batch_id} terminal={terminal}/{count} "
            f"pending={batch['pending_count']} running={batch['running_count']} "
            f"succeeded={batch['succeeded_count']} failed={batch['failed_count']}"
        )
        if terminal == count:
            break
        if datetime.now(UTC).timestamp() >= deadline:
            raise TimeoutError("real ASIN recollection exceeded two hours")
        if not result.processed:
            await asyncio.sleep(2)

    evidence = _evidence(batch_id)
    for item in evidence:
        print(
            f"result asin={item.parent_asin} job={item.job_status} "
            f"run={item.run_status or '-'} facts={item.raw_fact_success_count}/"
            f"{item.raw_fact_count} snapshot={item.snapshot_count} "
            f"occurrences={item.occurrence_count} outbox={item.outbox_status or '-'} "
            f"error={item.error_code or '-'}"
        )
    complete = [
        item
        for item in evidence
        if item.job_status == "SUCCEEDED"
        and item.raw_fact_count >= len(TOOL_NAMES)
        and item.raw_fact_success_count == item.raw_fact_count
        and item.snapshot_count == 1
        and item.outbox_status == "SUPPRESSED"
    ]
    print(
        f"summary batch_id={batch_id} jobs={len(evidence)} "
        f"facts_ok={sum(item.raw_fact_success_count for item in evidence)}/"
        f"{sum(item.raw_fact_count for item in evidence)} complete={len(complete)}/{count}"
    )
    return 0 if len(complete) == count else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Recollect a verified real-ASIN batch")
    parser.add_argument("--source-batch", action="append", default=[])
    parser.add_argument("--resume-batch")
    parser.add_argument("--parent-asin", action="append", default=[])
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    if args.count < 1 or args.count > 50:
        raise SystemExit("count must be between 1 and 50")
    if bool(args.source_batch) == bool(args.resume_batch):
        raise SystemExit("provide either --source-batch or --resume-batch")
    parent_asins = frozenset(str(value).strip().upper() for value in args.parent_asin)
    if parent_asins and len(parent_asins) != args.count:
        raise SystemExit("count must match the number of unique --parent-asin values")
    return asyncio.run(
        run(args.source_batch, args.count, parent_asins, args.resume_batch)
    )


if __name__ == "__main__":
    raise SystemExit(main())
