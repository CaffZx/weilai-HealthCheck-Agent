"""广告目标覆写值：MySQL(app_db) → 本地 SQLite → 巡检读取。

来源（辅助决策 agent 写入 MySQL）：
  - acos_override.value    → 目标ACOS（小数，如 0.30）
  - budget_override.value  → 目标每日预算（USD）

数据流：
  同步覆写表到本地()  ── 全量拉 MySQL 两表，合并成 (asin,shop)→目标值，灌进 local_store.ad_target
  读目标ACOS / 读目标每日预算  ── 巡检期只读本地 SQLite，不连 MySQL

设计原则：
  - 巡检不依赖 MySQL 常在线（读本地）；同步是独立的一步（今晚放行后跑）
  - 连不上 / 表不存在 / 没记录 → 该产品该字段为 None（不猜、不兜底）
  - 表结构容忍：SELECT * 后动态挑 (asin, shop_account, value, updated_at)
    asin/shop_account 缺列时视为 shop 级 / 全局单值，读取端按 (asin,'') 降级匹配
"""
from __future__ import annotations
import logging
import os
from typing import Any

from data import local_store as store

log = logging.getLogger(__name__)

# MySQL 连接配置（可用环境变量覆盖）
_CFG = {
    "host": os.getenv("AD_STATE_HOST", "36.140.52.167"),
    "port": int(os.getenv("AD_STATE_PORT", "3307")),
    "user": os.getenv("AD_STATE_USER", "ad_agent"),
    "password": os.getenv("AD_STATE_PASSWORD", "ad_agent_pass"),
    "database": os.getenv("AD_STATE_DB", "app_db"),
    "connect_timeout": 8,
    "read_timeout": 15,
    "charset": "utf8mb4",
}


# -----------------------------------------------------------------------------
# MySQL 侧：全量拉取
# -----------------------------------------------------------------------------
def _fetch_override(表名: str, 值缩放: float = 1.0) -> dict[str, tuple[float, str | None]]:
    """全表拉一次 → {asin: (value, created_at)}。
    真实表结构：主键 asin（无 shop_account），value + created_at + expires_at。
      - 值缩放：acos_override.value 是整数百分比（40 = 40%），传 0.01 转成 0.40；budget 传 1.0。
      - 忽略 expires_at：主键是 asin，每 asin 一行即最新目标，过期只表示"多久没更新"。
    表/列不存在或连不上 → 空 dict。"""
    try:
        import pymysql
    except ImportError:
        log.warning("pymysql 未安装，跳过 %s 读取", 表名)
        return {}

    try:
        conn = pymysql.connect(**_CFG)
    except Exception as e:
        log.warning("连接 app_db 失败：%s（%s）", e, 表名)
        return {}

    结果: dict[str, tuple[float, str | None]] = {}
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"SELECT * FROM `{表名}`")
            rows = cur.fetchall()
    except Exception as e:
        log.warning("查询 %s 失败：%s", 表名, e)
        conn.close()
        return {}
    conn.close()

    for row in rows:
        value = row.get("value")
        if value is None:
            continue
        try:
            v = float(value) * 值缩放
        except (TypeError, ValueError):
            continue
        asin = str(row.get("asin") or row.get("parent_asin") or "").strip()
        if not asin:
            continue
        created = row.get("created_at") or row.get("updated_at")
        结果[asin] = (v, str(created) if created is not None else None)
    return 结果


def 同步覆写表到本地(全量重灌: bool = True) -> dict[str, int]:
    """把 MySQL 两张覆写表拉全量，合并写进 local_store.ad_target（按 asin，shop 恒为 ''）。
    返回统计 {acos条数, budget条数, 写入行数}。连不上则返回 0，不动本地已有数据。"""
    acos = _fetch_override("acos_override", 值缩放=0.01)   # 整数百分比 → 小数
    budget = _fetch_override("budget_override", 值缩放=1.0)  # USD 原样

    if not acos and not budget:
        log.warning("覆写表全量为空或 MySQL 不可达，本地 ad_target 未改动")
        return {"acos条数": 0, "budget条数": 0, "写入行数": 0}

    所有asin = set(acos) | set(budget)
    if 全量重灌:
        store.clear_ad_target()

    写入 = 0
    for asin in 所有asin:
        目标ACOS = acos.get(asin, (None, None))[0]
        目标每日预算 = budget.get(asin, (None, None))[0]
        源时间 = (acos.get(asin, (None, None))[1]
                or budget.get(asin, (None, None))[1])
        store.upsert_ad_target(asin, "", 目标ACOS, 目标每日预算, 源时间)
        写入 += 1

    log.info("覆写表同步完成：acos %d 条 / budget %d 条 → 本地 ad_target %d 行",
             len(acos), len(budget), 写入)
    return {"acos条数": len(acos), "budget条数": len(budget), "写入行数": 写入}


# -----------------------------------------------------------------------------
# 巡检侧：只读本地
# -----------------------------------------------------------------------------
def 读目标ACOS(asin: str, shop_account: str) -> float | None:
    row = store.get_ad_target(asin, shop_account)
    return row.get("目标ACOS") if row else None


def 读目标每日预算(asin: str, shop_account: str) -> float | None:
    row = store.get_ad_target(asin, shop_account)
    return row.get("目标每日预算") if row else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    store.init_db()
    print("=== 探针：MySQL 两表全量（acos 已 ÷100）===")
    acos = _fetch_override("acos_override", 值缩放=0.01)
    budget = _fetch_override("budget_override", 值缩放=1.0)
    print(f"acos_override : {len(acos)} 个 asin，样例 {list(acos.items())[:3]}")
    print(f"budget_override: {len(budget)} 个 asin，样例 {list(budget.items())[:3]}")
    print("=== 同步到本地 ===")
    print(同步覆写表到本地())
