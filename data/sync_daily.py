"""真实调用 MCP → 写入本地缓存库。

用法：
    python -m data.sync_daily --limit 50 --days 14
    python -m data.sync_daily --key <PARENT_ASIN>__<SHOP_ID>   # 单个

策略：
  - 每日类(product_sales)：单日窗口循环，已冻结(>14天且库里有)的天跳过，不重复调用
  - 快照类(sales_performance/listing/stock/tags/competitors)：每次覆盖刷新
  - 全程 sync_log 记录，可断点续跑
  - 支持多线程并发：每 worker 独立 MCPSession，避免 sid 竞争

线程安全说明：
  - MCPSession 实例内部持有独立的 sid + headers，方法本身不加锁
  - 每个 worker 线程持有自己的 session，不跨线程共享
  - sync_product(session, ...) 只操作 session 参数指定的会话
"""
from __future__ import annotations
import os, re, json, time, argparse, datetime, urllib.request, urllib.error
from pathlib import Path

from data import local_store as store
from data import fixture_loader as fx

GATEWAY = os.environ["MCP_SYNC_GATEWAY"]  # 例：http://<mcp-host>/mcp
API_KEY = os.environ["MCP_API_KEY"]


class MCPSession:
    """独立的 MCP 会话。每个 worker 线程持有一个，避免 sid 竞争。
    404 会话失效自动重连一次。"""
    def __init__(self):
        self._sid: str | None = None
        self._headers = {}
        self._init()

    def _rpc(self, method, params=None, mid=1, _no_retry=False):
        body = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            body["params"] = params
        if method == "notifications/initialized":
            del body["id"]
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "X-Api-Key": API_KEY}
        if self._sid:
            h["Mcp-Session-Id"] = self._sid
        req = urllib.request.Request(GATEWAY, data=json.dumps(body).encode(), headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=90)
        except urllib.error.HTTPError as e:
            if e.code == 404 and not _no_retry and method not in ("initialize", "notifications/initialized"):
                print("      ⟳ MCP 会话失效(404)，自动重连…")
                self._init()
                return self._rpc(method, params, mid, _no_retry=True)
            raise
        self._headers = r.headers
        return r.read().decode()

    def _init(self):
        self._sid = None
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "sync", "version": "1"}})
        self._sid = self._headers.get("Mcp-Session-Id")
        self._rpc("notifications/initialized")

    def call(self, tool: str, args: dict):
        """返回 (status, rows|dict|None)。status: OK/EMPTY/NOPERM/ERR"""
        try:
            raw = self._rpc("tools/call", {"name": tool, "arguments": args}, mid=9)
        except Exception as e:
            return "ERR", {"err": str(e)[:100]}
        return _parse_mcp_response(raw)


# ==========================================================================
# 向后兼容：保留模块级 _rpc/mcp_init/mcp_call（单线程场景用）
# 内部委托给一个 default session
# ==========================================================================
_default_session: MCPSession | None = None
_HEADERS = {}   # 保留符号避免外部引用报错


def mcp_init():
    """初始化默认 session（单线程场景兼容）。多线程请自行 MCPSession()。"""
    global _default_session
    _default_session = MCPSession()


def mcp_call(tool: str, args: dict):
    """单线程兼容入口。多线程用 session.call(tool, args)。"""
    if _default_session is None:
        mcp_init()
    return _default_session.call(tool, args)


def _rpc(method, params=None, mid=1, _no_retry=False):
    """单线程兼容入口。"""
    if _default_session is None:
        mcp_init()
    return _default_session._rpc(method, params, mid, _no_retry)


def _parse_mcp_response(raw: str):
    """MCP SSE 返回三层剥壳。"""
    if isinstance(raw, tuple):
        return raw  # 已经是 (status, data)
    m = re.search(r"data:(\{.*\})", raw, re.S)
    if not m:
        return "ERR", {"err": "no sse"}
    res = json.loads(m.group(1)).get("result", {})
    if res.get("isError"):
        txt = res.get("content", [{}])[0].get("text", "")
        return ("NOPERM" if "权限" in txt else "ERR"), {"err": txt[:100]}
    try:
        inner = res["content"][0]["text"]
        l1 = json.loads(inner)
        l2 = json.loads(l1["content"][0]["text"]) if isinstance(l1, dict) and "content" in l1 else l1
    except Exception as e:
        return "ERR", {"err": f"parse:{e}"}
    if isinstance(l2, dict):
        if l2.get("error"):
            return ("NOPERM" if "权限" in str(l2["error"]) else "ERR"), {"err": str(l2["error"])[:100]}
        rows = l2.get("rows")
        if rows is None:
            dk = {k: v for k, v in l2.items() if k not in ("success", "found", "count", "message")}
            return ("OK", dk) if dk else ("EMPTY", None)
        return ("OK", rows) if rows else ("EMPTY", None)
    if isinstance(l2, list):
        return ("OK", l2) if l2 else ("EMPTY", None)
    return "EMPTY", None


def _daterange(days: int):
    today = datetime.date.today()
    for i in range(1, days + 1):  # 从昨天往前
        yield (today - datetime.timedelta(days=i)).isoformat()


