"""运营工作台任务/异常/复盘接口：/api/tasks/today, /api/anomalies, POST /api/tasks/{uid}/action, /api/review/due"""
from __future__ import annotations
import datetime as _dt
import json
import sqlite3

from fastapi import APIRouter, Body, HTTPException, Query

from data import local_store
from ..common import (
    PRIORITY_ORDER, TIER_ORDER,
    fetch_events_for_user, resolve_target_user,
)

router = APIRouter(prefix="/api")


@router.get("/tasks/today")
def api_tasks_today(
    userId: int = Query(..., description="登录人 user_id"),
    targetId: int | None = Query(None, description="主管查看下属时传下属 user_id"),
):
    """今日任务池：该用户名下所有开放事件，按 P0→P2 / score↓ / T0→T3 / days↓ 排序。"""
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    events = fetch_events_for_user(target, only_open=True)
    events.sort(key=lambda e: (
        PRIORITY_ORDER.get(e["priority"], 3),
        -float(e["score"] or 0),
        TIER_ORDER.get(e["tier"], 4),
        -e["days"],
    ))
    products = {}
    for event in events:
        key = (event.get("parent_asin"), event.get("shop_account"))
        current = products.get(key)
        if current is None or (
            PRIORITY_ORDER.get(event["priority"], 3), -float(event["score"] or 0)
        ) < (
            PRIORITY_ORDER.get(current["priority"], 3), -float(current["score"] or 0)
        ):
            products[key] = event
    p0 = sum(1 for e in products.values() if e["priority"] == "P0")
    p1 = sum(1 for e in products.values() if e["priority"] == "P1")
    p2 = sum(1 for e in products.values() if e["priority"] == "P2")

    all_events = fetch_events_for_user(target, only_open=False)
    completed_today_keys = set()
    with sqlite3.connect(local_store.DB_PATH) as c:
        today = _dt.date.today().isoformat()
        # 归属该 target 的事件，今天有人（自己或主管代操作）标记完成 = done
        done_today = c.execute("""
            SELECT COUNT(DISTINCT a.event_uid) FROM task_action a
            JOIN event_pool e ON e.唯一识别 = a.event_uid
            WHERE a.action_type='完成' AND date(a.created_at)=?
              AND EXISTS (
                SELECT 1 FROM asin_owner o
                WHERE o.asin=e.父ASIN AND o.shop_account=e.店铺账号
                  AND COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1)) = ?
              )
        """, (today, target)).fetchone()[0]
        done_today_products = c.execute("""
            SELECT DISTINCT e.父ASIN, e.店铺账号 FROM task_action a
            JOIN event_pool e ON e.唯一识别 = a.event_uid
            WHERE a.action_type='完成' AND date(a.created_at)=?
              AND EXISTS (
                SELECT 1 FROM asin_owner o
                WHERE o.asin=e.父ASIN AND o.shop_account=e.店铺账号
                  AND COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1)) = ?
              )
        """, (today, target)).fetchall()
        completed_today_keys = {(row[0], row[1]) for row in done_today_products}
    all_product_events = {}
    for event in all_events:
        all_product_events.setdefault((event.get("parent_asin"), event.get("shop_account")), []).append(event)
    done_today_product_count = sum(
        1 for key in completed_today_keys
        if all_product_events.get(key) and all(event["status"] == "已完成" for event in all_product_events[key])
    )
    return {
        "target_user_id": target,
        "total": len(products),
        "total_events": len(events),
        "total_products": len(products),
        "p0": p0, "p1": p1, "p2": p2,
        "done_today": done_today,
        "done_today_products": done_today_product_count,
        "events": events,
    }


@router.get("/anomalies")
def api_anomalies(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    priority: str | None = Query(None),
    category: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None),
):
    """全部异常池（含已关闭）。支持筛选。"""
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    events = fetch_events_for_user(target, only_open=False)
    if priority:
        events = [e for e in events if e["priority"] == priority]
    if category:
        events = [e for e in events if e["category"] == category]
    if status:
        events = [e for e in events if e["status"] == status]
    if q:
        ql = q.lower()
        events = [e for e in events if ql in (e.get("parent_asin") or "").lower() or ql in (e.get("issue") or "").lower()]
    events.sort(key=lambda e: (
        PRIORITY_ORDER.get(e["priority"], 3),
        -float(e["score"] or 0),
    ))
    return {"target_user_id": target, "total": len(events), "events": events}


