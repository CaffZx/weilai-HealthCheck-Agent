"""同步 ASIN 级负责人 + 系统用户字典。

数据来源（严格按 MCP 规范调用）：
  1. sys_user_query        分页拉全部用户 (id, userName, userAccount, userState)
  2. sprout_shop_query     分页拉 Amazon 店铺 (shop_id, account) 用于把 erp_config 里的
                           shop_id 数字翻成 shop_account 字符串（不落表，用完即扔）
  3. az_extend_detail      按 (shop_account, asin, seller_sku) 单条调用，取
                           ASIN_PRINCIPAL_USER_ID / EDITOR_ID / CREATOR_ID
                           erp_config 里 638 条独立 (asin, shop) → 8 线程并发 ~2 分钟

用法：
    python -m data.sync_asin_owner              # 全量三步
    python -m data.sync_asin_owner --skip-user  # 只跑店铺+ASIN
"""
from __future__ import annotations
import argparse
import concurrent.futures as _cf
import json
import logging
import os
import re
import sqlite3
import time
import urllib.request

from dotenv import load_dotenv

from data import local_store as store

load_dotenv()
log = logging.getLogger(__name__)

GATEWAY = os.getenv("MCP_SYNC_GATEWAY", "http://mcp-gateway.example.com/mcp")
API_KEY = os.getenv("MCP_API_KEY", "REMOVED_API_KEY")

# ============================================================
# MCP 协议封装（每个 worker 独占一个 session，避免线程共享 sid）
# ============================================================
class MCPSession:
    def __init__(self):
        self._sid: str | None = None
        self._init()

    def _rpc(self, method: str, params: dict | None = None, mid: int = 1) -> str:
        body: dict = {"jsonrpc": "2.0", "method": method}
        if method != "notifications/initialized":
            body["id"] = mid
        if params is not None:
            body["params"] = params
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Api-Key": API_KEY,
        }
        if self._sid:
            h["Mcp-Session-Id"] = self._sid
        req = urllib.request.Request(GATEWAY, data=json.dumps(body).encode(), headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 404 and method not in ("initialize", "notifications/initialized"):
                # 会话失效自动重连
                self._init()
                return self._rpc(method, params, mid)
            raise
        # initialize 时才需要抓 SID
        if method == "initialize":
            self._sid = r.headers.get("Mcp-Session-Id")
        return r.read().decode()

    def _init(self):
        self._sid = None
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "sync-asin-owner", "version": "1"},
        })
        self._rpc("notifications/initialized")

    def call(self, tool: str, args: dict) -> dict | list | None:
        try:
            raw = self._rpc("tools/call", {"name": tool, "arguments": args}, mid=99)
        except Exception as e:
            return {"error": f"http:{e}"}
        m = re.search(r"data:(\{.*\})", raw, re.S)
        if not m:
            return {"error": "no SSE frame"}
        outer = json.loads(m.group(1))
        if outer.get("result", {}).get("isError"):
            return {"error": outer["result"]["content"][0]["text"][:200]}
        try:
            inner = outer["result"]["content"][0]["text"]
            l1 = json.loads(inner)
            if isinstance(l1, dict) and "content" in l1:
                return json.loads(l1["content"][0]["text"])
            return l1
        except Exception as e:
            return {"error": f"parse:{e}"}


# ============================================================
# 1. sys_user 全量分页
# ============================================================
def sync_sys_user() -> int:
    """分页拉全部系统用户 → sys_user 表。返回条数。"""
    s = MCPSession()
    all_users: list[dict] = []
    page = 1
    while True:
        r = s.call("sys_user_query", {"pageNo": page, "pageSize": 10})
        if not isinstance(r, dict) or not r.get("success"):
            log.warning("sys_user_query 第 %d 页失败: %s", page, r)
            break
        data = r.get("data") or []
        all_users.extend(data)
        total = r.get("total", 0)
        if len(all_users) >= total or not data:
            break
        page += 1

    # 落库
    with sqlite3.connect(store.DB_PATH) as db:
        db.execute("DELETE FROM sys_user")
        for u in all_users:
            db.execute("""
                INSERT OR REPLACE INTO sys_user(id, user_name, user_account, user_state, fetched_at)
                VALUES(?,?,?,?, datetime('now','localtime'))
            """, (u.get("id"), u.get("userName"), u.get("userAccount"), u.get("userState")))
        db.commit()
    return len(all_users)


