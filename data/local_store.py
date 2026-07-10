"""本地数据缓存库 (SQLite) 访问层。

设计：所有 MCP 原生字段原封不动进 data(JSON)；热点字段用生成列(native 名)暴露。
加新字段：promote_field() 一行搞定，或直接用 json_extract 查。

用法：
    from data import local_store as store
    store.init_db()
    store.upsert_daily_sales(asin, parent_asin, shop_account, site_code, "2026-06-15", row_dict)
    rows = store.recent_daily_sales(asin, days=7)          # 近7天序列
    store.promote_field("daily_product_sales", "自然订单占比")  # 加新字段
"""
from __future__ import annotations
import sqlite3, json, datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "local" / "healthcheck.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
FREEZE_DAYS = 14  # 归因窗口，超过则冻结不再重取


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db() -> None:
    """建表（幂等）。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with _conn() as c:
        c.executescript(sql)


def _j(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)


# ---------------- 每日类（单日窗口循环写入） ----------------
def upsert_daily_sales(asin, parent_asin, shop_account, site_code, stat_date, row: dict) -> None:
    frozen = _is_frozen(stat_date)
    with _conn() as c:
        c.execute("""INSERT INTO daily_product_sales(asin,parent_asin,shop_account,site_code,stat_date,data,is_frozen)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(asin,stat_date) DO UPDATE SET
                       data=excluded.data, fetched_at=datetime('now','localtime'), is_frozen=excluded.is_frozen
                     WHERE daily_product_sales.is_frozen=0""",
                  (asin, parent_asin, shop_account, site_code, stat_date, _j(row), frozen))


def upsert_daily_ad(asin, parent_asin, shop_account, site_code, stat_date, row: dict) -> None:
    frozen = _is_frozen(stat_date)
    with _conn() as c:
        c.execute("""INSERT INTO daily_ad_product(asin,parent_asin,shop_account,site_code,stat_date,data,is_frozen)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(asin,stat_date) DO UPDATE SET
                       data=excluded.data, fetched_at=datetime('now','localtime'), is_frozen=excluded.is_frozen
                     WHERE daily_ad_product.is_frozen=0""",
                  (asin, parent_asin, shop_account, site_code, stat_date, _j(row), frozen))


def _is_frozen(stat_date: str) -> int:
    try:
        d = datetime.date.fromisoformat(stat_date)
    except ValueError:
        return 0
    return 1 if (datetime.date.today() - d).days > FREEZE_DAYS else 0


# ---------------- 快照类（每日/每周刷） ----------------
def upsert_sales_child(asin, parent_asin, shop_account, seller_sku, stat_month, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO sales_child(asin,parent_asin,shop_account,seller_sku,stat_month,data)
                     VALUES(?,?,?,?,?,?)
                     ON CONFLICT(asin,parent_asin) DO UPDATE SET
                       data=excluded.data, seller_sku=excluded.seller_sku,
                       stat_month=excluded.stat_month, fetched_at=datetime('now','localtime')""",
                  (asin, parent_asin, shop_account, seller_sku, stat_month, _j(row)))


def upsert_listing_baseline(parent_asin, shop_account, site_code, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO listing_baseline(parent_asin,shop_account,site_code,data)
                     VALUES(?,?,?,?)
                     ON CONFLICT(parent_asin,shop_account) DO UPDATE SET
                       data=excluded.data, site_code=excluded.site_code, fetched_at=datetime('now','localtime')""",
                  (parent_asin, shop_account, site_code, _j(row)))


def upsert_stock_summary(parent_asin, shop_account, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO stock_summary(parent_asin,shop_account,data)
                     VALUES(?,?,?)
                     ON CONFLICT(parent_asin,shop_account) DO UPDATE SET
                       data=excluded.data, fetched_at=datetime('now','localtime')""",
                  (parent_asin, shop_account, _j(row)))


