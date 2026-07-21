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
TIER_LABELS = {
    "T0": "战略级产品",
    "T1": "重点产品",
    "T2": "常规产品",
    "T3": "长尾产品",
}
TIER_PRODUCT_CODES = {"T0": "P0", "T1": "P1", "T2": "P2", "T3": "P3"}
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


def tier_display(tier: str | None) -> str:
    """产品定位展示名；定位 P 与异常严重度/执行优先级 P 分开表达。"""
    if not tier:
        return "待补充"
    label = TIER_LABELS.get(tier)
    code = TIER_PRODUCT_CODES.get(tier)
    return f"{tier} {label} ({code})" if label and code else tier


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
    """拉出该用户名下所有事件，事件状态只以 event_pool 正式状态机为准。"""
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT e.*
            FROM event_pool e
            WHERE EXISTS (
                SELECT 1 FROM product_access pa
                WHERE pa.parent_asin=e.父ASIN AND pa.shop_account=e.店铺账号
                  AND pa.user_id = ?
            )
        """, (user_id,)).fetchall()
        pos_rows = c.execute("""
            SELECT parent_asin, parent_seller_sku, shop_id, product_position
            FROM erp_config
        """).fetchall()
        pos_map = {(r["parent_asin"], str(r["shop_id"])): r["product_position"] for r in pos_rows if r["product_position"]}
        sku_map = {(r["parent_asin"], str(r["shop_id"])): r["parent_seller_sku"]
                   for r in pos_rows if r["parent_seller_sku"]}

        # parent_asin → shop_id 映射（用于拼 fixture_key = asin__shopId，AI 深度分析用）
        shop_rows = c.execute(
            "SELECT DISTINCT asin, shop_account, shop_id FROM asin_owner WHERE shop_id IS NOT NULL"
        ).fetchall()
        shop_id_map = {(r["asin"], r["shop_account"]): r["shop_id"] for r in shop_rows}

    name_map = local_store.query_product_names_by_shop()
    img_map = local_store.query_image_urls_by_shop()
    inspection_map = local_store.query_latest_inspection_results()
    ai_ready = _ai_ready_keys()
    out = []
    for r in rows:
        d = dict(r)
        uid = d["唯一识别"]
        lifecycle_status = d.get("当前状态") or "新发现"
        status = event_display_status(lifecycle_status)
        if only_open and status in ("已完成", "已关闭"):
            continue

        sev = d.get("严重度") or "S2"
        _shop_id = shop_id_map.get((d.get("父ASIN"), d.get("店铺账号")))
        position = pos_map.get((d.get("父ASIN"), str(_shop_id)), "") or ""
        parent_sku = d.get("父SKU") or sku_map.get((d.get("父ASIN"), str(_shop_id)))
        tier = POSITION_TO_TIER.get(position, "")
        days = days_since(d.get("首次命中时间"))
        try:
            judge_basis = _json.loads(d.get("判定依据") or "{}")
        except Exception:
            judge_basis = {"命中依据": d.get("判定依据") or ""}

        _fixture_key = f"{d.get('父ASIN')}__{_shop_id}" if _shop_id else None
        inspection = inspection_map.get((d.get("父ASIN"), d.get("店铺账号"))) or {}
        inspection_card = inspection.get("result_json") or {}
        inspection_priority = inspection_card.get("优先级信息") or {}
        # S 是单条异常严重度，P 是父 ASIN + 店铺的产品执行优先级，二者不能互相替代。
        priority = inspection_priority.get("执行优先级") or "P2"
        product_score = inspection_priority.get("产品执行分数")
        detail_map = {}
        for detail in inspection_card.get("异常明细") or []:
            key = (detail.get("问题点位"), detail.get("该条严重度"))
            detail_map.setdefault(key, detail)
        inspection_detail = detail_map.get((d.get("问题点位"), sev)) or {}
        if inspection_card.get("数据状态"):
            data_status = inspection_card["数据状态"]
        elif inspection:
            data_status = {
                "状态": "UNKNOWN",
                "状态文案": "本次巡检未记录数据状态",
                "状态说明": "这条异常已有巡检结果，但该次巡检发生在数据状态记录功能上线前，暂时无法确认数据是否完整。",
                "数据缺口": [{
                    "数据项": "数据状态记录",
                    "影响": "无法回溯确认本次巡检使用的数据是否完整",
                    "建议": "重新运行该产品巡检，生成新的数据状态",
                    "级别": "重要",
                }],
                "数据截止时间": None,
            }
        else:
            data_status = {
                "状态": "UNKNOWN",
                "状态文案": "数据状态暂未记录",
                "状态说明": "该事件没有关联到最近一次巡检结果，暂时无法确认数据是否完整。",
                "数据缺口": [{
                    "数据项": "巡检结果关联",
                    "影响": "无法确认本次事件使用的数据是否完整",
                    "建议": "重新运行巡检后再查看详情",
                    "级别": "重要",
                }],
            }
        latest_card_issues = {
            (detail.get("问题点位"), detail.get("该条严重度"))
            for detail in inspection_card.get("异常明细") or []
        }
        appears_in_latest_inspection = (
            (d.get("问题点位"), sev) in latest_card_issues
            if inspection else None
        )
        out.append({
            "event_uid": uid,
            "parent_asin": d.get("父ASIN"),
            "product_name": name_map.get((d.get("父ASIN"), d.get("店铺账号"))),
            "image_url": img_map.get((d.get("父ASIN"), d.get("店铺账号"))),
            "parent_sku": parent_sku,
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
            "product_priority": priority,
            "score": d.get("单异常执行分数"),
            "product_score": product_score,
            "position": position,
            "tier": tier,
            "tier_label": TIER_LABELS.get(tier, "待补充"),
            "tier_display": tier_display(tier),
            "days": days,
            "status": status,
            "internal_status": lifecycle_status,
            "lifecycle_status": lifecycle_status,
            "first_seen": d.get("首次命中时间"),
            "last_seen": d.get("最近命中时间"),
            "last_action": d.get("上次处理动作"),
            "last_action_at": d.get("上次处理时间"),
            "last_action_by": d.get("上次处理人"),
            "inspection_time": inspection.get("created_at"),
            "inspection_batch": inspection.get("batch_no"),
            "appears_in_latest_inspection": appears_in_latest_inspection,
            "judge_basis": judge_basis,
            "recommendation": inspection_detail.get("处理建议"),
            "steps": inspection_detail.get("执行步骤") or [],
            "summary_reason": inspection_detail.get("判断依据") or inspection_detail.get("具体表现"),
            "数据状态": data_status,
        })
    return out


def unassigned_event_summary() -> dict:
    """返回无法归属到负责人工作台的事件概览，供数据健康页提示。"""
    with sqlite3.connect(local_store.DB_PATH) as c:
        row = c.execute("""
            SELECT COUNT(*) AS event_count,
                   COUNT(DISTINCT e.父ASIN || char(31) || e.店铺账号) AS product_count
            FROM event_pool e
            WHERE NOT EXISTS (
                SELECT 1 FROM asin_owner o
                WHERE o.asin=e.父ASIN AND o.shop_account=e.店铺账号
            )
        """).fetchone()
    return {"event_count": row[0], "product_count": row[1]}


def event_display_status(lifecycle_status: str | None) -> str:
    """把事件生命周期映射为运营任务状态；复查状态单独由复盘页管理。"""
    if lifecycle_status == "处理中":
        return "处理中"
    if lifecycle_status in ("待观察", "长期跟进"):
        return "待复查"
    if lifecycle_status == "已处理待复扫":
        return "已完成"
    if lifecycle_status in ("误报", "忽略", "人工中断"):
        return "已关闭"
    if lifecycle_status == "已关闭":
        return "已完成"
    return "未完成"


def product_status_for_events(events: list[dict]) -> str:
    """父 ASIN + 店铺的唯一状态口径：全部异常处理完即产品完成。"""
    statuses = [event.get("status") for event in events]
    if not statuses:
        return "未完成"
    handled = {"已完成", "已关闭"}
    if any(status == "处理中" for status in statuses):
        return "处理中"
    if all(status in ("已完成", "已关闭") for status in statuses):
        return "已完成"
    if any(status in handled for status in statuses):
        return "处理中"
    if any(status == "待复查" for status in statuses):
        return "待复查"
    return "未完成"


def product_matches_status(product_status: str | None, requested_status: str | None) -> bool:
    """产品状态筛选口径：各状态互斥；未完成不包含处理中。"""
    return not requested_status or product_status == requested_status


def group_events_by_product(events: list[dict]) -> list[dict]:
    """集中构造产品 DTO，所有页面共享同一 ASIN + 店铺聚合口径。"""
    groups: dict[tuple[str | None, str | None], list[dict]] = {}
    for event in events:
        groups.setdefault((event.get("parent_asin"), event.get("shop_account")), []).append(event)

    products = []
    for (parent_asin, shop_account), product_events in groups.items():
        product_events.sort(key=lambda event: (
            PRIORITY_ORDER.get(event.get("priority"), 3),
            -float(event.get("score") or 0),
            TIER_ORDER.get(event.get("tier"), 4),
            -int(event.get("days") or 0),
        ))
        main = product_events[0]
        product_status = product_status_for_events(product_events)
        products.append({
            "key": f"{parent_asin or ''}__{shop_account or ''}",
            "parent_asin": parent_asin,
            "shop_account": shop_account,
            "product_name": main.get("product_name"),
            "image_url": next((event.get("image_url") for event in product_events if event.get("image_url")), ""),
            "parent_sku": main.get("parent_sku"),
            "site": main.get("site"),
            "priority": main.get("priority") or "P2",
            "product_score": next((event.get("product_score") for event in product_events if event.get("product_score") is not None), None),
            "tier": main.get("tier"),
            "tier_label": main.get("tier_label", TIER_LABELS.get(main.get("tier"), "待补充")),
            "tier_display": main.get("tier_display", tier_display(main.get("tier"))),
            "product_status": product_status,
            "event_count": len(product_events),
            "status_counts": {status: sum(1 for event in product_events if event.get("status") == status)
                              for status in ("未完成", "处理中", "待复查", "已完成", "已关闭")},
            "handled_event_count": sum(1 for event in product_events if event.get("status") in ("已完成", "已关闭")),
            "open_event_count": sum(1 for event in product_events if event.get("status") not in ("已完成", "已关闭")),
            "max_days": max(int(event.get("days") or 0) for event in product_events),
            "issue_names": list(dict.fromkeys(event.get("issue") or event.get("category") or "其他" for event in product_events)),
            "events": product_events,
        })
    products.sort(key=lambda product: (
        PRIORITY_ORDER.get(product["priority"], 3),
        -float(product.get("product_score") or max((event.get("score") or 0 for event in product["events"]), default=0)),
        TIER_ORDER.get(product.get("tier"), 4),
        -product["max_days"],
    ))
    return products


def attach_assignments(products: list[dict], viewer_id: int | None) -> list[dict]:
    """给产品 DTO 附加指派信息（被指派人名字 / 是否派给我 / 是否我派出）。
    viewer_id 是当前登录人（判断 to_me / by_me 的视角）。"""
    if not products:
        return products
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT ta.parent_asin, ta.shop_account, ta.assignee_id, ta.assigner_id, ta.note, ta.created_at,
                   ua.user_name AS assignee_name, ug.user_name AS assigner_name
            FROM task_assignment ta
            LEFT JOIN sys_user ua ON ua.id = ta.assignee_id
            LEFT JOIN sys_user ug ON ug.id = ta.assigner_id
        """).fetchall()
    amap: dict[tuple, list[dict]] = {}
    for r in rows:
        amap.setdefault((r["parent_asin"], r["shop_account"]), []).append({
            "assignee_id": r["assignee_id"],
            "assignee_name": r["assignee_name"] or f"用户{r['assignee_id']}",
            "assigner_id": r["assigner_id"],
            "assigner_name": r["assigner_name"] or f"用户{r['assigner_id']}",
            "note": r["note"],
            "created_at": r["created_at"],
        })
    for p in products:
        assigns = amap.get((p.get("parent_asin"), p.get("shop_account")), [])
        p["assignments"] = assigns
        p["is_assigned"] = bool(assigns)
        p["assignee_names"] = [a["assignee_name"] for a in assigns]
        p["assigned_to_me"] = any(a["assignee_id"] == viewer_id for a in assigns)
        p["assigned_by_me"] = any(a["assigner_id"] == viewer_id for a in assigns)
    return products


