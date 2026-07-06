"""本地离线测试台。全部数据来自 tests/fixtures/demo/，无需网络。
运行: python -m uvicorn web.backend.main:app --reload --port 8000
"""
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from core import knowledge_loader
from data import fixture_loader

app = FastAPI(title="weilai-HealthCheck-Agent · 本地测试台")

FRONT = Path(__file__).resolve().parent.parent / "frontend"


# --- 知识库 ---
@app.get("/api/knowledge")
def kb_index():
    return {"modules": knowledge_loader.list_modules()}


@app.get("/api/knowledge/{name}")
def kb_read(name: str):
    try:
        return {"name": name, "content": knowledge_loader.read_module(name)}
    except FileNotFoundError:
        raise HTTPException(404, "module not found")


# --- Fixture (70 ASIN) ---
@app.get("/api/asins")
def asins():
    """列出 70 个采样，附带 config 元数据（stage / position / site 等）。"""
    configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
    smap = fixture_loader.load_shop_map()
    out = []
    for key in fixture_loader.list_keys():
        cfg = configs.get(key, {})
        shop = smap.get(cfg.get("shop_id", ""), {})
        out.append({
            "key": key,
            "parent_asin": cfg.get("parent_asin"),
            "parent_seller_sku": cfg.get("parent_seller_sku"),
            "shop_id": cfg.get("shop_id"),
            "shop_account": shop.get("account"),
            "site_code": cfg.get("site_code"),
            "product_stage": cfg.get("product_stage"),
            "product_position": cfg.get("product_position"),
            "target_acos_suggest": cfg.get("target_acos_suggest"),
            "daily_budget_suggest": cfg.get("daily_budget_suggest"),
        })
    return out


@app.get("/api/fixture/{key}")
def fixture(key: str):
    if key not in fixture_loader.list_keys():
        raise HTTPException(404, "unknown fixture key")
    return {"key": key, "data": fixture_loader.load_bundle(key)}


# --- 前端静态资源 ---
if FRONT.exists():
    app.mount("/ui", StaticFiles(directory=str(FRONT), html=True), name="frontend")


@app.get("/")
def root():
    idx = FRONT / "index.html"
    return FileResponse(idx) if idx.exists() else {"ok": True}
