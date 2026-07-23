"""单异常效果观察的默认复查计划。后端是唯一日期计算来源。"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml


RULE_PATH = Path(__file__).resolve().parents[1] / "inspector" / "rules" / "R7_复查频次.yaml"


class ScheduleValidationError(ValueError):
    """运营提交的观察计划不满足时间约束。"""


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    with RULE_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _value(event: Mapping[str, Any], name: str) -> str:
    value = event[name] if name in event.keys() else None
    return str(value or "").strip()


def _parse_date(value: Any, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ScheduleValidationError(f"{label}必须是 YYYY-MM-DD 格式") from error


def _matched_rule(event: Mapping[str, Any]) -> dict[str, Any]:
    issue = _value(event, "问题点位")
    importance = _value(event, "变体重要性")
    rules = load_rules()
    for rule in rules.get("rules") or []:
        if issue not in (rule.get("issues") or []):
            continue
        expected_importance = rule.get("variant_importance") or []
        if expected_importance and importance not in expected_importance:
            continue
        return rule
    return rules.get("default") or {"id": "default", "name": "默认复查", "days": 3}


def resolve_schedule(
    event: Mapping[str, Any], payload: Mapping[str, Any] | None = None, *, today: dt.date | None = None,
) -> dict[str, Any]:
    """计算并校验一条异常的有效观察计划。

    review_at 是最早判断效果的日期；用于效果判断的巡检不得早于该日期。
    """
    payload = payload or {}
    current_day = today or dt.date.today()
    rule = _matched_rule(event)
    default_days = int(rule.get("days") or 3)
    default_review = current_day + dt.timedelta(days=default_days)
    expected_available_at = payload.get("expected_available_at")
    source = "rule"

    if expected_available_at:
        expected_day = _parse_date(expected_available_at, "预计到货/上架日")
        if rule.get("supports_expected_available_date"):
            default_review = max(current_day, expected_day)
            source = "expected_arrival"

    review_at = payload.get("review_at")
    if review_at:
        review_day = _parse_date(review_at, "复查时间")
        source = "manual"
    else:
        review_day = default_review

    inspection_at = payload.get("next_inspection_at")
    inspection_day = _parse_date(inspection_at, "下次巡检时间") if inspection_at else review_day
    if review_day < current_day:
        raise ScheduleValidationError("复查时间不能早于今天")
    if inspection_day < current_day:
        raise ScheduleValidationError("下次巡检时间不能早于今天")
    if inspection_day < review_day:
        raise ScheduleValidationError("用于效果判断的下次巡检时间不能早于复查时间")

    version = str(load_rules().get("version") or "unknown")
    return {
        "rule_id": str(rule.get("id") or "default"),
        "rule_name": str(rule.get("name") or "默认复查"),
        "rule_version": version,
        "follow_up_type": str(rule.get("name") or "默认复查"),
        "source": source,
        "default_observation_at": default_review.isoformat(),
        "default_next_inspection_at": default_review.isoformat(),
        "observation_at": review_day.isoformat(),
        "next_inspection_at": inspection_day.isoformat(),
        "expected_available_at": str(expected_available_at or "") or None,
        "supports_expected_available_date": bool(rule.get("supports_expected_available_date")),
        "days": max(0, (review_day - current_day).days),
    }
