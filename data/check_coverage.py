"""数据完整性检查 —— 抓取后跑一次，输出每个数据域的覆盖率 + 缺失清单。

用法：
    python -m data.check_coverage                 # 检查昨天
    python -m data.check_coverage --date 2026-7-12
    python -m data.check_coverage --days 3        # 检查近 3 天

设计原则：
  - erp_config 里 enabled=1 的产品为"应有"集合
  - 抓取表里当天存在数据 = "已抓到"
  - 缺失比例 > 阈值 → exit code 非 0，供 crontab / 监控识别

输出：
  - stdout 人类可读报告
  - logs/health-report-{YYYY-MM-DD}.log 结构化 JSON（前端 /api/sync-health 读取）
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import logging
import sqlite3
import sys
from pathlib import Path

from data import local_store as store

log = logging.getLogger(__name__)

# 报告输出目录
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# 覆盖率告警阈值
COVERAGE_WARN = 0.90   # < 90% 告警
COVERAGE_FAIL = 0.70   # < 70% 判定为失败退出


# ---- 待检查的数据域（表 → 检查列 → 描述） ----
# 说明：只检查"应该每天更新"的表；listing_baseline 这类快照表不看日期
DAILY_TABLES = {
    "daily_product_sales":     {"date_col": "stat_date",  "name": "每日销售"},
    "daily_ad_product":        {"date_col": "stat_date",  "name": "每日广告"},
    "daily_natural_ad_flow":   {"date_col": "stat_date",  "name": "自然/广告流量拆分"},
}

# 快照表（不看日期，只看当前是否有该产品）
SNAPSHOT_TABLES = {
    "listing_baseline":    {"name": "商品快照"},
    "stock_summary":       {"name": "库存汇总"},
    "product_tags":        {"name": "产品标签"},
    "stock_alert":         {"name": "库存预警"},
    "child_price_promo":   {"name": "子体价格/促销"},
    "listing_product_info":{"name": "产品信息（五点/标题）"},
}


def _get_enabled_products() -> list[tuple[str, str]]:
    """返回 [(parent_asin, shop_account)]，以产品和店铺作为完整覆盖率维度。"""
    with sqlite3.connect(store.DB_PATH) as c:
        try:
            rows = c.execute("""
                SELECT DISTINCT config.parent_asin, owner.shop_account
                FROM erp_config AS config
                JOIN asin_owner AS owner
                  ON owner.asin=config.parent_asin AND owner.shop_id=config.shop_id
                WHERE config.enabled=1
                  AND config.parent_asin IS NOT NULL
                  AND owner.shop_account IS NOT NULL AND owner.shop_account<>''
            """).fetchall()
            return [(r[0], r[1]) for r in rows]
        except sqlite3.OperationalError:
            log.warning("erp_config 表不存在，回退到 fixture 配置")
            from data import fixture_loader
            return [(c["parent_asin"], str(c["shop_id"]))
                    for c in fixture_loader.load_configs() if c.get("parent_asin")]


def _check_daily_coverage(check_date: str, table: str, date_col: str) -> dict:
    """检查某天在某个表里的产品覆盖情况，按父 ASIN + 店铺统计。"""
    products = _get_enabled_products()
    expected = set(products)

    with sqlite3.connect(store.DB_PATH) as c:
        try:
            rows = c.execute(f"""
                SELECT DISTINCT parent_asin, shop_account
                FROM {table}
                WHERE {date_col} = ? AND parent_asin IS NOT NULL AND shop_account IS NOT NULL
            """, (check_date,)).fetchall()
            found = {(r[0], r[1]) for r in rows}
        except sqlite3.OperationalError as e:
            # 若真是表结构问题，返回错误详情供排查
            return {"table": table, "error": f"查询失败: {str(e)[:80]}",
                    "expected": len(expected), "found": 0, "coverage": 0.0, "missing_count": 0}

    missing = sorted(expected - found)
    coverage = (len(expected & found) / len(expected)) if expected else 1.0
    return {
        "table": table,
        "date": check_date,
        "expected": len(expected),
        "found": len(expected & found),
        "coverage": round(coverage, 4),
        "missing_count": len(missing),
        "missing_sample": [{"parent_asin": parent_asin, "shop_account": shop_account}
                           for parent_asin, shop_account in missing[:20]],
    }


def _check_snapshot_coverage(table: str) -> dict:
    """检查快照表：每个 enabled 产品在表里是否至少有一条记录。
    子 ASIN 表通过 sales_child 映射回父 ASIN，再按父 ASIN + 店铺核验。"""
    products = _get_enabled_products()
    expected = set(products)

    with sqlite3.connect(store.DB_PATH) as c:
        try:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.OperationalError as e:
            return {"table": table, "error": f"读取表结构失败: {str(e)[:80]}",
                    "expected": len(expected), "found": 0, "coverage": 0.0}
        if not cols:
            return {"table": table, "error": "表不存在",
                    "expected": len(expected), "found": 0, "coverage": 0.0}
        key_col = "parent_asin" if "parent_asin" in cols else ("asin" if "asin" in cols else None)
        if key_col is None:
            return {"table": table, "error": "表无 parent_asin/asin 列",
                    "expected": len(expected), "found": 0, "coverage": 0.0}
        if "shop_account" not in cols:
            return {"table": table, "error": "表无 shop_account，无法按店铺核验",
                    "expected": len(expected), "found": 0, "coverage": 0.0}
        try:
            if key_col == "parent_asin":
                rows = c.execute(f"""
                    SELECT DISTINCT parent_asin, shop_account
                    FROM {table}
                    WHERE parent_asin IS NOT NULL AND shop_account IS NOT NULL
                """).fetchall()
            else:
                rows = c.execute(f"""
                    SELECT DISTINCT sales_child.parent_asin, source.shop_account
                    FROM {table} AS source
                    JOIN sales_child
                      ON sales_child.asin=source.{key_col}
                     AND sales_child.shop_account=source.shop_account
                    WHERE sales_child.parent_asin IS NOT NULL
                      AND source.shop_account IS NOT NULL
                """).fetchall()
            found = {(r[0], r[1]) for r in rows}
        except sqlite3.OperationalError as e:
            return {"table": table, "error": f"查询失败: {str(e)[:80]}",
                    "expected": len(expected), "found": 0, "coverage": 0.0}

    missing = sorted(expected - found)
    coverage = (len(expected & found) / len(expected)) if expected else 1.0
    return {
        "table": table,
        "expected": len(expected),
        "found": len(expected & found),
        "coverage": round(coverage, 4),
        "missing_count": len(missing),
        "missing_sample": [{"parent_asin": parent_asin, "shop_account": shop_account}
                           for parent_asin, shop_account in missing[:20]],
    }


def check(check_dates: list[str]) -> dict:
    """跑覆盖率检查，返回结构化报告。"""
    report: dict = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "check_dates": check_dates,
        "daily": {},        # 每天 → 表 → 报告
        "snapshot": {},     # 表 → 报告
        "summary": {},
    }

    # 逐天检查每日表
    for d in check_dates:
        daily_report: dict = {}
        for tbl, meta in DAILY_TABLES.items():
            r = _check_daily_coverage(d, tbl, meta["date_col"])
            r["name"] = meta["name"]
            daily_report[tbl] = r
        report["daily"][d] = daily_report

    # 快照表
    for tbl, meta in SNAPSHOT_TABLES.items():
        r = _check_snapshot_coverage(tbl)
        r["name"] = meta["name"]
        report["snapshot"][tbl] = r

    # 汇总最低覆盖率
    all_covs: list[float] = []
    for d, tbls in report["daily"].items():
        for t, r in tbls.items():
            if "error" not in r:
                all_covs.append(r["coverage"])
    for t, r in report["snapshot"].items():
        if "error" not in r:
            all_covs.append(r["coverage"])

    min_cov = min(all_covs) if all_covs else 0.0
    report["summary"] = {
        "min_coverage": round(min_cov, 4),
        "avg_coverage": round(sum(all_covs)/len(all_covs), 4) if all_covs else 0.0,
        "status": "ok" if min_cov >= COVERAGE_WARN else ("warn" if min_cov >= COVERAGE_FAIL else "fail"),
    }
    return report


def _print_report(report: dict) -> None:
    """人类可读的 stdout 打印。"""
    print("=" * 70)
    print(f"数据覆盖率报告 · {report['generated_at']}")
    print("=" * 70)

    # 每日表
    for d, tbls in report["daily"].items():
        print(f"\n▶ 日期 {d}")
        for tbl, r in tbls.items():
            if "error" in r:
                print(f"  ✗ {r['name']} ({tbl}) — {r['error']}")
                continue
            emoji = "✓" if r["coverage"] >= COVERAGE_WARN else ("⚠" if r["coverage"] >= COVERAGE_FAIL else "✗")
            print(f"  {emoji} {r['name']:20s}  {r['found']:>4}/{r['expected']:<4}  ({r['coverage']*100:.1f}%)  缺 {r['missing_count']}")
            if r["missing_count"] > 0 and r["coverage"] < COVERAGE_WARN:
                for m in r["missing_sample"][:5]:
                    # 兼容旧结构（有 shop_id）和新结构（只有 parent_asin）
                    tail = f" @ shop {m['shop_id']}" if "shop_id" in m else ""
                    print(f"      · {m['parent_asin']}{tail}")
                if r["missing_count"] > 5:
                    print(f"      · ... 共 {r['missing_count']} 条")

    # 快照表
    print(f"\n▶ 快照表（无日期）")
    for tbl, r in report["snapshot"].items():
        if "error" in r:
            print(f"  ✗ {r['name']} ({tbl}) — {r['error']}")
            continue
        emoji = "✓" if r["coverage"] >= COVERAGE_WARN else ("⚠" if r["coverage"] >= COVERAGE_FAIL else "✗")
        print(f"  {emoji} {r['name']:20s}  {r['found']:>4}/{r['expected']:<4}  ({r['coverage']*100:.1f}%)  缺 {r['missing_count']}")

    # 汇总
    s = report["summary"]
    print(f"\n{'='*70}")
    print(f"最低覆盖率 {s['min_coverage']*100:.1f}% · 平均 {s['avg_coverage']*100:.1f}% · 状态 {s['status'].upper()}")
    print(f"{'='*70}\n")


def _save_report(report: dict) -> Path:
    """结构化 JSON 写盘，供前端 /api/sync-health 读取。"""
    today = dt.date.today().isoformat()
    path = LOGS_DIR / f"health-report-{today}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    # 同时更新一个 latest.json 指向今天的报告
    latest = LOGS_DIR / "health-report-latest.json"
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=str, default=None, help="检查日期，默认昨天，格式 YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=1, help="检查最近 N 天，与 --date 互斥")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
    store.init_db()

    if args.date:
        check_dates = [args.date]
    else:
        today = dt.date.today()
        check_dates = [(today - dt.timedelta(days=i+1)).isoformat() for i in range(args.days)]
        check_dates.sort()

    report = check(check_dates)
    _print_report(report)
    path = _save_report(report)
    print(f"报告已写入 {path}")

    # exit code 反馈状态
    status = report["summary"]["status"]
    if status == "fail":
        sys.exit(2)
    if status == "warn":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
