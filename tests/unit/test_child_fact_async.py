from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clients.mcp_client import McpToolResult
from integrations.child_fact_async import ChildFactWorker, ClaimedChildFactTask


def _task() -> ClaimedChildFactTask:
    return ClaimedChildFactTask(
        task_id="cft_0123456789abcdef01234567",
        operating_unit_id="ou_0123456789abcdef01234567",
        child_asin="B0CHILD01",
        domain="child_detail",
        tool_name="erp_asin_full_detail",
        arguments={"asin": "B0CHILD01", "siteCode": "US"},
        retry_count=0,
        max_retry=3,
    )


class FakeService:
    def __init__(self, task: ClaimedChildFactTask | None) -> None:
        self.task = task
        self.successes: list[tuple[ClaimedChildFactTask, McpToolResult]] = []
        self.failures: list[tuple[ClaimedChildFactTask, Exception]] = []

    def claim(self, worker_id: str):
        assert worker_id == "worker-test"
        task, self.task = self.task, None
        return task

    def succeed(self, task, result):
        self.successes.append((task, result))

    def fail(self, task, error):
        self.failures.append((task, error))


class FakeClient:
    def __init__(self, result: McpToolResult | Exception) -> None:
        self.result = result
        self.calls = []

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_child_fact_worker_persists_successful_snapshot():
    task = _task()
    service = FakeService(task)
    result = McpToolResult(
        tool_name=task.tool_name,
        data=[{"asin": task.child_asin}],
        raw={"structuredContent": {"data": [{"asin": task.child_asin}]}},
        request_hash="sha256:" + "1" * 64,
        fetched_at=datetime.now(UTC),
    )
    client = FakeClient(result)
    worker = ChildFactWorker(
        service,
        client,
        worker_id="worker-test",
        minimum_interval_seconds=0,
    )

    assert await worker.process_once() is True
    assert service.successes == [(task, result)]
    assert service.failures == []


@pytest.mark.asyncio
async def test_child_fact_worker_records_failure_without_raising():
    task = _task()
    service = FakeService(task)
    error = RuntimeError("rate limited")
    worker = ChildFactWorker(
        service,
        FakeClient(error),
        worker_id="worker-test",
        minimum_interval_seconds=0,
    )

    assert await worker.process_once() is True
    assert service.successes == []
    assert service.failures == [(task, error)]
