"""知识库加载器 —— 按需读取 knowledge/*.md，支持热加载。"""
from __future__ import annotations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "knowledge"


def list_modules() -> list[str]:
    return sorted(p.name for p in ROOT.glob("*.md") if p.name != "README.md")


def read_module(name: str) -> str:
    p = ROOT / name
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(name)
    return p.read_text(encoding="utf-8")


def load_all() -> dict[str, str]:
    return {n: read_module(n) for n in list_modules()}
