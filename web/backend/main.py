"""FastAPI 后端入口。

app 装配 + 启动钩子。业务逻辑全部拆到 routers/ 与 common.py / batch_cache.py。
运行: python -m uvicorn web.backend.main:app --host 0.0.0.0 --port 8000

模块地图：
  common.py         - 跨路由共享的常量、工具、DB 查询
  batch_cache.py    - 批量代码巡检 + LLM 缓存（内存 + 磁盘）
  routers/
    pages.py        - / , /report, /ui 静态
    users.py        - /api/users, /api/reports (主管/下属)
    knowledge.py    - /api/knowledge*
    fixtures.py     - /api/asins, /api/fixture/{key}, /api/sync-health
    judge.py        - /api/judge, /api/inspect, /api/batch/*
    tasks.py        - /api/tasks/*, /api/anomalies, /api/review/due
    history.py      - /api/history*, /api/dashboard
"""
from __future__ import annotations
import logging
import threading

from dotenv import load_dotenv
from fastapi import FastAPI

from . import batch_cache
from .common import load_settings
from data import local_store
from data.observation_service import process_all_due_observations
from .routers import fixtures, history, judge, knowledge, pages, tasks, users

load_dotenv()
log = logging.getLogger(__name__)

app = FastAPI(title="weilai-HealthCheck-Agent")

# ---- API 路由 ----
app.include_router(users.router)
app.include_router(knowledge.router)
app.include_router(fixtures.router)
app.include_router(judge.router)
app.include_router(tasks.router)
app.include_router(history.router)

# ---- 页面 & 静态资源（最后挂：避免遮盖 API 路由）----
app.include_router(pages.router)
pages.mount_static(app)


# ---- 启动钩子 ----
# 启动行为由 config/settings.yaml → startup.auto_batch_inspect 控制
# 默认不自动跑（避免 uvicorn --reload 时双开、避免上线即打爆 LLM API）
# 需要触发时：POST /api/batch/inspect
@app.on_event("startup")
def _maybe_auto_batch():
    local_store.init_db()
    threading.Thread(target=_backfill_due_observations, daemon=True).start()
    settings = load_settings().get("startup", {})
    if not settings.get("auto_batch_inspect"):
        log.info("startup.auto_batch_inspect=false，跳过启动批量巡检（如需触发：POST /api/batch/inspect）")
        batch_cache.ready.set()      # 让 batch_status API 返回 ready，前端不会一直转
        return
    log.info("startup.auto_batch_inspect=true，启动后台批量巡检")
    threading.Thread(target=batch_cache.run_batch_inspect, daemon=True).start()


def _backfill_due_observations():
    try:
        observed_ids = process_all_due_observations()
        if observed_ids:
            log.info("服务启动时已补齐 %d 条效果观察结论", len(observed_ids))
    except Exception:
        log.exception("服务启动时补齐效果观察结论失败")
