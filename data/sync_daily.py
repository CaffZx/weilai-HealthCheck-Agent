"""真实调用 MCP → 写入本地缓存库。

用法：
    python -m data.sync_daily --limit 50 --days 14
    python -m data.sync_daily --key B0EXAMPLE0__1561   # 单个

策略：
  - 每日类(product_sales)：单日窗口循环，已冻结(>14天且库里有)的天跳过，不重复调用
  - 快照类(sales_performance/listing/stock/tags/competitors)：每次覆盖刷新
  - 全程 sync_log 记录，可断点续跑
"""
from __future__ import annotations
import os, re, json, time, argparse, datetime, urllib.request
from pathlib import Path

from data import local_store as store
from data import fixture_loader as fx

GATEWAY = os.environ.get("MCP_SYNC_GATEWAY", "http://mcp-gateway.example.com/mcp")
API_KEY = os.environ.get("MCP_API_KEY", "REMOVED_API_KEY")

_SID = None
_HEADERS = {}


def _rpc(method, params=None, mid=1):
    body = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        body["params"] = params
    if method == "notifications/initialized":
        del body["id"]
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "X-Api-Key": API_KEY}
    if _SID:
        h["Mcp-Session-Id"] = _SID
    req = urllib.request.Request(GATEWAY, data=json.dumps(body).encode(), headers=h)
    r = urllib.request.urlopen(req, timeout=90)
    global _HEADERS
    _HEADERS = r.headers
    return r.read().decode()


def mcp_init():
    global _SID
    _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "sync", "version": "1"}})
    _SID = _HEADERS.get("Mcp-Session-Id")
    _rpc("notifications/initialized")


def mcp_call(tool: str, args: dict):
    """返回 (status, rows|dict|None)。status: OK/EMPTY/NOPERM/ERR"""
    try:
        raw = _rpc("tools/call", {"name": tool, "arguments": args}, mid=9)
    except Exception as e:
        return "ERR", {"err": str(e)[:100]}
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


def sync_product(sa, pa, sku, sc, days=14):
    """同步单个产品的全部数据域。"""
    tag = f"{pa}@{sa}"
    print(f"  ▶ {tag} ({sc})")

    # --- 1. 每日销售序列（单日窗口循环）---
    got_days = 0
    for d in _daterange(days):
        if store.has_daily(pa, d):
            # 已在库且冻结的跳过；未冻结的重刷（归因窗口内）
            existing = store.get_one("daily_product_sales", asin=pa, stat_date=d)
            if existing and existing.get("is_frozen"):
                continue
        st, rows = mcp_call("product_sales", {"shop_account": sa, "parent_asin": pa,
                                              "parent_seller_sku": sku, "start_date": d, "end_date": d})
        if st == "OK" and rows:
            store.upsert_daily_sales(pa, pa, sa, sc, d, rows[0])
            got_days += 1
        store.log_sync("product_sales", pa, d, st.lower(), len(rows) if st == "OK" and rows else 0)
        time.sleep(0.15)
    print(f"      每日销售: +{got_days} 天")

    # --- 2. 子体销售 + 月度目标（快照）---
    month = datetime.date.today().strftime("%Y-%m")
    st, rows = mcp_call("sales_performance", {"shop_account": sa, "parent_asin": pa, "parent_seller_sku": sku,
                                             "start_date": (datetime.date.today() - datetime.timedelta(days=30)).isoformat(),
                                             "end_date": datetime.date.today().isoformat()})
    child_list = []
    if st == "OK" and rows:
        for r in rows:
            casin = r.get("asin") or pa
            store.upsert_sales_child(casin, pa, sa, r.get("seller_sku"), month, r)
            child_list.append((casin, r.get("seller_sku")))
    store.log_sync("sales_performance", pa, None, st.lower(), len(rows) if st == "OK" and rows else 0)
    print(f"      子体销售: {len(child_list)} 个子体")

    # --- 3. Listing 基线（V1 有转化率/类目均值）---
    st, rows = mcp_call("listing_basic_info", {"shop_account": sa, "parent_asin": pa, "parent_seller_sku": sku})
    if st == "OK" and rows:
        store.upsert_listing_baseline(pa, sa, sc, rows[0])
    store.log_sync("listing_basic_info", pa, None, st.lower(), len(rows) if st == "OK" and rows else 0)

    # --- 4. FBA 库存汇总 ---
    st, rows = mcp_call("parent_listing_stock_summary", {"shop_account": sa, "parent_asin": pa, "parent_seller_sku": sku})
    if st == "OK" and rows:
        store.upsert_stock_summary(pa, sa, rows[0])
    store.log_sync("parent_listing_stock_summary", pa, None, st.lower(), len(rows) if st == "OK" and rows else 0)

    # --- 5. 产品扩展标签（父 + 首个子体尝试）---
    done_tag = False
    for casin, csku in ([(pa, sku)] + child_list[:1]):
        st, rows = mcp_call("az_extend_detail", {"shop_account": sa, "asin": casin, "seller_sku": csku or sku})
        if st == "OK" and rows:
            store.upsert_product_tags(casin, sa, csku or sku, rows[0])
            done_tag = True
            break
    store.log_sync("az_extend_detail", pa, None, "ok" if done_tag else "empty")

    # --- 6. 直接竞品 ---
    st, rows = mcp_call("direct_competitors", {"shop_account": sa, "parent_asin": pa, "parent_seller_sku": sku})
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
