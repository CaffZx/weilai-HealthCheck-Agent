"""异常级效果观察：由成功巡检主动触发的可解释结论。"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3

from data import local_store


def parse_snapshot(snapshot: str | None) -> dict[str, float]:
    if not snapshot:
        return {}
    try:
        items = json.loads(snapshot)
    except (TypeError, json.JSONDecodeError):
        return {}
    out = {}
    for item in items if isinstance(items, list) else []:
        label, value = item.get("label"), str(item.get("value") or "")
        numbers = re.findall(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if label and numbers:
            out[label] = float(numbers[0])
    return out


def metric_direction(issue: str, label: str) -> int:
    """返回指标向改善方向：1 为越高越好，-1 为越低越好，0 为不自动判断。"""
    issue_text = str(issue or "").lower()
    label_text = str(label or "").lower()
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
    if "评分" in issue_text and any(word in label_text for word in ("rating", "review score")):
        return 1
    if "库存积压" in issue_text and any(word in label_text for word in ("库存", "inventory")):
        return -1
    return 0


def compare_observation_metrics(issue: str, comparable: list[tuple[str, float, float]]) -> tuple[str, str, float]:
    """只比较有明确业务方向的同名指标；不同方向或未知指标不强行汇总。"""
    judgments = []
    for label, old, new in comparable:
        direction = metric_direction(issue, label)
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


def observe_case(conn: sqlite3.Connection, case: sqlite3.Row) -> dict:
    """对一条已完成异常做执行前与观察期的可解释比对，并持久化结论。"""
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
    baseline = parse_snapshot(case["baseline_snapshot"])
    latest_issue = None
    if latest_card:
        latest_issue = next((item for item in latest_card.get("异常明细") or []
                             if item.get("问题点位") == case["issue"]
                             and (item.get("命中变体") or "") == (case["variant"] or "")), None)
    current = parse_snapshot(json.dumps((latest_issue or {}).get("数据快照") or [], ensure_ascii=False))
    comparable = [(label, old, current.get(label)) for label, old in baseline.items()
                  if current.get(label) is not None]
    if latest_result and latest_card and latest_issue is None:
        effect, reason, confidence = "变好", "下次巡检未再次命中该异常", 0.9
    elif not comparable:
        effect, reason, confidence = "数据不足", (
            "等待下次巡检数据" if not latest_result else "缺少执行前或观察期的可比指标"
        ), 0.0
    else:
        effect, reason, confidence = compare_observation_metrics(case["issue"] or "异常", comparable)
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


def process_due_observations(parent_asin: str, shop_account: str, *, today: dt.date | None = None) -> list[int]:
    """成功巡检后，自动处理该产品已经到期且已有有效巡检结果的观察任务。"""
    current_day = (today or dt.date.today()).isoformat()
    with local_store.connect() as conn:
        conn.row_factory = sqlite3.Row
        cases = conn.execute("""
            SELECT oc.*
            FROM observation_case oc
            WHERE oc.parent_asin=? AND oc.shop_account=?
              AND oc.status='观察中'
              AND oc.observation_at <= ? AND oc.next_inspection_at <= ?
              AND EXISTS (
                SELECT 1 FROM inspection_result ir
                WHERE ir.parent_asin=oc.parent_asin AND ir.shop_account=oc.shop_account
                  AND ir.result_status='SUCCESS' AND ir.created_at >= oc.observation_at
                  AND ir.created_at >= oc.next_inspection_at
              )
            ORDER BY oc.id ASC
        """, (parent_asin, shop_account, current_day, current_day)).fetchall()
        completed_ids = []
        for case in cases:
            observe_case(conn, case)
            completed_ids.append(case["id"])
        conn.commit()
    return completed_ids


def process_all_due_observations(*, today: dt.date | None = None) -> list[int]:
    """服务启动时补齐已有巡检结果对应的到期观察结论。"""
    current_day = (today or dt.date.today()).isoformat()
    with local_store.connect() as conn:
        rows = conn.execute("""
            SELECT DISTINCT parent_asin, shop_account
            FROM observation_case
            WHERE status='观察中'
              AND observation_at <= ? AND next_inspection_at <= ?
        """, (current_day, current_day)).fetchall()
    completed_ids = []
    for parent_asin, shop_account in rows:
        completed_ids.extend(process_due_observations(parent_asin, shop_account, today=today))
    return completed_ids
