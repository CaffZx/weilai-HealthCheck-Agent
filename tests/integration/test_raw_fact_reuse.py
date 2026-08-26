from __future__ import annotations

from datetime import date

import sqlalchemy as sa

from clients.mcp_client import FakeMcpClient
from core.operating_unit import OperatingUnitBinding
from facts.collector import FactCollector
from facts.normalizer import FactNormalizer
from facts.quality import FactQualityService
from facts.snapshot import FactSnapshotBuilder
from inspector.legacy_rule_adapter import LegacyRuleAdapter
from inspector.patrol_orchestrator import PatrolOrchestrator
from inspector.signal_builder import SignalBuilder
from inspector.signal_reconciler import SignalReconciler
from integrations.repositories.tables import metadata, patrol_raw_fact
from integrations.runtime_queue import ClaimedJob
from tests.conftest import healthy_mcp_payload


class RecordingSignalReconciler(SignalReconciler):
    def __init__(self) -> None:
        self.facts_complete_calls: list[bool] = []

    def reconcile(self, **kwargs):
        self.facts_complete_calls.append(kwargs["facts_complete"])
        return super().reconcile(**kwargs)


def make_job(
    binding: OperatingUnitBinding,
    *,
    suffix: str,
    reuse_fact_run_id: str | None = None,
) -> ClaimedJob:
    return ClaimedJob(
        job_id=f"job-{suffix}",
        batch_id=f"batch-{suffix}",
        request_id=f"request-{suffix}",
        job_type="patrol.run",
        operating_unit_id=binding.operating_unit_id,
        payload={
            "binding": {
                "shop_id": binding.shop_id,
                "site_code": binding.site_code,
                "parent_asin": binding.parent_asin,
                "parent_seller_sku": binding.parent_seller_sku,
                "shop_account": binding.shop_account,
            },
            "trigger_type": "MANUAL",
            "initialization_ready": True,
            "reuse_fact_run_id": reuse_fact_run_id,
        },
        retry_count=0,
        max_retry=3,
    )


async def test_explicit_reuse_skips_mcp_and_preserves_raw_fact_lineage(binding):
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    mcp = FakeMcpClient(responses=healthy_mcp_payload())
    reconciler = RecordingSignalReconciler()
    orchestrator = PatrolOrchestrator(
        engine=engine,
        collector=FactCollector(mcp),
        normalizer=FactNormalizer(),
        quality_service=FactQualityService(),
        snapshot_builder=FactSnapshotBuilder(),
        rule_adapter=LegacyRuleAdapter(),
        signal_builder=SignalBuilder(producer_version="test-raw-reuse"),
        reconciler=reconciler,
        service_version="test-raw-reuse",
        rule_versions={"R2": "test", "R3": "test", "R7": "test"},
    )

    first = await orchestrator.run_job(
        make_job(binding, suffix="first"),
        as_of=date(2026, 7, 29),
    )
    first_call_count = len(mcp.calls)
    # 未配置异步子体服务时，单次巡检只采集父体事实（子体走 child_fact 异步服务）。
    assert first_call_count == len(
        orchestrator.collector.build_calls(binding, date(2026, 7, 29))
    )
    assert reconciler.facts_complete_calls == [False]

    second = await orchestrator.run_job(
        make_job(
            binding,
            suffix="second",
            reuse_fact_run_id=first.run.run_id,
        ),
        as_of=date(2026, 8, 3),
    )

    assert len(mcp.calls) == first_call_count
    with engine.connect() as connection:
        reused = connection.execute(
            sa.select(patrol_raw_fact).where(
                patrol_raw_fact.c.run_id == second.run.run_id
            )
        ).mappings().all()
    assert len(reused) == first_call_count
    assert all(row["reused_from_raw_fact_id"] for row in reused)
    assert {row["as_of_date"] for row in reused} == {date(2026, 7, 29)}
    assert second.run.fact_snapshot_id != first.run.fact_snapshot_id
    assert reconciler.facts_complete_calls == [False, False]
