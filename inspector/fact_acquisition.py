"""事实采集组件。

把"取到并归档一个经营单元的原始事实"这件事从编排器里独立出来：
  - 复用分支：从历史 run 读回原始事实并校验调用签名一致；
  - 新采分支：父体+补充由 collector 并行采集，子体扩展事实经异步 child_fact
    服务入队并读回已就绪快照，未就绪的记为 pending；
  - 两条分支最终都把原始事实同事务落库。

编排器只需调用 :meth:`FactAcquisition.acquire`，拿到 :class:`AcquiredFacts`，
不再关心事实来自复用还是新采、父体还是子体。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from clients.mcp_client import McpToolResult
from core.operating_unit import OperatingUnitBinding
from facts.collector import FactCollector
from integrations.child_fact_async import MySqlChildFactService
from integrations.repositories import PatrolUnitOfWork


@dataclass(frozen=True)
class AcquiredFacts:
    """一次采集的产物：原始事实 + 落库/快照所需的时间与待就绪信息。"""

    as_of_date: date
    raw: dict[str, McpToolResult | Exception]
    fetched_at: datetime
    snapshot_as_of_time: datetime
    pending_child_calls: dict[str, tuple[str, dict[str, Any]]]


class FactAcquisition:
    def __init__(
        self,
        *,
        engine,
        collector: FactCollector,
        child_fact_service: MySqlChildFactService | None = None,
    ) -> None:
        self.engine = engine
        self.collector = collector
        self.child_fact_service = child_fact_service

    async def acquire(
        self,
        *,
        run_id: str,
        binding: OperatingUnitBinding,
        operating_unit_id: str,
        reuse_fact_run_id: str | None,
        as_of: date,
    ) -> AcquiredFacts:
        current_date = as_of
        pending_child_calls: dict[str, tuple[str, dict[str, Any]]] = {}
        reused_from = None

        if reuse_fact_run_id:
            with PatrolUnitOfWork(self.engine) as work:
                assert work.repository is not None
                (
                    current_date,
                    fetched_at,
                    calls,
                    raw,
                    reused_from,
                ) = work.repository.load_reusable_raw_facts(
                    source_run_id=str(reuse_fact_run_id),
                    operating_unit_id=operating_unit_id,
                    expected_fact_keys=frozenset(
                        self.collector.build_calls(binding, current_date)
                    ),
                )
            base_calls = self.collector.build_calls(binding, current_date)
            expected_calls = dict(base_calls)
            expected_calls.update(
                self.collector.build_child_calls(
                    binding,
                    current_date,
                    raw.get("product_info"),
                )
            )
            if not set(base_calls).issubset(calls) or any(
                expected_calls.get(key) != value for key, value in calls.items()
            ):
                raise ValueError(
                    "source raw fact requests do not match the current operating unit binding"
                )
        else:
            calls = self.collector.build_calls(binding, current_date)
            raw = await self.collector.collect(binding, current_date, calls=calls)
            if self.child_fact_service is not None:
                # 2026-08-21 方案：子体抓取仅周六触发（周六 00:05 定时巡检抓全，
                # 快照 TTL 120h 覆盖到周一判定）。其余天不产生新的子体抓取任务，
                # 只读回已有新鲜快照（周一判定用周六抓的数据）。
                if datetime.now().weekday() == 5:
                    self.child_fact_service.enqueue_missing(
                        binding,
                        self.collector,
                        raw.get("product_info"),
                        current_date,
                    )
                cached_child_results = self.child_fact_service.load_fresh_results(
                    operating_unit_id
                )
                expected_child_calls = self.collector.build_child_calls(
                    binding,
                    current_date,
                    raw.get("product_info"),
                )
                calls.update({
                    key: expected_child_calls[key]
                    for key in cached_child_results
                    if key in expected_child_calls
                })
                # 只把 expected_child_calls 里还存在的子体结果并入 raw，避免缓存里
                # 已禁用的扩展工具（如 recent_reviews）残留导致 calls/raw 键不一致。
                raw.update({
                    key: value
                    for key, value in cached_child_results.items()
                    if key in expected_child_calls
                })
                pending_child_calls = {
                    key: value
                    for key, value in expected_child_calls.items()
                    if key not in cached_child_results
                }
            fetched_at = datetime.now(UTC)

        snapshot_as_of_time = fetched_at if reuse_fact_run_id else datetime.now(UTC)
        with PatrolUnitOfWork(self.engine) as work:
            assert work.repository is not None
            work.repository.persist_raw_facts(
                run_id=run_id,
                operating_unit_id=operating_unit_id,
                as_of_date=current_date,
                calls=calls,
                results=raw,
                core_keys=self.collector.core_keys,
                fetched_at=fetched_at,
                reused_from=reused_from,
            )
            work.commit()

        return AcquiredFacts(
            as_of_date=current_date,
            raw=raw,
            fetched_at=fetched_at,
            snapshot_as_of_time=snapshot_as_of_time,
            pending_child_calls=pending_child_calls,
        )
