"""跨路由共享的工具函数、常量、DB 查询。

只放"多个 router 都要用"的东西。单一 router 内部用的辅助函数就留在那个 router 文件里。
"""
from __future__ import annotations
import datetime as _dt
import json as _json
import logging
import sqlite3
from pathlib import Path

import yaml

from data import local_store

log = logging.getLogger(__name__)

# ---- 路径 ----
ROOT = Path(__file__).resolve().parent.parent.parent
FRONT = Path(__file__).resolve().parent.parent / "frontend"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
MANAGER_MAP_PATH = ROOT / "config" / "manager_map.yaml"

# ---- 常量 ----
SEV_TO_PRIORITY = {"S0": "P0", "S1": "P1", "S2": "P2"}
POSITION_TO_TIER = {"P0_PRODUCT": "T0", "P1_PRODUCT": "T1", "P2_PRODUCT": "T2", "P3_PRODUCT": "T3"}
TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "": 4}
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# 各优先级处理时限（天）
SLA_DAYS = {"P0": 1, "P1": 3, "P2": 7}

# 实际动作 → 动作分类
ACTION_CATEGORIES = [
    ("补货/库存处理", ["补货", "库存", "断货", "补齐", "转仓"]),
    ("广告收缩", ["广告", "预算", "暂停", "acos", "关键词竞价", "词"]),
    ("价格与优惠调整", ["价格", "coupon", "优惠", "促销", "折扣", "定价"]),
    ("文案/关键词优化", ["文案", "标题", "search terms", "属性", "关键词", "listing"]),
    ("图片调整", ["主图", "图片", "视频", "a+", "副图"]),
]


