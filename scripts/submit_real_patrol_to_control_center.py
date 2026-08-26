from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import bindparam

from clients.operating_mode_mcp import CurrentOperatingMode, OperatingModeMcpClient
from core.contracts import canonical_json, sha256_json
from core.control_center_contracts import (
    PatrolUnitPayload,
    SubmitPatrolBatchRequest,
    SubmitPatrolBatchResult,
    _to_platform_site_code,
    apply_current_operating_mode,
    attach_operating_mode_error,
)
from scripts.recommendation_builder import (
    build_current_value,
    build_recommendation_single,
    build_recommendations,
)

logger = logging.getLogger(__name__)
from integrations.repositories.tables import (
    patrol_fact_snapshot,
    patrol_product_image,
    patrol_signal,
    patrol_signal_occurrence,
)
from web.backend.deps import get_database_engine

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "contracts/control-center-submit-patrol-batch.v2.schema.json"
AUDIT_DIR = ROOT / "artifacts/control-center-delivery"

ISSUE_CODE_MAP = {
    "LISTING_NOT_SELLABLE": ("LINK_STATUS", "LISTING_UNSELLABLE"),
    "VARIANT_DETACHED": ("LINK_STATUS", "VARIATION_DROPPED"),
    "PARENT_CHILD_RELATION_BROKEN": (
        "LINK_STATUS",
        "PARENT_CHILD_RELATION_ABNORMAL",
    ),
    "BUY_BOX_LOST": ("LINK_STATUS", "BUY_BOX_LOST"),
    "MAIN_IMAGE_ABNORMAL": ("CONTENT_INTEGRITY", "MAIN_IMAGE_ABNORMAL"),
    "GALLERY_IMAGE_ABNORMAL": ("CONTENT_INTEGRITY", "IMAGE_ABNORMAL"),
    "TITLE_ABNORMAL": ("CONTENT_INTEGRITY", "TITLE_ABNORMAL"),
    "BULLET_POINTS_ABNORMAL": ("CONTENT_INTEGRITY", "BULLET_POINTS_ABNORMAL"),
    "APLUS_CONTENT_ABNORMAL": ("CONTENT_INTEGRITY", "A_PLUS_ABNORMAL"),
    "VARIANT_PRICE_GAP": ("PRICE_PROMOTION", "VARIATION_PRICE_GAP_ABNORMAL"),
    "PROMOTION_ABNORMAL": ("PRICE_PROMOTION", "PROMOTION_ABNORMAL"),
    "SALES_DECLINE": ("TRANSACTION_PERFORMANCE", "SALES_ABNORMAL"),
    "CONVERSION_RATE_DROP": (
        "TRANSACTION_PERFORMANCE",
        "LISTING_CONVERSION_RATE_DROP",
    ),
    "ACOS_ABNORMAL": ("TRANSACTION_PERFORMANCE", "ACOS_ABNORMAL"),
    "AD_SPEND_ABNORMAL": ("TRANSACTION_PERFORMANCE", "AD_SPEND_ABNORMAL"),
    "TARGET_DEVIATION": ("TRANSACTION_PERFORMANCE", "TARGET_DEVIATION"),
    "NATURAL_TRAFFIC_ABNORMAL": (
        "TRANSACTION_PERFORMANCE",
        "ORGANIC_TRAFFIC_ABNORMAL",
    ),
    "KEYWORD_RANK_ABNORMAL": ("TRANSACTION_PERFORMANCE", "RANKING_ABNORMAL"),
    "SCALE_UP_NOT_EXECUTED": (
        "TRANSACTION_PERFORMANCE",
        "SCALING_NOT_EXECUTED",
    ),
    "INVENTORY_SHORTAGE": ("INVENTORY_AVAILABILITY", "INVENTORY_SHORTAGE"),
    "INVENTORY_OVERSTOCK": ("INVENTORY_AVAILABILITY", "INVENTORY_OVERSTOCK"),
    "SLOW_MOVING_INVENTORY": (
        "INVENTORY_AVAILABILITY",
        "SLOW_MOVING_INVENTORY",
    ),
    "FBA_AVAILABLE_ZERO": (
        "INVENTORY_AVAILABILITY",
        "FBA_AVAILABLE_INVENTORY_ZERO",
    ),
    "CATEGORY_MISMATCH": ("COMPLIANCE_ATTRIBUTE", "CATEGORY_ABNORMAL"),
    "ATTRIBUTE_MISMATCH": ("COMPLIANCE_ATTRIBUTE", "ATTRIBUTE_INFO_ABNORMAL"),
    "COMPLIANCE_ISSUE": ("COMPLIANCE_ATTRIBUTE", "COMPLIANCE_ABNORMAL"),
    "CONCENTRATED_NEGATIVE_REVIEWS": (
        "AFTER_SALES",
        "CONCENTRATED_NEGATIVE_REVIEWS",
    ),
    "RATING_ABNORMAL": ("AFTER_SALES", "RATING_ABNORMAL"),
    "REFUND_RATE_ABNORMAL": ("AFTER_SALES", "REFUND_ABNORMAL"),
}
SEVERITY_MAP = {"S0": "HIGH", "S1": "MEDIUM", "S2": "LOW", "S3": "LOW"}
DOMAIN_MAP = {
    "LINK_STATUS": "LISTING",
    "CONTENT_INTEGRITY": "LISTING",
    "PRICE_PROMOTION": "PRICE",
    "INVENTORY_AVAILABILITY": "INVENTORY",
    "TRANSACTION_PERFORMANCE": "OTHER",
    "COMPLIANCE_ATTRIBUTE": "LISTING",
    "AFTER_SALES": "OTHER",
}


class UnitNotSubmittable(ValueError):
    def __init__(self, operating_unit_id: str, missing_fields: list[str]) -> None:
        self.operating_unit_id = operating_unit_id
        self.missing_fields = missing_fields
        super().__init__(
            f"unit {operating_unit_id} cannot satisfy control-center v2: "
            + ", ".join(missing_fields)
        )