# ============================================================
# 2. 店铺 shop_id → account 映射（不落表，返回 dict）
# ============================================================
def fetch_shop_map() -> dict[int, dict]:
    """{shop_id: {account, name, siteCode}}"""
    s = MCPSession()
    all_shops: list[dict] = []
    page = 1
    while True:
        r = s.call("sprout_shop_query", {"pageNo": page, "pageSize": 10, "platformCode": "Amazon"})
        if not isinstance(r, dict) or not r.get("success"):
            log.warning("sprout_shop_query 第 %d 页失败: %s", page, r)
            break
        data = r.get("data") or []
        all_shops.extend(data)
        total = r.get("total", 0)
        if len(all_shops) >= total or not data:
            break
        page += 1

    mapping: dict[int, dict] = {}
    for sh in all_shops:
        sid = sh.get("id")
        if sid is None:
            continue
        mapping[int(sid)] = {
            "account": sh.get("account"),
            "name": sh.get("name"),
            "siteCode": sh.get("plSiteCode") or sh.get("platformSite"),
        }
    return mapping


# ============================================================
# 3. asin_owner 并发拉 az_extend_detail
# ============================================================
def _fetch_one_owner(session: MCPSession, shop_account: str, asin: str, seller_sku: str) -> dict | None:
    """返回 asin_owner 行 dict 或 None（失败）。"""
    r = session.call("az_extend_detail", {
        "shop_account": shop_account, "asin": asin, "seller_sku": seller_sku,
    })
    if not isinstance(r, dict) or not r.get("rows"):
        return None
    row = r["rows"][0]
    def _int(v):
        try:
            return int(v) if v not in (None, "", "None") else None
        except (TypeError, ValueError):
            return None
    return {
        "asin": asin,  # 始终使用 erp_config 的 parent_asin，保证与 /api/asins 过滤查询一致
        "seller_sku": row.get("SELLER_SKU") or seller_sku,
        "shop_id": _int(row.get("SHOP_ID")),
        "shop_account": row.get("ACCOUNT") or shop_account,
        "site_code": row.get("PL_SITE_CODE"),
        "principal_user_id": _int(row.get("ASIN_PRINCIPAL_USER_ID")),
        "editor_id": _int(row.get("EDITOR_ID")) or _int(row.get("EDITOR_BY")),
        "creator_id": _int(row.get("CREATOR_ID")),
        "source_update_time": row.get("UPDATE_TIME"),
    }


