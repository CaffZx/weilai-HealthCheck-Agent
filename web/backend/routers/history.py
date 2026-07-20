"""处理记录 + 数据看板接口：/api/history, /api/history/{id}, /api/dashboard"""
from __future__ import annotations
import datetime as _dt
import sqlite3
from collections import Counter

from fastapi import APIRouter, HTTPException, Query

from data import local_store
from ..common import (
    fetch_events_for_user, fetch_history_records, resolve_target_user,
)

router = APIRouter(prefix="/api")


@router.get("/history")
def api_history(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    q: str | None = Query(None),
    time_range: str = Query("all", alias="range"),   # all|today|7d|30d
    owner: str | None = Query(None),
    status: str | None = Query(None),   # 完整|待补
    event: str | None = Query(None),    # new|single
):
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    records = fetch_history_records(target)

    today = _dt.date.today()

    def in_range(r):
        if time_range == "all":
            return True
        try:
            d = _dt.date.fromisoformat(r["date_key"])
        except Exception:
            return False
        delta = (today - d).days
        return {"today": delta == 0, "7d": delta <= 7, "30d": delta <= 30}.get(time_range, True)

    def match(r):
        if not in_range(r):
            return False
        if q and q.lower() not in f"{r['record_id']}{r['event_id']}{r['parent_asin']}{r['parent_sku'] or ''}{r['product_name'] or ''}{r['issue'] or ''}{r['actual_action'] or ''}".lower():
            return False
        if owner and r["owner"] != owner:
            return False
        if status and r["complete"] != status:
            return False
        if event == "new" and not r["new_event"]:
            return False
        if event == "single" and r["new_event"]:
            return False
        return True

    filtered = [r for r in records if match(r)]
    return {
        "target_user_id": target,
        "total": len(filtered),
        "records": filtered,
        "kpi": {
            "proof": sum(1 for r in records if r["complete"] == "完整"),
            "pending": sum(1 for r in records if r["complete"] == "待补"),
            "with_new_event": sum(1 for r in records if r["new_event"]),
            "products": len({(r["parent_asin"], r["shop_account"]) for r in records}),
        },
    }


@router.get("/history/{record_id}")
def api_history_detail(record_id: str, userId: int = Query(...), targetId: int | None = Query(None)):
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    records = fetch_history_records(target)
    rec = next((r for r in records if r["record_id"] == record_id), None)
    if not rec:
        raise HTTPException(404, "record not found")

    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        acts = c.execute("""
            SELECT id, action_type, result, actual_action, review_at, notes, effect, created_at, user_id
            FROM task_action WHERE event_uid = ? ORDER BY created_at ASC
        """, (rec["event_uid"],)).fetchall()
    rec["timeline"] = [dict(a) for a in acts]
    return rec


