"""正向同步 asin_owner：按负责人姓名拉其负责的在线列表。

对比旧版 data/sync_asin_owner.py（反向：遍历 erp_config 全部父 ASIN，逐个 az_extend_detail）：
  · 正向：遍历负责人 → 一次调用拉他名下全部在线列表，调用次数从「ASIN 数」降到「负责人数」。
  · 工具：erp_listing_follow_up_by_principal（azlisting 网关），入参 principalName（姓名精确匹配）。
  · principal_user_id 直接取该姓名对应的 sys_user.id（按名查，天然知道归属，无需二次查询）。

工具返回是「子 ASIN 级 + 含 Inactive + 含跟卖」的宽集，本脚本默认收敛为与旧版一致的口径：
  · 仅 status=Active；
  · 折叠到父 ASIN（一父一店一行）；
  · 与 erp_config 在管父 ASIN(enabled=1) 取交集（避免宽集污染工作台范围）。
  上述每一项都可用开关放开。

安全：默认 --dry-run（只统计 + 与现有 asin_owner 对比，不写库）。--apply 才 DELETE+INSERT 覆盖 asin_owner。

负责人名单来源（优先级）：--principals 指定 > --from-existing > --all-active > 按角色（默认）。
默认按角色 GROUP_FBASaler（FBA销售=运营）筛，约 24 人；已知负责人 100% 挂此角色，
避免遍历全部 177 活跃用户（其中 ~150 是仓库/采购/质检，空跑）。首次会自动同步 user_role。

用法：
    python -m data.sync_asin_owner_forward --dry-run                 # 默认按运营角色(~24人)，只看统计
    python -m data.sync_asin_owner_forward --principals <姓名1>,<姓名2>   # 只跑指定负责人
    python -m data.sync_asin_owner_forward --from-existing --dry-run # 只刷新当前已在 asin_owner 里的负责人（快）
    python -m data.sync_asin_owner_forward --all-active --dry-run    # 遍历全部活跃用户（慢，会空跑）
    python -m data.sync_asin_owner_forward --apply                   # 确认无误后真正写库
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

from data import local_store as store
from data.sync_asin_owner import fetch_shop_map  # 复用 sprout_shop_query（shop_id ↔ account/site）

load_dotenv()
log = logging.getLogger(__name__)

GATEWAY = os.environ["AZLISTING_GATEWAY"]  # 例：http://<mcp-host>/mcp
API_KEY = os.getenv("MCP_API_KEY")
TOOL = "erp_listing_follow_up_by_principal"
PAGE_SIZE = 200  # 工具上限
OPERATOR_ROLE = "GROUP_FBASaler"  # Amazon 运营角色（FBA销售）；已知负责人 100% 挂此角色


# ============================================================
# azlisting MCP 会话（自带 data[0].records 双层剥壳）
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
                self._init()
                return self._rpc(method, params, mid)
            raise
        if method == "initialize":
            self._sid = r.headers.get("Mcp-Session-Id")
        return r.read().decode()

    def _init(self):
        self._sid = None
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "sync-asin-owner-forward", "version": "1"},
        })
        self._rpc("notifications/initialized")

    def _call_once(self, tool: str, args: dict) -> dict | None:
        """单次调用，剥壳返回业务 dict；失败返回 {'error': ..., '_retryable': bool}。"""
        try:
            raw = self._rpc("tools/call", {"name": tool, "arguments": args}, mid=99)
        except Exception as e:
            return {"error": f"http:{e}", "_retryable": True}
        if not raw or not raw.strip():
            return {"error": "empty response", "_retryable": True}
        m = re.search(r"data:(\{.*\})", raw, re.S)
        if not m:
            return {"error": "no SSE frame", "_retryable": True}
        try:
            outer = json.loads(m.group(1))
        except Exception as e:
            return {"error": f"outer-parse:{e}", "_retryable": True}
        if outer.get("result", {}).get("isError"):
            # 业务错误（如姓名不存在）→ 不重试
            return {"error": outer["result"]["content"][0]["text"][:200], "_retryable": False}
        try:
            l1 = json.loads(outer["result"]["content"][0]["text"])
            if isinstance(l1, dict) and "content" in l1:
                l1 = json.loads(l1["content"][0]["text"])
            return l1
        except Exception as e:
            # 内层空/非 JSON —— 网关瞬时限流的典型表现，可重试
            return {"error": f"parse:{e}", "_retryable": True}

    def call(self, tool: str, args: dict, retries: int = 3, backoff: float = 1.5) -> dict | None:
        """带重试的调用。瞬时错误（空返回/解析失败/HTTP）重试并退避、必要时重连会话。"""
        last = None
        for attempt in range(retries + 1):
            resp = self._call_once(tool, args)
            if not (isinstance(resp, dict) and resp.get("error")):
                return resp  # 成功
            last = resp
            if not resp.get("_retryable") or attempt == retries:
                break
            wait = backoff * (2 ** attempt)
            log.warning("调用 %s 失败(%s)，%.1fs 后重试 %d/%d", tool, resp.get("error"), wait, attempt + 1, retries)
            time.sleep(wait)
            try:
                self._init()  # 重连会话，规避会话级限流/失效
            except Exception as e:
                log.warning("会话重连失败：%s", e)
        return last


def _extract_block(resp: dict | None) -> dict | None:
    """从工具返回里取出 {totalCount,totalPages,pageNo,pageSize,records} 分页块。"""
    if not isinstance(resp, dict) or resp.get("error"):
        return None
    data = resp.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict) and "records" in data[0]:
        return data[0]
    # 兼容潜在的扁平结构
    if "records" in resp:
        return resp
    return None


# ============================================================
# 拉单个负责人全部在线列表
# ============================================================
def fetch_principal_listings(
    session: MCPSession, name: str, include_inactive: bool = False
) -> tuple[list[dict], int, bool]:
    """返回 (records, total, complete)。complete=False 表示有页重试到底仍失败，数据不全。"""
    first = session.call(TOOL, {"principalName": name, "pageNo": 1, "pageSize": PAGE_SIZE})
    blk = _extract_block(first)
    if blk is None:
        err = first.get("error") if isinstance(first, dict) else "unknown"
        log.warning("负责人 %s 首页拉取失败（已重试）：%s", name, err)
        return [], 0, False
    total = int(blk.get("totalCount") or 0)
    total_pages = int(blk.get("totalPages") or 1)
    records = list(blk.get("records") or [])
    complete = True
    for page in range(2, total_pages + 1):
        resp = session.call(TOOL, {"principalName": name, "pageNo": page, "pageSize": PAGE_SIZE})
        b = _extract_block(resp)
        if b is None:
            log.warning("负责人 %s 第 %d/%d 页重试到底仍失败，数据不全", name, page, total_pages)
            complete = False
            break
        records.extend(b.get("records") or [])
    if not include_inactive:
        records = [r for r in records if (r.get("status") or "").lower() == "active"]
    return records, total, complete


# ============================================================
# 主流程
# ============================================================
def _load_active_users() -> dict[str, int]:
    """{user_name: id}，仅活跃用户；重名则跳过并告警（避免归属串号）。"""
    with sqlite3.connect(store.DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, user_name FROM sys_user WHERE user_state=1 AND user_name IS NOT NULL"
        ).fetchall()
    name_to_ids: dict[str, list[int]] = {}
    for r in rows:
        name_to_ids.setdefault(r["user_name"], []).append(r["id"])
    result: dict[str, int] = {}
    for name, ids in name_to_ids.items():
        if len(ids) > 1:
            log.warning("姓名 %s 对应多个活跃用户 %s，按名查会串号，已跳过", name, ids)
            continue
        result[name] = ids[0]
    return result


def _load_erp_parents() -> set[tuple[str, int]]:
    """erp_config 在管父 ASIN：{(parent_asin, shop_id)}。"""
    with sqlite3.connect(store.DB_PATH) as db:
        rows = db.execute(
            "SELECT DISTINCT parent_asin, shop_id FROM erp_config WHERE enabled=1"
        ).fetchall()
    return {(pa, int(sid)) for pa, sid in rows if pa and sid is not None}


def _load_operator_names(role_code: str) -> list[str]:
    """活跃用户中挂了指定角色的姓名（默认运营角色 = FBA销售）。"""
    with sqlite3.connect(store.DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT DISTINCT u.user_name
            FROM sys_user u
            JOIN user_role r ON r.user_id = u.id
            WHERE u.user_state = 1 AND r.role_code = ? AND u.user_name IS NOT NULL
        """, (role_code,)).fetchall()
    return [r["user_name"] for r in rows]


