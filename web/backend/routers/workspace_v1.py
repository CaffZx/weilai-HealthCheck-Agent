from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from integrations.observability import RuntimeHealthService
from integrations.rollout import RolloutPolicy
from integrations.runtime_queries import RuntimeQueryService
from web.backend.deps import get_rollout_policy, get_runtime_health, get_runtime_queries

router = APIRouter(prefix="/api/v1/patrol/workspace", tags=["patrol-workspace"])


@router.get("/overview", summary="巡检工作台概览")
async def workspace_overview(
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
    health_service: Annotated[RuntimeHealthService, Depends(get_runtime_health)],
    rollout: Annotated[RolloutPolicy, Depends(get_rollout_policy)],
) -> dict[str, Any]:
    return {
        "rollout": {
            "stage": rollout.stage,
            "patrol_execution_enabled": rollout.patrol_execution_enabled,
            "result_delivery_enabled": rollout.result_delivery_enabled,
            "feedback_enabled": rollout.feedback_enabled,
            "allowlisted_shop_ids": sorted(rollout.allowlisted_shop_ids),
        },
        "summary": queries.workspace_summary(),
        "health": health_service.snapshot(),
    }


@router.get("/batches", summary="分页查询巡检批次")
async def list_batches(
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: str | None = None,
) -> dict[str, Any]:
    return queries.list_batches(page=page, page_size=page_size, status=status)


@router.get("/jobs", summary="分页查询巡检任务")
async def list_jobs(
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: str | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    return queries.list_jobs(
        page=page,
        page_size=page_size,
        status=status,
        batch_id=batch_id,
    )


@router.get("/runs", summary="分页查询巡检运行")
async def list_runs(
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: str | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    return queries.list_runs(
        page=page,
        page_size=page_size,
        status=status,
        batch_id=batch_id,
    )


@router.get("/runs/{run_id}/facts", summary="查询巡检事实元数据")
async def list_run_fact_metadata(
    run_id: str,
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
) -> dict[str, Any]:
    facts = queries.get_raw_facts(run_id, include_payload=False)
    return {"run_id": run_id, "fact_count": len(facts), "facts": facts}


@router.get("/signals", summary="分页查询巡检信号")
async def list_signals(
    queries: Annotated[RuntimeQueryService, Depends(get_runtime_queries)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    state: str | None = None,
    severity: str | None = None,
    signal_type: str | None = None,
    batch_id: str | None = None,
    parent_asin: str | None = None,
    latest_detection_only: bool = False,
) -> dict[str, Any]:
    return queries.list_signals(
        page=page,
        page_size=page_size,
        state=state,
        severity=severity,
        signal_type=signal_type,
        batch_id=batch_id,
        parent_asin=parent_asin,
        latest_detection_only=latest_detection_only,
    )
