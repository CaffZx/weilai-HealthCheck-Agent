"""运营工作台任务/异常/复盘接口。"""
from __future__ import annotations
import datetime as _dt
import json
import sqlite3

from fastapi import APIRouter, Body, HTTPException, Query

from data import local_store
from ..common import (
    PRIORITY_ORDER, TIER_ORDER,
    fetch_events_for_user, group_events_by_product, product_status_for_events,
    product_matches_status, attach_assignments,
    resolve_target_user,
)
from inspector.scheduler.event_pool import 流转状态_事务

router = APIRouter(prefix="/api")


def _attach_active_maintenance(products: list[dict]) -> None:
    """把进行中的产品级效果观察附加到任务产品，供工作台提示和避免重复创建。"""
    if not products:
        return
    keys = {(product["parent_asin"], product["shop_account"]) for product in products}
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT id, parent_asin, shop_account, observation_at, next_inspection_at
            FROM product_maintenance
            WHERE status IN ('观察中', '待运营确认')
            ORDER BY id DESC
        """).fetchall()
    active = {}
    for row in rows:
        key = (row["parent_asin"], row["shop_account"])
        if key in keys and key not in active:
            active[key] = dict(row)
    for product in products:
        maintenance = active.get((product["parent_asin"], product["shop_account"]))
        product["has_active_maintenance"] = maintenance is not None
        product["maintenance_id"] = maintenance["id"] if maintenance else None
        product["observation_at"] = maintenance["observation_at"] if maintenance else None
        product["next_inspection_at"] = maintenance["next_inspection_at"] if maintenance else None


def _maintenance_schedule(payload: dict) -> tuple[str, str]:
    """解析产品级观察时间；逐异常完成时未指定则默认三天后。"""
    review_at = payload.get("review_at") or (_dt.date.today() + _dt.timedelta(days=3)).isoformat()
    next_inspection_at = payload.get("next_inspection_at") or review_at
    try:
        review_date = _dt.date.fromisoformat(review_at)
        inspection_date = _dt.date.fromisoformat(next_inspection_at)
    except (TypeError, ValueError):
        raise HTTPException(400, "复查时间和下次巡检时间必须是 YYYY-MM-DD 格式")
    if review_date < _dt.date.today() or inspection_date < _dt.date.today():
        raise HTTPException(400, "复查时间和下次巡检时间不能早于今天")
    return review_at, next_inspection_at


def _link_latest_completion_actions(conn: sqlite3.Connection, maintenance_id: int, events: list[sqlite3.Row]) -> None:
    """把每条异常本轮最新完成动作归入同一产品维护凭证。"""
    for event in events:
        conn.execute("""
            UPDATE task_action SET maintenance_id=?
            WHERE id=(
                SELECT id FROM task_action
                WHERE event_uid=? AND action_type='完成' AND maintenance_id IS NULL
                ORDER BY id DESC LIMIT 1
            )
        """, (maintenance_id, event["唯一识别"]))


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
    _attach_active_maintenance(products)
    p0 = sum(1 for product in all_products if product["priority"] == "P0")
    p1 = sum(1 for product in all_products if product["priority"] == "P1")
    p2 = sum(1 for product in all_products if product["priority"] == "P2")

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
    # 单异常操作只维护事件状态；全部异常完成时自动形成一次产品级效果观察。
    review_at = None
    next_inspection_at = None
    maintenance_id = None
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        event = c.execute("""
            SELECT e.id, e.当前状态, e.父ASIN, e.店铺账号, e.问题点位, e.严重度,
                   e.inspection_result_id
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
            None,
            None,
            action_type, operator, event["id"],
        ))
        if action_type == "重新打开":
            c.execute("""
                UPDATE product_maintenance
                SET status='已被重新打开异常替代', updated_at=datetime('now','localtime')
                WHERE status IN ('观察中', '待运营确认')
                  AND EXISTS (
                    SELECT 1 FROM product_maintenance_event me
                    WHERE me.maintenance_id=product_maintenance.id AND me.event_uid=?
                  )
            """, (event_uid,))
        if action_type == "完成":
            product_events = c.execute("""
                SELECT id, 唯一识别, 当前状态, 父ASIN, 店铺账号,
                       问题点位, 严重度, 命中变体, inspection_result_id
                FROM event_pool
                WHERE 父ASIN=? AND 店铺账号=?
                  AND 当前状态 NOT IN ('已关闭', '误报', '忽略')
                ORDER BY id
            """, (event["父ASIN"], event["店铺账号"])).fetchall()
            if product_events and all(item["当前状态"] == "已处理待复扫" for item in product_events):
                review_at, next_inspection_at = _maintenance_schedule(payload)
                maintenance_id = local_store.create_product_maintenance(
                    c, parent_asin=event["父ASIN"], shop_account=event["店铺账号"],
                    user_id=payload.get("userId"), result=payload.get("result"),
                    actual_action=payload.get("actual_action"), notes=payload.get("notes"),
                    observation_at=review_at, next_inspection_at=next_inspection_at,
                    events=product_events,
                )
                _link_latest_completion_actions(c, maintenance_id, product_events)
                event_ids = [item["id"] for item in product_events]
                placeholders = ",".join("?" for _ in event_ids)
                c.execute(
                    f"""UPDATE event_pool
                        SET 下一次复查时间=?, 复查时间来源='manual',
                            更新时间=datetime('now','localtime')
                        WHERE id IN ({placeholders})""",
                    (review_at, *event_ids),
                )
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
        "maintenance_created": maintenance_id is not None,
        "maintenance_id": maintenance_id,
        "observation_at": review_at,
        "next_inspection_at": next_inspection_at,
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

    if not payload.get("review_at"):
        raise HTTPException(400, "完成产品维护必须设置复查日期")
    review_at, next_inspection_at = _maintenance_schedule(payload)

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
            SELECT id, 唯一识别, 当前状态, 父ASIN, 店铺账号,
                   问题点位, 严重度, 命中变体, inspection_result_id
            FROM event_pool
            WHERE 父ASIN=? AND 店铺账号=?
              AND 当前状态 NOT IN ('已关闭', '误报', '忽略')
            ORDER BY id
        """, (parent_asin, shop_account)).fetchall()
        if not events:
            raise HTTPException(409, "该产品没有待处理异常")
        all_completed = all(event["当前状态"] == "已处理待复扫" for event in events)
        active_maintenance = c.execute("""
            SELECT id, observation_at, next_inspection_at
            FROM product_maintenance
            WHERE parent_asin=? AND shop_account=?
              AND status IN ('观察中', '待运营确认')
            ORDER BY id DESC LIMIT 1
        """, (parent_asin, shop_account)).fetchone()
        if all_completed and active_maintenance:
            return {
                "ok": True,
                "already_observing": True,
                "parent_asin": parent_asin,
                "shop_account": shop_account,
                "maintenance_id": active_maintenance["id"],
                "observation_at": active_maintenance["observation_at"],
                "next_inspection_at": active_maintenance["next_inspection_at"],
                "product_status": "已完成",
                "events": [],
            }

        completed = []
        action_ids = []
        for event in events:
            if event["当前状态"] == "已处理待复扫":
                continue
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
            action_ids.append(action_cursor.lastrowid)
            c.execute("""
                UPDATE event_pool
                SET 下一次复查时间=?, 复查时间来源=?, 上次处理动作='完成产品维护',
                    上次处理时间=datetime('now','localtime'), 上次处理人=?, 更新时间=datetime('now','localtime')
                WHERE id=?
            """, (review_at, "manual" if review_at else None, operator, event["id"]))
            completed.append(event["唯一识别"])
        maintenance_id = local_store.create_product_maintenance(
            c, parent_asin=parent_asin, shop_account=shop_account,
            user_id=payload.get("userId"), result=payload.get("result"),
            actual_action=payload.get("actual_action"), notes=payload.get("notes"),
            observation_at=review_at, next_inspection_at=next_inspection_at,
            events=events,
        )
        _link_latest_completion_actions(c, maintenance_id, events)
        c.execute("UPDATE product_maintenance SET updated_at=datetime('now','localtime') WHERE id=?", (maintenance_id,))
        c.commit()

    refreshed_events = fetch_events_for_user(target, only_open=False)
    product = next(
        (item for item in group_events_by_product(refreshed_events)
         if item["parent_asin"] == parent_asin and item["shop_account"] == shop_account),
        None,
    )
    return {
        "ok": True,
        "parent_asin": parent_asin,
        "shop_account": shop_account,
        "completed_event_uids": completed,
        "completed_count": len(completed),
        "maintenance_created": True,
        "maintenance_id": maintenance_id,
        "observation_at": review_at,
        "next_inspection_at": next_inspection_at,
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


def _observe_maintenance(conn: sqlite3.Connection, maintenance: sqlite3.Row) -> dict:
    """用执行前/观察期快照生成可解释的自动观察结果。"""
    latest_result = conn.execute("""
        SELECT id, result_json, result_status, created_at
        FROM inspection_result
        WHERE parent_asin=? AND shop_account=? AND created_at >= ?
          AND result_status='SUCCESS'
        ORDER BY id DESC LIMIT 1
    """, (maintenance["parent_asin"], maintenance["shop_account"],
           maintenance["next_inspection_at"])).fetchone()
    latest_card = {}
    latest_inspection_at = latest_result["created_at"] if latest_result else None
    if latest_result:
        try:
            latest_card = json.loads(latest_result["result_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            latest_card = {}
    event_rows = conn.execute("""
        SELECT me.*
        FROM product_maintenance_event me
        WHERE me.maintenance_id=?
        ORDER BY me.severity, me.issue
    """, (maintenance["id"],)).fetchall()
    details, known = [], 0
    for event in event_rows:
        baseline = _parse_snapshot(event["baseline_snapshot"])
        current = {}
        latest_issue = None
        if latest_card:
            latest_issue = next((item for item in latest_card.get("异常明细") or []
                                 if item.get("问题点位") == event["issue"]
                                 and (item.get("命中变体") or "") == (event["variant"] or "")), None)
            detail = latest_issue or {}
            current = {item.get("label"): item.get("value") for item in (detail.get("数据快照") or [])}
            current = _parse_snapshot(json.dumps([{"label": k, "value": v} for k, v in current.items()], ensure_ascii=False))
        comparable = [(label, old, current.get(label)) for label, old in baseline.items()
                      if current.get(label) is not None]
        issue = event["issue"] or "异常"
        if latest_result and latest_card and latest_issue is None:
            effect, reason, confidence = "变好", "下次巡检未再次命中该异常", 0.9
            known += 1
        elif not comparable:
            effect, reason, confidence = "数据不足", (
                "等待下次巡检数据" if not latest_result
                else "缺少执行前或观察期的可比指标"
            ), 0.0
        else:
            known += 1
            effect, reason, confidence = _compare_observation_metrics(issue, comparable)
        details.append({"event_uid": event["event_uid"], "issue": issue, "severity": event["severity"],
                        "effect": effect, "reason": reason, "baseline": baseline,
                        "current": current, "confidence": confidence})
    if not details or known == 0:
        effect = "数据不足"
        summary = "当前缺少足够的执行前/观察期可比数据，Agent 暂不判断效果。"
    elif any(item["effect"] == "数据不足" for item in details):
        effect = "数据不足"
        summary = "部分异常缺少可比指标，Agent 暂不对产品整体效果下结论，建议继续观察。"
    elif any(item["effect"] == "变差" for item in details):
        effect = "变差"; summary = "至少一项核心指标向异常方向变化，建议重新检查处理动作。"
    elif any(item["effect"] == "变好" for item in details):
        effect = "变好"; summary = "至少一项核心指标向改善方向变化，建议运营确认是否保留当前动作。"
    else:
        effect = "无明显变化"; summary = "观察期内指标变化不明显，建议延长观察或补充数据。"
    report = {
        "effect": effect,
        "summary": summary,
        "details": details,
        "latest_inspection_at": latest_inspection_at,
        "confidence": min((item["confidence"] for item in details), default=0.0),
    }
    conn.execute("""
        INSERT INTO observation_report (maintenance_id, agent_status, agent_effect, confidence, summary, report_json)
        VALUES (?, '待运营确认', ?, ?, ?, ?)
        ON CONFLICT(maintenance_id) DO UPDATE SET
          agent_status='待运营确认', agent_effect=excluded.agent_effect,
          confidence=excluded.confidence, summary=excluded.summary,
          report_json=excluded.report_json, observed_at=datetime('now','localtime'),
          updated_at=datetime('now','localtime')
    """, (maintenance["id"], effect,
           min((item["confidence"] for item in details), default=0.0), summary,
           json.dumps(report, ensure_ascii=False)))
    conn.execute("""
        UPDATE product_maintenance
        SET status='待运营确认', agent_effect=?, agent_summary=?,
            agent_observed_at=datetime('now','localtime'), agent_payload=?,
            updated_at=datetime('now','localtime')
        WHERE id=?
    """, (effect, summary, json.dumps(report, ensure_ascii=False), maintenance["id"]))
    return report


@router.get("/review/observations")
def api_review_observations(userId: int = Query(...), targetId: int | None = Query(None)):
    target = resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    today = _dt.date.today().isoformat()
    with local_store.connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT pm.*, orp.agent_status, orp.agent_effect, orp.confidence, orp.summary, orp.report_json
            FROM product_maintenance pm
            JOIN observation_report orp ON orp.maintenance_id=pm.id
            WHERE EXISTS (SELECT 1 FROM product_access pa WHERE pa.parent_asin=pm.parent_asin AND pa.shop_account=pm.shop_account AND pa.user_id=?)
              AND pm.status IN ('观察中', '待运营确认')
            ORDER BY CASE pm.status WHEN '待运营确认' THEN 0 ELSE 1 END,
                     pm.observation_at ASC, pm.id ASC
        """, (target,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["maintenance_id"] = row["id"]
            days_until = (_dt.date.fromisoformat(row["observation_at"]) - _dt.date.today()).days
            item["days_until_observation"] = max(days_until, 0)
            if days_until > 0:
                item.update({
                    "observation_phase": "观察中",
                    "agent_status": "待观察",
                    "agent_effect": None,
                    "summary": f"观察期进行中，预计 {row['observation_at']} 进入效果判断。",
                    "report_json": "{}",
                    "can_confirm": False,
                    "allowed_confirmations": [],
                })
            else:
                latest_result = c.execute("""
                    SELECT id, created_at
                    FROM inspection_result
                    WHERE parent_asin=? AND shop_account=? AND created_at >= ?
                      AND result_status='SUCCESS'
                    ORDER BY id DESC LIMIT 1
                """, (row["parent_asin"], row["shop_account"], row["next_inspection_at"])).fetchone()
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
                    report = _observe_maintenance(c, row)
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
            out.append(item)
        c.commit()
    return {
        "target_user_id": target,
        "total": len(out),
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
            SELECT pm.* FROM product_maintenance pm
            WHERE pm.id=? AND pm.status='待运营确认'
              AND EXISTS (SELECT 1 FROM product_access pa WHERE pa.parent_asin=pm.parent_asin AND pa.shop_account=pm.shop_account AND pa.user_id=?)
              AND NOT EXISTS (
                SELECT 1 FROM product_maintenance newer
                WHERE newer.parent_asin=pm.parent_asin AND newer.shop_account=pm.shop_account
                  AND newer.id>pm.id AND newer.status IN ('观察中', '待运营确认')
              )
        """, (maintenance_id, target)).fetchone()
        if not row:
            raise HTTPException(404, "观察任务不存在或无操作权限")
        today = _dt.date.today().isoformat()
        report = c.execute("""
            SELECT agent_status, agent_effect
            FROM observation_report
            WHERE maintenance_id=?
        """, (maintenance_id,)).fetchone()
        if row["observation_at"] > today:
            raise HTTPException(409, f"该产品仍在观察期，{row['observation_at']} 后才能确认")
        if row["status"] != "待运营确认" or not report or report["agent_status"] != "待运营确认":
            raise HTTPException(409, "Agent 尚未完成效果判断，暂不能确认")
        if report["agent_effect"] == "数据不足" and confirmation != "继续观察":
            raise HTTPException(409, "当前数据不足，只能选择继续观察")
        c.execute("""
            UPDATE task_action SET effect=?
            WHERE maintenance_id=? AND action_type='完成'
        """, (report["agent_effect"], maintenance_id))
        state = {"保留当前动作": "已确认改善", "继续观察": "观察中", "重新处理": "已确认需重新处理"}[confirmation]
        if confirmation == "继续观察":
            next_observation = (_dt.date.today() + _dt.timedelta(days=3)).isoformat()
            c.execute("""
                UPDATE product_maintenance
                SET status='观察中', observation_at=?, next_inspection_at=?,
                    inspection_time_source='follow_review', confirmed_at=datetime('now','localtime'),
                    confirmed_by=?, confirmation=?, updated_at=datetime('now','localtime')
                WHERE id=?
            """, (next_observation, next_observation, payload.get("userId"), confirmation, maintenance_id))
            c.execute("""
                UPDATE observation_report
                SET agent_status='待观察', agent_effect=NULL, confidence=NULL,
                    summary=NULL, report_json='{}', updated_at=datetime('now','localtime')
                WHERE maintenance_id=?
            """, (maintenance_id,))
        else:
            c.execute("UPDATE product_maintenance SET status=?, confirmed_at=datetime('now','localtime'), confirmed_by=?, confirmation=?, updated_at=datetime('now','localtime') WHERE id=?",
                      (state, payload.get("userId"), confirmation, maintenance_id))
        event_rows = c.execute("""
            SELECT e.id, e.当前状态 FROM product_maintenance_event me
            JOIN event_pool e ON e.唯一识别=me.event_uid
            WHERE me.maintenance_id=?
        """, (maintenance_id,)).fetchall()
        if confirmation == "重新处理":
            target_status = "新发现"
        elif confirmation == "保留当前动作":
            target_status = "已关闭"
        else:
            target_status = "待观察"
        for event in event_rows:
            if event["当前状态"] == target_status:
                continue
            ok, message = 流转状态_事务(
                c, event["id"], target_status,
                f"运营确认 Agent 观察结果：{confirmation}",
                str(payload.get("userId") or "运营"), "Agent效果观察确认",
                "复查确认恢复" if target_status == "已关闭" else None,
                {"maintenance_id": maintenance_id, "confirmation": confirmation},
            )
            if not ok:
                raise HTTPException(409, message)
        if confirmation == "重新处理":
            c.execute("UPDATE event_pool SET 下一次复查时间=NULL, 更新时间=datetime('now','localtime') WHERE id IN (SELECT e.id FROM product_maintenance_event me JOIN event_pool e ON e.唯一识别=me.event_uid WHERE me.maintenance_id=?)", (maintenance_id,))
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
