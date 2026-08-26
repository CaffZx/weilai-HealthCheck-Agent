from __future__ import annotations

from datetime import date
from uuid import uuid4

from sqlalchemy import text

from clients.mcp_client import FakeMcpClient
from core.enums import ExecutionReadiness, JobStatus, SignalType, TriggerType
from core.operating_unit import OperatingUnitBinding
from facts.collector import (
    DEFAULT_DISABLED_FACT_KEYS,
    TOOL_NAMES,
    FactCollector,
)
from facts.normalizer import FactNormalizer
from facts.quality import FactQualityService
from facts.snapshot import FactSnapshotBuilder
from inspector.legacy_rule_adapter import LegacyRuleAdapter
from inspector.patrol_orchestrator import PatrolOrchestrator
from inspector.signal_builder import SignalBuilder
from inspector.signal_reconciler import SignalReconciler
from integrations.database import create_database_engine
from integrations.rollout import RolloutPolicy
from integrations.runtime_queue import MySqlRuntimeQueue, RuntimeQueueConflict
from integrations.runtime_worker import MySqlRuntimeWorker
from tests.conftest import healthy_mcp_payload


def cleanup(engine, *, batch_id: str, run_id: str, signal_ids: list[str]) -> None:
    with engine.begin() as connection:
        run_ids = set(
            connection.execute(
                text(
                    "SELECT run_id FROM t_patrol_run WHERE job_id IN "
                    "(SELECT job_id FROM t_patrol_job WHERE batch_id=:batch_id)"
                ),
                {"batch_id": batch_id},
            ).scalars()
        )
        if run_id:
            run_ids.add(run_id)
        for actual_run_id in run_ids:
            connection.execute(
                text("DELETE FROM t_patrol_delivery_outbox WHERE aggregate_id=:run_id"),
                {"run_id": actual_run_id},
            )
            connection.execute(
                text("DELETE FROM t_patrol_raw_fact WHERE run_id=:run_id"),
                {"run_id": actual_run_id},
            )
            signal_ids.extend(
                value
                for value in connection.execute(
                    text(
                        "SELECT signal_id FROM t_patrol_signal_occurrence "
                        "WHERE run_id=:run_id"
                    ),
                    {"run_id": actual_run_id},
                ).scalars()
                if value not in signal_ids
            )
            connection.execute(
                text("DELETE FROM t_patrol_signal_occurrence WHERE run_id=:run_id"),
                {"run_id": actual_run_id},
            )
        if signal_ids:
            placeholders = ",".join(f":signal_{index}" for index in range(len(signal_ids)))
            params = {f"signal_{index}": value for index, value in enumerate(signal_ids)}
            connection.execute(
                text(f"DELETE FROM t_patrol_signal WHERE signal_id IN ({placeholders})"),
                params,
            )
        for actual_run_id in run_ids:
            connection.execute(
                text("DELETE FROM t_patrol_fact_snapshot WHERE run_id=:run_id"),
                {"run_id": actual_run_id},
            )
            connection.execute(
                text("DELETE FROM t_patrol_run WHERE run_id=:run_id"),
                {"run_id": actual_run_id},
            )
        connection.execute(
            text("DELETE FROM t_patrol_job WHERE batch_id=:batch_id"),
            {"batch_id": batch_id},
        )
        connection.execute(
            text("DELETE FROM t_patrol_batch WHERE batch_id=:batch_id"),
            {"batch_id": batch_id},
        )