@router.get("/dashboard")
def api_dashboard(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    days: str = Query("7", alias="range"),     # 7|14|30
):
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    try:
        days = int(days)
    except Exception:
        days = 7

    today = _dt.date.today()
    since = today - _dt.timedelta(days=days - 1)

    records = fetch_history_records(target)
    all_events = fetch_events_for_user(target, only_open=False)
    in_period = [r for r in records if r["date_key"] >= since.isoformat()]

    # KPI
    completed = sum(1 for r in in_period if r["action_type"] == "完成")
    target_num = days * 80
    goal_rate = round(completed / target_num * 100, 1) if target_num else 0
    on_time_cnt = sum(1 for r in in_period if r["on_time"] and r["action_type"] == "完成")
    on_time_rate = round(on_time_cnt / completed * 100, 1) if completed else 0
    with_review = [r for r in in_period if r["effect"] in ("变好", "变差", "待观察")]
    recovered_cnt = sum(1 for r in with_review if r["effect"] == "变好")
    recovery_rate = round(recovered_cnt / len(with_review) * 100, 1) if with_review else 0
    p0_records = [r for r in in_period if r["priority"] == "P0" and r["action_type"] == "完成"]
    p0_ontime = sum(1 for r in p0_records if r["on_time"])
    p0_rate = round(p0_ontime / len(p0_records) * 100, 1) if p0_records else 0

    product_counts = Counter((e["parent_asin"], e["shop_account"]) for e in all_events)
    repeat_products = sum(1 for c in product_counts.values() if c >= 2)
    repeat_rate = round(repeat_products / (len(product_counts) or 1) * 100, 1)

    # 每日完成量
    day_map = {(since + _dt.timedelta(days=i)).isoformat(): 0 for i in range(days)}
    for r in in_period:
        if r["action_type"] == "完成" and r["date_key"] in day_map:
            day_map[r["date_key"]] += 1
    daily = [{"date": d, "count": c, "target": 80} for d, c in sorted(day_map.items())]

    # 异常大类分布
    cat_counter = Counter()
    cat_products: dict[str, set] = {}
    for e in all_events:
        cat = e.get("category") or "其他"
        cat_counter[cat] += 1
        cat_products.setdefault(cat, set()).add((e["parent_asin"], e["shop_account"]))
    anomaly = sorted(
        [{"name": k, "events": v, "products": len(cat_products[k])} for k, v in cat_counter.items()],
        key=lambda x: -x["events"],
    )

    # 恢复率趋势
    trend = []
    for i in range(days):
        cutoff = (since + _dt.timedelta(days=i)).isoformat()
        subset = [r for r in records if r["date_key"] <= cutoff and r["effect"] in ("变好", "变差", "待观察")]
        rec_num = sum(1 for r in subset if r["effect"] == "变好")
        trend.append({"date": cutoff, "rate": round(rec_num / len(subset) * 100, 1) if subset else 0})

    # 动作有效率
    action_stats: dict[str, dict] = {}
    for r in in_period:
        if r["action_type"] != "完成":
            continue
        s = action_stats.setdefault(r["action_class"], {"name": r["action_class"], "total": 0, "checked": 0, "good": 0})
        s["total"] += 1
        if r["effect"] in ("变好", "变差", "待观察"):
            s["checked"] += 1
            if r["effect"] == "变好":
                s["good"] += 1
    action_effects = sorted(
        [{"name": s["name"], "total": s["total"], "checked": s["checked"],
          "rate": round(s["good"] / s["checked"] * 100, 1) if s["checked"] else 0}
         for s in action_stats.values()],
        key=lambda x: -x["total"],
    )

    # 高频异常产品
    top_products = [key for key, count in product_counts.most_common(20) if count >= 2]
    name_map = local_store.query_product_names_by_shop()
    repeat_products_list = []
    for parent_asin, shop_account in top_products:
        product_events = [e for e in all_events if (e["parent_asin"], e["shop_account"]) == (parent_asin, shop_account)]
        latest = max(product_events, key=lambda e: e.get("last_seen") or "")
        actions_cnt = sum(1 for r in records if (r["parent_asin"], r["shop_account"]) == (parent_asin, shop_account) and r["action_type"] == "完成")
        recent = next((r for r in records if (r["parent_asin"], r["shop_account"]) == (parent_asin, shop_account)), None)
        repeat_products_list.append({
            "parent_asin": parent_asin,
            "shop_account": shop_account,
            "product_name": name_map.get((parent_asin, shop_account)),
            "count": product_counts[(parent_asin, shop_account)],
            "issue": latest.get("issue"),
            "actions": actions_cnt,
            "effect": (recent or {}).get("effect") or "-",
            "priority": latest.get("priority"),
            "score": latest.get("score"),
            "days": latest.get("days"),
        })

    return {
        "target_user_id": target,
        "range_days": days,
        "kpi": {
            "completed": completed, "target": target_num, "goal_rate": goal_rate,
            "on_time_rate": on_time_rate, "on_time_count": on_time_cnt,
            "recovery_rate": recovery_rate,
            "recovered": recovered_cnt,
            "not_recovered": sum(1 for r in with_review if r["effect"] == "变差"),
            "watching": sum(1 for r in with_review if r["effect"] == "待观察"),
            "p0_ontime_rate": p0_rate,
            "p0_overdue": max(0, len(p0_records) - p0_ontime),
            "repeat_rate": repeat_rate, "repeat_products": repeat_products,
        },
        "daily": daily,
        "anomaly": anomaly,
        "trend": trend,
        "action_effects": action_effects,
        "repeat_products": repeat_products_list[:10],
    }
