"""FastAPI 后端入口（v2.0）。

运行: python -m uvicorn web.backend.main:app --host 0.0.0.0 --port 8000

模块地图：
  deps.py           - 组合根：把 clients/facts/inspector/integrations 拼成编排器
  routers/
    patrol_v1.py    - 【新】POST /api/v1/patrol/runs 发起巡检
    review_v1.py    - 旧复盘 API 退役占位（410 Gone）
    knowledge.py    - /api/knowledge*

【v2.0 的两个关键变更】
  1. 新增 patrol_v1 / feedback_v1 接口，走 MySQL 运行态。
  2. 退役启动即跑全量巡检的 threading.Thread —— 方案 §19 明确要求：
     不要在 Uvicorn 启动时自动跑全量巡检，避免多 Worker 重复执行。
     每日巡检改由独立 Scheduler 触发，`python -m integrations.worker` 消费 MySQL 队列。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .legacy_sqlite_guard import LegacySqliteReadOnlyMiddleware
from .routers import (
    feedback_v1,
    knowledge,
    mcp_auth,
    mcp_proxy,
    patrol_v1,
    review_v1,
    workspace_v1,
)

load_dotenv()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """服务启动不建表、不写业务数据、不自动执行巡检。"""
    log.info("weilai-HealthCheck-Agent v2.0 started")
    yield


app = FastAPI(
    title="weilai-HealthCheck-Agent",
    version="2.0.0",
    description=(
        "Amazon 运营闭环 · 业务巡检 Agent。\n\n"
        "职责：自主调度 → MCP 经营事实 → R2/R3 确定性规则 → 信号生命周期 → "
        "可靠结果投递。\n\n"
        "边界：不判断经营模式、不生成执行建议、不审批、不执行。"
    ),
    lifespan=lifespan,
)
app.add_middleware(LegacySqliteReadOnlyMiddleware)

# ---- v1 契约接口（新）----
app.include_router(patrol_v1.router)
app.include_router(feedback_v1.router)
app.include_router(review_v1.router)
app.include_router(workspace_v1.router)

# ---- 与 SQLite 无关的辅助接口 ----
app.include_router(knowledge.router)
app.include_router(mcp_auth.router)
app.include_router(mcp_proxy.router)

frontend_root = Path(__file__).resolve().parent.parent / "frontend"
app.mount(
    "/ui/assets",
    StaticFiles(directory=frontend_root / "assets"),
    name="patrol-ui-assets",
)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui", status_code=307)


@app.get("/ui", include_in_schema=False)
async def patrol_workspace() -> FileResponse:
    return FileResponse(frontend_root / "v2.html")

@app.get("/api/v1/health", tags=["ops"], summary="服务健康检查")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "weilai-HealthCheck-Agent", "version": "2.0.0"}
