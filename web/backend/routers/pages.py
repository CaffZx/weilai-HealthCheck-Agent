"""静态页面路由：/  /report  /legacy + /ui 静态目录挂载。"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..common import FRONT

router = APIRouter()

_CACHE_HEADERS = {"Cache-Control": "private, max-age=300, stale-while-revalidate=600"}


@router.get("/")
def root():
    """前端入口 —— 加 HTTP 缓存头，让 iframe 重跳时命中浏览器 disk cache。"""
    idx = FRONT / "index.html"
    if not idx.exists():
        return {"ok": True}
    return FileResponse(idx, headers=_CACHE_HEADERS)


@router.get("/report")
def report_page():
    """ERP iframe 嵌入入口，参数通过 querystring 传入。"""
    idx = FRONT / "index.html"
    if not idx.exists():
        return {"error": "index not found"}
    return FileResponse(idx, headers=_CACHE_HEADERS)


@router.get("/legacy")
def legacy_page():
    """旧版技术台前端：改判定参数、看知识库、单产品跑判定用。"""
    idx = FRONT / "index.legacy.html"
    if not idx.exists():
        raise HTTPException(404, "legacy frontend missing")
    return FileResponse(idx, headers=_CACHE_HEADERS)


def mount_static(app) -> None:
    """把前端静态目录挂到 /ui（供 index.html 引用 /ui/assets/... 之类）。"""
    if FRONT.exists():
        app.mount("/ui", StaticFiles(directory=str(FRONT), html=True), name="frontend")
