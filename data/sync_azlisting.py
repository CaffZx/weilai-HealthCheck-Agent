"""azlisting-mcpserver 数据同步（B 方案）

同步 4 个新数据源：
  1. erp_listing_natural_advert_flow  → daily_natural_ad_flow
  2. erp_listing_monthly_goal         → monthly_goal
  3. erp_listing_stock_alert          → stock_alert
  4. erp_listing_inventory_cost_analysis → inventory_cost

用法：
    python -m data.sync_azlisting --limit 5 --days 30
    python -m data.sync_azlisting --key B0EXAMPLE0__35451

【设计约定】
- 独立模块，不动 sync_daily.py（B 方案实验期，方便回退）
- 所有字段名沿用 MCP 原生（camelCase：adOrderNum、canSaleNum 等），不改中文
- 数据不足场景明确记录到 sync_log
"""
from __future__ import annotations
import argparse
import datetime
import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path

from data import local_store as store
from data import fixture_loader as fx

log = logging.getLogger(__name__)

GATEWAY = os.environ["AZLISTING_GATEWAY"]  # 例：http://<mcp-host>/mcp
API_KEY = os.environ["MCP_API_KEY"]

_SID: str | None = None
_HEADERS: dict = {}


# ==========================================================================
# 线程安全的 MCPSession（多 worker 并发场景每个 worker 独立会话）
# 同 sync_daily.MCPSession，保持结构一致
# ==========================================================================
class MCPSession:
    """独立的 azlisting MCP 会话。404 会话失效自动重连一次。"""
    def __init__(self):
        self._sid: str | None = None
        self._headers: dict = {}
        self._init()

    def _rpc(self, method, params=None, mid=1, _no_retry=False):
        body = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            body["params"] = params
        if method == "notifications/initialized":
            del body["id"]
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Api-Key": API_KEY,
        }
        if self._sid:
            h["Mcp-Session-Id"] = self._sid
        req = urllib.request.Request(GATEWAY, data=json.dumps(body).encode(), headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=90)
        except urllib.error.HTTPError as e:
            if e.code == 404 and not _no_retry and method not in ("initialize", "notifications/initialized"):
                log.warning("azlisting 会话失效(404)，自动重连…")
                self._init()
                return self._rpc(method, params, mid, _no_retry=True)
            raise
        self._headers = dict(r.headers)
        return r.read().decode()

    def _init(self):
        self._sid = None
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "sync-azlist", "version": "1"}}, _no_retry=True)
        self._sid = self._headers.get("Mcp-Session-Id")
        self._rpc("notifications/initialized", _no_retry=True)

    def call(self, tool: str, args: dict):
        try:
            raw = self._rpc("tools/call", {"name": tool, "arguments": args}, mid=99)
        except Exception as e:
            return "ERR", {"err": str(e)[:120]}
        return _parse_response(raw)


def _parse_response(raw: str):
    """MCP SSE 返回三层剥壳。"""
    m = re.search(r"data:(\{.*\})", raw, re.S)
    if not m:
        return "ERR", {"err": "no SSE frame"}
    obj = json.loads(m.group(1))
    res = obj.get("result", {})
    if res.get("isError"):
        msg = res["content"][0]["text"][:200]
        # 网关把"没数据"也用 isError=True 包出来（如"未查询到…"），语义等同 EMPTY
        if _is_no_data_message(msg):
            return "EMPTY", {"err": msg}
        return "ERR", {"err": msg}
    try:
        inner = res["content"][0]["text"]
        l1 = json.loads(inner)
        if isinstance(l1, dict) and "content" in l1:
            l2 = json.loads(l1["content"][0]["text"])
        else:
            l2 = l1
    except Exception as e:
        return "ERR", {"err": f"parse: {e}"}
    if isinstance(l2, dict) and "data" in l2:
        data = l2["data"]
        if data in (None, [], {}):
            return "EMPTY", {"err": str(l2.get("message") or "MCP 返回空数据")[:200]}
        return "OK", data
    if isinstance(l2, list):
        return ("OK", l2) if l2 else ("EMPTY", {"err": "MCP 返回空列表"})
    return "OK", l2


