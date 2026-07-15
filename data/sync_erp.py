"""从 ERP MySQL 全量拉数据 → 本地 SQLite。

三张目标表：
  1. erp_config          ← t_advert_agent_decision_config（10703 行）
  2. erp_decision_latest ← t_advert_agent_decision WHERE is_latest=1（621 行）
  3. erp_data_metrics    ← t_advert_agent_data_metrics（22500 行历史快照）

用法：
  python -m data.sync_erp                  # 全量三张表
  python -m data.sync_erp --skip-metrics   # 只拉 config + decision（快，秒级）

连接从 .env 读：ERP_HOST / ERP_PORT / ERP_USER / ERP_PASSWORD / ERP_DB
"""
from __future__ import annotations
import argparse
import logging
import os
import sqlite3
import time
from typing import Iterator

from dotenv import load_dotenv

from data import local_store as store

load_dotenv()
log = logging.getLogger(__name__)

_CFG = {
    "host": os.getenv("ERP_HOST", "127.0.0.1"),
    "port": int(os.getenv("ERP_PORT", "3306")),
    "user": os.getenv("ERP_USER", "app_db"),
    "password": os.getenv("ERP_PASSWORD", ""),
    "database": os.getenv("ERP_DB", "app_db"),
    "charset": "utf8mb4",
    "connect_timeout": 10,
    "read_timeout": 120,
}


def _open() -> "pymysql.connections.Connection":
    import pymysql
    return pymysql.connect(**_CFG)


def _iter_rows(cur, batch: int = 2000) -> Iterator[dict]:
    """分批 fetch，避免大表一次拉爆内存。"""
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        for r in rows:
            yield r


def _to_float(v, scale: float = 1.0) -> float | None:
    if v is None:
        return None
    try:
        return float(v) * scale
    except (TypeError, ValueError):
        return None


def _dt_str(v) -> str | None:
    return None if v is None else str(v)


def sync_config() -> int:
    """全量拉 t_advert_agent_decision_config → erp_config。"""
    conn = _open()
    try:
        with conn.cursor() as cur:
            import pymysql.cursors
            cur = conn.cursor(pymysql.cursors.DictCursor)
            cur.execute("""
                SELECT id, shop_id, parent_asin, parent_seller_sku, site_code,
                       day_range, product_position, product_stage, season_type,
                       advert_purposes, target_keyword_types,
                       target_acos_suggest, daily_budget_suggest,
                       advert_direction_types, enabled, frequency,
                       create_time, update_time
                FROM t_advert_agent_decision_config
            """)
            n = 0
            with sqlite3.connect(store.DB_PATH) as db:
                db.execute("DELETE FROM erp_config")   # 全量重灌
                cursor = db.cursor()
                for r in _iter_rows(cur):
                    cursor.execute("""
                        INSERT INTO erp_config VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
                    """, (
                        r["id"], r["shop_id"], r["parent_asin"], r["parent_seller_sku"],
                        r["site_code"], r["day_range"], r["product_position"], r["product_stage"],
                        r["season_type"], r["advert_purposes"], r["target_keyword_types"],
                        _to_float(r["target_acos_suggest"], scale=0.01),  # 40 → 0.40
                        _to_float(r["daily_budget_suggest"], scale=1.0),
                        r["advert_direction_types"], int(r["enabled"] or 0),
                        r["frequency"], _dt_str(r["create_time"]), _dt_str(r["update_time"]),
                    ))
                    n += 1
                    if n % 2000 == 0:
                        db.commit()
                        print(f"  ...{n} 行")
                db.commit()
            return n
    finally:
        conn.close()


def sync_decision_latest() -> int:
    """拉 t_advert_agent_decision WHERE is_latest=1 → erp_decision_latest（拿 product_name）。"""
    conn = _open()
    try:
        import pymysql.cursors
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("""
            SELECT id, parent_asin, shop_id, site_code, product_name,
                   product_position, product_stage, season_type,
                   target_acos_suggest, daily_budget_suggest, create_time
            FROM t_advert_agent_decision WHERE is_latest=1
        """)
        n = 0
        with sqlite3.connect(store.DB_PATH) as db:
            db.execute("DELETE FROM erp_decision_latest")
            cursor = db.cursor()
            for r in _iter_rows(cur):
                cursor.execute("""
                    INSERT INTO erp_decision_latest VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
                """, (
                    r["id"], r["parent_asin"], r["shop_id"], r["site_code"], r["product_name"],
                    r["product_position"], r["product_stage"], r["season_type"],
                    _to_float(r["target_acos_suggest"], scale=0.01),
                    _to_float(r["daily_budget_suggest"], scale=1.0),
                    _dt_str(r["create_time"]),
                ))
                n += 1
            db.commit()
        return n
    finally:
        conn.close()


def sync_data_metrics() -> int:
    """拉 t_advert_agent_data_metrics 全量 → erp_data_metrics（历史快照，22500 行）。"""
    conn = _open()
    try:
        import pymysql.cursors
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("""
            SELECT id, decision_id, metrics_type, day_str,
                   avg_daily_sale_num, acos, organic_order_rate, tacos, overall_cvr,
                   cvr, ctr, cpc, daily_cost, create_time
            FROM t_advert_agent_data_metrics
        """)
        n = 0
        with sqlite3.connect(store.DB_PATH) as db:
            db.execute("DELETE FROM erp_data_metrics")
            cursor = db.cursor()
            for r in _iter_rows(cur):
                cursor.execute("""
                    INSERT INTO erp_data_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
                """, (
                    r["id"], r["decision_id"], r["metrics_type"], r["day_str"],
                    _to_float(r["avg_daily_sale_num"]), _to_float(r["acos"]),
                    _to_float(r["organic_order_rate"]), _to_float(r["tacos"]),
                    _to_float(r["overall_cvr"]), _to_float(r["cvr"]), _to_float(r["ctr"]),
                    _to_float(r["cpc"]), _to_float(r["daily_cost"]),
                    _dt_str(r["create_time"]),
                ))
                n += 1
                if n % 5000 == 0:
                    db.commit()
                    print(f"  ...{n} 行")
            db.commit()
        return n
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-metrics", action="store_true", help="跳过 data_metrics（22500 行历史快照）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
    store.init_db()

    print("=" * 70)
    print(f"从 ERP MySQL 全量拉数据 → 本地 SQLite")
    print(f"  host={_CFG['host']}:{_CFG['port']} db={_CFG['database']}")
    print("=" * 70)

    t0 = time.time()
    print("\n【1/3】erp_config …")
    n1 = sync_config()
    print(f"  ✓ {n1} 行")

    print("\n【2/3】erp_decision_latest（拿 product_name）…")
    n2 = sync_decision_latest()
    print(f"  ✓ {n2} 行")

    n3 = 0
    if not args.skip_metrics:
        print("\n【3/3】erp_data_metrics（历史快照，可能耗时 30s）…")
        n3 = sync_data_metrics()
        print(f"  ✓ {n3} 行")
    else:
        print("\n【3/3】跳过 erp_data_metrics")

    print("\n" + "=" * 70)
    print(f"✅ 完成 · 总耗时 {time.time()-t0:.1f}s")
    print(f"   erp_config: {n1} · erp_decision_latest: {n2} · erp_data_metrics: {n3}")


if __name__ == "__main__":
    main()
