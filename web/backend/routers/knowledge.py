"""知识库接口：/api/knowledge  /api/knowledge/{name}"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from core import knowledge_loader

router = APIRouter(prefix="/api/knowledge")


@router.get("")
def kb_index():
    return {"modules": knowledge_loader.list_modules()}


@router.get("/{name}")
def kb_read(name: str):
    try:
        return {"name": name, "content": knowledge_loader.read_module(name)}
    except FileNotFoundError as exc:
        raise HTTPException(404, "module not found") from exc
