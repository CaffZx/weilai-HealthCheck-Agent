"""运营工作台任务/异常/复盘接口。"""
from __future__ import annotations
import datetime as _dt
import json
import sqlite3

from fastapi import APIRouter, Body, HTTPException, Query

from data import local_store
from data.review_schedule import ScheduleValidationError, resolve_schedule
from ..common import (
    PRIORITY_ORDER, TIER_ORDER,
    fetch_events_for_user, group_events_by_product, product_status_for_events,
    product_matches_status, attach_assignments,
    resolve_target_user,
)
from inspector.scheduler.event_pool import 流转状态_事务

router = APIRouter(prefix="/api")


def _attach_active_maintenance(products: list[dict]) -> None:
    """附加异常级观察摘要；产品仍按父 ASIN + 店铺聚合。"""
    if not products:
        return
    keys = {(product["parent_asin"], product["shop_account"]) for product in products}
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT id, parent_asin, shop_account, event_uid, observation_at, next_inspection_at, status
            FROM observation_case
            WHERE status IN ('观察中', '待运营确认')
            ORDER BY observation_at ASC, id ASC
        """).fetchall()
    active: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (row["parent_asin"], row["shop_account"])
        if key in keys:
            active.setdefault(key, []).append(dict(row))
    for product in products:
        raw_cases = active.get((product["parent_asin"], product["shop_account"]), [])
        # 数据库有唯一约束；此处仍按 event_uid 去重，避免迁移/异常数据短暂影响运营展示。
        cases_by_event = {case["event_uid"]: case for case in raw_cases}
        cases = list(cases_by_event.values())
        first_case = cases[0] if cases else None
        product["has_active_maintenance"] = bool(cases)
        product["active_observation_count"] = len(cases)
        product["observing_event_uids"] = [case["event_uid"] for case in cases]
        product["maintenance_id"] = first_case["id"] if first_case else None  # 兼容旧前端字段
        product["observation_at"] = first_case["observation_at"] if first_case else None
        product["next_inspection_at"] = first_case["next_inspection_at"] if first_case else None


def _event_requires_action(event: dict, observing_event_uids: set[str]) -> bool:
    """判断单异常是否仍需运营动作，观察期内的已处理异常除外。"""
    closed_statuses = {"已关闭", "误报", "忽略", "人工中断"}
    lifecycle_status = (
        event.get("internal_status")
        or event.get("lifecycle_status")
        or event.get("status")
    )
    if lifecycle_status in closed_statuses:
        return False
    return not (
        lifecycle_status == "已处理待复扫"
        and event.get("event_uid") in observing_event_uids
    )


def _product_requires_action(product: dict) -> bool:
    """判断产品是否仍应计入当前任务池。

    产品仍按父 ASIN + 店铺聚合，但观察是事件级的：只有正处于有效
    观察期的“已处理待复扫”事件才能从当前任务中排除。历史遗留的同状态
    事件没有 observation_case，仍需运营处理，不能被误当成已完成。
    """
    observing_event_uids = set(product.get("observing_event_uids") or [])
    return any(
        _event_requires_action(event, observing_event_uids)
        for event in product.get("events") or []
    )


def _maintenance_schedule(event: sqlite3.Row | dict, payload: dict) -> dict:
    """后端唯一的单异常复查计划计算入口。"""
    try:
        return resolve_schedule(event, payload)
    except ScheduleValidationError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/tasks/{event_uid}/schedule-preview")
def api_schedule_preview(
    event_uid: str,
    userId: int = Query(...),
    targetId: int | None = Query(None),
    expected_available_at: str | None = Query(None),
):
    """返回单异常按规则计算的默认计划；前端只展示，不自行决定默认日期。"""
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        event = c.execute("""
            SELECT e.id, e.唯一识别, e.父ASIN, e.店铺账号, e.问题点位, e.严重度,
                   e.命中变体, e.变体重要性, e.当前状态
            FROM event_pool e
            WHERE e.唯一识别=? AND EXISTS (
              SELECT 1 FROM product_access pa
              WHERE pa.parent_asin=e.父ASIN AND pa.shop_account=e.店铺账号 AND pa.user_id=?
            )
        """, (event_uid, target)).fetchone()
    if not event:
        raise HTTPException(404, "事件不存在或无操作权限")
    schedule = _maintenance_schedule(event, {"expected_available_at": expected_available_at})
    return {"ok": True, **schedule}


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
    _attach_active_maintenance(all_products)
    products = [product for product in all_products
                if product_matches_status(product["product_status"], status)]
    current_task_products = [
        product for product in all_products if _product_requires_action(product)
    ]
    p0 = sum(1 for product in current_task_products if product["priority"] == "P0")
    p1 = sum(1 for product in current_task_products if product["priority"] == "P1")
    p2 = sum(1 for product in current_task_products if product["priority"] == "P2")

    with local_store.connect() as c:
        today = _dt.date.today().isoformat()
        solved_today = c.execute("""
            SELECT DISTINCT e.父ASIN, e.店铺账号
            FROM event_state_log l
            JOIN event_pool e ON e.id=l.event_id
            WHERE l.变更后状态 IN ('已处理待复扫', '已关闭', '误报', '忽略')
              AND date(l.变更时间)=?
              AND EXISTS (
                SELECT 1 FROM product_access pa
                WHERE pa.parent_asin=e.父ASIN AND pa.shop_account=e.店铺账号
                  AND pa.user_id = ?
              )
        """, (today, target)).fetchall()
    solved_today_keys = {(row[0], row[1]) for row in solved_today}
    done_today_product_count = sum(
        1 for product in all_products
        if (product["parent_asin"], product["shop_account"]) in solved_today_keys
        and product["product_status"] == "已完成"
    )
    attach_assignments(products, userId)
    return {
        "target_user_id": target,
        # total/total_products 保持“当前任务”口径；products 则始终返回完整
        # 产品集合，供状态筛选、已完成记录和复盘入口继续使用。
        "total": len(current_task_products),
        "total_events": len(all_events),
        "total_products": len(current_task_products),
        "p0": p0, "p1": p1, "p2": p2,
        "current_task_count": len(current_task_products),
        "current_task_p0": p0,
        "current_task_p1": p1,
        "current_task_p2": p2,
        "current_task_event_count": sum(
            sum(
                1
                for event in product["events"]
                if _event_requires_action(
                    event, set(product.get("observing_event_uids") or [])
                )
            )
            for product in current_task_products
        ),
        "current_task_product_keys": [product["key"] for product in current_task_products],
        "all_product_count": len(all_products),
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
    _attach_active_maintenance(products)
    attach_assignments(products, userId)
    return {
        "target_user_id": target,
        "total": len(products),
        "total_events": sum(len(product["events"]) for product in products),
        "events": [event for product in products for event in product["events"]],
        "products": products,
    }


@router.post("/tasks/assign")
def api_tasks_assign(payload: dict = Body(...)):
    """批量指派产品给另一运营（共享，可撤回）。
    payload: {userId(指派人), assignee_id(被指派人), products:[{parent_asin,shop_account}], note?}
    只能指派自己名下（asin_owner）的产品。"""
    assigner = payload.get("userId")
    assignee = payload.get("assignee_id")
    products = payload.get("products") or []
    note = payload.get("note")
    if not assigner:
        raise HTTPException(400, "userId required")
    if not assignee:
        raise HTTPException(400, "assignee_id required")
    if assignee == assigner:
        raise HTTPException(400, "不能指派给自己")
    if not products:
        raise HTTPException(400, "products 不能为空")

    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        who = c.execute("SELECT user_name FROM sys_user WHERE id=? AND user_state=1", (assignee,)).fetchone()
        if not who:
            raise HTTPException(404, "被指派人不存在或已停用")
        assigned, skipped = [], []
        for p in products:
            pa, shop = p.get("parent_asin"), p.get("shop_account")
            if not pa or not shop:
                continue
            owned = c.execute("""
                SELECT 1 FROM asin_owner
                WHERE asin=? AND shop_account=?
                  AND COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1))=?
            """, (pa, shop, assigner)).fetchone()
            if not owned:
                skipped.append({"parent_asin": pa, "shop_account": shop, "reason": "非本人名下产品"})
                continue
            c.execute("""
                INSERT OR REPLACE INTO task_assignment
                  (parent_asin, shop_account, assignee_id, assigner_id, note, created_at)
                VALUES (?,?,?,?,?, datetime('now','localtime'))
            """, (pa, shop, assignee, assigner, note))
            assigned.append({"parent_asin": pa, "shop_account": shop})
        c.commit()
    return {"ok": True, "assignee_id": assignee, "assignee_name": who["user_name"],
            "assigned_count": len(assigned), "assigned": assigned, "skipped": skipped}


@router.post("/tasks/unassign")
def api_tasks_unassign(payload: dict = Body(...)):
    """批量撤回指派。指派人本人或产品负责人均可撤回。
    payload: {userId, products:[{parent_asin,shop_account}], assignee_id?}"""
    user = payload.get("userId")
    products = payload.get("products") or []
    assignee = payload.get("assignee_id")
    if not user:
        raise HTTPException(400, "userId required")
    if not products:
        raise HTTPException(400, "products 不能为空")
    removed = 0
    removed_products = []
    with local_store.connect() as c:
        for p in products:
            pa, shop = p.get("parent_asin"), p.get("shop_account")
            if not pa or not shop:
                continue
            params = [pa, shop, user, pa, shop, user]
            extra = ""
            if assignee:
                extra = " AND assignee_id=?"
                params.append(assignee)
            cur = c.execute(f"""
                DELETE FROM task_assignment
                WHERE parent_asin=? AND shop_account=?
                  AND (assigner_id=? OR EXISTS (
                    SELECT 1 FROM asin_owner o
                    WHERE o.asin=? AND o.shop_account=?
                      AND COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1))=?
                  )){extra}
            """, params)
            removed += cur.rowcount
            if cur.rowcount:
                removed_products.append({"parent_asin": pa, "shop_account": shop})
        c.commit()
    return {"ok": True, "removed_count": removed, "removed": removed_products}


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
        "重新打开": "新发现",
    }
    if action_type not in transitions:
        raise HTTPException(400, "不支持的 action_type")
    target = resolve_target_user(payload.get("userId"), payload.get("targetId"))
    if target is None:
        raise HTTPException(400, "userId required")
    # 单异常完成立即形成独立观察，绝不等待同产品其他异常。
    review_at = None
    next_inspection_at = None
    observation_id = None
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        event = c.execute("""
            SELECT e.id, e.唯一识别, e.当前状态, e.父ASIN, e.店铺账号, e.问题点位, e.严重度,
                   e.命中变体, e.变体重要性, e.inspection_result_id
            FROM event_pool e
            WHERE e.唯一识别=? AND EXISTS (
              SELECT 1 FROM product_access pa
              WHERE pa.parent_asin=e.父ASIN AND pa.shop_account=e.店铺账号
                AND pa.user_id=?
            )
        """, (event_uid, target)).fetchone()
        if not event:
            raise HTTPException(404, "事件不存在或无操作权限")
        next_status = transitions[action_type]
        schedule = None
        if action_type == "完成":
            schedule = _maintenance_schedule(event, payload)
            review_at = schedule["observation_at"]
            next_inspection_at = schedule["next_inspection_at"]
            active_case = c.execute("""
                SELECT id FROM observation_case
                WHERE event_uid=? AND status IN ('观察中', '待运营确认')
                LIMIT 1
            """, (event_uid,)).fetchone()
            if active_case:
                raise HTTPException(409, "该异常正在效果观察，请等待 Agent 结论或重新打开异常")
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
        action_cursor = c.execute("""
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
            schedule["source"] if schedule else None,
            action_type, operator, event["id"],
        ))
        if action_type == "重新打开":
            c.execute("""
                UPDATE observation_case
                SET status='已被重新打开异常替代', updated_at=datetime('now','localtime')
                WHERE event_uid=? AND status IN ('观察中', '待运营确认')
            """, (event_uid,))
        if action_type == "完成":
            try:
                observation_id = local_store.create_observation_case(
                    c, event=event, completion_action_id=action_cursor.lastrowid,
                    user_id=payload.get("userId"), result=payload.get("result"),
                    actual_action=payload.get("actual_action"), notes=payload.get("notes"),
                    observation_at=review_at, next_inspection_at=next_inspection_at, schedule=schedule,
                )
            except local_store.ActiveObservationExistsError as error:
                raise HTTPException(409, "该异常正在效果观察，请等待 Agent 结论或重新打开异常") from error
        c.commit()
    refreshed_events = fetch_events_for_user(target, only_open=False)
    product_key = (event["父ASIN"], event["店铺账号"])
    refreshed_products = group_events_by_product(refreshed_events)
    _attach_active_maintenance(refreshed_products)
    product = next(
        (
            product for product in refreshed_products
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
        "maintenance_created": observation_id is not None,
        "maintenance_id": observation_id,
        "observation_id": observation_id,
        "observation_at": review_at,
        "next_inspection_at": next_inspection_at,
        "schedule": schedule,
        "has_active_maintenance": bool(product and product["has_active_maintenance"]),
        "active_observation_count": product["active_observation_count"] if product else 0,
        "observing_event_uids": product["observing_event_uids"] if product else [],
    }


@router.post("/products/action")
def api_product_action(payload: dict = Body(...)):
    """按父 ASIN + 店铺批量完成当前仍需处理的异常，并为每条异常保留独立凭证。"""
    if payload.get("action_type") != "完成产品维护":
        raise HTTPException(400, "不支持的产品操作")
    parent_asin = payload.get("parent_asin")
    shop_account = payload.get("shop_account")
    if not parent_asin or not shop_account:
        raise HTTPException(400, "parent_asin 和 shop_account 必填")
    target = resolve_target_user(payload.get("userId"), payload.get("targetId"))
    if target is None:
        raise HTTPException(400, "userId required")

    operator = str(payload.get("userId") or "运营")
    reason = payload.get("notes") or payload.get("actual_action") or "完成该产品维护"
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        owned = c.execute("""
            SELECT 1 FROM product_access
            WHERE parent_asin=? AND shop_account=? AND user_id=?
        """, (parent_asin, shop_account, target)).fetchone()
        if not owned:
            raise HTTPException(404, "产品不存在或无操作权限")
        events = c.execute("""
            SELECT e.id, e.唯一识别, e.当前状态, e.父ASIN, e.店铺账号,
                   e.问题点位, e.严重度, e.命中变体, e.变体重要性, e.inspection_result_id,
                   (
                     SELECT oc.id FROM observation_case oc
                     WHERE oc.event_uid=e.唯一识别
                       AND oc.status IN ('观察中', '待运营确认')
                     LIMIT 1
                   ) AS active_observation_id
            FROM event_pool e
            WHERE e.父ASIN=? AND e.店铺账号=?
              AND e.当前状态 NOT IN ('已关闭', '误报', '忽略')
            ORDER BY e.id
        """, (parent_asin, shop_account)).fetchall()
        if not events:
            raise HTTPException(409, "该产品没有待处理异常")
        completed = []
        observation_ids = []
        schedules = []
        for event in events:
            # 已在效果观察的异常只能等待 Agent 结论，不能创建第二个观察周期。
            if event["active_observation_id"] or event["当前状态"] == "已处理待复扫":
                continue
            schedule = _maintenance_schedule(event, payload)
            review_at = schedule["observation_at"]
            next_inspection_at = schedule["next_inspection_at"]
            ok, message = 流转状态_事务(
                c, event["id"], "已处理待复扫", reason, operator,
                "运营产品维护", None,
                {"action_type": "完成产品维护", "user_id": payload.get("userId")},
            )
            if not ok:
                raise HTTPException(409, f"异常 {event['唯一识别']} 无法完成：{message}")
            action_cursor = c.execute("""
                INSERT INTO task_action
                  (event_uid, user_id, action_type, result, actual_action, review_at, notes, before_metrics, after_metrics, effect)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                event["唯一识别"], payload.get("userId"), "完成",
                payload.get("result"), payload.get("actual_action"), review_at, payload.get("notes"),
                json.dumps(payload["before_metrics"], ensure_ascii=False) if payload.get("before_metrics") else None,
                json.dumps(payload["after_metrics"], ensure_ascii=False) if payload.get("after_metrics") else None,
                payload.get("effect"),
            ))
            try:
                observation_ids.append(local_store.create_observation_case(
                    c, event=event, completion_action_id=action_cursor.lastrowid,
                    user_id=payload.get("userId"), result=payload.get("result"),
                    actual_action=payload.get("actual_action"), notes=payload.get("notes"),
                    observation_at=review_at, next_inspection_at=next_inspection_at, schedule=schedule,
                ))
            except local_store.ActiveObservationExistsError as error:
                raise HTTPException(409, f"异常 {event['唯一识别']} 已在效果观察") from error
            c.execute("""
                UPDATE event_pool
                SET 下一次复查时间=?, 复查时间来源=?, 上次处理动作='完成产品维护',
                    上次处理时间=datetime('now','localtime'), 上次处理人=?, 更新时间=datetime('now','localtime')
                WHERE id=?
            """, (review_at, schedule["source"], operator, event["id"]))
            completed.append(event["唯一识别"])
            schedules.append({"event_uid": event["唯一识别"], **schedule})
        active_cases = c.execute("""
            SELECT id, observation_at, next_inspection_at
            FROM observation_case
            WHERE parent_asin=? AND shop_account=?
              AND status IN ('观察中', '待运营确认')
            ORDER BY observation_at ASC, id ASC
        """, (parent_asin, shop_account)).fetchall()
        c.commit()

    refreshed_events = fetch_events_for_user(target, only_open=False)
    refreshed_products = group_events_by_product(refreshed_events)
    _attach_active_maintenance(refreshed_products)
    product = next(
        (item for item in refreshed_products
         if item["parent_asin"] == parent_asin and item["shop_account"] == shop_account),
        None,
    )
    return {
        "ok": True,
        "parent_asin": parent_asin,
        "shop_account": shop_account,
        "completed_event_uids": completed,
        "completed_count": len(completed),
        "maintenance_created": bool(observation_ids),
        "maintenance_id": active_cases[0]["id"] if active_cases else None,
        "observation_ids": observation_ids,
        "active_observation_ids": [case["id"] for case in active_cases],
        "active_observation_count": len(active_cases),
        "has_active_maintenance": bool(active_cases),
        "observing_event_uids": product["observing_event_uids"] if product else [],
        "already_observing": bool(active_cases) and not observation_ids,
        "observation_at": active_cases[0]["observation_at"] if active_cases else None,
        "next_inspection_at": active_cases[0]["next_inspection_at"] if active_cases else None,
        "schedules": schedules,
        "product_status": product["product_status"] if product else "未完成",
        "events": product["events"] if product else [],
    }


@router.get("/review/due")
def api_review_due(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    period: str = Query("today", description="today|3d|7d|30d|all"),
):
    """旧版逐异常复盘读取接口已停用，统一使用产品级效果观察。"""
    raise HTTPException(410, "旧版复盘已停用，请使用 /api/review/observations")

    """调整复盘：正式状态为待复查且存在复查日期的事件。"""
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    today = _dt.date.today()
    with local_store.connect() as c:
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
                SELECT 1 FROM product_access pa
                WHERE pa.parent_asin=e.父ASIN AND pa.shop_account=e.店铺账号
                  AND pa.user_id = ?
              )
            ORDER BY e.下一次复查时间 ASC
        """, (target,)).fetchall()

    groups: dict[tuple[str | None, str | None, str | None], dict] = {}
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
        key = (d.get("父ASIN"), d.get("店铺账号"), review_at)
        group = groups.setdefault(key, {
            **d,
            "event_uids": [],
            "issue_names": [],
            "review_scope": "product",
        })
        group["event_uids"].append(d["event_uid"])
        issue = d.get("问题点位") or "异常"
        if issue not in group["issue_names"]:
            group["issue_names"].append(issue)
        if d.get("effect"):
            group["effect"] = d["effect"]
    out = []
    for group in groups.values():
        group["parent_asin"] = group.get("父ASIN")
        group["shop_account"] = group.get("店铺账号")
        group["event_count"] = len(group["event_uids"])
        group["issue_summary"] = "、".join(group["issue_names"])
        out.append(group)
    out.sort(key=lambda item: item.get("review_at") or "")
    return {"target_user_id": target, "period": period, "total": len(out), "records": out}


def _parse_snapshot(snapshot: str | None) -> dict[str, float]:
    if not snapshot:
        return {}
    try:
        items = json.loads(snapshot)
    except (TypeError, json.JSONDecodeError):
        return {}
    out = {}
    for item in items if isinstance(items, list) else []:
        label, value = item.get("label"), str(item.get("value") or "")
        numbers = __import__("re").findall(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if label and numbers:
            out[label] = float(numbers[0])
    return out


def _metric_direction(issue: str, label: str) -> int:
    """返回指标向改善方向：1 为越高越好，-1 为越低越好，0 为不自动判断。"""
    issue_text = str(issue or "").lower()
    label_text = str(label or "").lower()

    # 先按指标本身判断，避免“目标偏离”把订单、销量等改善指标误认为越低越好。
    lower_is_better = (
        "偏离", "差距", "缺口", "库存天数", "可售天数", "覆盖月", "积压",
        "acos", "花费", "退款", "退货", "差评", "负评", "缺货", "断货",
    )
    higher_is_better = (
        "评分", "星级", "销量", "订单", "单量", "转化", "达成", "完成率",
        "自然流量", "曝光", "点击", "排名", "buy box", "购物车", "在售率",
    )
    if any(word in label_text for word in lower_is_better):
        return -1
    if any(word in label_text for word in higher_is_better):
        return 1

    # 只有标签没有足够语义时，才以异常类型做受限兜底。
    if "评分" in issue_text and any(word in label_text for word in ("rating", "review score")):
        return 1
    if "库存积压" in issue_text and any(word in label_text for word in ("库存", "inventory")):
        return -1
    return 0


def _compare_observation_metrics(issue: str, comparable: list[tuple[str, float, float]]) -> tuple[str, str, float]:
    """只比较有明确业务方向的同名指标；不同方向或未知指标不强行汇总。"""
    judgments = []
    for label, old, new in comparable:
        direction = _metric_direction(issue, label)
        if not direction:
            continue
        delta = new - old
        change = "无明显变化" if delta == 0 else ("变好" if delta * direction > 0 else "变差")
        judgments.append((label, old, new, change))
    if not judgments:
        return "数据不足", "缺少可自动解释方向的同名核心指标", 0.0
    changes = {item[3] for item in judgments if item[3] != "无明显变化"}
    if len(changes) > 1:
        return "数据不足", "核心指标变化方向不一致，Agent 不做单一效果结论", 0.0
    label, old, new, effect = judgments[0]
    return effect, f"{label} {old:g} → {new:g}，变化 {new - old:+g}", min(0.9, 0.6 + 0.1 * len(judgments))


def _observe_case(conn: sqlite3.Connection, case: sqlite3.Row) -> dict:
    """对一条已完成异常做基线与观察期的可解释比对。"""
    latest_result = conn.execute("""
        SELECT id, result_json, created_at
        FROM inspection_result
        WHERE parent_asin=? AND shop_account=? AND created_at >= ? AND created_at >= ?
          AND result_status='SUCCESS'
        ORDER BY id DESC LIMIT 1
    """, (case["parent_asin"], case["shop_account"], case["observation_at"], case["next_inspection_at"])).fetchone()
    latest_card = {}
    if latest_result:
        try:
            latest_card = json.loads(latest_result["result_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            latest_card = {}
    baseline = _parse_snapshot(case["baseline_snapshot"])
    latest_issue = None
    if latest_card:
        latest_issue = next((item for item in latest_card.get("异常明细") or []
                             if item.get("问题点位") == case["issue"]
                             and (item.get("命中变体") or "") == (case["variant"] or "")), None)
    current = _parse_snapshot(json.dumps(
        (latest_issue or {}).get("数据快照") or [], ensure_ascii=False
    ))
    comparable = [(label, old, current.get(label)) for label, old in baseline.items()
                  if current.get(label) is not None]
    if latest_result and latest_card and latest_issue is None:
        effect, reason, confidence = "变好", "下次巡检未再次命中该异常", 0.9
    elif not comparable:
        effect, reason, confidence = "数据不足", (
            "等待下次巡检数据" if not latest_result else "缺少执行前或观察期的可比指标"
        ), 0.0
    else:
        effect, reason, confidence = _compare_observation_metrics(case["issue"] or "异常", comparable)
    detail = {
        "event_uid": case["event_uid"], "issue": case["issue"] or "异常",
        "severity": case["severity"] or "S2", "effect": effect, "reason": reason,
        "baseline": baseline, "current": current, "confidence": confidence,
    }
    report = {
        "effect": effect, "summary": reason, "details": [detail],
        "latest_inspection_at": latest_result["created_at"] if latest_result else None,
        "confidence": confidence,
    }
    conn.execute("""
        UPDATE observation_case
        SET status='待运营确认', agent_effect=?, agent_summary=?,
            agent_observed_at=datetime('now','localtime'), agent_payload=?,
            updated_at=datetime('now','localtime')
        WHERE id=?
    """, (effect, reason, json.dumps(report, ensure_ascii=False), case["id"]))
    return report


@router.get("/review/observations")
def api_review_observations(
    userId: int = Query(...), targetId: int | None = Query(None),
    q: str | None = Query(None), shop: str | None = Query(None),
    phase: str | None = Query(None), effect: str | None = Query(None),
    severity: str | None = Query(None), review_from: str | None = Query(None),
    review_to: str | None = Query(None),
):
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    today = _dt.date.today().isoformat()
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT oc.*
            FROM observation_case oc
            WHERE EXISTS (SELECT 1 FROM product_access pa WHERE pa.parent_asin=oc.parent_asin AND pa.shop_account=oc.shop_account AND pa.user_id=?)
              AND oc.status IN ('观察中', '待运营确认')
            ORDER BY CASE oc.status WHEN '待运营确认' THEN 0 ELSE 1 END,
                     oc.observation_at ASC, oc.id ASC
        """, (target,)).fetchall()
        name_map = local_store.query_product_names_by_shop()
        image_map = local_store.query_image_urls_by_shop()
        out = []
        for row in rows:
            item = dict(row)
            item["observation_id"] = row["id"]
            item["maintenance_id"] = row["id"]  # 兼容旧前端客户端
            days_until = (_dt.date.fromisoformat(row["observation_at"]) - _dt.date.today()).days
            inspection_days_until = (_dt.date.fromisoformat(row["next_inspection_at"]) - _dt.date.today()).days
            item["days_until_observation"] = max(days_until, 0)
            if days_until > 0 or inspection_days_until > 0:
                summary = (
                    f"观察期进行中，预计 {row['observation_at']} 进入效果判断。"
                    if days_until > 0
                    else f"已到观察节点，等待计划于 {row['next_inspection_at']} 的巡检后由 Agent 判断效果。"
                )
                item.update({
                    "observation_phase": "观察中",
                    "agent_status": "待观察",
                    "agent_effect": None,
                    "summary": summary,
                    "report_json": "{}",
                    "can_confirm": False,
                    "allowed_confirmations": [],
                })
            else:
                latest_result = c.execute("""
                    SELECT id, created_at
                    FROM inspection_result
                    WHERE parent_asin=? AND shop_account=? AND created_at >= ? AND created_at >= ?
                      AND result_status='SUCCESS'
                    ORDER BY id DESC LIMIT 1
                """, (row["parent_asin"], row["shop_account"], row["observation_at"], row["next_inspection_at"])).fetchone()
                if not latest_result:
                    item.update({
                        "observation_phase": "等待巡检",
                        "agent_status": "等待巡检数据",
                        "agent_effect": None,
                        "summary": "已到观察节点，等待本轮巡检完成后由 Agent 判断效果。",
                        "report_json": "{}",
                        "can_confirm": False,
                        "allowed_confirmations": [],
                    })
                else:
                    report = _observe_case(c, row)
                    item.update(report)
                    item["observation_phase"] = "待运营确认"
                    item["agent_status"] = "待运营确认"
                    item["agent_effect"] = report["effect"]
                    item["report_json"] = json.dumps(report, ensure_ascii=False)
                    item["can_confirm"] = True
                    item["allowed_confirmations"] = (
                        ["继续观察"] if report["effect"] == "数据不足"
                        else ["保留当前动作", "继续观察", "重新处理"]
                    )
            item["product_name"] = name_map.get((row["parent_asin"], row["shop_account"]))
            item["image_url"] = image_map.get((row["parent_asin"], row["shop_account"]))
            haystack = " ".join(str(item.get(key) or "") for key in (
                "parent_asin", "shop_account", "product_name", "issue",
            )).lower()
            if q and q.lower() not in haystack:
                continue
            if shop and item["shop_account"] != shop:
                continue
            if phase and item["observation_phase"] != phase:
                continue
            if effect and item.get("agent_effect") != effect:
                continue
            if severity and item.get("severity") != severity:
                continue
            if review_from and item["observation_at"] < review_from:
                continue
            if review_to and item["observation_at"] > review_to:
                continue
            out.append(item)
        c.commit()
    return {
        "target_user_id": target,
        "total": len(out),
        "product_count": len({(item["parent_asin"], item["shop_account"]) for item in out}),
        "observing_count": sum(item["observation_phase"] == "观察中" for item in out),
        "waiting_inspection_count": sum(item["observation_phase"] == "等待巡检" for item in out),
        "confirmation_count": sum(item["observation_phase"] == "待运营确认" for item in out),
        "records": out,
    }


@router.post("/review/observations/{maintenance_id}/confirm")
def api_confirm_observation(maintenance_id: int, payload: dict = Body(...)):
    confirmation = payload.get("confirmation")
    if confirmation not in ("保留当前动作", "继续观察", "重新处理"):
        raise HTTPException(400, "confirmation 必须是 保留当前动作/继续观察/重新处理")
    target = resolve_target_user(payload.get("userId"), payload.get("targetId"))
    if target is None:
        raise HTTPException(400, "userId required")
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        row = c.execute("""
            SELECT oc.* FROM observation_case oc
            WHERE oc.id=? AND oc.status='待运营确认'
              AND EXISTS (SELECT 1 FROM product_access pa WHERE pa.parent_asin=oc.parent_asin AND pa.shop_account=oc.shop_account AND pa.user_id=?)
        """, (maintenance_id, target)).fetchone()
        if not row:
            raise HTTPException(404, "观察任务不存在或无操作权限")
        today = _dt.date.today().isoformat()
        if row["observation_at"] > today:
            raise HTTPException(409, f"该异常仍在观察期，{row['observation_at']} 后才能确认")
        if row["status"] != "待运营确认" or not row["agent_effect"]:
            raise HTTPException(409, "Agent 尚未完成效果判断，暂不能确认")
        if row["agent_effect"] == "数据不足" and confirmation != "继续观察":
            raise HTTPException(409, "当前数据不足，只能选择继续观察")
        event = c.execute("""
            SELECT id, 唯一识别, 父ASIN, 店铺账号, 问题点位, 严重度, 命中变体, 变体重要性, 当前状态
            FROM event_pool WHERE 唯一识别=?
        """, (row["event_uid"],)).fetchone()
        if not event:
            raise HTTPException(404, "关联异常不存在")
        c.execute("""
            UPDATE task_action SET effect=?
            WHERE id=? AND action_type='完成'
        """, (row["agent_effect"], row["completion_action_id"]))
        state = {"保留当前动作": "已确认改善", "继续观察": "观察中", "重新处理": "已确认需重新处理"}[confirmation]
        if confirmation == "继续观察":
            schedule = _maintenance_schedule(event, {})
            c.execute("""
                UPDATE observation_case
                SET status='观察中', observation_at=?, next_inspection_at=?,
                    inspection_time_source='follow_review', confirmed_at=datetime('now','localtime'),
                    confirmed_by=?, confirmation=?, agent_effect=NULL, agent_summary=NULL,
                    agent_observed_at=NULL, agent_payload=NULL, schedule_rule_id=?, schedule_rule_version=?,
                    follow_up_type=?, schedule_source=?, default_observation_at=?,
                    default_next_inspection_at=?, expected_available_at=NULL, updated_at=datetime('now','localtime')
                WHERE id=?
            """, (schedule["observation_at"], schedule["next_inspection_at"], payload.get("userId"), confirmation,
                  schedule["rule_id"], schedule["rule_version"], schedule["follow_up_type"], schedule["source"],
                  schedule["default_observation_at"], schedule["default_next_inspection_at"], maintenance_id))
            c.execute("""
                UPDATE event_pool SET 下一次复查时间=?, 复查时间来源=?, 更新时间=datetime('now','localtime')
                WHERE id=?
            """, (schedule["observation_at"], schedule["source"], event["id"]))
        else:
            c.execute("UPDATE observation_case SET status=?, confirmed_at=datetime('now','localtime'), confirmed_by=?, confirmation=?, updated_at=datetime('now','localtime') WHERE id=?",
                      (state, payload.get("userId"), confirmation, maintenance_id))
        if confirmation == "重新处理":
            target_status = "新发现"
        elif confirmation == "保留当前动作":
            target_status = "已关闭"
        else:
            # 继续使用同一观察记录，事件仍应维持“已处理待复扫”，不能重新进入可完成队列。
            target_status = "已处理待复扫"
        if event["当前状态"] != target_status:
            ok, message = 流转状态_事务(
                c, event["id"], target_status,
                f"运营确认 Agent 观察结果：{confirmation}",
                str(payload.get("userId") or "运营"), "Agent效果观察确认",
                "复查确认恢复" if target_status == "已关闭" else None,
                {"observation_id": maintenance_id, "confirmation": confirmation},
            )
            if not ok:
                raise HTTPException(409, message)
        if confirmation == "重新处理":
            c.execute("UPDATE event_pool SET 下一次复查时间=NULL, 更新时间=datetime('now','localtime') WHERE id=?", (event["id"],))
        c.commit()
    return {"ok": True, "maintenance_id": maintenance_id, "status": state, "confirmation": confirmation}


@router.post("/review/product")
def api_product_review_post(payload: dict = Body(...)):
    """旧版手工产品复盘写接口已停用，避免绕过 Agent 效果判断。"""
    raise HTTPException(410, "旧版复盘已停用，请使用 /api/review/observations/{maintenance_id}/confirm")

    """按产品提交复盘结果，覆盖同一复查节点下的全部异常。"""
    effect = payload.get("effect")
    conclusion = payload.get("conclusion")
    parent_asin = payload.get("parent_asin")
    shop_account = payload.get("shop_account")
    review_at = payload.get("review_at")
    if effect not in ("变好", "变差", "待观察"):
        raise HTTPException(400, "effect 必填且必须是 变好/变差/待观察")
    if conclusion and conclusion not in ("保留动作", "继续观察", "二次调整"):
        raise HTTPException(400, "不支持的复盘结论")
    if not parent_asin or not shop_account or not review_at:
        raise HTTPException(400, "parent_asin、shop_account、review_at 必填")
    if conclusion == "保留动作" and effect != "变好":
        raise HTTPException(409, "只有复查确认变好，才能结束该产品维护")
    target = resolve_target_user(payload.get("userId"), payload.get("targetId"))
    if target is None:
        raise HTTPException(400, "userId required")

    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        events = c.execute("""
            SELECT e.id, e.唯一识别, e.当前状态,
                   (SELECT id FROM task_action
                    WHERE event_uid=e.唯一识别 AND action_type='完成' AND review_at=?
                    ORDER BY id DESC LIMIT 1) AS action_id
            FROM event_pool e
            WHERE e.父ASIN=? AND e.店铺账号=?
              AND e.当前状态 IN ('已处理待复扫', '待观察')
              AND e.下一次复查时间=?
              AND EXISTS (
                SELECT 1 FROM product_access pa
                WHERE pa.parent_asin=e.父ASIN AND pa.shop_account=e.店铺账号
                  AND pa.user_id=?
              )
            ORDER BY e.id
        """, (review_at, parent_asin, shop_account, review_at, target)).fetchall()
        if not events:
            raise HTTPException(404, "未找到该产品本次待复查记录")
        if any(not event["action_id"] for event in events):
            raise HTTPException(409, "该产品存在缺失执行记录的异常，无法统一复盘")

        transition = {
            "保留动作": "已关闭" if effect == "变好" else None,
            "继续观察": "待观察",
            "二次调整": "新发现",
        }.get(conclusion)
        operator = str(payload.get("userId") or "运营")
        reason = payload.get("notes") or f"产品复盘结论：{conclusion or effect}"
        for event in events:
            c.execute("UPDATE task_action SET effect=? WHERE id=?", (effect, event["action_id"]))
            if transition:
                ok, message = 流转状态_事务(
                    c, event["id"], transition, reason, operator, "产品复盘结论",
                    "产品复查确认恢复" if transition == "已关闭" else None,
                    {"effect": effect, "conclusion": conclusion, "scope": "product"},
                )
                if not ok:
                    raise HTTPException(409, message)
            if conclusion:
                c.execute("""
                    INSERT INTO task_action (event_uid, user_id, action_type, notes, effect)
                    VALUES (?, ?, '复盘', ?, ?)
                """, (event["唯一识别"], payload.get("userId"), reason, effect))
                next_review = (_dt.date.today() + _dt.timedelta(days=3)).isoformat() if conclusion == "继续观察" else None
                c.execute("""
                    UPDATE event_pool
                    SET 下一次复查时间=?, 复查时间来源=?, 更新时间=datetime('now','localtime')
                    WHERE id=?
                """, (next_review, "manual" if next_review else None, event["id"]))
        c.commit()
    return {"ok": True, "parent_asin": parent_asin, "shop_account": shop_account,
            "event_count": len(events), "effect": effect, "conclusion": conclusion}


@router.post("/review/{event_uid}")
def api_review_post(event_uid: str, payload: dict = Body(...)):
    """旧版手工异常复盘写接口已停用，避免绕过 Agent 效果判断。"""
    raise HTTPException(410, "旧版复盘已停用，请使用 /api/review/observations/{maintenance_id}/confirm")

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
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        event = c.execute("""
            SELECT e.id, e.当前状态 FROM event_pool e
            WHERE e.唯一识别=? AND EXISTS (
              SELECT 1 FROM product_access pa
              WHERE pa.parent_asin=e.父ASIN AND pa.shop_account=e.店铺账号
                AND pa.user_id=?
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
