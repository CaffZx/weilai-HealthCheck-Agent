"""静态页面路由：/  /report  + /ui 静态目录挂载。"""
from __future__ import annotations
from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..common import FRONT

router = APIRouter()

# HTML 骨架用 no-cache（每次 revalidate，改前端立即生效）
# 里面引用的 /ui/assets/*.js?v=x 靠 query string 版本号做缓存失效
_CACHE_HEADERS = {"Cache-Control": "no-cache"}


@router.get("/")
def root():
    """前端入口。"""
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


def mount_static(app) -> None:
    """把前端静态目录挂到 /ui（供 index.html 引用 /ui/assets/... 之类）。"""
    if FRONT.exists():
        app.mount("/ui", StaticFiles(directory=str(FRONT), html=True), name="frontend")
