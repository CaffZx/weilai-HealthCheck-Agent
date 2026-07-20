"""运营工作台任务/异常/复盘接口：/api/tasks/today, /api/anomalies, POST /api/tasks/{uid}/action, /api/review/due"""
from __future__ import annotations
import datetime as _dt
import json
import sqlite3

from fastapi import APIRouter, Body, HTTPException, Query

from data import local_store
from ..common import (
    PRIORITY_ORDER, TIER_ORDER,
    fetch_events_for_user, group_events_by_product, product_status_for_events,
    product_matches_status,
    resolve_target_user,
)
from inspector.scheduler.event_pool import 流转状态_事务

router = APIRouter(prefix="/api")


@router.get("/tasks/today")
def api_tasks_today(
    userId: int = Query(..., description="登录人 user_id"),
    targetId: int | None = Query(None, description="主管查看下属时传下属 user_id"),
    status: str | None = Query(None, description="产品状态：未完成|处理中|待复查|已完成|已关闭"),
    includeCompleted: bool = Query(False, description="兼容旧调用；始终返回完整产品集合"),
):
    """今日任务池：该用户名下所有开放事件，按 P0→P2 / score↓ / T0→T3 / days↓ 排序。"""
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    all_events = fetch_events_for_user(target, only_open=False)
    # 只要异常仍未关闭，就必须进入运营任务池；不能因为最新巡检暂未再次命中
    # 就把未完成产品从今日列表和状态筛选中隐藏。
    all_products = group_events_by_product(all_events)
    products = [product for product in all_products
                if product_matches_status(product["product_status"], status)]
    p0 = sum(1 for product in all_products if product["priority"] == "P0")
    p1 = sum(1 for product in all_products if product["priority"] == "P1")
    p2 = sum(1 for product in all_products if product["priority"] == "P2")

    with sqlite3.connect(local_store.DB_PATH) as c:
        today = _dt.date.today().isoformat()
        solved_today = c.execute("""
            SELECT DISTINCT e.父ASIN, e.店铺账号
            FROM event_state_log l
            JOIN event_pool e ON e.id=l.event_id
            WHERE l.变更后状态 IN ('已处理待复扫', '已关闭', '误报', '忽略')
              AND date(l.变更时间)=?
              AND EXISTS (
                SELECT 1 FROM asin_owner o
                WHERE o.asin=e.父ASIN AND o.shop_account=e.店铺账号
                  AND COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1)) = ?
              )
        """, (today, target)).fetchall()
    solved_today_keys = {(row[0], row[1]) for row in solved_today}
    done_today_product_count = sum(
        1 for product in all_products
        if (product["parent_asin"], product["shop_account"]) in solved_today_keys
        and product["product_status"] == "已完成"
    )
    return {
        "target_user_id": target,
        "total": len(all_products),
        "total_events": len(all_events),
        "total_products": len(all_products),
        "p0": p0, "p1": p1, "p2": p2,
        "done_today": done_today_product_count,
        "done_today_products": done_today_product_count,
        "historical_event_count": 0,
        "historical_product_count": 0,
        "products": products,
        "events": [event for product in products for event in product["events"]],
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
    products = group_events_by_product(events)
    if priority:
        products = [product for product in products if product["priority"] == priority]
    if category:
        products = [product for product in products if any(event["category"] == category for event in product["events"])]
    if status:
        products = [product for product in products
                    if product_matches_status(product["product_status"], status)]
    if q:
        ql = q.lower()
        products = [product for product in products if ql in "".join(
            f"{event.get('parent_asin') or ''}{event.get('product_name') or ''}{event.get('issue') or ''}"
            for event in product["events"]
        ).lower()]
    return {
        "target_user_id": target,
        "total": len(products),
        "total_events": sum(len(product["events"]) for product in products),
        "events": [event for product in products for event in product["events"]],
        "products": products,
    }


@router.post("/tasks/{event_uid}/action")
def api_task_action(event_uid: str, payload: dict = Body(...)):
    """原子写入处理记录并推进 event_pool 正式状态机。
    payload: {userId, action_type, result?, actual_action?, review_at?, notes?, before_metrics?, after_metrics?, effect?}
    """
    action_type = payload.get("action_type")
    transitions = {
        "标记处理中": "处理中",
        "完成": "已处理待复扫",
        "不处理": "忽略",
    }
    if action_type not in transitions:
        raise HTTPException(400, "不支持的 action_type")
    target = resolve_target_user(payload.get("userId"), payload.get("targetId"))
    if target is None:
        raise HTTPException(400, "userId required")
    review_at = payload.get("review_at")
    if action_type == "完成" and review_at:
        try:
            review_date = _dt.date.fromisoformat(review_at)
        except (TypeError, ValueError):
            raise HTTPException(400, "review_at 必须是 YYYY-MM-DD 格式")
        if review_date < _dt.date.today():
            raise HTTPException(400, "复查日期不能早于今天")
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        event = c.execute("""
            SELECT e.id, e.当前状态, e.父ASIN, e.店铺账号
            FROM event_pool e
            WHERE e.唯一识别=? AND EXISTS (
              SELECT 1 FROM asin_owner o
              WHERE o.asin=e.父ASIN AND o.shop_account=e.店铺账号
                AND COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1))=?
            )
        """, (event_uid, target)).fetchone()
        if not event:
            raise HTTPException(404, "事件不存在或无操作权限")
        next_status = transitions[action_type]
        if event["当前状态"] == next_status:
            raise HTTPException(409, f"该异常已经处于“{next_status}”状态，请勿重复提交")
        operator = str(payload.get("userId") or "运营")
        reason = payload.get("notes") or payload.get("actual_action") or action_type
        ok, message = 流转状态_事务(
            c, event["id"], next_status, reason, operator,
            "运营操作", "运营不处理" if action_type == "不处理" else None,
            {"action_type": action_type, "user_id": payload.get("userId")},
        )
        if not ok:
            raise HTTPException(409, message)
        c.execute("""
            INSERT INTO task_action
              (event_uid, user_id, action_type, result, actual_action, review_at, notes, before_metrics, after_metrics, effect)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            event_uid, payload.get("userId"), action_type,
            payload.get("result"), payload.get("actual_action"),
            review_at, payload.get("notes"),
            json.dumps(payload["before_metrics"], ensure_ascii=False) if payload.get("before_metrics") else None,
            json.dumps(payload["after_metrics"], ensure_ascii=False) if payload.get("after_metrics") else None,
            payload.get("effect"),
        ))
        c.execute("""
            UPDATE event_pool
            SET 下一次复查时间=?, 复查时间来源=?, 上次处理动作=?, 上次处理时间=datetime('now','localtime'),
                上次处理人=?, 更新时间=datetime('now','localtime')
            WHERE id=?
        """, (
            review_at if action_type == "完成" else None,
            "manual" if action_type == "完成" and review_at else None,
            action_type, operator, event["id"],
        ))
        c.commit()
    refreshed_events = fetch_events_for_user(target, only_open=False)
    product_key = (event["父ASIN"], event["店铺账号"])
    product = next(
        (
            product for product in group_events_by_product(refreshed_events)
            if (product["parent_asin"], product["shop_account"]) == product_key
        ),
        None,
    )
    return {
        "ok": True,
        "event_uid": event_uid,
        "lifecycle_status": next_status,
        "event_status": next(
            (event_item["status"] for event_item in refreshed_events if event_item["event_uid"] == event_uid),
            "未完成",
        ),
        "product_status": product["product_status"] if product else "未完成",
    }


@router.get("/review/due")
def api_review_due(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    period: str = Query("today", description="today|3d|7d|30d|all"),
):
    """调整复盘：正式状态为待复查且存在复查日期的事件。"""
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    today = _dt.date.today()
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT a.*, e.id AS event_id, e.当前状态 AS lifecycle_status,
                   e.下一次复查时间 AS scheduled_review_at,
                   e.父ASIN, e.父SKU, e.店铺账号, e.问题点位, e.严重度,
                   ir.created_at AS inspection_time, ir.batch_no AS inspection_batch
            FROM event_pool e
            JOIN task_action a ON a.id=(
              SELECT id FROM task_action
              WHERE event_uid=e.唯一识别 AND action_type='完成'
              ORDER BY id DESC LIMIT 1
            )
            LEFT JOIN inspection_result ir ON ir.id = e.inspection_result_id
            WHERE e.当前状态 IN ('已处理待复扫', '待观察')
              AND e.下一次复查时间 IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM asin_owner o
                WHERE o.asin=e.父ASIN AND o.shop_account=e.店铺账号
                  AND COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1)) = ?
              )
            ORDER BY e.下一次复查时间 ASC
        """, (target,)).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        review_at = d.get("scheduled_review_at")
        d["review_at"] = review_at
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
        if period == "30d" and (delta is None or not (0 <= delta <= 30)):
            continue
        out.append(d)
    return {"target_user_id": target, "period": period, "total": len(out), "records": out}


@router.post("/review/{event_uid}")
def api_review_post(event_uid: str, payload: dict = Body(...)):
    """复盘结果原子写入：复查确认恢复才会关闭事件。"""
    effect = payload.get("effect")
    conclusion = payload.get("conclusion")
    if effect not in ("变好", "变差", "待观察"):
        raise HTTPException(400, "effect 必填且必须是 变好/变差/待观察")

    target = resolve_target_user(payload.get("userId"), payload.get("targetId"))
    if target is None:
        raise HTTPException(400, "userId required")
    if conclusion and conclusion not in ("保留动作", "继续观察", "二次调整"):
        raise HTTPException(400, "不支持的复盘结论")
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        event = c.execute("""
            SELECT e.id, e.当前状态 FROM event_pool e
            WHERE e.唯一识别=? AND EXISTS (
              SELECT 1 FROM asin_owner o
              WHERE o.asin=e.父ASIN AND o.shop_account=e.店铺账号
                AND COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1))=?
            )
        """, (event_uid, target)).fetchone()
        if not event:
            raise HTTPException(404, "事件不存在或无操作权限")
        row = c.execute(
            "SELECT id FROM task_action WHERE event_uid=? AND action_type='完成' ORDER BY id DESC LIMIT 1",
            (event_uid,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "该 event 尚无处理记录")
        c.execute("UPDATE task_action SET effect=? WHERE id=?", (effect, row[0]))

        transition = {
            "保留动作": "已关闭" if effect == "变好" else None,
            "继续观察": "待观察",
            "二次调整": "新发现",
        }.get(conclusion)
        if conclusion == "保留动作" and effect != "变好":
            raise HTTPException(409, "只有复查确认变好，才能结束该异常")
        if transition:
            ok, message = 流转状态_事务(
                c, event["id"], transition,
                payload.get("notes") or f"复盘结论：{conclusion}（{effect}）",
                str(payload.get("userId") or "运营"), "复盘结论",
                "复查确认恢复" if transition == "已关闭" else None,
                {"effect": effect, "conclusion": conclusion},
            )
            if not ok:
                raise HTTPException(409, message)
        if conclusion:
            c.execute("""
                INSERT INTO task_action (event_uid, user_id, action_type, notes, effect)
                VALUES (?, ?, '复盘', ?, ?)
            """, (event_uid, payload.get("userId"),
                  f"复盘结论：{conclusion}（{payload.get('notes') or effect}）", effect))
            next_review = None
            if conclusion == "继续观察":
                next_review = (_dt.date.today() + _dt.timedelta(days=3)).isoformat()
            c.execute("""
                UPDATE event_pool
                SET 下一次复查时间=?, 复查时间来源=?, 更新时间=datetime('now','localtime')
                WHERE id=?
            """, (next_review, "manual" if next_review else None, event["id"]))
        c.commit()
    return {"ok": True, "event_uid": event_uid, "effect": effect, "conclusion": conclusion}
