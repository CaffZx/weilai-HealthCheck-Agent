"""Fixture 加载器 —— 离线读取 tests/fixtures/demo/，返回和 MCP 同结构的 dict。

原始文件是 MCP 网关返回的 SSE 帧（`data:{...}`），需要三层剥壳：
1. `data:` 之后是外层 JSON-RPC 响应
2. `result.content[0].text` 是一层字符串 JSON
3. 里面的 `content[0].text` 再一层字符串 JSON —— 才是业务数据

【ERP 全量源模式】
  环境变量 USE_ERP_CONFIGS=1（.env 或 export）→ list_keys/load_configs 从本地 erp_config 拉全量。
  未拉过 sync_erp 时自动回落到 fixture 70 个。
"""
from __future__ import annotations
import json, os, re
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


def _use_erp() -> bool:
    return os.getenv("USE_ERP_CONFIGS", "").strip() in ("1", "true", "TRUE", "yes")


def _erp_available() -> bool:
    """本地 erp_config 表存在且非空 → 可切 ERP 全量源。"""
    try:
        from data import erp_config_reader
        return len(erp_config_reader.列出所有产品()) > 0
    except Exception:
        return False


@lru_cache(maxsize=1)
def list_keys() -> list[str]:
    if _use_erp() and _erp_available():
        from data import erp_config_reader
        return sorted(c["fixture_key"] for c in erp_config_reader.列出所有产品())
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
    """产品配置列表。默认走 fixture 70 条；USE_ERP_CONFIGS=1 时走本地 erp_config 全量。"""
    if _use_erp() and _erp_available():
        from data import erp_config_reader
        return erp_config_reader.列出所有产品()
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
    """shop_id → {account, site_code}。
    优先 asin_owner 表（真实 MCP 映射）→ fixture 21 家 → 兜底 shop_{id}。"""
    m: dict[str, dict] = {}
    # 1) 真实来源：asin_owner (来自 MCP az_extend_detail)
    try:
        import sqlite3
        from data import local_store as _store
        with sqlite3.connect(_store.DB_PATH) as _c:
            for sid, acc, sc in _c.execute("""
                SELECT DISTINCT shop_id, shop_account, site_code FROM asin_owner
                WHERE shop_id IS NOT NULL AND shop_account IS NOT NULL AND shop_account != ''
            """).fetchall():
                m[str(sid)] = {"shop_id": str(sid), "account": acc, "site_code": sc or "Amazon_US"}
    except Exception:
        pass
    # 2) fixture 兜底（补 asin_owner 未覆盖的历史店铺）
    p = ROOT / "configs/shop_map.tsv"
    if p.exists():
        for line in p.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or parts[0] in m:
                continue
            m[parts[0]] = {"shop_id": parts[0], "account": parts[1], "site_code": parts[2]}
    # 3) ERP 模式最后兜底（避免完全无映射）
    if _use_erp() and _erp_available():
        from data import erp_config_reader
        for c in erp_config_reader.列出所有产品():
            sid = str(c.get("shop_id") or "")
            if sid and sid not in m:
                m[sid] = {"shop_id": sid, "account": f"shop_{sid}", "site_code": c.get("site_code") or "Amazon_US"}
    return m
