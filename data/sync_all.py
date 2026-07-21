"""全量抓数据一键编排 —— 今晚服务器放行后跑这一个就够。

顺序：
  1. sync_daily     每日销售/广告 + 快照类（streamable 网关）
  2. sync_azlisting 自然广告拆分/月目标/库存预警/仓租/变体价促（azlisting 网关）
  3. ad_state_reader 广告目标覆写表 acos_override/budget_override（MySQL app_db）

设计原则（不求效率，只求抓全）：
  - 每个产品、每个数据域独立 try/except，单点失败不影响全局
  - 会话失效自动重连（daily/azlisting 的 _rpc 已内置 404 重连）
  - 全程 sync_log 落库，可断点续跑（已冻结/已入库的天自动跳过）
  - 结束打印各表行数，方便核对

用法：
  python -m data.sync_all                    # 全量 70 个产品，daily 14 天 / azlisting 30 天
  python -m data.sync_all --days 14 --az-days 30
  python -m data.sync_all --limit 5          # 只跑前 5 个（联调用）
  python -m data.sync_all --skip-override    # 跳过 MySQL 覆写表（未放行时）
"""
from __future__ import annotations
import argparse
import concurrent.futures as _cf
import logging
import sqlite3
import time

# 显式加载 .env，crontab 环境不会自动加载 shell profile
from dotenv import load_dotenv
load_dotenv()

from data import local_store as store
from data import sync_daily
from data import sync_azlisting
from data import sync_images
from data import sync_keyword_rank
from data import ad_state_reader

# MCP 并发线程数（每 worker 独立 MCPSession，避免 sid 竞争）
# 6 线程是相对温和的选择：单线程 ~80s/产品，6 线程理论 ~15s/产品，MCP 网关 QPS ~1.2 稳
DEFAULT_CONCURRENCY = 6

log = logging.getLogger(__name__)

# 一个足够大的数，覆盖全部产品（当前 70 个）
ALL = 100000


def _table_counts() -> dict[str, int]:
    tables = [
        "daily_product_sales", "daily_ad_product", "sales_child", "listing_baseline",
        "stock_summary", "product_tags", "competitors",
        "daily_natural_ad_flow", "monthly_goal", "stock_alert", "inventory_cost",
        "child_price_promo", "listing_inspection_snapshot", "keyword_rank_daily", "ad_target",
    ]
    out: dict[str, int] = {}
    with sqlite3.connect(store.DB_PATH) as c:
        for t in tables:
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = -1  # 表不存在
    return out


def run_daily(limit: int, days: int, concurrency: int = DEFAULT_CONCURRENCY) -> None:
    print("\n" + "=" * 70)
    print(f"【1/4】sync_daily · 每日销售/广告 + 快照 · {days} 天窗口 · {concurrency} 线程并发")
    print("=" * 70)
    store.init_db()
    products = sync_daily.resolve_products(limit)
    total = len(products)
    print(f"共 {total} 个产品\n")
    t0 = time.time()

    # 进度计数（线程安全用 counter dict + 锁；简单起见每 worker 完成后 print 顺序号）
    done = [0]
    fail = [0]

    def _worker(task):
        idx, (sa, pa, sku, sc) = task
        # 每个 worker 首次调用时创建独立 MCPSession（同 sync_asin_owner 模式）
        if not hasattr(_worker, "sess"):
            _worker.sess = sync_daily.MCPSession()
        sess = _worker.sess
        try:
            sync_daily.sync_product(sa, pa, sku, sc, days, session=sess)
            done[0] += 1
            print(f"[{done[0]+fail[0]}/{total}] ✓ 完成 (总 ok={done[0]} fail={fail[0]})")
        except Exception as e:
            fail[0] += 1
            print(f"[{done[0]+fail[0]}/{total}] ✗ {pa}@{sa} 异常: {e}")

    with _cf.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="dly") as pool:
        list(pool.map(_worker, enumerate(products, 1)))

    print(f"\nsync_daily 完成 · 耗时 {time.time()-t0:.0f}s · ok={done[0]} fail={fail[0]}")


