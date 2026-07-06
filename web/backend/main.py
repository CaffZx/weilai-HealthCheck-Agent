"""FastAPI 测试页 —— 单 ASIN 试跑 / 知识库预览 / MCP 直调。
运行: uvicorn web.backend.main:app --reload --port 8000
"""
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

load_dotenv()

from core import knowledge_loader
from core.inspector import inspect_one
from data import erp_repo, shop_map
from data.mcp_client import MCPClient

app = FastAPI(title="weilai-HealthCheck-Agent")

FRONT = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/api/knowledge")
def kb_index():
    return {"modules": knowledge_loader.list_modules()}


@app.get("/api/knowledge/{name}")
def kb_read(name: str):
    try:
        return {"name": name, "content": knowledge_loader.read_module(name)}
    except FileNotFoundError:
        raise HTTPException(404, "module not found")


@app.get("/api/shops")
def shops():
    return list(shop_map.load_all().values())


@app.get("/api/configs")
def configs(limit: int = 20):
    return erp_repo.fetch_decision_configs(limit=limit)


@app.post("/api/inspect")
def inspect(payload: dict):
    """payload: 单条 config_row 或 {shop_id, parent_asin, parent_seller_sku}"""
    return inspect_one(payload)


@app.post("/api/mcp/{tool}")
def mcp_call(tool: str, arguments: dict):
    return MCPClient().call(tool, arguments)


if FRONT.exists():
    app.mount("/", StaticFiles(directory=FRONT, html=True), name="frontend")


@app.get("/")
def _root():
    idx = FRONT / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return {"ok": True, "hint": "frontend not built yet"}