def _is_no_data_message(message: str) -> bool:
    """识别业务上的无数据响应，避免把无目标误报成接口故障。"""
    return any(text in message for text in (
        "未查询到", "没有查询到", "无数据", "暂无目标", "未设置目标",
        "目标为空", "目标不存在", "未配置目标", "没有配置目标",
    ))


def _is_retryable_monthly_error(status: str, data) -> bool:
    """判断月度目标请求是否适合短暂重试一次。"""
    if status != "ERR":
        return False
    message = str(data.get("err") if isinstance(data, dict) else data or "").lower()
    return any(text in message for text in (
        "parse:", "no sse", "empty response", "http 429", "429 too many",
        "timed out", "timeout", "502", "503", "504",
    ))


# ==========================================================================
# 单线程向后兼容（旧调用者用）
# ==========================================================================
_default_session: MCPSession | None = None


def mcp_init():
    """初始化默认 session（单线程兼容）。多线程请自行 MCPSession()。"""
    global _default_session
    _default_session = MCPSession()


def _rpc(method, params=None, mid=1, _no_retry=False):
    global _default_session
    if _default_session is None:
        mcp_init()
    return _default_session._rpc(method, params, mid, _no_retry)


def mcp_call(tool: str, args: dict):
    """单线程兼容入口。多线程用 session.call(tool, args)。"""
    if _default_session is None:
        mcp_init()
    return _default_session.call(tool, args)


# ---------------- 单个数据域同步 ----------------
def sync_natural_advert_flow(sa, pa, sku, days=30, _c=None) -> tuple[int, str]:
    _c = _c or mcp_call
    """同步 §3.6/§3.8 需要的每日自然/广告拆分数据。

    实测发现：返回的行没有 recordDate 字段，但按子ASIN分组后每子体正好 N 行
    对应 N 天（按 startDate → endDate 顺序）。故按行序推日期。
    """
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=days)
    end_date = today

    st, data = _c("erp_listing_natural_advert_flow", {
        "shopAccount": sa, "parentAsin": pa, "parentSeller": sku,
        "startDate": start_date.isoformat(), "endDate": end_date.isoformat(),
    })
    if st != "OK":
        store.log_sync("erp_listing_natural_advert_flow", pa, None, st.lower(),
                       note=(data.get("err") if isinstance(data, dict) else ""))
        return 0, st

    # 按子ASIN分组，保持返回顺序
    from collections import OrderedDict
    grouped: "OrderedDict[str, list[dict]]" = OrderedDict()
    for row in data:
        asin = row.get("asin")
        if not asin:
            continue
        grouped.setdefault(asin, []).append(row)

    if not grouped:
        store.log_sync("erp_listing_natural_advert_flow", pa, None, "empty")
        return 0, "OK"

    # 期望天数 = endDate - startDate + 1
    期望天数 = (end_date - start_date).days + 1
    end_date_iso = end_date.isoformat()

    n = 0
    异常子体数 = 0
    超窗行数 = 0
    for asin, rows in grouped.items():
        实际天数 = len(rows)
        if 实际天数 != 期望天数:
            # 部分子体可能返回不齐，按实际行数从 endDate 往前推
            异常子体数 += 1
        # 行序 → 日期映射：第一行 = startDate，最后一行 = startDate + (实际-1)
        # 关键护栏：idx 不能超出窗口，否则会推到未来日期（历史 bug 导致表里出现 2027 年记录）
        for idx, row in enumerate(rows):
            if idx >= 期望天数:
                超窗行数 += 1
                break
            stat_date = (start_date + datetime.timedelta(days=idx)).isoformat()
            # 双保险：日期不能大于今天
            if stat_date > end_date_iso:
                超窗行数 += 1
                continue
            store.upsert_natural_ad_flow(
                asin=asin,
                parent_asin=row.get("parentAsin") or pa,
                shop_account=sa,
                seller_sku=row.get("sellerSku"),
                parent_seller_sku=row.get("parentSeller") or sku,
                stat_date=stat_date,
                is_summary=row.get("isSummary", False),
                row=row,
            )
            n += 1

    note = f"总{len(data)}行/{len(grouped)}子体,期望{期望天数}天/子体"
    if 异常子体数:
        note += f",{异常子体数}子体行数异常"
    store.log_sync("erp_listing_natural_advert_flow", pa, None, "ok", rows_count=n, note=note)
    return n, "OK"


