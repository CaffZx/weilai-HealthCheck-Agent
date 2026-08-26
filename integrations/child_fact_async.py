from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.mysql import insert as mysql_insert

from clients.mcp_client import McpToolResult
from core.operating_unit import OperatingUnitBinding
from facts.collector import FactCollector, McpPort
from integrations.repositories.patrol import _utc_naive
from integrations.repositories.tables import (
    patrol_child_fact_snapshot,
    patrol_child_fact_task,
)

#: 子体扩展工具超时/重试覆盖。爬虫类工具（前台实时抓取）统一加大超时与重试：
#: erp_asin_full_detail / erp_listing_price_promotion_analysis / erp_listing_review_analysis
#: 上游 best-effort 偶发返回"空结构"（字段全空/null）或慢响应，empty_retry_attempts 做
#: 空结构秒级重试，max_attempts 处理异常/超时重试；其余业务工具保持轻量避免长尾卡死。
CHILD_TOOL_CALL_OVERRIDES: dict[str, dict[str, Any]] = {
    "erp_listing_price_promotion_analysis": {
        "timeout_seconds": 60.0,
        "max_attempts": 3,
        "empty_retry_attempts": 3,
    },
    "erp_amazon_account_health_issue": {"timeout_seconds": 30.0, "max_attempts": 1},
    "erp_listing_asin_keyword_rank_history": {"timeout_seconds": 90.0, "max_attempts": 3},
    "erp_asin_full_detail": {
        "timeout_seconds": 90.0,
        "max_attempts": 3,
        "empty_retry_attempts": 3,
    },
    "erp_listing_review_analysis": {
        "timeout_seconds": 180.0,
        "max_attempts": 3,
        "empty_retry_attempts": 2,
    },
}


def _child_detail_empty(result: McpToolResult) -> bool:
    """child_detail 空结构判定：linkStatus/images/aplus/bonus 全为空对象。

    上游 erp_asin_full_detail 对部分子体返回 isError=false 但字段全空的响应
    （best-effort 偶发），这类"空结构"不算获取到前台数据，应触发短间隔重试。
    """
    rows = result.data if isinstance(result.data, list) else []
    if not rows:
        return True

    def _empty(value: Any) -> bool:
        if isinstance(value, dict):
            return not value
        return value in (None, "", [])

    return all(
        _empty(row.get("linkStatus"))
        and _empty(row.get("images"))
        and _empty(row.get("aplus"))
        and _empty(row.get("bonus"))
        for row in rows
    )


def _price_promotion_empty(result: McpToolResult) -> bool:
    """price_promotion 空结构判定：核心促销/价格字段全为 null。

    实测 erp_listing_price_promotion_analysis 对 73% 子体返回 isError=false 但
    price/coupon/promotions/strikethroughPrice/savingsPercentage 全 null 的"空壳"，
    与 child_detail 同类（上游 best-effort），应触发短间隔重试。
    """
    rows = result.data if isinstance(result.data, list) else []
    if not rows:
        return True

    def _null(value: Any) -> bool:
        return value in (None, "", 0, [])

    return all(
        _null(row.get("price"))
        and _null(row.get("coupon"))
        and _null(row.get("promotions"))
        and _null(row.get("strikethroughPrice"))
        and _null(row.get("savingsPercentage"))
        for row in rows
    )


def _tool_result_empty(tool_name: str, result: McpToolResult) -> bool:
    """按工具分发的空结构判定（child_detail / price_promotion）。"""
    if tool_name == "erp_asin_full_detail":
        return _child_detail_empty(result)
    if tool_name == "erp_listing_price_promotion_analysis":
        return _price_promotion_empty(result)
    return False


@dataclass(frozen=True, slots=True)
class ClaimedChildFactTask:
    task_id: str
    operating_unit_id: str
    child_asin: str
    domain: str
    tool_name: str
    arguments: dict[str, Any]
    retry_count: int
    max_retry: int