def run_azlisting(limit: int, days: int, concurrency: int = DEFAULT_CONCURRENCY) -> None:
    print("\n" + "=" * 70)
    print(f"【2/4】sync_azlisting · 自然广告/月目标/库存预警/仓租/变体价促 · {days} 天窗口 · {concurrency} 线程并发")
    print("=" * 70)
    store.init_db()
    products = sync_azlisting.resolve_products(limit)
    total = len(products)
    print(f"共 {total} 个产品\n")
    t0 = time.time()

    done = [0]
    fail = [0]

    def _worker(task):
        idx, (sa, pa, sku, sc) = task
        if not hasattr(_worker, "sess"):
            _worker.sess = sync_azlisting.MCPSession()
        sess = _worker.sess
        try:
            sync_azlisting.sync_product(sa, pa, sku, sc, days=days, session=sess)
            done[0] += 1
            print(f"[{done[0]+fail[0]}/{total}] ✓ 完成 (总 ok={done[0]} fail={fail[0]})")
        except Exception as e:
            fail[0] += 1
            print(f"[{done[0]+fail[0]}/{total}] ✗ {pa}@{sa} 异常: {e}")

    with _cf.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="azl") as pool:
        list(pool.map(_worker, enumerate(products, 1)))

    print(f"\nsync_azlisting 完成 · 耗时 {time.time()-t0:.0f}s · ok={done[0]} fail={fail[0]}")


def run_override() -> None:
    print("\n" + "=" * 70)
    print("【4/4】ad_state_reader · 广告目标覆写表（MySQL app_db）")
    print("=" * 70)
    store.init_db()
    try:
        stats = ad_state_reader.同步覆写表到本地(全量重灌=True)
        print(f"覆写表同步: acos {stats['acos条数']} 条 / budget {stats['budget条数']} 条 → "
              f"本地 ad_target {stats['写入行数']} 行")
        if stats["写入行数"] == 0:
            print("⚠️  覆写表为空或 MySQL 不可达 —— 若服务器已放行请检查连接/白名单")
    except Exception as e:
        print(f"✗ 覆写表同步异常: {e}")


def run_page_details(limit: int) -> None:
    print("\n" + "=" * 70)
    print("【3/4】sync_images · 前台详情（主图/副图/A+/可购性）")
    print("=" * 70)
    try:
        stats = sync_images.run(limit=limit, interval=0.8, concurrency=2, skip_existing=False)
        print(f"前台详情同步: {stats}")
    except Exception as e:
        print(f"✗ 前台详情同步异常: {e}")


def run_keyword_rank(limit: int) -> None:
    print("\n" + "=" * 70)
    print("【4/5】sync_keyword_rank · 卡位（核心词逐日自然排名）")
    print("=" * 70)
    try:
        stats = sync_keyword_rank.run(limit=limit, concurrency=3)
        print(f"卡位同步: {stats}")
    except Exception as e:
        print(f"✗ 卡位同步异常: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=ALL, help="产品数上限，默认全部")
    ap.add_argument("--days", type=int, default=14, help="sync_daily 每日窗口天数")
    ap.add_argument("--az-days", type=int, default=30, help="sync_azlisting 每日窗口天数")
    ap.add_argument("--skip-daily", action="store_true")
    ap.add_argument("--skip-azlisting", action="store_true")
    ap.add_argument("--skip-keyword-rank", action="store_true")
    ap.add_argument("--skip-page-details", action="store_true", help="跳过前台详情抓取")
    ap.add_argument("--skip-override", action="store_true", help="跳过 MySQL 覆写表（未放行时用）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")
    T0 = time.time()
    print("★ 全量抓数据开始 ★  limit=%s daily=%d天 az=%d天" % (
        "全部" if args.limit >= ALL else args.limit, args.days, args.az_days))

    if not args.skip_daily:
        try:
            run_daily(args.limit, args.days)
        except Exception as e:
            print(f"✗ sync_daily 整体异常（继续下一步）: {e}")

    if not args.skip_azlisting:
        try:
            run_azlisting(args.limit, args.az_days)
        except Exception as e:
            print(f"✗ sync_azlisting 整体异常（继续下一步）: {e}")

    if not args.skip_page_details:
        run_page_details(args.limit)

    if not args.skip_keyword_rank:
        run_keyword_rank(args.limit)

    if not args.skip_override:
        run_override()

    print("\n" + "=" * 70)
    print(f"★ 全部完成 · 总耗时 {time.time()-T0:.0f}s ★")
    print("=" * 70)
    print("各表行数：")
    for t, n in _table_counts().items():
        flag = "（表不存在）" if n < 0 else ""
        print(f"  {t:<26} {max(n,0):>7} 行 {flag}")


if __name__ == "__main__":
    main()