def sync_product_info(sa, pa, sku, _c=None) -> tuple[int, str]:
    """拉产品信息（五点/标题/类目/变体主题）。paramsJson 入参。"""
    _c = _c or mcp_call
    st, data = _c("erp_listing_product_info", {
        "paramsJson": json.dumps({
            "shopAccount": sa, "parentAsin": pa, "parentSellerSku": sku,
        }, ensure_ascii=False),
    })
    if st != "OK":
        store.log_sync("erp_listing_product_info", pa, None, st.lower())
        return 0, st
    # data 是 list（每个子体一行），我们取第一行的父级字段汇总即可（fiveBulletPoint 等父卡共享）
    row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
    if not row:
        store.log_sync("erp_listing_product_info", pa, None, "empty")
        return 0, "OK"
    store.upsert_listing_inspection_snapshot(pa, sku, sa, "product_info", data)
    store.upsert_product_info(
        parent_asin=pa, parent_seller_sku=sku, shop_account=sa, row=row,
    )
    store.log_sync("erp_listing_product_info", pa, None, "ok", rows_count=1)
    return 1, "OK"


def _sync_inspection_source(tool: str, source: str, sa: str, pa: str, sku: str,
                            args: dict, _c=None) -> tuple[int, str]:
    caller = _c or mcp_call
    status, data = caller(tool, args)
    if status != "OK":
        error = data.get("err") if isinstance(data, dict) else data
        store.log_sync(tool, pa, None, status.lower(), note=str(error or "")[:500])
        return 0, status
    store.upsert_listing_inspection_snapshot(pa, sku, sa, source, data)
    count = len(data) if isinstance(data, list) else 1
    store.log_sync(tool, pa, None, "ok", rows_count=count)
    return count, "OK"


def sync_business_report(sa, pa, sku, _c=None) -> tuple[int, str]:
    return _sync_inspection_source(
        "erp_listing_business_report", "business_report", sa, pa, sku,
        {"shopAccount": sa, "parentAsin": pa, "parentSellerSku": sku}, _c,
    )


def sync_platform_activity(sa, pa, sku, _c=None) -> tuple[int, str]:
    return _sync_inspection_source(
        "erp_listing_platform_activity", "platform_activity", sa, pa, sku,
        {"shopAccount": sa, "parentAsin": pa, "parentSellerSku": sku}, _c,
    )


def sync_refund_rate(sa, pa, sku, _c=None) -> tuple[int, str]:
    return _sync_inspection_source(
        "erp_listing_refund_rate", "refund_rate", sa, pa, sku,
        {"shopAccount": sa, "parentAsin": pa, "parentSellerSku": sku}, _c,
    )


def sync_unsalable_product_line(sa, pa, sku, _c=None) -> tuple[int, str]:
    return _sync_inspection_source(
        "erp_listing_unsalable_product_line", "unsalable_product_line", sa, pa, sku,
        {"shopAccount": sa, "parentAsin": pa, "parentSellerSku": sku}, _c,
    )


