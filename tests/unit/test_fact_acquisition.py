"""FactAcquisition 组件的独立测试。

抽出采集组件的目的就是让"取数并归档"可以脱离编排器单独验证。
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa

from clients.mcp_client import FakeMcpClient, McpToolResult
from core.enums import InspectionRunStatus, TriggerType
from core.inspection_run import InspectionRun
from facts.collector import FactCollector
from inspector.fact_acquisition import AcquiredFacts, FactAcquisition
from integrations.repositories import PatrolUnitOfWork
from integrations.repositories.tables import metadata, patrol_raw_fact
from tests.conftest import healthy_mcp_payload


def _create_run(engine, binding, run_id: str) -> None:
    run = InspectionRun(
        run_id=run_id,
        job_id="job-test",
        batch_id="batch-test",
        request_id="request-test",
        operating_unit_id=binding.operating_unit_id,
        trigger_type=TriggerType.MANUAL,
        status=InspectionRunStatus.RECEIVED,
        rule_versions={"R2": "test", "R3": "test", "R7": "test"},
        started_at=datetime.now(UTC),
    )
    with PatrolUnitOfWork(engine) as work:
        assert work.repository is not None
        work.repository.create_run(run)
        work.commit()


@pytest.mark.asyncio
async def test_fresh_acquisition_collects_parents_and_archives_raw_facts(binding):
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    _create_run(engine, binding, "pr_0123456789abcdef01234567")
    mcp = FakeMcpClient(responses=healthy_mcp_payload())
    acquisition = FactAcquisition(engine=engine, collector=FactCollector(mcp))

    acquired = await acquisition.acquire(
        run_id="pr_0123456789abcdef01234567",
        binding=binding,
        operating_unit_id=binding.operating_unit_id,
        reuse_fact_run_id=None,
        as_of=date(2026, 7, 29),
    )

    assert isinstance(acquired, AcquiredFacts)
    assert acquired.as_of_date == date(2026, 7, 29)
    # 未配置异步子体服务：只有父体事实，无待就绪子体。
    assert acquired.pending_child_calls == {}
    assert isinstance(acquired.raw["product_info"], McpToolResult)

    expected_calls = FactCollector(mcp).build_calls(binding, date(2026, 7, 29))
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(patrol_raw_fact).where(
                patrol_raw_fact.c.run_id == "pr_0123456789abcdef01234567"
            )
        ).mappings().all()
    assert len(rows) == len(expected_calls)
    assert all(row["reused_from_raw_fact_id"] is None for row in rows)