def upsert_product_tags(asin, shop_account, seller_sku, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO product_tags(asin,shop_account,seller_sku,data)
                     VALUES(?,?,?,?)
                     ON CONFLICT(asin,shop_account) DO UPDATE SET
                       data=excluded.data, seller_sku=excluded.seller_sku, fetched_at=datetime('now','localtime')""",
                  (asin, shop_account, seller_sku, _j(row)))


def upsert_competitor(parent_asin, shop_account, competitor_asin, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO competitors(parent_asin,shop_account,competitor_asin,data)
                     VALUES(?,?,?,?)
                     ON CONFLICT(parent_asin,competitor_asin) DO UPDATE SET
                       data=excluded.data, fetched_at=datetime('now','localtime')""",
                  (parent_asin, shop_account, competitor_asin, _j(row)))


def upsert_keyword_flow(parent_asin, asin, shop_account, keyword, source, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO keyword_flow(parent_asin,asin,shop_account,keyword,source,data)
                     VALUES(?,?,?,?,?,?)
                     ON CONFLICT(shop_account,keyword,source,asin) DO UPDATE SET
                       data=excluded.data, parent_asin=excluded.parent_asin, fetched_at=datetime('now','localtime')""",
                  (parent_asin, asin or "", shop_account, keyword, source, _j(row)))


def log_sync(tool, target_key, stat_date, status, rows_count=0, note="") -> None:
    with _conn() as c:
        c.execute("""INSERT INTO sync_log(tool,target_key,stat_date,status,rows_count,note)
                     VALUES(?,?,?,?,?,?)""", (tool, target_key, stat_date, status, rows_count, note))


# ---------------- 读取（供 rule_engine 用） ----------------
def recent_daily_sales(asin: str, days: int = 7) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""SELECT * FROM daily_product_sales WHERE asin=?
                            ORDER BY stat_date DESC LIMIT ?""", (asin, days)).fetchall()
    return [dict(r) for r in rows]