def sync_monthly_goal(sa, pa, sku, _c=None) -> tuple[int, str]:
    _c = _c or mcp_call
    # 网关 2026-07 起改成必传 parentSellerSku；不传全部 ERR
    args = {
        "shopAccount": sa, "parentAsin": pa, "parentSellerSku": sku,
    }
    st, data = _c("erp_listing_monthly_goal", args)
    if _is_retryable_monthly_error(st, data):
        time.sleep(0.5)
        retry_status, retry_data = _c("erp_listing_monthly_goal", args)
        if retry_status != "ERR" or not _is_retryable_monthly_error(retry_status, retry_data):
            st, data = retry_status, retry_data
        else:
            first_error = data.get("err") if isinstance(data, dict) else data
            second_error = retry_data.get("err") if isinstance(retry_data, dict) else retry_data
            st, data = retry_status, {
                "err": f"首次: {first_error}; 重试: {second_error}",
            }
    if st != "OK":
        error = data.get("err") if isinstance(data, dict) else data
        store.log_sync("erp_listing_monthly_goal", pa, None, st.lower(),
                       note=str(error or "MCP 未返回月度目标数据")[:500])
        return 0, st

    if isinstance(data, dict):
        data = data.get("rows") or data.get("data") or [data]
    if not isinstance(data, list):
        store.log_sync("erp_listing_monthly_goal", pa, None, "err",
                       note=f"响应格式异常: {type(data).__name__}")
        return 0, "ERR"

    normalized_rows = []
    for row in data:
        if not isinstance(row, dict):
            normalized_rows.append(row)
            continue
        monthly_goals = row.get("monthlyGoals")
        if isinstance(monthly_goals, list):
            normalized_rows.extend(monthly_goals)
        else:
            normalized_rows.append(row)

    n = 0
    skipped = []
    for row in normalized_rows:
        if not isinstance(row, dict):
            skipped.append(f"非对象行:{type(row).__name__}")
            continue
        warn = row.get("warn")
        if warn:
            skipped.append(str(warn))
            continue
        month_str = row.get("everyMonthStr")
        if not month_str:
            skipped.append("缺少 everyMonthStr")
            continue
        store.upsert_monthly_goal(
            parent_asin=pa,
            parent_seller_sku=row.get("parentSellerSku") or sku,
            shop_account=sa,
            month_str=month_str,
            row=row,
        )
        n += 1
    note = "；".join(dict.fromkeys(skipped))
    store.log_sync("erp_listing_monthly_goal", pa, None, "ok" if n > 0 else "empty",
                   rows_count=n, note=note[:500])
    return n, "OK"


def sync_stock_alert(sa, pa, sku, _c=None) -> tuple[int, str]:
    _c = _c or mcp_call
    st, data = _c("erp_listing_stock_alert", {
        "shopAccount": sa, "parentAsin": pa, "parentSeller": sku,
    })
    if st != "OK":
        store.log_sync("erp_listing_stock_alert", pa, None, st.lower())
        return 0, st
    n = 0
    for row in data:
        store.upsert_stock_alert(
            parent_asin=pa,
            parent_seller_sku=sku,
            shop_account=sa,
            row=row,
        )
        n += 1
    store.log_sync("erp_listing_stock_alert", pa, None, "ok" if n > 0 else "empty", rows_count=n)
    return n, "OK"


def sync_inventory_cost(sa, pa, sku, _c=None) -> tuple[int, str]:
    _c = _c or mcp_call
    st, data = _c("erp_listing_inventory_cost_analysis", {
        "shopAccount": sa, "parentAsin": pa, "parentSeller": sku,
    })
    if st != "OK":
        store.log_sync("erp_listing_inventory_cost_analysis", pa, None, st.lower())
        return 0, st
    n = 0
    for parent_row in data:
        report_month = parent_row.get("reportMonth")
        children = parent_row.get("children", [])
        for child in children:
            store.upsert_inventory_cost(
                parent_asin=pa,
                parent_seller_sku=sku,
                shop_account=sa,
                child_asin=child.get("asin"),
                seller_sku=child.get("sellerSku"),
                fn_sku=child.get("fnSku"),
                report_month=report_month,
                row=child,
            )
            n += 1
    store.log_sync("erp_listing_inventory_cost_analysis", pa, None,
                   "ok" if n > 0 else "empty", rows_count=n)
    return n, "OK"


def sync_price_promo(sa, pa, sc, _c=None) -> tuple[int, str]:
    _c = _c or mcp_call
    """子ASIN 实时价格促销快照。逐子体调用（实时爬）。
    子体列表从本地 sales_child 表取（需先跑过 sync_daily.sales_performance）。"""
    import sqlite3
    conn = sqlite3.connect(store.DB_PATH)
    child_rows = conn.execute(
        "SELECT DISTINCT asin FROM sales_child WHERE parent_asin=? AND asin IS NOT NULL AND asin != ''",
        (pa,)
    ).fetchall()
    conn.close()

    child_asins = [r[0] for r in child_rows]
    if not child_asins:
        store.log_sync("erp_listing_price_promotion_analysis", pa, None, "empty",
                       note="本地无子ASIN列表（请先跑 sync_daily.sales_performance）")
        return 0, "EMPTY"

    snapshot_date = datetime.date.today().isoformat()
    n = 0
    errs = 0
    for ca in child_asins:
        st, data = _c("erp_listing_price_promotion_analysis", {"asin": ca, "siteCode": sc})
        if st != "OK":
            errs += 1
            continue
        # data 可能是 list（多站点/多品种）或 dict
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict):
                continue
            # 确保 asin 一致
            row_asin = row.get("asin") or ca
            store.upsert_child_price_promo(
                child_asin=row_asin, parent_asin=pa,
                shop_account=sa, site_code=sc,
                snapshot_date=snapshot_date,
                row=row,
            )
            n += 1
        time.sleep(0.1)  # 实时爬控速

    store.log_sync("erp_listing_price_promotion_analysis", pa, None,
                   "ok" if n > 0 else "empty",
                   rows_count=n,
                   note=f"共{len(child_asins)}个子体，成功{n}，失败{errs}")
    return n, "OK" if n > 0 else "ERR"