class EmptySubmittablePayload(ValueError):
    """All successful runs in a slice lack real control-center required fields."""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _ratio_from_percent(*values: Any) -> float | None:
    """把百分比数值（如 7.0 表示 7%）转成 0-1 比例；取第一个非空值。

    上游可能返回 >100% 的脏数据（如退款率 185%），钳制到 [0,1]，避免违反合同 le=1。
    """
    for value in values:
        num = _number(value)
        if num is not None:
            return max(0.0, min(1.0, num / 100))
    return None


def _owner_user_ids(value: Any) -> list[int]:
    parsed = _json(value) if isinstance(value, str) else value
    if not isinstance(parsed, (list, tuple)):
        return []
    owner_ids: set[int] = set()
    for item in parsed:
        try:
            owner_id = int(item)
        except (TypeError, ValueError):
            continue
        if owner_id > 0:
            owner_ids.add(owner_id)
    return sorted(owner_ids)


PRODUCT_LEVEL_MAP = {
    "P0_PRODUCT": "P0_PRODUCT",
    "战略级产品": "P0_PRODUCT",
    "P1_PRODUCT": "P1_PRODUCT",
    "重点产品": "P1_PRODUCT",
    "P2_PRODUCT": "P2_PRODUCT",
    "常规产品": "P2_PRODUCT",
    "P3_PRODUCT": "P3_PRODUCT",
    "长尾产品": "P3_PRODUCT",
}
PRODUCT_STAGE_MAP = {
    "TESTING": "TESTING",
    "测试": "TESTING",
    "测试期": "TESTING",
    "PUSHING": "PUSHING",
    "PROMOTING": "PUSHING",
    "推进": "PUSHING",
    "推进期": "PUSHING",
    "HARVESTING": "HARVESTING",
    "HARVEST_PROFIT": "HARVESTING",
    "收割利润": "HARVESTING",
    "收割利润期": "HARVESTING",
    "MAINTAINING": "MAINTAINING",
    "维持": "MAINTAINING",
    "维持期": "MAINTAINING",
    "LIQUIDATING": "LIQUIDATING",
    "清货": "LIQUIDATING",
    "清货期": "LIQUIDATING",
}
SEASON_STAGE_MAP = {
    "OFF": "OFF_SEASON",
    "OFF_SEASON": "OFF_SEASON",
    "淡季": "OFF_SEASON",
    "PEAK_SEASON_PREPARE": "PRE_PEAK",
    "PEAK_PREPARE": "PRE_PEAK",
    "旺季前期": "PRE_PEAK",
    "旺季准备": "PRE_PEAK",
    "PEAK": "PEAK",
    "PEAK_SEASON": "PEAK",
    "BIG_PEAK_SEASON": "PEAK",
    "旺季": "PEAK",
    "PEAK_LATE": "LATE_PEAK",
    "PEAK_SEASON_LATE": "LATE_PEAK",
    "LATE_PEAK_SEASON": "LATE_PEAK",
    "旺季末期": "LATE_PEAK",
    "大旺季": "PEAK",
}
AD_PURPOSE_MAP = {
    "流量型": "TRAFFIC",
    "引流": "TRAFFIC",
    "引流型": "TRAFFIC",
    "转化型": "CONVERSION",
    "转化": "CONVERSION",
    "排名型": "RANKING",
    "提升排名": "RANKING",
    "利润型": "PROFIT",
    "利润": "PROFIT",
    "盈利型": "PROFIT",
}
TARGET_KEYWORD_MAP = {
    "大词": "GENERIC",
    "泛词": "GENERIC",
    "长尾词": "LONG_TAIL",
    "竞品词": "COMPETITOR",
    "品牌词": "BRAND",
    "自定义词": "CUSTOM",
}
AD_DIRECTION_MAP = {
    "推进自然位": "PUSH_NATURAL_RANK",
    "新增扩词": "EXPAND_KEYWORDS",
    "优化 ACOS": "OPTIMIZE_ACOS",
    "优化ACOS": "OPTIMIZE_ACOS",
    "平衡维持": "BALANCE_MAINTAIN",
}


PARENT_PRODUCT_STAGE_MAP = {
    "TESTING_PERIOD": "TESTING",
    "测试期": "TESTING",
    "STARTING_PERIOD": "PUSHING",
    "起步期": "PUSHING",
    "PROGRESS_PERIOD": "PUSHING",
    "进展期": "PUSHING",
    "SPRINT_PERIOD": "PUSHING",
    "冲刺期": "PUSHING",
    "COMPLETION_PERIOD": "HARVESTING",
    "达成期": "HARVESTING",
    "EXCEED_EXPECTATION": "HARVESTING",
    "超预期": "HARVESTING",
    "CLEAR_GOODS_ELIMINATE": "LIQUIDATING",
    "清货中/淘汰": "LIQUIDATING",
}
PARENT_SEASON_STAGE_MAP = {
    "OFF_SEASON": "OFF_SEASON",
    "淡季": "OFF_SEASON",
    "PEAK_SEASON_PREPARE": "PRE_PEAK",
    "旺季准备": "PRE_PEAK",
    "PEAK_SEASON": "PEAK",
    "BIG_PEAK_SEASON": "PEAK",
    "大旺季": "PEAK",
    "LATE_PEAK_SEASON": "LATE_PEAK",
    "旺季末期": "LATE_PEAK",
}