def recent_daily_ad(asin: str, days: int = 7) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""SELECT * FROM daily_ad_product WHERE asin=?
                            ORDER BY stat_date DESC LIMIT ?""", (asin, days)).fetchall()
    return [dict(r) for r in rows]


def get_one(table: str, **where) -> dict | None:
    cond = " AND ".join(f'"{k}"=?' for k in where)
    with _conn() as c:
        r = c.execute(f"SELECT * FROM {table} WHERE {cond} LIMIT 1", tuple(where.values())).fetchone()
    return dict(r) if r else None


def has_daily(asin: str, stat_date: str) -> bool:
    with _conn() as c:
        r = c.execute("SELECT 1 FROM daily_product_sales WHERE asin=? AND stat_date=?",
                      (asin, stat_date)).fetchone()
    return r is not None


# ---------------- 聚合查询（供 daily_monitor 用） ----------------
def query_parent_daily_sales(parent_asin: str, days: int = 30) -> list[dict]:
    """获取父ASIN下所有子体的每日销售数据，同一天多子体汇总为一条。
    返回按 stat_date 降序排列，含所有生成列。"""
    with _conn() as c:
        rows = c.execute("""
            SELECT
                parent_asin,
                shop_account,
                site_code,
                stat_date,
                SUM("全部单量")    AS "全部单量",
                SUM("全部销量")    AS "全部销量",
                SUM("全部销售额")  AS "全部销售额",
                SUM("广告花费")    AS "广告花费",
                SUM("广告销售额")  AS "广告销售额",
                SUM("广告单量")    AS "广告单量",
                -- 加权平均：Σ(毛利率×销售额) / Σ(销售额)；分母为 0 时返回 NULL
                SUM("毛利率" * "全部销售额") / NULLIF(SUM("全部销售额"), 0) AS "毛利率",
                CASE WHEN SUM("广告花费") > 0 AND SUM("广告销售额") > 0
                     THEN SUM("广告花费") / SUM("广告销售额")
                     ELSE NULL END  AS "ACOS"
            FROM daily_product_sales
            WHERE parent_asin=?
            GROUP BY stat_date
            ORDER BY stat_date DESC
            LIMIT ?
        """, (parent_asin, days)).fetchall()
    return [dict(r) for r in rows]


def query_active_products() -> list[dict]:
    """获取所有活跃产品：从 daily_product_sales 去重 (parent_asin, shop_account, site_code)。"""
    with _conn() as c:
        rows = c.execute("""
            SELECT DISTINCT parent_asin, shop_account, MAX(site_code) AS site_code
            FROM daily_product_sales
            WHERE parent_asin IS NOT NULL
            GROUP BY parent_asin, shop_account
            ORDER BY parent_asin
        """).fetchall()
    return [dict(r) for r in rows]


def query_sales_children(parent_asin: str, shop_account: str | None = None) -> list[dict]:
    """获取某父ASIN的所有子体销售/目标数据（sales_child 表）。"""
    with _conn() as c:
        if shop_account:
            rows = c.execute(
                'SELECT * FROM sales_child WHERE parent_asin=? AND shop_account=?',
                (parent_asin, shop_account)
            ).fetchall()
        else:
            rows = c.execute(
                'SELECT * FROM sales_child WHERE parent_asin=?',
                (parent_asin,)
            ).fetchall()
    return [dict(r) for r in rows]


def query_image_urls(parent_asins: list[str] | None = None) -> dict[str, str]:
    """{父ASIN: 主图URL} 映射。传 parent_asins 只查这批；不传查全部。
    只包含 data.主图URL 非空的父ASIN。"""
    sql = "SELECT parent_asin, json_extract(data,'$.主图URL') AS url FROM listing_baseline WHERE url IS NOT NULL"
    params: tuple = ()
    if parent_asins:
        placeholders = ",".join("?" * len(parent_asins))
        sql += f" AND parent_asin IN ({placeholders})"
        params = tuple(parent_asins)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return {r["parent_asin"]: r["url"] for r in rows if r["url"]}


def query_product_snapshots(parent_asin: str, shop_account: str) -> dict | None:
    """一次性获取 listing_baseline + stock_summary + product_tags 三表快照。
    返回合并后的 dict，含键: listing, stock, tags。任一表缺失对应键为 None。"""
    result = {"listing": None, "stock": None, "tags": None}
    with _conn() as c:
        lb = c.execute(
            'SELECT * FROM listing_baseline WHERE parent_asin=? AND shop_account=?',
            (parent_asin, shop_account)
        ).fetchone()
        if lb:
            result["listing"] = dict(lb)

        ss = c.execute(
            'SELECT * FROM stock_summary WHERE parent_asin=? AND shop_account=?',
            (parent_asin, shop_account)
        ).fetchone()
        if ss:
            result["stock"] = dict(ss)

        pt = c.execute(
            'SELECT * FROM product_tags WHERE asin=? AND shop_account=?',
            (parent_asin, shop_account)
        ).fetchone()
        if pt:
            result["tags"] = dict(pt)
    # 全部缺失 → None
    if all(v is None for v in result.values()):
        return None
    return result


# ---------------- 加新字段 ----------------
def promote_field(table: str, field: str, sqltype: str = "REAL") -> None:
    """把 data 里的某原生字段提升为可查询生成列（立即对全部历史行生效，零回填）。"""
    with _conn() as c:
        try:
            c.execute(f'''ALTER TABLE {table} ADD COLUMN "{field}" {sqltype}
                          GENERATED ALWAYS AS (json_extract(data,'$."{field}"')) VIRTUAL''')
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise


def stats() -> dict:
    """各表行数概览。包含 MCP 缓存表 + 事件池 3 张表。"""
    tables = [
        # MCP 缓存表
        "daily_product_sales", "daily_ad_product", "sales_child", "listing_baseline",
        "stock_summary", "product_tags", "competitors", "keyword_flow", "sync_log",
        # 事件池 3 张表
        "event_pool", "event_state_log", "inspection_batch",
    ]
    out = {}
    with _conn() as c:
        for t in tables:
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                out[t] = None       # 表还没建
    return out


if __name__ == "__main__":
    init_db()
    print("DB:", DB_PATH)
    print("表行数:", stats())