class MySqlChildFactService:
    def __init__(
        self,
        engine: Engine,
        *,
        snapshot_ttl: timedelta = timedelta(hours=24),
        failure_cooldown: timedelta = timedelta(minutes=15),
        max_retry: int = 3,
    ) -> None:
        self.engine = engine
        self.snapshot_ttl = snapshot_ttl
        self.failure_cooldown = failure_cooldown
        self.max_retry = max_retry

    @staticmethod
    def _task_key(operating_unit_id: str, child_asin: str, domain: str) -> str:
        raw = f"{operating_unit_id}|{child_asin}|{domain}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def enqueue_missing(
        self,
        binding: OperatingUnitBinding,
        collector: FactCollector,
        product_info: McpToolResult | Exception | None,
        as_of,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> int:
        current = now or datetime.now(UTC)
        calls = collector.build_child_calls(binding, as_of, product_info)
        fresh = {} if force else self.load_fresh_results(binding.operating_unit_id, now=current)
        rows = []
        for fact_key, (tool_name, arguments) in calls.items():
            domain, _, child_asin = fact_key.partition(":")
            if fact_key in fresh:
                continue
            seller_sku = next(
                (
                    str(row.get("sellerSku") or "").strip()
                    for row in (
                        product_info.data
                        if isinstance(product_info, McpToolResult)
                        else []
                    )
                    if str(row.get("asin") or "").strip().upper() == child_asin
                ),
                "",
            )
            if not seller_sku:
                continue
            rows.append({
                "task_id": f"cft_{uuid4().hex[:24]}",
                "task_key": self._task_key(binding.operating_unit_id, child_asin, domain),
                "operating_unit_id": binding.operating_unit_id,
                "child_asin": child_asin,
                "seller_sku": seller_sku,
                "domain": domain,
                "tool_name": tool_name,
                "arguments_json": arguments,
                "status": "PENDING",
                "retry_count": 0,
                "max_retry": self.max_retry,
                "next_attempt_at": _utc_naive(current),
                "locked_by": None,
                "locked_at": None,
                "last_error_code": None,
                "last_error_message": None,
                "created_at": _utc_naive(current),
                "updated_at": _utc_naive(current),
            })
        if rows:
            task_keys = [row["task_key"] for row in rows]
            with self.engine.connect() as connection:
                active_or_dead = set(connection.execute(
                    sa.select(patrol_child_fact_task.c.task_key).where(
                        patrol_child_fact_task.c.task_key.in_(task_keys),
                        patrol_child_fact_task.c.status.in_({
                            "PENDING", "RUNNING", "DEAD"
                        }),
                    )
                ).scalars())
            rows = [row for row in rows if row["task_key"] not in active_or_dead]
        with self.engine.begin() as connection:
            for row in rows:
                statement = mysql_insert(patrol_child_fact_task).values(**row)
                connection.execute(
                    statement.on_duplicate_key_update(
                        arguments_json=statement.inserted.arguments_json,
                        tool_name=statement.inserted.tool_name,
                        seller_sku=statement.inserted.seller_sku,
                        status="PENDING",
                        retry_count=0,
                        next_attempt_at=statement.inserted.next_attempt_at,
                        locked_by=None,
                        locked_at=None,
                        last_error_code=None,
                        last_error_message=None,
                        updated_at=statement.inserted.updated_at,
                    )
                )
        return len(rows)

    def load_fresh_results(
        self,
        operating_unit_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, McpToolResult]:
        current = now or datetime.now(UTC)
        ranked = sa.select(
            patrol_child_fact_snapshot,
            sa.func.row_number().over(
                partition_by=(
                    patrol_child_fact_snapshot.c.child_asin,
                    patrol_child_fact_snapshot.c.domain,
                ),
                order_by=patrol_child_fact_snapshot.c.fetched_at.desc(),
            ).label("position"),
        ).where(
            patrol_child_fact_snapshot.c.operating_unit_id == operating_unit_id,
            patrol_child_fact_snapshot.c.expires_at > _utc_naive(current),
        ).subquery()
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(ranked).where(ranked.c.position == 1)
            ).mappings().all()
        return {
            f"{row['domain']}:{row['child_asin']}": McpToolResult(
                tool_name=row["tool_name"],
                data=list(row["extracted_data_json"]),
                raw=dict(row["response_json"]),
                request_hash=f"sha256:{row['request_hash']}",
                fetched_at=row["fetched_at"].replace(tzinfo=UTC),
            )
            for row in rows
        }

    def claim(self, worker_id: str, *, now: datetime | None = None) -> ClaimedChildFactTask | None:
        current = now or datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                sa.select(patrol_child_fact_task)
                .where(
                    patrol_child_fact_task.c.status == "PENDING",
                    patrol_child_fact_task.c.next_attempt_at <= _utc_naive(current),
                )
                .order_by(
                    patrol_child_fact_task.c.next_attempt_at,
                    patrol_child_fact_task.c.created_at,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            ).mappings().one_or_none()
            if row is None:
                return None
            connection.execute(
                sa.update(patrol_child_fact_task)
                .where(patrol_child_fact_task.c.task_id == row["task_id"])
                .values(
                    status="RUNNING",
                    locked_by=worker_id,
                    locked_at=_utc_naive(current),
                    updated_at=_utc_naive(current),
                )
            )
        return ClaimedChildFactTask(
            task_id=row["task_id"],
            operating_unit_id=row["operating_unit_id"],
            child_asin=row["child_asin"],
            domain=row["domain"],
            tool_name=row["tool_name"],
            arguments=dict(row["arguments_json"]),
            retry_count=int(row["retry_count"]),
            max_retry=int(row["max_retry"]),
        )

    def succeed(
        self,
        task: ClaimedChildFactTask,
        result: McpToolResult,
        *,
        now: datetime | None = None,
    ) -> str:
        current = now or datetime.now(UTC)
        snapshot_id = f"cfs_{uuid4().hex[:24]}"
        with self.engine.begin() as connection:
            connection.execute(sa.insert(patrol_child_fact_snapshot).values(
                snapshot_id=snapshot_id,
                task_id=task.task_id,
                operating_unit_id=task.operating_unit_id,
                child_asin=task.child_asin,
                domain=task.domain,
                tool_name=task.tool_name,
                request_hash=result.request_hash.removeprefix("sha256:"),
                response_content_hash=result.content_hash.removeprefix("sha256:"),
                response_json=result.raw,
                extracted_data_json=result.data,
                fetched_at=_utc_naive(result.fetched_at or current),
                expires_at=_utc_naive(current + self.snapshot_ttl),
            ))
            connection.execute(
                sa.update(patrol_child_fact_task)
                .where(patrol_child_fact_task.c.task_id == task.task_id)
                .values(
                    status="SUCCEEDED",
                    locked_by=None,
                    locked_at=None,
                    last_error_code=None,
                    last_error_message=None,
                    updated_at=_utc_naive(current),
                )
            )
        return snapshot_id

    def fail(
        self,
        task: ClaimedChildFactTask,
        error: Exception,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(UTC)
        retry_count = task.retry_count + 1
        # MCP 的确定性业务错误（例如站点不支持、入参不合法）不应进入
        # 队列级重试；网络超时等 retryable 错误才按 max_retry 继续排队。
        retryable = bool(getattr(error, "retryable", True))
        status = "DEAD" if not retryable or retry_count >= task.max_retry else "PENDING"
        with self.engine.begin() as connection:
            connection.execute(
                sa.update(patrol_child_fact_task)
                .where(patrol_child_fact_task.c.task_id == task.task_id)
                .values(
                    status=status,
                    retry_count=retry_count,
                    next_attempt_at=(
                        None
                        if status == "DEAD"
                        else _utc_naive(current + self.failure_cooldown)
                    ),
                    locked_by=None,
                    locked_at=None,
                    last_error_code=type(error).__name__.upper()[:64],
                    last_error_message=type(error).__name__[:65535],
                    updated_at=_utc_naive(current),
                )
            )


class ChildFactWorker:
    def __init__(
        self,
        service: MySqlChildFactService,
        client: McpPort,
        *,
        worker_id: str,
        minimum_interval_seconds: float = 0.25,
        batch_size: int = 4,
    ) -> None:
        self.service = service
        self.client = client
        self.worker_id = worker_id
        self.minimum_interval_seconds = minimum_interval_seconds
        self.batch_size = max(1, batch_size)

    async def process_once(self) -> bool:
        import asyncio

        # 一次领取 batch_size 个任务并发执行，解决串行 worker 消化慢的问题。
        # 评论爬虫 180s 是长尾，串行时一个 worker 卡在评论上就空转；并发领取
        # 让每个 worker 同时跑多个任务，吞吐随 batch_size 线性放大。
        tasks = []
        for _ in range(self.batch_size):
            task = self.service.claim(self.worker_id)
            if task is None:
                break
            tasks.append(task)
        if not tasks:
            return False

        async def run(task):
            overrides = dict(CHILD_TOOL_CALL_OVERRIDES.get(task.tool_name, {}))
            empty_retry_attempts = int(overrides.pop("empty_retry_attempts", 1))
            for attempt in range(1, empty_retry_attempts + 1):
                try:
                    result = await self.client.call_tool(
                        task.tool_name,
                        task.arguments,
                        **overrides,
                    )
                except Exception as exc:
                    self.service.fail(task, exc)
                    return
                # 空结构重试：child_detail / price_promotion 上游 best-effort 偶发返回
                # 空结构（字段全空/null），秒级重试可救回大部分（同一 ASIN 多次调用多数成功）。
                # 重试耗尽仍空 → 保留空快照，由"前台不可得就不判"的判定逻辑兜底。
                if _tool_result_empty(task.tool_name, result) and attempt < empty_retry_attempts:
                    await asyncio.sleep(2.0 * attempt)
                    continue
                self.service.succeed(task, result)
                return

        await asyncio.gather(*(run(task) for task in tasks))
        await asyncio.sleep(self.minimum_interval_seconds)
        return True