@router.post("/tasks/{event_uid}/action")
def api_task_action(event_uid: str, payload: dict = Body(...)):
    """写入一条 task_action。前端每次「完成/不处理/待复查/备注」都调这里。
    payload: {userId, action_type, result?, actual_action?, review_at?, notes?, before_metrics?, after_metrics?, effect?}
    """
    action_type = payload.get("action_type")
    if not action_type:
        raise HTTPException(400, "action_type required")
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.execute("""
            INSERT INTO task_action
              (event_uid, user_id, action_type, result, actual_action, review_at, notes, before_metrics, after_metrics, effect)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            event_uid, payload.get("userId"), action_type,
            payload.get("result"), payload.get("actual_action"),
            payload.get("review_at"), payload.get("notes"),
            json.dumps(payload["before_metrics"], ensure_ascii=False) if payload.get("before_metrics") else None,
            json.dumps(payload["after_metrics"], ensure_ascii=False) if payload.get("after_metrics") else None,
            payload.get("effect"),
        ))
        c.commit()
    return {"ok": True, "event_uid": event_uid}


@router.get("/review/due")
def api_review_due(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    period: str = Query("today", description="today|3d|7d|all"),
):
    """调整复盘：已执行过动作且到达复查时间的事件。"""
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    today = _dt.date.today()
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT a.*, e.父ASIN, e.父SKU, e.店铺账号, e.问题点位, e.严重度
            FROM task_action a
            JOIN event_pool e ON e.唯一识别 = a.event_uid
            WHERE a.review_at IS NOT NULL
              AND e.父ASIN IN (
                SELECT DISTINCT asin FROM asin_owner
                WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
              )
            ORDER BY a.review_at ASC
        """, (target,)).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        review_at = d.get("review_at")
        try:
            rd = _dt.datetime.fromisoformat(review_at).date() if review_at else None
        except Exception:
            rd = None
        delta = (rd - today).days if rd else None
        if period == "today" and delta != 0:
            continue
        if period == "3d" and (delta is None or not (0 <= delta <= 3)):
            continue
        if period == "7d" and (delta is None or not (0 <= delta <= 7)):
            continue
        out.append(d)
    return {"target_user_id": target, "period": period, "total": len(out), "records": out}


@router.post("/review/{event_uid}")
def api_review_post(event_uid: str, payload: dict = Body(...)):
    """复盘结果写入：{userId, effect: 变好|变差|待观察, conclusion: 保留动作|继续观察|二次调整, notes?}
    - effect: 更新最新那条 task_action 的 effect 字段（这条 event 的复查结论）
    - conclusion=='二次调整' 时额外插入一条 action_type='待复查'，把 event 重新拉回今日任务池
    """
    effect = payload.get("effect")
    conclusion = payload.get("conclusion")
    if effect not in ("变好", "变差", "待观察"):
        raise HTTPException(400, "effect 必填且必须是 变好/变差/待观察")

    with sqlite3.connect(local_store.DB_PATH) as c:
        # 更新最新那条 action 的 effect
        row = c.execute(
            "SELECT id FROM task_action WHERE event_uid=? ORDER BY id DESC LIMIT 1",
            (event_uid,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "该 event 尚无处理记录")
        c.execute("UPDATE task_action SET effect=? WHERE id=?", (effect, row[0]))

        # 二次调整 → 新增一条待复查（把 event 重新拉回开放态）
        if conclusion == "二次调整":
            c.execute("""
                INSERT INTO task_action (event_uid, user_id, action_type, notes)
                VALUES (?, ?, '待复查', ?)
            """, (event_uid, payload.get("userId"),
                  f"复盘结论：二次调整（{payload.get('notes') or '效果不佳，重新处理'}）"))
        c.commit()
    return {"ok": True, "event_uid": event_uid, "effect": effect, "conclusion": conclusion}