# ---- Settings ----
def load_settings() -> dict:
    """每次读，方便改配置不重启。"""
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_manager_map() -> dict[int, list[int]]:
    """{主管 user_id: [下属 user_id]}。每次读，改配置不重启。"""
    if not MANAGER_MAP_PATH.exists():
        return {}
    try:
        with open(MANAGER_MAP_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return {int(k): [int(x) for x in (v or [])] for k, v in raw.items()}
    except Exception as e:
        log.warning("manager_map.yaml 解析失败: %s", e)
        return {}


# ---- 通用工具 ----
def days_since(iso_ts: str | None) -> int:
    if not iso_ts:
        return 0
    try:
        d = _dt.datetime.fromisoformat(iso_ts).date()
        return max(0, (_dt.date.today() - d).days)
    except Exception:
        return 0


def resolve_target_user(viewer_id: int | None, target_id: int | None) -> int | None:
    """主管可以查看下属；普通运营只能查自己；target_id=None 表示查自己。"""
    if viewer_id is None:
        return None
    if target_id is None or target_id == viewer_id:
        return viewer_id
    reports = load_manager_map().get(viewer_id, [])
    if target_id in reports:
        return target_id
    return viewer_id  # 权限不足回退查自己


def classify_action(text: str) -> str:
    """从实际动作文本推断动作类型。"""
    if not text:
        return "其他"
    t = text.lower()
    for name, keys in ACTION_CATEGORIES:
        if any(k.lower() in t for k in keys):
            return name
    return "其他"


def format_record_id(row_id: int, created_at: str) -> str:
    """ACT-YYYYMMDD-NNNN，同日内按 id 后 4 位。"""
    try:
        dt_part = created_at[:10].replace("-", "")
    except Exception:
        dt_part = "00000000"
    return f"ACT-{dt_part}-{row_id:04d}"


def format_event_id(uid: str, first_seen: str | None) -> str:
    """EVT-YYYYMMDD-<uid前4位>，让运营看到日期。"""
    dt_part = (first_seen or "")[:10].replace("-", "") or "00000000"
    return f"EVT-{dt_part}-{(uid or '')[:4]}"


# ---- DB 查询：事件池 ----
def _ai_ready_keys() -> set[str]:
    """返回 batch_cache 中已有 LLM 分析结果的 fixture_key 集合（用于事件级 has_ai_result 标记）。"""
    from . import batch_cache
    return {k for k, v in batch_cache.get_cache().items() if v.get("llm_judgment")}


def fetch_events_for_user(user_id: int, only_open: bool = True) -> list[dict]:
    """拉出该用户名下所有事件，附带产品定位/持续天数/最新 action 状态。"""
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT e.*
            FROM event_pool e
            WHERE e.父ASIN IN (
                SELECT DISTINCT asin FROM asin_owner
                WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
            )
        """, (user_id,)).fetchall()
        pos_rows = c.execute("SELECT parent_asin, product_position FROM erp_config").fetchall()
        pos_map = {r["parent_asin"]: r["product_position"] for r in pos_rows}

        # parent_asin → shop_id 映射（用于拼 fixture_key = asin__shopId，AI 深度分析用）
        shop_rows = c.execute(
            "SELECT DISTINCT asin, shop_id FROM asin_owner WHERE shop_id IS NOT NULL"
        ).fetchall()
        shop_id_map = {r["asin"]: r["shop_id"] for r in shop_rows}

    name_map = local_store.query_product_names()
    img_map = local_store.query_image_urls()
    ai_ready = _ai_ready_keys()
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        act_rows = c.execute("""
            SELECT event_uid, action_type, result, actual_action, review_at, notes, effect, created_at
            FROM task_action
            WHERE id IN (SELECT MAX(id) FROM task_action GROUP BY event_uid)
        """).fetchall()
        act_map = {r["event_uid"]: dict(r) for r in act_rows}

    out = []
    for r in rows:
        d = dict(r)
        uid = d["唯一识别"]
        latest_action = act_map.get(uid)
        status = d.get("当前状态") or "新发现"
        if latest_action:
            t = latest_action.get("action_type")
            status = {"完成": "已完成", "不处理": "已关闭", "标记处理中": "处理中", "待复查": "待复查"}.get(t, status)
        if only_open and status in ("已关闭", "已完成"):
            continue

        sev = d.get("严重度") or "S2"
        position = pos_map.get(d.get("父ASIN"), "") or ""
        tier = POSITION_TO_TIER.get(position, "")
        priority = SEV_TO_PRIORITY.get(sev, "P2")
        days = days_since(d.get("首次命中时间"))
        try:
            judge_basis = _json.loads(d.get("判定依据") or "{}")
        except Exception:
            judge_basis = {"命中依据": d.get("判定依据") or ""}

        _shop_id = shop_id_map.get(d.get("父ASIN"))
        _fixture_key = f"{d.get('父ASIN')}__{_shop_id}" if _shop_id else None
        out.append({
            "event_uid": uid,
            "parent_asin": d.get("父ASIN"),
            "product_name": name_map.get(d.get("父ASIN")),
            "image_url": img_map.get(d.get("父ASIN")),
            "parent_sku": d.get("父SKU"),
            "shop_id": _shop_id,
            "fixture_key": _fixture_key,
            "has_ai_result": bool(_fixture_key and _fixture_key in ai_ready),
            "shop_account": d.get("店铺账号"),
            "site": d.get("站点"),
            "category": d.get("异常大类") or "其他",
            "issue": d.get("问题点位"),
            "scope": d.get("作用层级"),
            "variant": d.get("命中变体"),
            "variant_importance": d.get("变体重要性"),
            "severity": sev,
            "priority": priority,
            "score": d.get("单异常执行分数") or 0,
            "position": position,
            "tier": tier,
            "days": days,
            "status": status,
            "first_seen": d.get("首次命中时间"),
            "last_seen": d.get("最近命中时间"),
            "last_action": d.get("上次处理动作"),
            "last_action_at": d.get("上次处理时间"),
            "last_action_by": d.get("上次处理人"),
            "judge_basis": judge_basis,
            "latest_action": latest_action,
        })
    return out


def fetch_history_records(target_user_id: int) -> list[dict]:
    """拉出该 target 名下所有处理记录（不含"备注"类）。"""
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT a.*, e.父ASIN as parent_asin, e.父SKU as parent_sku, e.店铺账号 as shop_account,
                   e.异常大类 as category, e.问题点位 as issue, e.严重度 as severity,
                   e.单异常执行分数 as score, e.首次命中时间 as first_seen
            FROM task_action a
            JOIN event_pool e ON e.唯一识别 = a.event_uid
            WHERE a.action_type IN ('完成', '不处理')
              AND e.父ASIN IN (
                SELECT DISTINCT asin FROM asin_owner
                WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
              )
            ORDER BY a.created_at DESC
        """, (target_user_id,)).fetchall()

        uids = {r["user_id"] for r in rows if r["user_id"]}
        users = {}
        if uids:
            qs = ",".join("?" * len(uids))
            for u in c.execute(f"SELECT id, user_name FROM sys_user WHERE id IN ({qs})", list(uids)).fetchall():
                users[u["id"]] = u["user_name"]

        events_after = c.execute("""
            SELECT 唯一识别 as uid, 父ASIN as parent_asin, 异常大类 as category, 问题点位 as issue,
                   首次命中时间 as first_seen, 严重度 as severity
            FROM event_pool
            WHERE 父ASIN IN (
                SELECT DISTINCT asin FROM asin_owner
                WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
            )
        """, (target_user_id,)).fetchall()

    idx = {}
    for e in events_after:
        idx.setdefault((e["parent_asin"], e["category"] or ""), []).append(dict(e))

    name_map = local_store.query_product_names()

    out = []
    for r in rows:
        d = dict(r)
        priority = SEV_TO_PRIORITY.get(d.get("severity") or "S2", "P2")
        try:
            fs = _dt.datetime.fromisoformat(d["first_seen"]).date() if d["first_seen"] else None
            ca = _dt.datetime.fromisoformat(d["created_at"]).date() if d["created_at"] else None
            handle_days = (ca - fs).days if fs and ca else None
        except Exception:
            handle_days = None
        on_time = (handle_days is not None and handle_days <= SLA_DAYS.get(priority, 7))
        complete = "完整" if (d.get("result") and d.get("actual_action")) else "待补"

        new_event = None
        for e in idx.get((d["parent_asin"], d["category"] or ""), []):
            if e["uid"] == d["event_uid"]:
                continue
            if e["first_seen"] and d["created_at"] and e["first_seen"] > d["created_at"]:
                new_event = e
                break

        if d.get("effect") == "变好":
            review = "已恢复"
        elif d.get("effect") == "变差":
            review = "未恢复"
        else:
            review = "待复查" if d.get("review_at") else "无复查"

        out.append({
            "record_id": format_record_id(d["id"], d["created_at"]),
            "row_id": d["id"],
            "event_id": format_event_id(d["event_uid"], d["first_seen"]),
            "event_uid": d["event_uid"],
            "time": d["created_at"],
            "date_key": d["created_at"][:10] if d["created_at"] else "",
            "owner": users.get(d["user_id"], f"用户{d['user_id']}") if d["user_id"] else "-",
            "owner_id": d["user_id"],
            "parent_asin": d["parent_asin"],
            "parent_sku": d["parent_sku"],
            "product_name": name_map.get(d["parent_asin"]),
            "shop_account": d["shop_account"],
            "category": d["category"] or "其他",
            "issue": d["issue"],
            "severity": d["severity"],
            "priority": priority,
            "score": d["score"] or 0,
            "action_type": d["action_type"],
            "result": d["result"],
            "actual_action": d["actual_action"],
            "action_class": classify_action(d["actual_action"]),
            "review_at": d["review_at"],
            "effect": d["effect"],
            "review": review,
            "notes": d["notes"],
            "complete": complete,
            "handle_days": handle_days,
            "on_time": on_time,
            "new_event": {
                "event_id": format_event_id(new_event["uid"], new_event["first_seen"]),
                "event_uid": new_event["uid"],
                "issue": new_event["issue"],
                "first_seen": new_event["first_seen"],
                "severity": new_event["severity"],
            } if new_event else None,
        })
    return out


def config_by_key(key: str) -> dict | None:
    """从 fixture 里查一个产品的配置（附带 shop_account）。"""
    from data import fixture_loader
    for c in fixture_loader.load_configs():
        if c["fixture_key"] == key:
            smap = fixture_loader.load_shop_map().get(c["shop_id"], {})
            c["shop_account"] = smap.get("account")
            return c
    return None
