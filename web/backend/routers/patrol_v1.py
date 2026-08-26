"""巡检 v1 接口。

改造方案 v1.0 §18。

【为什么是 202 + job_id 而不是同步返回结果】
  一轮巡检要跑 8 个 MCP 调用 + 2 个 Agent 调用 + 中控提交，秒级到十秒级。
  同步接口会把调用方拖死，而且失败后没有可恢复的载体。
  改成入队 + 轮询：任务持久化，进程重启也能接着跑。

【幂等】
  X-Request-Id 就是幂等键。同一个 request_id 重复调用返回同一个 job_id，
  不会产生第二轮巡检。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from core.enums import TriggerType
from core.operating_unit import OperatingUnitBinding, OperatingUnitIdentityError, normalize_identity
from facts.collector import TOOL_NAMES
from integrations.observability import RuntimeHealthService, prometheus_metrics
from integrations.rollout import RolloutPolicy
from integrations.runtime_queries import RuntimeQueryService
from integrations.runtime_queue import MySqlRuntimeQueue, RuntimeQueueConflict
from web.backend.deps import (
    get_rollout_policy,
    get_runtime_health,
    get_runtime_queries,
    get_runtime_queue,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/patrol", tags=["patrol-v1"])

ROUTE_CREATE_RUN = "POST /api/v1/patrol/runs"


class OperatingUnitInput(BaseModel):
    """接口入参用中控业务键 + 取数属性，operating_unit_id 由服务端派生。

    extra="forbid" 是刻意的：调用方如果自带 operating_unit_id，直接 422 拒绝，
    而不是默默忽略后返回一个不同的 ID —— 那种"我明明传了却不生效"的场景
    排查起来最费时间。身份必须由标准算法算出来，才有
    "同一经营单元必然同一 ID"的保证。
    """

    model_config = ConfigDict(extra="forbid")

    shop_id: int = Field(ge=1)
    site_code: str
    parent_asin: str
    shop_account: str
    parent_seller_sku: str = Field(min_length=1, max_length=128)


class PatrolRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operating_unit: OperatingUnitInput
    trigger_type: Literal[TriggerType.MANUAL] = TriggerType.MANUAL
    initialization_ready: bool = True
    reuse_fact_run_id: str | None = Field(
        default=None,
        pattern=r"^pr_[0-9a-f]{24}$",
        description="显式复用某次历史 Run 的完整原始事实；为空时实时调用 MCP",
    )


class PatrolRunAccepted(BaseModel):
    job_id: str
    request_id: str
    operating_unit_id: str
    status: str


@router.post(
    "/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PatrolRunAccepted,
    summary="发起一轮巡检",
)
async def create_patrol_run(
    request: PatrolRunRequest,
    x_request_id: Annotated[str, Header(alias="X-Request-Id")],
    x_trace_id: Annotated[str, Header(alias="X-Trace-Id")],
    task_queue: Annotated[MySqlRuntimeQueue, Depends(get_runtime_queue)],
    rollout_policy: Annotated[RolloutPolicy, Depends(get_rollout_policy)],
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
    x_actor_id: Annotated[str, Header(alias="X-Actor-Id")] = "system",
) -> PatrolRunAccepted:
    if not rollout_policy.patrol_execution_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "PATROL_DISABLED",
                "message": "manual patrol is disabled during INTERNAL_ONLY rollout",
            },
        )
    unit = request.operating_unit
    try:
        shop_id, site_code, parent_asin, parent_sku = normalize_identity(
            unit.shop_id, unit.site_code, unit.parent_asin, unit.parent_seller_sku,
        )
    except OperatingUnitIdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_OPERATING_UNIT", "message": str(exc)},
        ) from exc

    from core.operating_unit import derive_operating_unit_id

    operating_unit_id = derive_operating_unit_id(shop_id, parent_asin, parent_sku)
    if not rollout_policy.permits_shop(shop_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SHOP_NOT_ALLOWLISTED",
                "message": "shop is outside the current rollout scope",
            },
        )
    owner_user_ids = queries.get_catalog_owner_user_ids(operating_unit_id)
    binding = OperatingUnitBinding(
        shop_id=shop_id,
        site_code=site_code,
        parent_asin=parent_asin,
        parent_seller_sku=parent_sku,
        shop_account=unit.shop_account,
        owner_user_ids=tuple(owner_user_ids),
    )
    if request.reuse_fact_run_id:
        reusable_facts = queries.get_raw_facts(
            request.reuse_fact_run_id,
            include_payload=False,
        )
        if not reusable_facts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "REUSABLE_FACTS_NOT_FOUND",
                    "message": "the source run has no archived raw facts",
                },
            )
        reusable_unit_ids = {item["operating_unit_id"] for item in reusable_facts}
        reusable_keys = {item["fact_key"] for item in reusable_facts}
        if reusable_unit_ids != {operating_unit_id}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "REUSABLE_FACTS_UNIT_MISMATCH",
                    "message": "the source run belongs to another operating unit",
                },
            )
        if not set(TOOL_NAMES).issubset(reusable_keys) or any(
            item["status"] != "SUCCESS" for item in reusable_facts
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "REUSABLE_FACTS_INCOMPLETE",
                    "message": "the source run does not contain the complete successful fact set",
                },
            )
    try:
        batch_id, _ = task_queue.create_batch(
            trigger_type=TriggerType.MANUAL,
            business_date=date.today(),
            units=[binding],
            rule_bundle_version="current",
            idempotency_key=f"MANUAL:{x_actor_id}:{x_request_id}",
            scope={
                "kind": "MANUAL",
                "request_id": x_request_id,
                "trace_id": x_trace_id,
                "initialization_ready": request.initialization_ready,
                "reuse_fact_run_id": request.reuse_fact_run_id,
                "operating_unit_ids": [operating_unit_id],
            },
        )
    except RuntimeQueueConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_CONFLICT", "message": str(exc)},
        ) from exc
    jobs = task_queue.jobs_for_batch(batch_id)
    if len(jobs) != 1:
        raise HTTPException(status_code=500, detail="manual batch must contain exactly one job")
    job_id = jobs[0]["job_id"]
    response = PatrolRunAccepted(
        job_id=job_id,
        request_id=x_request_id,
        operating_unit_id=operating_unit_id,
        status="ACCEPTED",
    )
    return response


async def _get_patrol_job(
    job_id: str,
    task_queue: MySqlRuntimeQueue,
) -> dict[str, Any]:
    job = task_queue.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"job {job_id} not found"},
        )
    return {
        "job_id": job["job_id"],
        "request_id": job["payload_json"].get("client_request_id") or job["request_id"],
        "status": job["status"],
        "operating_unit_id": job["operating_unit_id"],
        "retry_count": job["retry_count"],
        "last_error": job["last_error_message"],
        "result_ref": job["result_ref"],
    }


@router.get("/jobs/{job_id}", summary="查询巡检任务状态")
async def get_patrol_job(
    job_id: str,
    task_queue: Annotated[MySqlRuntimeQueue, Depends(get_runtime_queue)],
) -> dict[str, Any]:
    return await _get_patrol_job(job_id, task_queue)


@router.get("/runs/{run_id}", summary="查询单次巡检运行和投递状态")
async def get_patrol_run(
    run_id: str,
    task_queue: Annotated[MySqlRuntimeQueue, Depends(get_runtime_queue)],
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
) -> dict[str, Any]:
    if run_id.startswith("job_"):
        return await _get_patrol_job(run_id, task_queue)
    run = queries.get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"run {run_id} not found"},
        )
    return run


@router.get("/signals/{signal_id}", summary="查询信号当前态和历史")
async def get_patrol_signal(
    signal_id: str,
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
) -> dict[str, Any]:
    signal = queries.get_signal(signal_id)
    if signal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"signal {signal_id} not found"},
        )
    return signal


@router.get("/runs/{run_id}/facts", summary="查询巡检使用的完整原始事实")
async def get_patrol_raw_facts(
    run_id: str,
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
) -> dict[str, Any]:
    facts = queries.get_raw_facts(run_id)
    if not facts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"raw facts for {run_id} not found"},
        )
    return {"run_id": run_id, "fact_count": len(facts), "facts": facts}


@router.get("/batches/{batch_id}", summary="查询巡检批次聚合状态")
async def get_patrol_batch(
    batch_id: str,
    task_queue: Annotated[MySqlRuntimeQueue, Depends(get_runtime_queue)],
) -> dict[str, Any]:
    batch = task_queue.get_batch(batch_id)
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"batch {batch_id} not found"},
        )
    return {
        "batch_id": batch["batch_id"],
        "trigger_type": batch["trigger_type"],
        "business_date": batch["business_date"],
        "status": batch["status"],
        "total_count": batch["total_count"],
        "pending_count": batch["pending_count"],
        "running_count": batch["running_count"],
        "succeeded_count": batch["succeeded_count"],
        "failed_count": batch["failed_count"],
        "started_at": batch["started_at"],
        "finished_at": batch["finished_at"],
    }


@router.get("/health", summary="巡检 Agent 内部运行态")
async def patrol_health(
    health_service: Annotated[RuntimeHealthService, Depends(get_runtime_health)],
) -> dict[str, Any]:
    """MySQL 主链路运行态、积压、死信和反馈健康快照。"""
    return health_service.snapshot()


@router.get("/metrics", summary="巡检 Agent Prometheus 指标")
async def patrol_metrics(
    health_service: Annotated[RuntimeHealthService, Depends(get_runtime_health)],
) -> Response:
    return Response(
        content=prometheus_metrics(health_service.snapshot()),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