def fetch_history_records(target_user_id: int) -> list[dict]:
    """拉出该 target 名下所有处理记录（不含"备注"类）。"""
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT a.*, e.父ASIN as parent_asin, e.父SKU as parent_sku, e.店铺账号 as shop_account,
                   e.异常大类 as category, e.问题点位 as issue, e.严重度 as severity,
                   e.单异常执行分数 as score, e.首次命中时间 as first_seen,
                   ir.created_at as inspection_time, ir.batch_no as inspection_batch
            FROM task_action a
            JOIN event_pool e ON e.唯一识别 = a.event_uid
            LEFT JOIN inspection_result ir ON ir.id = e.inspection_result_id
            WHERE a.action_type IN ('完成', '不处理')
              AND EXISTS (
                SELECT 1 FROM product_access pa
                WHERE pa.parent_asin=e.父ASIN AND pa.shop_account=e.店铺账号
                  AND pa.user_id = ?
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
            SELECT 唯一识别 as uid, 父ASIN as parent_asin, 店铺账号 as shop_account,
                   异常大类 as category, 问题点位 as issue,
                   首次命中时间 as first_seen, 严重度 as severity
            FROM event_pool
            WHERE EXISTS (
                SELECT 1 FROM product_access pa
                WHERE pa.parent_asin=event_pool.父ASIN AND pa.shop_account=event_pool.店铺账号
                  AND pa.user_id = ?
            )
        """, (target_user_id,)).fetchall()

    idx = {}
    for e in events_after:
        idx.setdefault((e["parent_asin"], e["shop_account"], e["category"] or ""), []).append(dict(e))

    name_map = local_store.query_product_names_by_shop()

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
        for e in idx.get((d["parent_asin"], d["shop_account"], d["category"] or ""), []):
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
            "inspection_time": d.get("inspection_time"),
            "inspection_batch": d.get("inspection_batch"),
            "date_key": d["created_at"][:10] if d["created_at"] else "",
            "owner": users.get(d["user_id"], f"用户{d['user_id']}") if d["user_id"] else "-",
            "owner_id": d["user_id"],
            "parent_asin": d["parent_asin"],
            "parent_sku": d["parent_sku"],
            "product_name": name_map.get((d["parent_asin"], d["shop_account"])),
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
