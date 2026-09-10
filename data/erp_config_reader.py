"""从本地 erp_config（来源：t_advert_agent_decision_config）读取目标值和产品列表。

数据流：
  sync_erp → SQLite.erp_config → 巡检读取
  目标值缺失（NULL）→ 前端展示"配置待补"
"""
from __future__ import annotations
import logging
import sqlite3

from data import local_store as store

log = logging.getLogger(__name__)


def 读目标ACOS(parent_asin: str, shop_account: str | int | None = None) -> float | None:
    """按 parent_asin（可选 shop_id）读 target_acos_erp。多条同 asin 时取最新 update_time。"""
    with sqlite3.connect(store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        # shop 优先精确匹配；不给或给的是 account 字符串则退回 asin 唯一
        if shop_account and str(shop_account).isdigit():
            r = c.execute("""
                SELECT target_acos_erp FROM erp_config
                WHERE parent_asin=? AND shop_id=? AND target_acos_erp IS NOT NULL
                ORDER BY update_time DESC LIMIT 1
            """, (parent_asin, int(shop_account))).fetchone()
            if r:
                return r["target_acos_erp"]
        r = c.execute("""
            SELECT target_acos_erp FROM erp_config
            WHERE parent_asin=? AND target_acos_erp IS NOT NULL
            ORDER BY update_time DESC LIMIT 1
        """, (parent_asin,)).fetchone()
        return r["target_acos_erp"] if r else None


def 读目标每日预算(parent_asin: str, shop_account: str | int | None = None) -> float | None:
    with sqlite3.connect(store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        if shop_account and str(shop_account).isdigit():
            r = c.execute("""
                SELECT daily_budget_erp FROM erp_config
                WHERE parent_asin=? AND shop_id=? AND daily_budget_erp IS NOT NULL
                ORDER BY update_time DESC LIMIT 1
            """, (parent_asin, int(shop_account))).fetchone()
            if r:
                return r["daily_budget_erp"]
        r = c.execute("""
            SELECT daily_budget_erp FROM erp_config
            WHERE parent_asin=? AND daily_budget_erp IS NOT NULL
            ORDER BY update_time DESC LIMIT 1
        """, (parent_asin,)).fetchone()
        return r["daily_budget_erp"] if r else None


def 列出所有产品(enabled_only: bool = True) -> list[dict]:
    """从 erp_config 拉全量产品列表（供巡检/前端）。
    返回字段对齐 fixture_loader.load_configs() 的形态：
      fixture_key, parent_asin, parent_seller_sku, shop_id, site_code,
      product_position, product_stage, season_type,
      target_acos_suggest, daily_budget_suggest
    多条同 parent_asin+shop_id 的记录取最新 update_time。
    """
    with sqlite3.connect(store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        where = "WHERE enabled=1" if enabled_only else ""
        rows = c.execute(f"""
            SELECT id, parent_asin, parent_seller_sku, shop_id, site_code,
                   product_position, product_stage, season_type,
                   target_acos_erp, daily_budget_erp, update_time
            FROM erp_config
            {where}
        """).fetchall()

    # 去重：同 (parent_asin, shop_id) 取最新记录；空字段回填最近一条非空历史值。
    grouped: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        grouped.setdefault((r["parent_asin"], r["shop_id"]), []).append(dict(r))

    best: dict[tuple[str, int], dict] = {}
    fill_fields = (
        "parent_seller_sku", "site_code", "product_position", "product_stage",
        "season_type", "target_acos_erp", "daily_budget_erp",
    )
    for key, history in grouped.items():
        history.sort(key=lambda row: row.get("update_time") or "", reverse=True)
        merged = dict(history[0])
        for field in fill_fields:
            if merged.get(field) in (None, ""):
                merged[field] = next(
                    (row.get(field) for row in history[1:] if row.get(field) not in (None, "")),
                    merged.get(field),
                )
        best[key] = merged

    out = []
    for (pa, sid), r in best.items():
        out.append({
            "fixture_key": f"{pa}__{sid}",
            "parent_asin": pa,
            "parent_seller_sku": r["parent_seller_sku"],
            "shop_id": str(sid),
            "site_code": r["site_code"],
            "product_position": r["product_position"],
            "product_stage": r["product_stage"],
            "season_type": r["season_type"],
            "target_acos_suggest": r["target_acos_erp"],
            "daily_budget_suggest": r["daily_budget_erp"],
        })
    return out


if __name__ == "__main__":
    print(f"erp_config 产品数(enabled=1): {len(列出所有产品())}")
    print(f"某示例 目标ACOS: {读目标ACOS('B0EXAMPLE01', 1001)}")
