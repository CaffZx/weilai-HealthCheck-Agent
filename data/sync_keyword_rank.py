"""同步关键词逐日排名（卡位）。

两步跨网关：
  1. own_keyword_flow (streamable-mcpserver)
       按 父ASIN/父SKU/店铺 拿产品自身跟踪的核心关键词 → 选周搜索量最大的一条作代表词
  2. erp_listing_asin_keyword_rank_history (azlisting-mcpserver)
       按 (child_asin, keyword, siteCode) 拿该词的逐日自然排名位（crawNatureRank，越大越靠后）
       → 落 keyword_rank_daily，供 判卡位异常 算 近3天/近7天/逐日 卡位

用法：
    python -m data.sync_keyword_rank              # 全量
    python -m data.sync_keyword_rank --limit 5    # 抽样
    python -m data.sync_keyword_rank --key B0XXX__1622
"""
from __future__ import annotations
import argparse
import concurrent.futures as _cf
import logging
import time

from data import local_store as store
from data import fixture_loader as fx
from data.sync_asin_owner import MCPSession as StreamSession   # streamable 网关
from data.sync_azlisting import MCPSession as AzSession        # azlisting 网关

log = logging.getLogger(__name__)

# flow/排名工具仅支持 US/UK/DE
_SUPPORTED_SITES = {"US", "UK", "DE"}


def _short_site(site_code: str | None) -> str | None:
    """Amazon_US → US。"""
    if not site_code:
        return None
    s = site_code.replace("Amazon_", "").upper()
    return s if s in _SUPPORTED_SITES else None


def 选核心词(stream: StreamSession, pa: str, sku: str, sa: str) -> dict | None:
    """own_keyword_flow → 周搜索量最大的词。返回 {keyword, child_asin, 周搜索量} 或 None。"""
    r = stream.call("own_keyword_flow", {
        "parent_asin": pa, "parent_seller_sku": sku, "shop_account": sa,
    })
    rows = (r or {}).get("rows") or [] if isinstance(r, dict) else []
    rows = [x for x in rows if x.get("关键词") and x.get("ASIN")]
    if not rows:
        return None
    best = max(rows, key=lambda x: x.get("周搜索量") or 0)
    return {"keyword": best["关键词"], "child_asin": best["ASIN"],
            "周搜索量": best.get("周搜索量")}


def 拉逐日排名(az: AzSession, child: str, keyword: str, site: str) -> list[dict]:
    """erp_listing_asin_keyword_rank_history → [{stat_date, nature_rank, sp_rank, raw}]。"""
    st, data = az.call("erp_listing_asin_keyword_rank_history", {
        "asin": child, "keyword": keyword, "siteCode": site,
    })
    if st != "OK" or not isinstance(data, list):
        return []
    out = []
    for row in data:
        d = (row.get("createTime") or "")[:10]
        rank = row.get("crawNatureRank")
        if not d or rank is None:
            continue
        out.append({"stat_date": d, "nature_rank": rank,
                    "sp_rank": row.get("crawSpRank"), "raw": row})
    return sorted(out, key=lambda x: x["stat_date"])


def sync_one(stream: StreamSession, az: AzSession, pa: str, sku: str, sa: str,
             site_code: str) -> tuple[int, str]:
    """单产品：选核心词 → 拉逐日排名 → 落库。返回 (落库行数, 状态)。"""
    site = _short_site(site_code)
    if site is None:
        store.log_sync("keyword_rank", pa, None, "skip", note=f"站点 {site_code} 不支持")
        return 0, "SKIP"

    core = 选核心词(stream, pa, sku, sa)
    if not core:
        store.log_sync("keyword_rank", pa, None, "empty", note="无跟踪关键词")
        return 0, "EMPTY"

    series = 拉逐日排名(az, core["child_asin"], core["keyword"], site)
    if not series:
        store.log_sync("keyword_rank", pa, None, "empty",
                       note=f"核心词 {core['keyword']} 无排名历史")
        return 0, "EMPTY"

    for pt in series:
        store.upsert_keyword_rank(
            parent_asin=pa, shop_account=sa, child_asin=core["child_asin"],
            keyword=core["keyword"], site_code=site_code, stat_date=pt["stat_date"],
            nature_rank=pt["nature_rank"], sp_rank=pt["sp_rank"], is_core=1, row=pt["raw"],
        )
    store.log_sync("keyword_rank", pa, None, "ok", rows_count=len(series),
                   note=f"核心词={core['keyword']}")
    return len(series), "OK"


def resolve_products(limit: int, only_key: str | None = None) -> list[tuple[str, str, str, str]]:
    smap = fx.load_shop_map()
    out = []
    for c in fx.load_configs():
        if only_key and c["fixture_key"] != only_key:
            continue
        acct = (smap.get(str(c.get("shop_id") or "")) or {}).get("account")
        if not acct:
            continue
        out.append((acct, c["parent_asin"], c["parent_seller_sku"], c.get("site_code") or ""))
        if not only_key and len(out) >= limit:
            break
    return out


def run(limit: int = 10_000, only_key: str | None = None, concurrency: int = 3) -> dict:
    store.init_db()
    products = resolve_products(limit, only_key)
    print(f"卡位同步 {len(products)} 个产品 · {concurrency} 线程")

    from collections import Counter
    stat = Counter()

    def worker(task):
        acct, pa, sku, site = task
        if not hasattr(worker, "stream"):
            worker.stream = StreamSession()
            worker.az = AzSession()
        try:
            n, st = sync_one(worker.stream, worker.az, pa, sku, acct, site)
        except Exception as e:
            log.warning("卡位同步失败 %s@%s: %s", pa, acct, e)
            n, st = 0, "ERR"
        time.sleep(0.3)   # 节流，避开 429
        return st

    t0 = time.time()
    with _cf.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="kw") as pool:
        for i, st in enumerate(pool.map(worker, products), 1):
            stat[st] += 1
            if i % 50 == 0:
                print(f"  {i}/{len(products)}  {time.time()-t0:.0f}s  {dict(stat)}")
    print(f"完成 {time.time()-t0:.0f}s · {dict(stat)}")
    return dict(stat)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10_000)
    ap.add_argument("--key", type=str, default=None, help="只跑某个 fixture_key")
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")
    run(limit=args.limit, only_key=args.key, concurrency=args.concurrency)


if __name__ == "__main__":
    main()