def sync_asin_owner(threads: int = 8) -> tuple[int, int]:
    """从 erp_config 拉 (asin, sku, shop_id) 列表，并发拉 az_extend_detail → asin_owner。
    返回 (成功条数, 失败条数)。"""
    # 从 erp_config 拉三元组
    with sqlite3.connect(store.DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT DISTINCT parent_asin, parent_seller_sku, shop_id
            FROM erp_config
            WHERE parent_asin IS NOT NULL AND parent_seller_sku IS NOT NULL AND shop_id IS NOT NULL
        """).fetchall()
    triples = [(r["parent_asin"], r["parent_seller_sku"], int(r["shop_id"])) for r in rows]
    print(f"  从 erp_config 拿到 {len(triples)} 个三元组")

    # 拉 shop_map
    print("  拉店铺映射…")
    shop_map = fetch_shop_map()
    print(f"  拿到 {len(shop_map)} 家店铺")

    # 过滤没店铺账号的
    tasks = []
    未映射 = 0
    for asin, sku, sid in triples:
        acc = (shop_map.get(sid) or {}).get("account")
        if not acc:
            未映射 += 1
            continue
        tasks.append((acc, asin, sku, sid))
    if 未映射:
        print(f"  跳过 {未映射} 条（shop_id 未在 sprout_shop_query 里）")
    print(f"  待拉取: {len(tasks)} 条，{threads} 线程并发")

    # 线程池并发
    ok_rows: list[dict] = []
    fail = 0
    lock = None
    done_count = 0

    def worker(task):
        acc, asin, sku, sid = task
        # 每 worker 一个 session
        if not hasattr(worker, "sess"):
            worker.sess = MCPSession()
        row = _fetch_one_owner(worker.sess, acc, asin, sku)
        return row, task

    t0 = time.time()
    with _cf.ThreadPoolExecutor(max_workers=threads, thread_name_prefix="own") as pool:
        for row, task in pool.map(worker, tasks):
            done_count += 1
            if row:
                # 兜底填 shop_id（若返回值缺）
                if row.get("shop_id") is None:
                    row["shop_id"] = task[3]
                if not row.get("shop_account"):
                    row["shop_account"] = task[0]
                ok_rows.append(row)
            else:
                fail += 1
            if done_count % 50 == 0:
                elapsed = time.time() - t0
                print(f"    {done_count}/{len(tasks)} 完成（成功 {len(ok_rows)} / 失败 {fail}）· {elapsed:.0f}s")

    # 落库
    with sqlite3.connect(store.DB_PATH) as db:
        db.execute("DELETE FROM asin_owner")
        for r in ok_rows:
            db.execute("""
                INSERT OR REPLACE INTO asin_owner(
                    asin, seller_sku, shop_id, shop_account, site_code,
                    principal_user_id, editor_id, creator_id,
                    source_update_time, fetched_at
                ) VALUES(?,?,?,?,?, ?,?,?, ?, datetime('now','localtime'))
            """, (
                r["asin"], r["seller_sku"], r["shop_id"], r["shop_account"], r["site_code"],
                r["principal_user_id"], r["editor_id"], r["creator_id"],
                r["source_update_time"],
            ))
        db.commit()
    return len(ok_rows), fail


# ============================================================
# 入口
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-user", action="store_true", help="跳过 sys_user 拉取")
    ap.add_argument("--threads", type=int, default=8, help="az_extend_detail 并发线程数")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
    store.init_db()

    print("=" * 70)
    print("同步 ASIN 负责人 + 系统用户字典")
    print("=" * 70)

    t0 = time.time()
    if not args.skip_user:
        print("\n【1/2】sys_user 分页拉取…")
        n_user = sync_sys_user()
        print(f"  ✓ sys_user: {n_user} 行")

    print(f"\n【2/2】asin_owner（az_extend_detail × {args.threads} 线程并发）…")
    ok, fail = sync_asin_owner(threads=args.threads)
    print(f"  ✓ asin_owner: {ok} 行成功 / {fail} 失败")

    # 覆盖率统计
    with sqlite3.connect(store.DB_PATH) as db:
        n_principal = db.execute(
            "SELECT COUNT(*) FROM asin_owner WHERE principal_user_id IS NOT NULL"
        ).fetchone()[0]
        n_editor = db.execute(
            "SELECT COUNT(*) FROM asin_owner WHERE editor_id IS NOT NULL"
        ).fetchone()[0]
        n_creator = db.execute(
            "SELECT COUNT(*) FROM asin_owner WHERE creator_id IS NOT NULL"
        ).fetchone()[0]
        n_named = db.execute("""
            SELECT COUNT(*) FROM asin_owner o
            WHERE COALESCE(o.principal_user_id, o.editor_id, o.creator_id) IS NOT NULL
        """).fetchone()[0]

    print(f"\n覆盖率：principal={n_principal} · editor={n_editor} · creator={n_creator}")
    print(f"任意一项非空: {n_named} / {ok} = {100*n_named/max(ok,1):.1f}%")
    print(f"\n✅ 完成 · 总耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