def _control_center_tags(
    profit: dict[str, Any],
    identity: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    source = profit.get("operating_tags") or {}
    single_mappings = {
        "productLevel": ("product_position", PRODUCT_LEVEL_MAP),
        "productStage": ("product_stage", PRODUCT_STAGE_MAP),
        "seasonStage": ("season_type", SEASON_STAGE_MAP),
    }
    # 中控 v1.0 多值协议：这三个标签用复数数组 + 小写业务编码
    list_mappings = {
        "adPurposes": ("advert_purposes", AD_PURPOSE_MAP),
        "targetKeywordStrategies": ("target_keyword_types", TARGET_KEYWORD_MAP),
        "adDirections": ("advert_direction_types", AD_DIRECTION_MAP),
    }
    tags: dict[str, Any] = {}
    unmapped: dict[str, str] = {}

    def _lookup(part: str, mapping: dict[str, str]) -> str | None:
        return mapping.get(part) or mapping.get(part.upper())

    def _parts(raw: str) -> list[str]:
        return [part.strip() for part in raw.replace("、", ",").split(",") if part.strip()]

    for target, (source_key, mapping) in single_mappings.items():
        raw = str(source.get(source_key) or "").strip()
        if not raw:
            continue
        parts = _parts(raw)
        mapped_values = {v for p in parts if (v := _lookup(p, mapping))}
        mapped = next(iter(mapped_values)) if len(mapped_values) == 1 else None
        if mapped:
            tags[target] = mapped
        else:
            unmapped[source_key] = raw

    for target, (source_key, mapping) in list_mappings.items():
        raw = str(source.get(source_key) or "").strip()
        if not raw:
            continue
        parts = _parts(raw)
        mapped_list = list(dict.fromkeys(v for p in parts if (v := _lookup(p, mapping))))
        if mapped_list:
            tags[target] = mapped_list
        if any(_lookup(p, mapping) is None for p in parts):
            unmapped[source_key] = raw

    parent_extension = (identity or {}).get("parent_extension") or {}
    parent_stage = str(
        parent_extension.get("progress_preparation_code")
        or parent_extension.get("progress_preparation")
        or ""
    ).strip().upper()
    parent_season = str(
        parent_extension.get("seasonality_code")
        or parent_extension.get("seasonality")
        or ""
    ).strip().upper()
    if parent_stage and "productStage" not in tags:
        # 产品阶段以 erp_listing_advert_agent_config 的 productStage 优先；
        # parent_extension 的 progress_preparation 只在广告配置缺失时兜底。
        mapped = PARENT_PRODUCT_STAGE_MAP.get(parent_stage) or PRODUCT_STAGE_MAP.get(parent_stage)
        if mapped:
            tags["productStage"] = mapped
        else:
            unmapped["parent_extension.progress_preparation"] = parent_stage
    if parent_season and "seasonStage" not in tags:
        # 淡旺季以 erp_listing_advert_agent_config 的 seasonType 优先；
        # parent_extension 的 seasonality 只在广告配置缺失时兜底。
        mapped = PARENT_SEASON_STAGE_MAP.get(parent_season) or SEASON_STAGE_MAP.get(parent_season)
        if mapped:
            tags["seasonStage"] = mapped
        else:
            unmapped["parent_extension.seasonality"] = parent_season
    return tags, unmapped


def _advert_mode_candidate(profit: dict[str, Any]) -> dict[str, Any] | None:
    source_name = str((profit.get("operating_tags") or {}).get("operating_mode") or "").strip()
    if not source_name:
        return None
    return {
        "sourceTool": "erp_listing_advert_agent_config",
        "sourceModeName": source_name,
    }


def _required_number(value: Any, field: str, missing_fields: list[str]) -> float | None:
    number = _number(value)
    if number is None:
        missing_fields.append(field)
    return number


def _latest_complete_daily_metric(
    rows: list[dict[str, Any]],
    *,
    before_date: date,
) -> dict[str, Any] | None:
    required_fields = ("revenue", "units", "ad_cost")
    candidates: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        try:
            metric_date = date.fromisoformat(str(row.get("stat_date") or ""))
        except ValueError:
            continue
        if metric_date >= before_date:
            continue
        if any(_number(row.get(field)) is None for field in required_fields):
            continue
        candidates.append((metric_date, row))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _latest_sources(
    connection: sa.Connection,
    *,
    source_batch_ids: list[str],
    expected_count: int,
    probe: bool,
    offset: int = 0,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.text(
            """
            WITH candidates AS (
                SELECT j.job_id,j.batch_id,j.shop_id,j.site_code,j.parent_asin,
                       j.parent_seller_sku,j.payload_json,j.result_ref,j.status,b.scope_json,
                       JSON_EXTRACT(j.payload_json, '$.binding.owner_user_ids')
                           AS binding_owner_user_ids,
                       JSON_EXTRACT(j.payload_json, '$.binding.shop_account')
                           AS binding_shop_account,
                       ROW_NUMBER() OVER (
                           PARTITION BY j.shop_id,j.parent_asin,j.parent_seller_sku
                           ORDER BY j.created_at DESC,j.job_id DESC
                       ) AS rank_no
                FROM t_patrol_job j
                JOIN t_patrol_batch b ON b.batch_id=j.batch_id
                WHERE j.batch_id IN :source_batch_ids
            )
            SELECT * FROM candidates WHERE rank_no=1
            ORDER BY parent_asin,shop_id
            """
        ).bindparams(bindparam("source_batch_ids", expanding=True)),
        {"source_batch_ids": source_batch_ids},
    ).mappings().all()
    selected = [dict(row) for row in rows]
    if len(selected) != expected_count:
        raise RuntimeError(
            f"expected {expected_count} latest operating units from explicit source batches, "
            f"found {len(selected)}"
        )
    successful = [
        row
        for row in selected
        if row["status"] == "SUCCEEDED" and row["result_ref"] is not None
    ]
    return successful[offset : offset + limit if limit is not None else None]


def _load_snapshot(connection: sa.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        sa.select(patrol_fact_snapshot).where(patrol_fact_snapshot.c.run_id == run_id)
    ).mappings().one()
    result = dict(row)
    for name in (
        "source_refs_json",
        "data_gaps_json",
        "normalized_summary_json",
        "raw_reference_json",
    ):
        result[name] = _json(result[name]) if result[name] is not None else None
    return result


def _load_signals(connection: sa.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.select(patrol_signal)
        .join(
            patrol_signal_occurrence,
            sa.and_(
                patrol_signal_occurrence.c.signal_id == patrol_signal.c.signal_id,
                patrol_signal_occurrence.c.run_id == run_id,
                patrol_signal_occurrence.c.occurred_at == patrol_signal.c.last_detected_at,
                patrol_signal_occurrence.c.occurrence_type.in_(
                    ("DETECTED", "DATA_GAP", "RECURRED")
                ),
            ),
        )
        .where(patrol_signal.c.last_scan_run_id == run_id)
        .order_by(patrol_signal.c.signal_type, patrol_signal.c.signal_id)
    ).mappings().all()
    signals = []
    for row in rows:
        item = dict(row)
        item["signal_payload_json"] = _json(item["signal_payload_json"])
        signals.append(item)
    return signals


def _load_enrichment(connection: sa.Connection, operating_unit_id: str) -> str | None:
    image_url = connection.execute(
        sa.select(patrol_product_image.c.image_url).where(
            patrol_product_image.c.operating_unit_id == operating_unit_id
        )
    ).scalar_one_or_none()
    return image_url


def _fact_snapshot(
    snapshot: dict[str, Any],
    snapshot_type: str,
    section: dict[str, Any],
) -> dict[str, Any]:
    inspection_date = snapshot["as_of"]
    data_cutoff = inspection_date - timedelta(days=1)
    stored_window_start = snapshot.get("window_start")
    stored_window_end = snapshot.get("window_end")
    window_start_date = (
        stored_window_start.date()
        if isinstance(stored_window_start, datetime)
        else data_cutoff - timedelta(days=29)
    )
    window_end_date = (
        stored_window_end.date()
        if isinstance(stored_window_end, datetime)
        else data_cutoff
    )
    window_start = datetime.combine(window_start_date, datetime.min.time(), tzinfo=UTC)
    window_end = datetime.combine(window_end_date, datetime.max.time(), tzinfo=UTC)
    metrics = {
        key: value
        for key, value in section.items()
        if isinstance(value, (str, int, float, bool)) and value is not None
    }
    return {
        "snapshotType": snapshot_type,
        "sourceSystem": "weilai-healthcheck-agent-v2",
        "windowStart": window_start.isoformat(),
        "windowEnd": window_end.isoformat(),
        "contentHash": sha256_json(
            {
                "sourceSnapshotId": snapshot["snapshot_id"],
                "snapshotType": snapshot_type,
                "section": section,
            }
        ),
        "qualityStatus": (
            "COMPLETE" if snapshot["quality_status"] == "COMPLETE" else "PARTIAL"
        ),
        "completenessScore": (
            float(snapshot["completeness_score"])
            if snapshot["completeness_score"] is not None
            else None
        ),
        "keyMetricsJson": metrics or {"sourceSnapshotId": snapshot["snapshot_id"]},
        "rawReferenceJson": {
            "sourceSnapshotId": snapshot["snapshot_id"],
            "sourceSnapshotHash": f"sha256:{snapshot['content_hash']}",
            "sourceRunId": snapshot["run_id"],
        },
    }


def _target(signal: dict[str, Any], operating_unit_id: str) -> tuple[str, str]:
    # 子 ASIN 异常已在规则适配层合并为经营单元级，统一指向经营单元；
    # 命中子体经 summary 传递（见 _anomalies_and_details）。
    return "OPERATING_UNIT", operating_unit_id


def _child_labels(
    child_asins: list[str], snapshot: dict[str, Any]
) -> list[str]:
    """把命中子体 ASIN 拼上颜色/尺码（如 B0XX（Black-XX-Large））。

    颜色/尺码来自快照 identity.children[].color / .size（上游 productColor/productSize）；
    子体缺失或字段为空时回退为纯 ASIN。
    """
    children = (
        (snapshot.get("normalized_summary_json") or {})
        .get("identity", {})
        .get("children")
        or []
    )
    by_asin: dict[str, dict[str, Any]] = {}
    for child in children:
        asin = child.get("child_asin") or child.get("asin")
        if asin:
            by_asin[asin] = child
    labels: list[str] = []
    for asin in child_asins:
        child = by_asin.get(asin) or {}
        color = (child.get("color") or "").strip() or None
        size = (child.get("size") or "").strip() or None
        if color and size:
            labels.append(f"{asin}（{color}-{size}）")
        elif color or size:
            labels.append(f"{asin}（{color or size}）")
        else:
            labels.append(asin)
    return labels


def _anomalies_and_details(
    signals: list[dict[str, Any]],
    snapshot: dict[str, Any],
    operating_unit_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    anomalies = []
    details = []
    recommendation_inputs: list[dict[str, Any]] = []
    business_signals = [item for item in signals if item["signal_type"] == "ANOMALY"]
    for index, signal in enumerate(business_signals, 1):
        issue_code = signal["issue_code"]
        try:
            category, anomaly_code = ISSUE_CODE_MAP[issue_code]
        except KeyError as exc:
            raise UnitNotSubmittable(
                operating_unit_id,
                [f"proposal.anomalies: unsupported issueCode={issue_code}"],
            ) from exc
        target_type, target_id = _target(signal, operating_unit_id)
        payload = signal["signal_payload_json"]
        diagnosis = payload.get("diagnosis") or {}
        child_scope = payload.get("child_scope") or {}
        child_asins = list(child_scope.get("child_asins") or [])
        anomaly_uid = f"{signal['signal_id']}-{index}"
        evidence_refs = list(payload.get("evidence_refs") or [snapshot["snapshot_id"]])
        summary = diagnosis.get("summary") or signal["issue_code"]
        if child_asins:
            summary = (
                f"{summary}（命中子体：{', '.join(_child_labels(child_asins, snapshot))}）"
            )
        recommendation_inputs.append({"issue_code": issue_code, "summary": summary})
        detail_recommendation = build_recommendation_single(
            {"issue_code": issue_code, "summary": summary}
        )
        anomalies.append(
            {
                "anomalyUid": anomaly_uid,
                "anomalyCategory": category,
                "anomalyCode": anomaly_code,
                "causeMode": "DIRECT" if diagnosis.get("known_causes") else "INVESTIGATE",
                "targetType": target_type,
                "targetId": target_id,
                "severity": SEVERITY_MAP.get(signal["severity"], "MEDIUM"),
                "summary": summary,
                "evidenceRefs": evidence_refs,
                "rootCauseStatus": (
                    "CONFIRMED" if diagnosis.get("known_causes") else "PENDING"
                ),
                "rootCauses": [
                    {"description": item} for item in diagnosis.get("known_causes") or []
                ],
                "detectedAt": signal["last_detected_at"].replace(tzinfo=UTC).isoformat(),
                "status": "PROPOSED",
            }
        )
        detail_current_value = build_current_value(issue_code, summary)
        details.append(
            {
                "anomalyUid": anomaly_uid,
                "domain": DOMAIN_MAP[category],
                "actionType": "OTHER",
                "targetType": target_type,
                "targetId": target_id,
                "currentValue": detail_current_value,
                "proposedValue": {
                    "action": "MANUAL_REVIEW_ONLY",
                    "autoExecutionAllowed": False,
                },
                "reason": detail_recommendation,
                "risk": {"automaticExecution": "PROHIBITED"},
                "evidenceRefs": evidence_refs,
                "boundaryCheck": {"passed": True, "autoExecutionAllowed": False},
                "approvalLevel": "H3",
                "reversible": True,
                "priority": index,
                "observationWindowDays": 7,
                "status": "PENDING_APPROVAL",
            }
        )

    if not anomalies:
        sales = (snapshot.get("normalized_summary_json") or {}).get("sales") or {}
        # 未设置月度/日均目标：只提示、不伪造「目标偏离」低级异常投中控。
        if sales.get("monthly_target") is None and sales.get("daily_target_orders") is None:
            raise UnitNotSubmittable(
                operating_unit_id,
                ["proposal.anomalies: monthly target not configured; tip only"],
            )
        anomaly_uid = f"DATA-GAP-{snapshot['run_id']}"
        anomalies.append(
            {
                "anomalyUid": anomaly_uid,
                "anomalyCategory": "TRANSACTION_PERFORMANCE",
                "anomalyCode": "TARGET_DEVIATION",
                "causeMode": "INVESTIGATE",
                "targetType": "OPERATING_UNIT",
                "targetId": operating_unit_id,
                "severity": "LOW",
                "summary": "数据缺口待修复，当前经营目标偏离无法完整判定",
                "evidenceRefs": [snapshot["snapshot_id"]],
                "rootCauseStatus": "PENDING",
                "rootCauses": [{"description": "巡检必需事实缺失，需补齐后复查"}],
                "detectedAt": datetime.combine(
                    snapshot["as_of"], datetime.min.time(), tzinfo=UTC
                ).isoformat(),
                "status": "PROPOSED",
            }
        )
        details.append(
            {
                "anomalyUid": anomaly_uid,
                "domain": "OTHER",
                "actionType": "OTHER",
                "targetType": "OPERATING_UNIT",
                "targetId": operating_unit_id,
                "proposedValue": {
                    "action": "REPAIR_DATA_AND_REVIEW",
                    "autoExecutionAllowed": False,
                },
                "reason": "本轮未检出可提交的业务异常，需补齐数据缺口后复查",
                "risk": {"automaticExecution": "PROHIBITED"},
                "evidenceRefs": [snapshot["snapshot_id"]],
                "boundaryCheck": {"passed": True, "autoExecutionAllowed": False},
                "approvalLevel": "H3",
                "reversible": True,
                "priority": 1,
                "observationWindowDays": 7,
                "status": "PENDING_APPROVAL",
            }
        )
    return anomalies, details, recommendation_inputs


def _build_unit(
    source: dict[str, Any],
    snapshot: dict[str, Any],
    signals: list[dict[str, Any]],
    current_operating_mode: CurrentOperatingMode | None = None,
):
    normalized = snapshot["normalized_summary_json"]
    identity = dict(normalized.get("identity") or {})
    sales = dict(normalized.get("sales") or {})
    profit = dict(normalized.get("profit") or {})
    inventory = dict(normalized.get("inventory") or {})
    price = dict(normalized.get("price") or {})
    quality = dict(normalized.get("quality") or {})
    operating_mode_code = None
    latest_rows = list(sales.get("daily_rows") or [])
    operating_unit_id = snapshot["operating_unit_id"]
    missing_fields: list[str] = []

    # 只取昨天（as_of - 1day），不回溯
    yesterday_str = (snapshot["as_of"] - timedelta(days=1)).isoformat()
    latest = _latest_complete_daily_metric(
        latest_rows,
        before_date=snapshot["as_of"],
    )
    # 但要求 stat_date 必须是昨天
    if latest is None or latest.get("stat_date") != yesterday_str:
        missing_fields.append("operatingMetric.yesterday_metric")
        raise UnitNotSubmittable(operating_unit_id, missing_fields)

    sales_amount = _number(latest.get("revenue"))
    if sales_amount is None:
        missing_fields.append("operatingMetric.salesAmount")
    ad_cost = _number(latest.get("ad_cost"))
    if ad_cost is None:
        missing_fields.append("operatingMetric.adCost")
    available_inventory = _number(inventory.get("fba_available"))
    if available_inventory is None:
        missing_fields.append("operatingMetric.canSaleNum")
    units = _number(latest.get("units"))

    # contributionProfit: 优先经营模式Agent，兜底同源 gross_profit_amount
    agent_unit_contribution = (
        _number(current_operating_mode.parent_unit_contribution)
        if current_operating_mode is not None
        else None
    )
    if agent_unit_contribution is not None and units is not None:
        contribution_profit = agent_unit_contribution * units
        contribution_source = "operating_mode_agent"
        contribution_currency = current_operating_mode.currency or "CNY"
    else:
        gross_profit_amount = _number(latest.get("gross_profit_amount"))
        if gross_profit_amount is not None:
            contribution_profit = gross_profit_amount
            contribution_source = "erp_listing_gross_profit_history.grossProfitAmount"
            contribution_currency = profit.get("currency") or "SITE_SETTLEMENT"
        else:
            missing_fields.append("operatingMetric.contributionProfit")
            raise UnitNotSubmittable(operating_unit_id, missing_fields)

    parent_seller_sku = source["parent_seller_sku"]
    if not parent_seller_sku:
        missing_fields.append("listing.parentSellerSku")
    if missing_fields:
        raise UnitNotSubmittable(operating_unit_id, missing_fields)
    tags, unmapped_tags = _control_center_tags(profit, identity)
    anomalies, details, recommendation_inputs = _anomalies_and_details(
        signals, snapshot, operating_unit_id
    )
    data_gaps = [
        {
            "code": item.get("code") or item.get("gap_code") or "UNKNOWN",
            "field": item.get("field"),
            "impact": item.get("impact"),
            "repairAction": item.get("repair_action"),
            "blocking": item.get("blocking"),
            "sourceTool": item.get("source_tool"),
        }
        for item in snapshot["data_gaps_json"] or []
    ]
    listing = {
        "shopId": str(source["shop_id"]),
        "siteCode": _to_platform_site_code(source["site_code"]),
        "parentAsin": source["parent_asin"],
        "parentSellerSku": parent_seller_sku,
        "productName": identity.get("product_name"),
        "productPicUrl": source.get("_cached_image_url")
        or (identity.get("storefront") or {}).get("main_image_url"),
        "status": (
            "ONLINE" if identity.get("sellable_child_count") else "OFFLINE"
        ) if identity.get("sellable_child_count") is not None else None,
        "businessModel": None,
        "shopAccount": str(source.get("shop_account") or source.get("binding_shop_account") or ""),
    }
    snapshot_owner_ids = _owner_user_ids(identity.get("owner_user_ids"))
    binding_owner_ids = _owner_user_ids(source.get("binding_owner_user_ids"))
    owner_ids = snapshot_owner_ids or binding_owner_ids
    owner_source = (
        identity.get("owner_source") or "product_info"
        if snapshot_owner_ids
        else "operating_unit_catalog_fallback"
        if binding_owner_ids
        else None
    )
    if not owner_ids:
        raise UnitNotSubmittable(operating_unit_id, ["listing.ownerUserId: 无负责人"])
    listing["ownerUserId"] = str(owner_ids[0])
    return {
        "listing": {key: value for key, value in listing.items() if value is not None},
        "tags": tags,
        "operatingMetric": {
            "metricDate": latest.get("stat_date"),
            "salesAmount": sales_amount,
            "salesQuantity": int(units) if units is not None else None,
            "orderQuantity": (
                int(value) if (value := _number(latest.get("orders"))) is not None else None
            ),
            "productCost": _number(profit.get("product_cost")),
            "azCommissionFee": _number(profit.get("az_commission_fee")),
            "fbaPackFee": _number(profit.get("fba_pack_fee")),
            "refundRate": _ratio_from_percent(
                quality.get("refund_rate_16w"),
                quality.get("refund_rate_32w"),
            ),
            "contributionProfit": contribution_profit,
            "profitRate": _number(latest.get("gross_margin")),
            "calculateMonthStorageFee": _number(inventory.get("monthly_storage_fee")),
            "adCost": ad_cost,
            "adClick": (
                int(value) if (value := _number(latest.get("ad_click"))) is not None else None
            ),
            "adImpressions": _number(latest.get("ad_impressions")),
            "adSalesAmount": _number(latest.get("ad_sales")),
            "adOrder": (
                int(value)
                if (value := _number(latest.get("ad_sale_num"))) is not None
                else None
            ),
            "canSaleNum": int(available_inventory),
        },
        "factSnapshots": [
            _fact_snapshot(snapshot, "SALES", sales),
            _fact_snapshot(snapshot, "PROFIT", profit),
            _fact_snapshot(snapshot, "INVENTORY", inventory),
            _fact_snapshot(snapshot, "PRICE", price),
            _fact_snapshot(snapshot, "LISTING", identity),
        ],
        "proposal": {
            "status": "PENDING_APPROVAL",
            "recommendedBusinessModel": operating_mode_code,
            "modeReasonSummary": (
                f"经营模式由经营模式 Agent 判定为{operating_mode_code}"
                if operating_mode_code
                else None
            ),
            "problemSummary": (
                "；".join(item["summary"] for item in anomalies)
                if anomalies
                else "本轮存在数据缺口，未检出可提交的业务异常"
            ),
            "dataGaps": data_gaps,
            "recommendationSummary": build_recommendations(recommendation_inputs),
            "goal": "确认巡检异常与数据缺口，并由运营人员决定后续动作",
            "riskSummary": {
                "automaticExecution": "PROHIBITED",
                "reason": "当前无价格 Agent，专业执行合同尚未冻结",
            },
            "confidence": 0.8 if any(s["signal_type"] == "ANOMALY" for s in signals) else 0.5,
            "approvalLevel": "H3",
            "observationWindowDays": 7,
            "rawAnalysis": {
                "realPatrolRunId": source["result_ref"],
                "realFactSnapshotId": snapshot["snapshot_id"],
                "factQualityStatus": snapshot["quality_status"],
                "factCompletenessScore": float(snapshot["completeness_score"] or 0),
                "businessSignalCount": sum(s["signal_type"] == "ANOMALY" for s in signals),
                "dataGapSignalCount": sum(s["signal_type"] == "DATA_QUALITY" for s in signals),
                "fieldCoverage": {
                    "operatingMetric": {
                        "required": {
                            "metricDate": "AVAILABLE",
                            "salesAmount": "AVAILABLE",
                            "contributionProfit": "AVAILABLE",
                            "adCost": "AVAILABLE",
                            "canSaleNum": "AVAILABLE",
                        },
                        "optionalNullFields": sorted(
                            key
                            for key, value in {
                                "orderQuantity": _number(latest.get("orders")),
                                "profitRate": _number(latest.get("gross_margin")),
                                "adClick": _number(latest.get("ad_click")),
                                "adImpressions": _number(latest.get("ad_impressions")),
                                "adSalesAmount": _number(latest.get("ad_sales")),
                                "adOrder": _number(latest.get("ad_sale_num")),
                            }.items()
                            if value is None
                        ),
                    },
                    "dataGapCount": len(data_gaps),
                },
                "simulatedFields": [],
                "ownerUserIds": owner_ids,
                "ownerSource": owner_source,
                "unmappedOperatingTags": unmapped_tags,
                "autoExecutionAllowed": False,
                "operatingMetricSelection": {
                    "selectedMetricDate": latest.get("stat_date"),
                    "inspectionDate": snapshot["as_of"].isoformat(),
                    "policy": "YESTERDAY_BEFORE_INSPECTION_DATE",
                },
                "operatingMode": _advert_mode_candidate(profit),
                "operatingModeCandidate": _advert_mode_candidate(profit),
                "operatingMetricContribution": {
                    "source": contribution_source,
                    "unitContribution": agent_unit_contribution,
                    "salesQuantity": int(units) if units is not None else None,
                    "contributionProfit": contribution_profit,
                    "currency": contribution_currency,
                },
            },
            "anomalies": anomalies,
            "details": details,
        },
    }


def build_request(
    patrol_batch_no: str,
    *,
    source_batch_ids: list[str],
    expected_count: int,
    probe: bool,
    offset: int = 0,
    limit: int | None = None,
    skip_unsubmittable: bool = True,
    advert_config_only: bool = False,
    operating_modes: dict[tuple[str, str, str], CurrentOperatingMode | None] | None = None,
) -> SubmitPatrolBatchRequest:
    engine = get_database_engine()
    units = []
    rejected: list[str] = []
    slice_after_filtering = skip_unsubmittable and not probe
    with engine.connect() as connection:
        for source in _latest_sources(
            connection,
            source_batch_ids=source_batch_ids,
            expected_count=expected_count,
            probe=False,
            offset=0 if slice_after_filtering else offset,
            limit=None if (probe and skip_unsubmittable) or slice_after_filtering else limit,
        ):
            snapshot = _load_snapshot(connection, source["result_ref"])
            signals = _load_signals(connection, source["result_ref"])
            image_url = _load_enrichment(
                connection,
                snapshot["operating_unit_id"],
            )
            source["_cached_image_url"] = image_url
            try:
                key = (
                    str(source["shop_id"]),
                    str(source["parent_asin"]).strip().upper(),
                    str(source["parent_seller_sku"]).strip(),
                )
                unit = _build_unit(
                    source,
                    snapshot,
                    signals,
                    None if operating_modes is None else operating_modes.get(key),
                )
                if advert_config_only:
                    candidate = (unit["proposal"].get("rawAnalysis") or {}).get(
                        "operatingModeCandidate"
                    ) or {}
                    if not str(candidate.get("sourceModeName") or "").strip():
                        continue
                units.append(unit)
            except UnitNotSubmittable as exc:
                rejected.append(str(exc))
            if probe and units:
                break
    if slice_after_filtering:
        units = units[offset : offset + limit if limit is not None else None]
    if rejected:
        logger.warning(
            "build_request: %d unit(s) rejected for missing required fields:\n- %s",
            len(rejected),
            "\n- ".join(rejected),
        )
    if rejected and not skip_unsubmittable:
        raise RuntimeError(
            "control-center v2 payload has missing or unrepresentable data:\n- "
            + "\n- ".join(rejected)
        )
    if not units:
        if skip_unsubmittable:
            raise EmptySubmittablePayload(
                "no successful unit satisfies the control-center contract"
            )
        raise RuntimeError("control-center payload contains no units")
    request = SubmitPatrolBatchRequest.model_validate(
        {"patrolBatchNo": patrol_batch_no, "units": units}
    )
    payload = request.model_dump(mode="json", by_alias=True)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)
    return request


def _structured_result(response: Any) -> dict[str, Any]:
    payload = response.model_dump(mode="json", by_alias=True)
    structured = payload.get("structuredContent") or payload.get("structured_content")
    if isinstance(structured, dict):
        return structured
    texts = [item.get("text") for item in payload.get("content", []) if item.get("type") == "text"]
    for value in texts:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    message = " | ".join(str(item) for item in texts if item)
    raise RuntimeError(f"control center returned no structured result: {message[:1000]}")


async def submit(url: str, token: str, request: SubmitPatrolBatchRequest) -> dict[str, Any]:
    async with httpx.AsyncClient(
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        timeout=httpx.Timeout(30, read=90),
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                response = await session.call_tool(
                    "submit_patrol_batch",
                    request.model_dump(mode="json", by_alias=True),
                )
    if response.is_error:
        texts = [getattr(item, "text", "") for item in response.content]
        raise RuntimeError("control center rejected request: " + " | ".join(texts)[:2000])
    result = SubmitPatrolBatchResult.model_validate(_structured_result(response))
    return result.model_dump(mode="json", by_alias=True)


async def _apply_operating_modes(request: SubmitPatrolBatchRequest) -> SubmitPatrolBatchRequest:
    client = OperatingModeMcpClient(
        os.environ.get("OPERATING_MODE_MCP_URL", "http://10.0.0.0:8001/mcp"),
        os.environ.get("OPERATING_MODE_MCP_TOKEN", ""),
        timeout_seconds=5,
    )
    logger.info("operating mode lookup starting for %d units", len(request.units))
    try:
        async def _evaluate_one(unit: PatrolUnitPayload) -> PatrolUnitPayload:
            listing = unit.listing
            try:
                # site_code 需要 AMAZON_US 格式（_to_platform_site_code 转成了 Amazon_US）
                raw_site = str(listing.site_code or "").upper().replace("AMAZON_", "")
                if raw_site and not raw_site.startswith("AMAZON_"):
                    raw_site = f"AMAZON_{raw_site}"
                current = await client.get_evaluate_by_key(
                    shop_id=listing.shop_id,
                    shop_account=listing.shop_account or "",
                    site_code=raw_site,
                    parent_asin=listing.parent_asin,
                    parent_seller_sku=listing.parent_seller_sku,
                )
            except Exception as exc:
                logger.warning(
                    "operating mode MCP error for %s/%s: %s",
                    listing.shop_id, listing.parent_asin, exc,
                )
                return attach_operating_mode_error(
                    unit,
                    error_code="MODE_AGENT_ERROR",
                    message=f"{type(exc).__name__}: {exc}",
                    source_tool="get_evaluate",
                )
            return apply_current_operating_mode(unit, current)

        enriched_units = await asyncio.gather(
            *(_evaluate_one(unit) for unit in request.units)
        )
        return request.model_copy(update={"units": list(enriched_units)})
    finally:
        await client.aclose()


async def _load_operating_modes(
    source_batch_ids: list[str],
    expected_count: int,
) -> dict[tuple[str, str, str], CurrentOperatingMode | None]:
    with get_database_engine().connect() as connection:
        sources = _latest_sources(
            connection,
            source_batch_ids=source_batch_ids,
            expected_count=expected_count,
            probe=False,
        )
    client = OperatingModeMcpClient(
        os.environ.get("OPERATING_MODE_MCP_URL", "http://10.0.0.0:8001/mcp"),
        os.environ.get("OPERATING_MODE_MCP_TOKEN", ""),
        timeout_seconds=5,
    )
    try:
        result: dict[tuple[str, str, str], CurrentOperatingMode | None] = {}
        for source in sources:
            key = (
                str(source["shop_id"]),
                str(source["parent_asin"]).strip().upper(),
                str(source["parent_seller_sku"]).strip(),
            )
            try:
                current = await client.get_evaluate_by_key(
                    shop_id=str(source["shop_id"]),
                    shop_account=str(
                        source.get("binding_shop_account") or source.get("shop_account") or ""
                    ).strip().strip("\""),
                    site_code=str(source.get("site_code") or "").strip(),
                    parent_asin=str(source["parent_asin"]).strip().upper(),
                    parent_seller_sku=str(source["parent_seller_sku"]).strip(),
                )
            except Exception as exc:
                logger.warning(
                    "operating mode MCP error for %s/%s: %s",
                    source.get("shop_id"), source.get("parent_asin"), exc,
                )
                current = None
            result[key] = current
        return result
    finally:
        await client.aclose()


def _apply_advert_config_only(request: SubmitPatrolBatchRequest) -> SubmitPatrolBatchRequest:
    return request.model_copy(
        update={"units": [apply_current_operating_mode(unit, None) for unit in request.units]}
    )


def _audit(request: SubmitPatrolBatchRequest, receipt: dict[str, Any], phase: str) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / f"{request.patrol_batch_no}-{phase}.json"
    document = {
        "recordedAt": datetime.now(UTC).isoformat(),
        "phase": phase,
        "patrolBatchNo": request.patrol_batch_no,
        "payloadHash": sha256_json(
            request.model_dump(mode="json", by_alias=True)
        ),
        "unitCount": len(request.units),
        "receipt": receipt,
    }
    path.write_text(canonical_json(document) + "\n", encoding="utf-8")
    return path


async def run(args: argparse.Namespace) -> int:
    operating_modes = (
        None
        if args.advert_config_only
        else await _load_operating_modes(args.source_batch, args.expected_count)
    )
    request = build_request(
        args.patrol_batch_no,
        source_batch_ids=args.source_batch,
        expected_count=args.expected_count,
        probe=args.phase == "probe",
        offset=args.offset,
        limit=args.limit,
        skip_unsubmittable=args.skip_unsubmittable,
        advert_config_only=args.advert_config_only,
        operating_modes=operating_modes,
    )
    request = (
        _apply_advert_config_only(request)
        if args.advert_config_only
        else await _apply_operating_modes(request)
    )
    request = SubmitPatrolBatchRequest.model_validate(
        request.model_dump(mode="json", by_alias=True)
    )
    print(f"validated phase={args.phase} units={len(request.units)}")
    token = os.environ.get("CONTROL_CENTER_MCP_TOKEN", "")
    if not token:
        raise RuntimeError("CONTROL_CENTER_MCP_TOKEN is required")
    receipt = await submit(args.url, token, request)
    path = _audit(request, receipt, args.phase)
    print(
        f"receipt received={receipt['receivedCount']} success={receipt['successCount']} "
        f"partial={receipt['partialSuccessCount']} failed={receipt['failedCount']}"
    )
    for item in receipt["results"]:
        print(
            f"unit asin={item['parentAsin']} status={item['status']} "
            f"code={item['code']} proposalId={item.get('proposalId') or '-'}"
        )
    print(f"audit={path.relative_to(ROOT)}")
    return 0 if receipt["failedCount"] == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit real patrol results to control center")
    parser.add_argument("--url", required=True)
    parser.add_argument("--patrol-batch-no", required=True)
    parser.add_argument("--phase", choices=("probe", "remaining"), required=True)
    parser.add_argument(
        "--source-batch",
        action="append",
        required=True,
        help="MySQL patrol batch to read; repeat only when deliberately merging batches",
    )
    parser.add_argument("--expected-count", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--skip-unsubmittable", action="store_true")
    parser.add_argument(
        "--advert-config-only",
        action="store_true",
        help="Use user-authorized advert_config.operatingMode fallback and skip OM Agent",
    )
    args = parser.parse_args()
    if args.expected_count < 1 or args.expected_count > 200:
        parser.error("--expected-count must be between 1 and 200")
    if args.offset < 0 or args.limit < 1 or args.limit > 50:
        parser.error("--offset must be non-negative and --limit between 1 and 50")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
