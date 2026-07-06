"""Fixture 加载器 —— 离线读取 tests/fixtures/demo/，返回和 MCP 同结构的 dict。

原始文件是 MCP 网关返回的 SSE 帧（`data:{...}`），需要三层剥壳：
1. `data:` 之后是外层 JSON-RPC 响应
2. `result.content[0].text` 是一层字符串 JSON
3. 里面的 `content[0].text` 再一层字符串 JSON —— 才是业务数据
"""
from __future__ import annotations
import json, re
from pathlib import Path
from functools import lru_cache

ROOT = Path(__file__).resolve().parent.parent / "tests/fixtures/demo"
KINDS = ("listing", "sales", "campaigns")


def _parse_sse(raw: str) -> dict:
    m = re.search(r"data:(\{.*\})", raw, re.S)
    if not m:
        return {"success": False, "error": "empty SSE"}
    outer = json.loads(m.group(1))
    lvl1 = json.loads(outer["result"]["content"][0]["text"])
    lvl2 = json.loads(lvl1["content"][0]["text"])
    return lvl2


@lru_cache(maxsize=1)
def list_keys() -> list[str]:
    if not (ROOT / "listing").exists():
        return []
    return sorted(p.stem for p in (ROOT / "listing").glob("*.json"))


@lru_cache(maxsize=512)
def load(kind: str, key: str) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    f = ROOT / kind / f"{key}.json"
    if not f.exists():
        return {"success": False, "found": False, "error": "fixture missing"}
    return _parse_sse(f.read_text())


def load_bundle(key: str) -> dict:
    """一次拿一个 ASIN 的全部三工具数据。"""
    return {k: load(k, key) for k in KINDS}


def load_configs() -> list[dict]:
    """读取 configs/rows.tsv，还原 70 条 ERP config 元数据。"""
    p = ROOT / "configs/rows.tsv"
    if not p.exists():
        return []
    cols = ["id", "shop_id", "parent_asin", "parent_seller_sku", "site_code",
            "product_position", "product_stage", "season_type", "advert_purposes",
            "target_keyword_types", "target_acos_suggest", "daily_budget_suggest"]
    out = []
    for line in p.read_text().splitlines():
        parts = line.split("\t")
        row = dict(zip(cols, parts + [""] * (len(cols) - len(parts))))
        row["fixture_key"] = f"{row['parent_asin']}__{row['shop_id']}"
        out.append(row)
    return out


def load_shop_map() -> dict[str, dict]:
    p = ROOT / "configs/shop_map.tsv"
    if not p.exists():
        return {}
    m = {}
    for line in p.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        m[parts[0]] = {"shop_id": parts[0], "account": parts[1], "site_code": parts[2]}
    return m