def _user_role_empty() -> bool:
    with sqlite3.connect(store.DB_PATH) as db:
        return db.execute("SELECT COUNT(*) FROM user_role").fetchone()[0] == 0


def _ensure_multi_owner_pk(db: sqlite3.Connection) -> None:
    """确保 asin_owner 主键含 principal_user_id（支持一产品挂多负责人/助理）。
    旧库主键是 (asin,seller_sku,shop_id) → 就地重建表结构（数据随后由本次同步全量重写，不保留旧行）。"""
    pk_cols = [r[1] for r in db.execute("PRAGMA table_info(asin_owner)").fetchall() if r[5]]
    if "principal_user_id" in pk_cols:
        return  # 已是新主键
    log.warning("检测到旧主键 %s，重建 asin_owner 为多负责人结构", pk_cols)
    db.execute("DROP TABLE IF EXISTS asin_owner")
    db.execute("""
        CREATE TABLE asin_owner (
          asin                TEXT NOT NULL,
          seller_sku          TEXT NOT NULL,
          shop_id             INTEGER,
          shop_account        TEXT,
          site_code           TEXT,
          principal_user_id   INTEGER,
          editor_id           INTEGER,
          creator_id          INTEGER,
          source_update_time  TEXT,
          fetched_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          PRIMARY KEY (asin, seller_sku, shop_id, principal_user_id)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_asin_owner_asin ON asin_owner(asin)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_asin_owner_principal ON asin_owner(principal_user_id)")


def _load_existing_owner_names() -> list[str]:
    """当前 asin_owner 里已出现的负责人姓名（供 --from-existing 增量刷新）。"""
    with sqlite3.connect(store.DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT DISTINCT u.user_name
            FROM asin_owner o
            JOIN sys_user u ON u.id = COALESCE(o.principal_user_id, o.editor_id, o.creator_id)
            WHERE u.user_name IS NOT NULL
        """).fetchall()
    return [r["user_name"] for r in rows]