def sync_product(sa, pa, sku, sc, days=14, session: MCPSession | None = None):
    """同步单个产品的全部数据域。
    session：不传 → 用模块级 default_session（单线程）；传入 → 使用该 session（多线程场景每 worker 独立 session）
    """
    _c = session.call if session is not None else mcp_call
    tag = f"{pa}@{sa}"
    print(f"  ▶ {tag} ({sc})")

    # --- 1. 每日销售序列（单日窗口循环）---
    got_days = 0
    for d in _daterange(days):
        if store.has_daily(pa, d, sa):
            existing = store.get_one("daily_product_sales", asin=pa, shop_account=sa, stat_date=d)
            if existing and existing.get("is_frozen"):
                continue
        st, rows = _c("product_sales", {"shop_account": sa, "parent_asin": pa,
                                        "parent_seller_sku": sku, "start_date": d, "end_date": d})
        if st == "OK" and rows:
            store.upsert_daily_sales(pa, pa, sa, sc, d, rows[0])
            got_days += 1
        store.log_sync("product_sales", pa, d, st.lower(), len(rows) if st == "OK" and rows else 0)
        time.sleep(0.15)
    print(f"      每日销售: +{got_days} 天  ({tag})")

    # --- 1.5 每日广告日报 ---
    ad_days = 0
    for d in _daterange(days):
        existing = store.get_one("daily_ad_product", asin=pa, shop_account=sa, stat_date=d)
        if existing and existing.get("is_frozen"):
            continue
        st, rows = _c("ad_product_report", {"shop_account": sa, "parent_asin": pa,
                                            "parent_seller_sku": sku,
                                            "start_date": d, "end_date": d})
        if st == "OK" and rows:
            store.upsert_daily_ad(pa, pa, sa, sc, d, rows[0])
            ad_days += 1
        store.log_sync("ad_product_report", pa, d, st.lower(), len(rows) if st == "OK" and rows else 0)
        time.sleep(0.15)
    print(f"      每日广告: +{ad_days} 天  ({tag})")

    # --- 2. 子体销售 + 月度目标（快照）---
    month = datetime.date.today().strftime("%Y-%m")
    st, rows = _c("sales_performance", {"shop_account": sa, "parent_asin": pa, "parent_seller_sku": sku,
                                        "start_date": (datetime.date.today() - datetime.timedelta(days=30)).isoformat(),
                                        "end_date": datetime.date.today().isoformat()})
    child_list = []
    if st == "OK" and rows:
        for r in rows:
            casin = r.get("asin") or pa
            store.upsert_sales_child(casin, pa, sa, r.get("seller_sku"), month, r)
            child_list.append((casin, r.get("seller_sku")))
    store.log_sync("sales_performance", pa, None, st.lower(), len(rows) if st == "OK" and rows else 0)
    print(f"      子体销售: {len(child_list)} 个子体  ({tag})")

    # --- 3. Listing 基线 ---
    st, rows = _c("listing_basic_info", {"shop_account": sa, "parent_asin": pa, "parent_seller_sku": sku})
    if st == "OK" and rows:
        store.upsert_listing_baseline(pa, sa, sc, rows[0])
    store.log_sync("listing_basic_info", pa, None, st.lower(), len(rows) if st == "OK" and rows else 0)

    # --- 4. FBA 库存汇总 ---
    st, rows = _c("parent_listing_stock_summary", {"shop_account": sa, "parent_asin": pa, "parent_seller_sku": sku})
    if st == "OK" and rows:
        store.upsert_stock_summary(pa, sa, rows[0])
    store.log_sync("parent_listing_stock_summary", pa, None, st.lower(), len(rows) if st == "OK" and rows else 0)

    # --- 5. 产品扩展标签 ---
    done_tag = False
    for casin, csku in ([(pa, sku)] + child_list[:1]):
        st, rows = _c("az_extend_detail", {"shop_account": sa, "asin": casin, "seller_sku": csku or sku})
        if st == "OK" and rows:
            store.upsert_product_tags(casin, sa, csku or sku, rows[0])
            done_tag = True
            break
    store.log_sync("az_extend_detail", pa, None, "ok" if done_tag else "empty")

    # --- 6. 直接竞品 ---
    st, rows = _c("direct_competitors", {"shop_account": sa, "parent_asin": pa, "parent_seller_sku": sku})
    if st == "OK" and rows:
        for r in rows:
            comp = r.get("竞品") or r.get("父ASIN") or r.get("asin")
            if comp:
                store.upsert_competitor(pa, sa, str(comp), r)
    store.log_sync("direct_competitors", pa, None, st.lower(), len(rows) if st == "OK" and rows else 0)


def resolve_products(limit, only_key=None):
    """从 fixture configs + shop_map 解析出 (shop_account, parent_asin, sku, site_code)。"""
    smap = fx.load_shop_map()
    out = []
    for c in fx.load_configs():
        if only_key and c["fixture_key"] != only_key:
            continue
        shop = smap.get(c["shop_id"], {})
        acct = shop.get("account")
        if not acct:
            continue
        sc = (c.get("site_code") or "").replace("Amazon_", "") or "US"
        out.append((acct, c["parent_asin"], c["parent_seller_sku"], sc))
        if not only_key and len(out) >= limit:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--key", type=str, default=None)
    args = ap.parse_args()

    store.init_db()
    mcp_init()
    products = resolve_products(args.limit, args.key)
    print(f"同步 {len(products)} 个产品，每日窗口 {args.days} 天\n" + "=" * 60)
    t0 = time.time()
    for i, (sa, pa, sku, sc) in enumerate(products, 1):
        print(f"[{i}/{len(products)}]", end=" ")
        try:
            sync_product(sa, pa, sku, sc, args.days)
        except Exception as e:
            print(f"      ✗ 异常: {e}")
    print("=" * 60)
    print(f"完成，耗时 {time.time()-t0:.0f}s")
    print("库存量:", store.stats())


if __name__ == "__main__":
    main()