async def verify() -> None:
    engine = create_database_engine()
    queue = MySqlRuntimeQueue(engine)
    token = uuid4().hex[:12]
    binding = OperatingUnitBinding(
        shop_id=920000000 + int(token[:8], 16),
        site_code="US",
        parent_asin="B0TEST123",
        parent_seller_sku="PSKU-001",
        shop_account=f"verify-s3-{token}",
    )
    batch_id = ""
    run_id = ""
    signal_ids: list[str] = []
    try:
        batch_id, created = queue.create_batch(
            trigger_type=TriggerType.MANUAL,
            business_date=date(2026, 7, 31),
            units=[binding],
            rule_bundle_version="verify-s3",
            idempotency_key=f"VERIFY_S3:{token}",
            scope={
                "kind": "MANUAL",
                "request_id": f"client-{token}",
                "trace_id": f"trace-{token}",
                "initialization_ready": False,
                "operating_unit_ids": [binding.operating_unit_id],
            },
        )
        assert created
        replay_batch_id, replay_created = queue.create_batch(
            trigger_type=TriggerType.MANUAL,
            business_date=date(2026, 7, 31),
            units=[binding],
            rule_bundle_version="verify-s3",
            idempotency_key=f"VERIFY_S3:{token}",
            scope={
                "kind": "MANUAL",
                "request_id": f"client-{token}",
                "trace_id": f"trace-{token}",
                "initialization_ready": False,
                "operating_unit_ids": [binding.operating_unit_id],
            },
        )
        assert replay_batch_id == batch_id
        assert not replay_created
        try:
            queue.create_batch(
                trigger_type=TriggerType.MANUAL,
                business_date=date(2026, 7, 31),
                units=[binding],
                rule_bundle_version="verify-s3",
                idempotency_key=f"VERIFY_S3:{token}",
                scope={
                    "kind": "MANUAL",
                    "request_id": f"client-{token}",
                    "trace_id": "different-trace",
                    "initialization_ready": False,
                    "operating_unit_ids": [binding.operating_unit_id],
                },
            )
        except RuntimeQueueConflict:
            pass
        else:
            raise AssertionError("same idempotency key with changed scope must conflict")
        queued_jobs = queue.jobs_for_batch(batch_id)
        assert len(queued_jobs) == 1
        queued_job = queued_jobs[0]
        assert queued_job["payload_json"]["client_request_id"] == f"client-{token}"
        assert queued_job["payload_json"]["trace_id"] == f"trace-{token}"
        assert queued_job["payload_json"]["initialization_ready"] is False

        mcp_payload = healthy_mcp_payload()
        for row in mcp_payload["erp_listing_product_info"]:
            row["SHOP_ID"] = binding.shop_id
        mcp = FakeMcpClient(responses=mcp_payload)
        orchestrator = PatrolOrchestrator(
            engine=engine,
            collector=FactCollector(mcp),
            normalizer=FactNormalizer(),
            quality_service=FactQualityService(),
            snapshot_builder=FactSnapshotBuilder(),
            rule_adapter=LegacyRuleAdapter(),
            signal_builder=SignalBuilder(producer_version="verify-s3"),
            reconciler=SignalReconciler(),
            service_version="verify-s3",
            rule_versions={"R2": "verify", "R3": "verify", "R7": "verify"},
        )
        captured_envelopes = []

        async def patrol_handler(job):
            envelope = await orchestrator.run_job(job, as_of=date(2026, 7, 29))
            captured_envelopes.append(envelope)
            return envelope.run.run_id

        worker = MySqlRuntimeWorker(
            queue=queue,
            worker_id="verify-s3-worker",
            handlers={"patrol.run": patrol_handler},
            rollout_policy=RolloutPolicy(
                stage="CANARY",
                allowlisted_shop_ids=[binding.shop_id],
            ),
            allowed_batch_ids=frozenset({batch_id}),
        )
        worker_result = await worker.process_once()
        assert worker_result.status is JobStatus.SUCCEEDED, queue.get_job(
            worker_result.job_id or ""
        )
        assert len(captured_envelopes) == 1
        envelope = captured_envelopes[0]
        run_id = envelope.run.run_id
        signal_ids = [signal.signal_id for signal in envelope.signals]

        payload = envelope.model_dump(mode="json")
        forbidden = {
            "mode_decision",
            "recommended_mode",
            "advertising_proposal",
            "decision_proposal",
            "approval_level",
            "proposed_actions",
            "llm_output",
        }
        assert not forbidden.intersection(payload)
        # 未配置异步子体服务的单次巡检只采集父体事实；子体扩展事实由 child_fact
        # 异步服务在后续轮次合并（见 test_child_fact_async 与 test_raw_fact_reuse）。
        expected_call_count = len(set(TOOL_NAMES) - DEFAULT_DISABLED_FACT_KEYS)
        expected_tools = {
            tool_name
            for key, tool_name in TOOL_NAMES.items()
            if key not in DEFAULT_DISABLED_FACT_KEYS
        }
        assert len(mcp.calls) == expected_call_count
        assert {tool_name for tool_name, _ in mcp.calls} == expected_tools
        assert envelope.signals
        assert all(
            signal.execution_readiness is ExecutionReadiness.BLOCKED_DEPENDENCY
            for signal in envelope.signals
            if signal.signal_type is not SignalType.DATA_QUALITY
        )

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT status FROM t_patrol_run WHERE run_id=:run_id"),
                {"run_id": run_id},
            ).scalar_one() in {"COMPLETED", "COMPLETED_WITH_GAPS", "BLOCKED"}
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_patrol_delivery_outbox "
                    "WHERE aggregate_id=:run_id AND status='PENDING'"
                ),
                {"run_id": run_id},
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT status FROM t_patrol_job WHERE job_id=:job_id"),
                {"job_id": queued_job["job_id"]},
            ).scalar_one() == JobStatus.SUCCEEDED.value
            raw_facts = connection.execute(
                text(
                    "SELECT fact_key,status,response_json,extracted_data_json,snapshot_id "
                    "FROM t_patrol_raw_fact WHERE run_id=:run_id ORDER BY fact_key"
                ),
                {"run_id": run_id},
            ).mappings().all()
            assert len(raw_facts) == expected_call_count
            assert all(row["status"] == "SUCCESS" for row in raw_facts)
            assert all(row["response_json"] is not None for row in raw_facts)
            assert all(row["extracted_data_json"] is not None for row in raw_facts)
            assert all(row["snapshot_id"] == envelope.run.fact_snapshot_id for row in raw_facts)
        print(
            "S3 pure patrol orchestration, raw fact archive, MySQL transaction, "
            "outbox, and zero-overreach verification passed"
        )
    finally:
        if batch_id:
            cleanup(engine, batch_id=batch_id, run_id=run_id, signal_ids=signal_ids)
        engine.dispose()


def main() -> None:
    import asyncio

    asyncio.run(verify())


if __name__ == "__main__":
    main()