def sync_forward(
    principals: list[str] | None = None,
    include_inactive: bool = False,
    erp_filter: bool = True,
    apply: bool = False,
    force_incomplete: bool = False,
) -> dict:
    users = _load_active_users()
    if principals:
        unknown = [p for p in principals if p not in users]
        if unknown:
            log.warning("以下姓名不在活跃用户里，跳过：%s", unknown)
        principals = [p for p in principals if p in users]
    else:
        principals = list(users.keys())

    print(f"  负责人数：{len(principals)}（活跃用户共 {len(users)}）")

    # account -> (shop_id, site_code)
    print("  拉店铺映射（sprout_shop_query）…")
    shop_map = fetch_shop_map()
    acc_to_shop = {
        (v.get("account") or ""): (sid, v.get("siteCode"))
        for sid, v in shop_map.items() if v.get("account")
    }
    print(f"  店铺映射 {len(acc_to_shop)} 家")

    erp_parents = _load_erp_parents() if erp_filter else None
    if erp_filter:
        print(f"  erp_config 在管父 ASIN×店铺：{len(erp_parents)} 组（将取交集）")

    session = MCPSession()
    # (asin, seller_sku, shop_id, principal_user_id) -> row
    rows: dict[tuple, dict] = {}
    no_shop = 0
    dropped_by_erp = 0
    t0 = time.time()

    incomplete: list[str] = []
    for i, name in enumerate(principals, 1):
        recs, total, complete = fetch_principal_listings(session, name, include_inactive)
        if not complete:
            incomplete.append(name)
        uid = users[name]
        kept = 0
        for rec in recs:
            parent = rec.get("parentAsin")
            acc = rec.get("shopAccount")
            if not parent or not acc:
                continue
            shop = acc_to_shop.get(acc)
            if not shop:
                no_shop += 1
                continue
            shop_id, site = shop
            if erp_parents is not None and (parent, int(shop_id)) not in erp_parents:
                dropped_by_erp += 1
                continue
            # 键含 principal_user_id：同一产品可挂多个负责人/助理（多行，重复派单是预期）
            key = (parent, rec.get("parentSellerSku") or "", int(shop_id), uid)
            if key in rows:
                continue  # 同一负责人 × 同一父ASIN 的多个子体，折叠为一行
            rows[key] = {
                "asin": parent,
                "seller_sku": rec.get("parentSellerSku") or "",
                "shop_id": int(shop_id),
                "shop_account": acc,
                "site_code": site,
                "principal_user_id": uid,
                "editor_id": None,
                "creator_id": None,
                "source_update_time": None,
            }
            kept += 1
        if recs or total:
            flag = "" if complete else "  ⚠️不全"
            print(f"    [{i}/{len(principals)}] {name}: 拉回 {len(recs)} 条(总 {total}) → 入选 {kept}{flag}")

    if incomplete:
        print(f"\n  ⚠️ {len(incomplete)} 个负责人数据不全（重试到底仍失败）：{incomplete}")

    # 统计：产品数 vs 行数（行数含多负责人重复），及多人产品数
    products: dict[tuple, set] = {}
    for (pa, sku, sid, uid) in rows:
        products.setdefault((pa, sku, sid), set()).add(uid)
    multi = sum(1 for owners in products.values() if len(owners) > 1)
    print(f"\n  聚合结果：{len(rows)} 行（产品×负责人）· {len(products)} 个产品 · 其中 {multi} 个挂多人 · 耗时 {time.time()-t0:.0f}s")
    print(f"  跳过：无店铺映射 {no_shop} · 非在管(erp过滤) {dropped_by_erp}")

    # 与现有 asin_owner 对比
    with sqlite3.connect(store.DB_PATH) as db:
        cur_prod = {
            (r[0], r[1] or "", int(r[2]))
            for r in db.execute("SELECT asin, seller_sku, shop_id FROM asin_owner").fetchall()
            if r[2] is not None
        }
    new_prod = set(products.keys())
    print(f"\n  对比现有 asin_owner（按产品 asin×sku×shop）：现有 {len(cur_prod)} / 新算 {len(new_prod)}")
    print(f"     仅现有（新版缺）: {len(cur_prod - new_prod)}")
    print(f"     仅新版（现有缺）: {len(new_prod - cur_prod)}")
    print(f"     两者都有        : {len(cur_prod & new_prod)}")

    if apply and incomplete and not force_incomplete:
        print(f"\n  ⛔ 拒绝写库：{len(incomplete)} 个负责人数据不全，写库会清空他们的产品归属。")
        print(f"     请重跑；确实要在数据不全下写库，加 --force-incomplete。")
        apply = False

    if apply:
        with sqlite3.connect(store.DB_PATH) as db:
            _ensure_multi_owner_pk(db)
            db.execute("DELETE FROM asin_owner")
            for r in rows.values():
                db.execute("""
                    INSERT OR REPLACE INTO asin_owner(
                        asin, seller_sku, shop_id, shop_account, site_code,
                        principal_user_id, editor_id, creator_id,
                        source_update_time, fetched_at
                    ) VALUES(?,?,?,?,?, ?,?,?, ?, datetime('now','localtime'))
                """, (
                    r["asin"], r["seller_sku"], r["shop_id"], r["shop_account"], r["site_code"],
                    r["principal_user_id"], r["editor_id"], r["creator_id"], r["source_update_time"],
                ))
            db.commit()
        print(f"\n  ✅ 已写入 asin_owner：{len(rows)} 行 / {len(products)} 个产品")
    else:
        print("\n  （dry-run，未写库；确认无误后加 --apply）")

    return {"rows": len(rows), "products": len(products), "multi_owner": multi,
            "no_shop": no_shop, "dropped_by_erp": dropped_by_erp}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--principals", type=str, help="逗号分隔的负责人姓名，只跑这些人")
    ap.add_argument("--from-existing", action="store_true", help="只刷新当前已在 asin_owner 里的负责人")
    ap.add_argument("--role", type=str, default=OPERATOR_ROLE,
                    help=f"按角色筛负责人（默认 {OPERATOR_ROLE}=FBA销售运营）")
    ap.add_argument("--all-active", action="store_true", help="不按角色筛，遍历全部活跃用户（慢，会空跑仓库/采购等）")
    ap.add_argument("--include-inactive", action="store_true", help="包含 Inactive 列表（默认仅 Active）")
    ap.add_argument("--no-erp-filter", action="store_true", help="不与 erp_config 在管集取交集（默认取）")
    ap.add_argument("--apply", action="store_true", help="真正写库（DELETE+INSERT 覆盖 asin_owner）")
    ap.add_argument("--force-incomplete", action="store_true", help="即使有负责人数据不全也强制写库（不推荐）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库（默认行为，此开关仅为显式表达）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
    store.init_db()

    if not API_KEY:
        raise SystemExit("MCP_API_KEY 未设置（检查 .env）")

    # 决定负责人名单：显式 > from-existing > all-active > 按角色（默认）
    principals = None
    source = ""
    if args.principals:
        principals = [x.strip() for x in args.principals.split(",") if x.strip()]
        source = "指定名单"
    elif args.from_existing:
        principals = _load_existing_owner_names()
        source = "现有 asin_owner 负责人"
    elif not args.all_active:
        if _user_role_empty():
            print("  user_role 为空，先同步系统用户角色（sync_sys_user）…")
            from data.sync_asin_owner import sync_sys_user
            n = sync_sys_user()
            print(f"  已同步 {n} 个用户及其角色")
        principals = _load_operator_names(args.role)
        source = f"角色 {args.role}"
        if not principals:
            raise SystemExit(f"角色 {args.role} 未筛到任何活跃用户（检查角色码或先跑 sync_sys_user）")
    else:
        source = "全部活跃用户"

    print("=" * 70)
    print(f"正向同步 asin_owner（{TOOL}）· {'写库' if args.apply else 'DRY-RUN'}")
    print(f"负责人来源：{source}" + (f"（{len(principals)} 人）" if principals else ""))
    print("=" * 70)

    sync_forward(
        principals=principals,
        include_inactive=args.include_inactive,
        erp_filter=not args.no_erp_filter,
        apply=args.apply,
        force_incomplete=args.force_incomplete,
    )


if __name__ == "__main__":
    main()