# ---------------- 产品级同步（跑一个 ASIN 的全部数据域）----------------
def sync_product(sa, pa, sku, sc, days=30, with_price_promo=True, session: MCPSession | None = None):
    """同步单个产品的 azlisting 数据域。session 传入时走并发路径。"""
    _c = session.call if session is not None else mcp_call
    tag = f"{pa}@{sa}"

    n1, s1 = sync_natural_advert_flow(sa, pa, sku, days=days, _c=_c)
    print(f"  natural_advert_flow: {s1} · +{n1} 行  ({tag})")
    time.sleep(0.2)

    n2, s2 = sync_monthly_goal(sa, pa, sku, _c=_c)
    print(f"  monthly_goal       : {s2} · +{n2} 行  ({tag})")
    time.sleep(0.2)

    n2b, s2b = sync_product_info(sa, pa, sku, _c=_c)
    print(f"  product_info       : {s2b} · +{n2b} 行  ({tag})")
    time.sleep(0.2)

    n2c, s2c = sync_business_report(sa, pa, sku, _c=_c)
    print(f"  business_report    : {s2c} · +{n2c} 行  ({tag})")
    time.sleep(0.2)

    n2d, s2d = sync_platform_activity(sa, pa, sku, _c=_c)
    print(f"  platform_activity  : {s2d} · +{n2d} 行  ({tag})")
    time.sleep(0.2)

    n2e, s2e = sync_refund_rate(sa, pa, sku, _c=_c)
    print(f"  refund_rate        : {s2e} · +{n2e} 行  ({tag})")
    time.sleep(0.2)

    n2f, s2f = sync_unsalable_product_line(sa, pa, sku, _c=_c)
    print(f"  unsalable_line     : {s2f} · +{n2f} 行  ({tag})")
    time.sleep(0.2)

    n3, s3 = sync_stock_alert(sa, pa, sku, _c=_c)
    print(f"  stock_alert        : {s3} · +{n3} 行  ({tag})")
    time.sleep(0.2)

    n4, s4 = sync_inventory_cost(sa, pa, sku, _c=_c)
    print(f"  inventory_cost     : {s4} · +{n4} 行  ({tag})")

    if with_price_promo:
        time.sleep(0.2)
        n5, s5 = sync_price_promo(sa, pa, sc, _c=_c)
        print(f"  price_promo (实时) : {s5} · +{n5} 行  ({tag})")


def resolve_products(limit, only_key=None):
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
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--key", type=str, default=None)
    args = ap.parse_args()

    store.init_db()
    mcp_init()

    products = resolve_products(args.limit, args.key)
    print(f"azlisting 同步 {len(products)} 个产品 · 每日窗口 {args.days} 天")
    print("=" * 70)
    t0 = time.time()
    for i, (sa, pa, sku, sc) in enumerate(products, 1):
        print(f"[{i}/{len(products)}]", end="")
        try:
            sync_product(sa, pa, sku, sc, days=args.days)
        except Exception as e:
            print(f"  ✗ 异常: {e}")
    print("=" * 70)
    print(f"完成 · 耗时 {time.time()-t0:.0f}s\n")

    # 简要统计
    import sqlite3
    with sqlite3.connect(store.DB_PATH) as c:
        for t in ("daily_natural_ad_flow", "monthly_goal", "listing_product_info", "stock_alert", "inventory_cost"):
            n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {n} 行")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")
    main()
