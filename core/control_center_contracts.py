from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from core.contracts import DataGap, OperatingFactSnapshot, StrictModel, sha256_json
from core.operating_unit import OperatingUnitBinding

if TYPE_CHECKING:
    from clients.operating_mode_mcp import CurrentOperatingMode


class ControlCenterModel(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
    )


BusinessModel = Literal[
    "NEW_PRODUCT_VALIDATION",
    "TIME_BOXED_REPAIR",
    "STABLE_OPERATION",
    "CONTROLLED_GROWTH",
    "PROFIT_HARVEST",
    "CONTROLLED_CLEARANCE",
    "IMMEDIATE_EXIT",
]


class PatrolListing(ControlCenterModel):
    shop_id: str = Field(min_length=1)
    site_code: str = Field(min_length=1)
    parent_asin: str = Field(min_length=1)
    parent_seller_sku: str = Field(min_length=1)
    product_name: str | None = None
    product_pic_url: str | None = None
    owner_user_id: str | None = Field(default=None, min_length=1)
    status: Literal["ONLINE", "OFFLINE", "DISABLED"] | None = None
    business_model: BusinessModel | None = None
    shop_account: str = Field(default="", exclude=True)

    @classmethod
    def from_binding(
        cls,
        binding: OperatingUnitBinding,
        **listing_fields: Any,
    ) -> PatrolListing:
        owner_user_id = (
            str(binding.owner_user_ids[0])
            if len(binding.owner_user_ids) == 1
            else None
        )
        return cls(
            shop_id=str(binding.shop_id),
            site_code=binding.site_code,
            parent_asin=binding.parent_asin,
            parent_seller_sku=binding.parent_seller_sku or "",
            shop_account=binding.shop_account or "",
            owner_user_id=owner_user_id,
            **listing_fields,
        )


class PatrolTags(ControlCenterModel):
    product_level: Literal["P0_PRODUCT", "P1_PRODUCT", "P2_PRODUCT", "P3_PRODUCT"] | None = None
    product_stage: (
        Literal["TESTING", "PUSHING", "HARVESTING", "MAINTAINING", "LIQUIDATING"] | None
    ) = None
    season_stage: Literal["OFF_SEASON", "PRE_PEAK", "PEAK", "LATE_PEAK"] | None = None
    ad_purposes: list[Literal["TRAFFIC", "CONVERSION", "RANKING", "PROFIT"]] | None = None
    target_keyword_strategies: (
        list[Literal["GENERIC", "LONG_TAIL", "COMPETITOR", "BRAND", "CUSTOM"]] | None
    ) = None
    ad_directions: (
        list[
            Literal[
                "PUSH_NATURAL_RANK",
                "EXPAND_KEYWORDS",
                "OPTIMIZE_ACOS",
                "BALANCE_MAINTAIN",
            ]
        ]
        | None
    ) = None
    ad_budget_group: (
        Literal[
            "EXACT_CORE_GROUP",
            "EXACT_TESTING_GROUP",
            "AUTO_BROAD_GROUP",
            "LOW_BID_RETENTION_GROUP",
        ]
        | None
    ) = None
    special_scenario: (
        Literal[
            "PROMOTION",
            "INVENTORY_LOW",
            "BAD_REVIEWS",
            "LISTING_OPTIMIZATION",
            "URGENT_LIQUIDATION",
        ]
        | None
    ) = None
    placement: Literal["TOP_OF_SEARCH", "PRODUCT_PAGES", "REST_OF_SEARCH"] | None = None

class OperatingMetric(ControlCenterModel):
    metric_date: date
    sales_amount: float = Field(ge=0)
    sales_quantity: int | None = Field(default=None, ge=0)
    order_quantity: int | None = Field(default=None, ge=0)
    product_cost: float | None = Field(default=None, ge=0)
    az_commission_fee: float | None = Field(default=None, ge=0)
    fba_pack_fee: float | None = Field(default=None, ge=0)
    refund_rate: float | None = Field(default=None, ge=0, le=1)
    contribution_profit: float
    profit_rate: float | None = None
    calculate_month_storage_fee: float | None = Field(default=None, ge=0)
    ad_cost: float = Field(ge=0)
    ad_click: int | None = Field(default=None, ge=0)
    ad_impressions: float | None = Field(default=None, ge=0)
    ad_sales_amount: float | None = Field(default=None, ge=0)
    ad_order: int | None = Field(default=None, ge=0)
    available_inventory: int = Field(ge=0, alias="canSaleNum")

    @field_validator("metric_date", mode="before")
    @classmethod
    def _validate_metric_date_format(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            raise ValueError("metricDate must use yyyy-MM-dd")
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("metricDate must use yyyy-MM-dd") from exc
            if parsed.isoformat() != value:
                raise ValueError("metricDate must use yyyy-MM-dd")
        return value


class FactSnapshotPayload(ControlCenterModel):
    snapshot_type: Literal[
        "SALES", "PROFIT", "ADVERTISING", "PRICE", "INVENTORY", "LISTING", "OPERATING_UNIT"
    ]
    source_system: str = Field(min_length=1)
    window_start: datetime | None = None
    window_end: datetime | None = None
    content_hash: str | None = Field(default=None, min_length=1)
    quality_status: Literal["COMPLETE", "PARTIAL", "INVALID"] | None = None
    completeness_score: float | None = Field(default=None, ge=0, le=100)
    key_metrics_json: dict[str, Any] | None = None
    raw_reference_json: dict[str, Any] | None = None

    @property
    def business_key(self) -> tuple[str, str]:
        return self.snapshot_type, self.source_system

    @model_validator(mode="after")
    def _validate_window(self) -> FactSnapshotPayload:
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end < self.window_start
        ):
            raise ValueError("windowEnd must not be earlier than windowStart")
        return self


TargetType = Literal[
    "OPERATING_UNIT",
    "CAMPAIGN",
    "AD_GROUP",
    "KEYWORD",
    "PRODUCT_TARGET",
    "ASIN",
    "SKU",
    "LISTING",
    "OTHER",
]


class PatrolAnomaly(ControlCenterModel):
    anomaly_uid: str = Field(min_length=1)
    anomaly_category: Literal[
        "LINK_STATUS",
        "CONTENT_INTEGRITY",
        "PRICE_PROMOTION",
        "INVENTORY_AVAILABILITY",
        "TRANSACTION_PERFORMANCE",
        "COMPLIANCE_ATTRIBUTE",
        "AFTER_SALES",
    ]
    anomaly_code: Literal[
        "LISTING_UNSELLABLE",
        "VARIATION_DROPPED",
        "PARENT_CHILD_RELATION_ABNORMAL",
        "BUY_BOX_LOST",
        "MAIN_IMAGE_ABNORMAL",
        "IMAGE_ABNORMAL",
        "TITLE_ABNORMAL",
        "BULLET_POINTS_ABNORMAL",
        "A_PLUS_ABNORMAL",
        "VARIATION_PRICE_GAP_ABNORMAL",
        "PROMOTION_ABNORMAL",
        "FBA_AVAILABLE_INVENTORY_ZERO",
        "INVENTORY_SHORTAGE",
        "INVENTORY_OVERSTOCK",
        "SLOW_MOVING_INVENTORY",
        "LISTING_CONVERSION_RATE_DROP",
        "SALES_ABNORMAL",
        "ACOS_ABNORMAL",
        "AD_SPEND_ABNORMAL",
        "TARGET_DEVIATION",
        "ORGANIC_TRAFFIC_ABNORMAL",
        "RANKING_ABNORMAL",
        "SCALING_NOT_EXECUTED",
        "CATEGORY_ABNORMAL",
        "ATTRIBUTE_INFO_ABNORMAL",
        "COMPLIANCE_ABNORMAL",
        "CONCENTRATED_NEGATIVE_REVIEWS",
        "RATING_ABNORMAL",
        "REFUND_ABNORMAL",
    ]
    cause_mode: Literal["DIRECT", "INVESTIGATE"]
    target_type: TargetType
    target_id: str = Field(min_length=1)
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    summary: str = Field(min_length=1)
    metric: dict[str, Any] | None = None
    threshold: dict[str, Any] | None = None
    evidence_refs: list[str] | None = None
    root_cause_status: Literal[
        "NOT_REQUIRED", "PENDING", "INVESTIGATING", "CONFIRMED", "UNCERTAIN"
    ]
    root_causes: list[dict[str, Any]] | None = None
    detected_at: datetime
    status: Literal[
        "DETECTED",
        "CAUSE_PENDING",
        "CAUSE_CONFIRMED",
        "PROPOSED",
        "PROCESSING",
        "RESOLVED",
        "CLOSED",
    ]

    @field_validator("anomaly_uid", "target_id", "summary")
    @classmethod
    def _reject_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class ProposalDetail(ControlCenterModel):
    anomaly_uid: str = Field(min_length=1)
    domain: Literal[
        "ADVERTISING", "PRICE", "INVENTORY", "LISTING", "CLEARANCE", "REPLENISHMENT", "OTHER"
    ]
    action_type: Literal[
        "ADJUST_BUDGET",
        "ADJUST_BID",
        "ENABLE_AD",
        "PAUSE_AD",
        "ADJUST_PRICE",
        "CREATE_PROMOTION",
        "REPLENISH",
        "STOP_REPLENISH",
        "CLEAR_INVENTORY",
        "UPDATE_LISTING",
        "OTHER",
    ]
    target_type: TargetType
    target_id: str = Field(min_length=1)
    current_value: dict[str, Any] | None = None
    proposed_value: dict[str, Any] | None = None
    reason: str | None = Field(default=None, min_length=1)
    expected_effect: dict[str, Any] | None = None
    risk: dict[str, Any] | None = None
    evidence_refs: list[str] | None = None
    boundary_check: dict[str, Any] | None = None
    approval_level: Literal["H0", "H1", "H2", "H3"] | None = None
    reversible: bool | None = None
    priority: int | None = None
    observation_window_days: int | None = Field(default=None, ge=1)
    status: (
        Literal[
            "PENDING_APPROVAL",
            "APPROVED",
            "RETURNED",
            "REJECTED",
            "EXECUTING",
            "SUCCEEDED",
            "FAILED",
            "EXPIRED",
            "CANCELLED",
        ]
        | None
    ) = "PENDING_APPROVAL"

    @field_validator("anomaly_uid", "target_id", "reason")
    @classmethod
    def _reject_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def _validate_evidence_refs(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [reference.strip() for reference in value]
        if any(not reference for reference in normalized):
            raise ValueError("evidenceRefs must not contain blank references")
        if len(normalized) != len(set(normalized)):
            raise ValueError("evidenceRefs must not contain duplicates")
        return normalized

    @property
    def business_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.anomaly_uid,
            self.domain,
            self.action_type,
            self.target_type,
            self.target_id.casefold(),
        )


class PatrolProposal(ControlCenterModel):
    status: (
        Literal[
            "PENDING_APPROVAL",
            "PARTIALLY_APPROVED",
            "APPROVED",
            "REJECTED",
            "EXECUTING",
            "COMPLETED",
            "EXPIRED",
            "CLOSED",
        ]
        | None
    ) = "PENDING_APPROVAL"
    recommended_business_model: BusinessModel | None = None
    mode_confidence: float | None = Field(default=None, ge=0, le=1)
    mode_reason_summary: str | None = None
    problem_summary: str | None = Field(default=None, min_length=1)
    root_causes: list[dict[str, Any]] | None = None
    opportunity_summary: str | None = None
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    data_gaps: list[dict[str, Any]] = Field(default_factory=list)
    recommendation_summary: str | None = Field(default=None, min_length=1)
    goal: str | None = Field(default=None, min_length=1)
    expected_effect: dict[str, Any] | None = None
    risk_summary: dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    approval_level: Literal["H0", "H1", "H2", "H3"] | None = None
    observation_window_days: int | None = Field(default=None, ge=1)
    review_due_at: datetime | None = None
    raw_analysis: dict[str, Any] | None = None
    anomalies: list[PatrolAnomaly] = Field(min_length=1)
    details: list[ProposalDetail] = Field(min_length=1)

    @field_validator("problem_summary", "recommendation_summary", "goal")
    @classmethod
    def _reject_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @model_validator(mode="after")
    def _validate_mode_and_details(self) -> PatrolProposal:
        if self.mode_reason_summary is not None:
            self.mode_reason_summary = self.mode_reason_summary.strip()
            if not self.mode_reason_summary:
                raise ValueError("modeReasonSummary must not be blank")

        keys = [detail.business_key for detail in self.details]
        if len(keys) != len(set(keys)):
            raise ValueError("proposal details must not contain duplicate anomalyUid values")

        anomaly_uids = [anomaly.anomaly_uid for anomaly in self.anomalies]
        if len(anomaly_uids) != len(set(anomaly_uids)):
            raise ValueError("proposal anomalies must not contain duplicate anomalyUid values")
        unknown_uids = {detail.anomaly_uid for detail in self.details} - set(anomaly_uids)
        if unknown_uids:
            unknown = ", ".join(sorted(unknown_uids))
            raise ValueError(f"proposal details reference unknown anomalyUid values: {unknown}")
        return self


class PatrolUnitPayload(ControlCenterModel):
    listing: PatrolListing
    tags: PatrolTags
    operating_metric: OperatingMetric
    fact_snapshots: list[FactSnapshotPayload] = Field(min_length=1)
    proposal: PatrolProposal

    @property
    def business_key(self) -> tuple[str, str, str]:
        return (
            self.listing.shop_id,
            self.listing.parent_asin,
            self.listing.parent_seller_sku,
        )

    @model_validator(mode="after")
    def _validate_fact_and_proposal_consistency(self) -> PatrolUnitPayload:
        keys = [snapshot.business_key for snapshot in self.fact_snapshots]
        if len(keys) != len(set(keys)):
            raise ValueError("factSnapshots must not contain duplicate snapshotType/sourceSystem")

        return self


MODE_AGENT_TO_CONTROL_CENTER: dict[str, BusinessModel] = {
    "NEW_PRODUCT_VALIDATION": "NEW_PRODUCT_VALIDATION",
    "IMMEDIATE_EXIT": "IMMEDIATE_EXIT",
    "CONTROLLED_CLEARANCE": "CONTROLLED_CLEARANCE",
    "TIME_BOXED_REPAIR": "TIME_BOXED_REPAIR",
    "STABLE_OPERATION": "STABLE_OPERATION",
    "ACTIVE_ADVANCE": "CONTROLLED_GROWTH",
    "PROFIT_HARVEST": "PROFIT_HARVEST",
}

ADVERT_MODE_NAME_TO_CODE: dict[str, str] = {
    "立即退出": "IMMEDIATE_EXIT",
    "控制清货": "CONTROLLED_CLEARANCE",
    "限时修复": "TIME_BOXED_REPAIR",
    "稳定经营": "STABLE_OPERATION",
    "积极推进": "ACTIVE_ADVANCE",
    "获取利润": "PROFIT_HARVEST",
}

LISTING_STATUS_MAP: dict[str, Literal["ONLINE", "OFFLINE", "DISABLED"]] = {
    "可售": "ONLINE",
    "在售": "ONLINE",
    "正常": "ONLINE",
    "在售中": "ONLINE",
    "不可售": "OFFLINE",
    "已下架": "DISABLED",
}


def _parent_listing_status(snapshot: Any, parent_asin: str) -> str | None:
    identity = getattr(snapshot, "identity", None) or {}
    target = parent_asin.strip().upper()
    for child in identity.get("children") or []:
        child_asin = str(child.get("child_asin") or child.get("asin") or "").strip().upper()
        if child_asin == target:
            raw_status = child.get("status")
            mapped = LISTING_STATUS_MAP.get(str(raw_status or "").strip()) if raw_status else None
            return mapped
    return None


def _product_pic_url_from_snapshot(snapshot: Any) -> str | None:
    identity = getattr(snapshot, "identity", None) or {}
    storefront = identity.get("storefront") or {}
    url = storefront.get("main_image_url")
    return str(url).strip() if url else None


def _to_platform_site_code(internal: str) -> str:
    """Convert internal canonical site_code (AMAZON_US) to control-center PlatformSiteCode
    (Amazon_US). CC enforces mixed-case prefix; internal storage uses uppercase for
    deduplication. Non-AMAZON_ prefixes pass through unchanged."""
    value = str(internal or "").strip()
    if value.startswith("AMAZON_"):
        return "Amazon_" + value[len("AMAZON_"):]
    return value


def build_patrol_listing(
    binding: OperatingUnitBinding,
    snapshot: Any,
    *,
    product_pic_url_cache: str | None = None,
) -> PatrolListing:
    """Build PatrolListing from a fact snapshot, honoring real status + product image.

    Reads listing status from the parent row of snapshot.identity.children (Chinese →
    ONLINE/OFFLINE/DISABLED). Product image prefers snapshot.identity.storefront.main_image_url;
    falls back to a caller-provided cache value (e.g., from t_patrol_product_image).
    site_code is converted to CC PlatformSiteCode format (Amazon_US).
    """
    identity = getattr(snapshot, "identity", None) or {}
    status = _parent_listing_status(snapshot, binding.parent_asin)
    pic_url = _product_pic_url_from_snapshot(snapshot) or product_pic_url_cache
    listing = PatrolListing.from_binding(
        binding,
        product_name=identity.get("product_name"),
        product_pic_url=pic_url,
        status=status,
        shop_account=binding.shop_account or "",
    )
    return listing.model_copy(update={"site_code": _to_platform_site_code(listing.site_code)})


def derive_advert_mode_candidate(snapshot: Any) -> dict[str, Any] | None:
    """Extract advert_config.operatingMode (Chinese) into an operatingModeCandidate dict.

    Returned dict, when placed into proposal.raw_analysis.operatingModeCandidate, is picked
    up by apply_advert_config_fallback() when the authoritative OM Agent does not return
    DECIDED. Returns None when advert_config did not supply an operatingMode value.
    """
    profit = getattr(snapshot, "profit", None) or {}
    tags = profit.get("operating_tags") or {}
    name = str(tags.get("operating_mode") or "").strip()
    if not name or name not in ADVERT_MODE_NAME_TO_CODE:
        return None
    return {
        "sourceSystem": "advert-agent-decision",
        "sourceTool": "erp_listing_advert_agent_config",
        "sourceModeName": name,
        "sourceModeCode": ADVERT_MODE_NAME_TO_CODE[name],
    }


def apply_advert_config_fallback(
    unit: PatrolUnitPayload,
    current: CurrentOperatingMode | None,
) -> PatrolUnitPayload:
    """[DEPRECATED] 广告配置回落已停用。

    自 2026-08-10 起，经营模式 Agent 失败时不再回落到 advert_config，
    而是保留原 unit（不做回落）。本函数保留仅供审计参考，不应被调用。
    """
    if current is not None and current.decision_status == "DECIDED":
        return unit
    raw_analysis = dict(unit.proposal.raw_analysis or {})
    candidate = dict(raw_analysis.get("operatingModeCandidate") or {})
    source_name = str(candidate.get("sourceModeName") or "").strip()
    source_mode = ADVERT_MODE_NAME_TO_CODE.get(source_name)
    if source_mode is None:
        return unit
    business_model = MODE_AGENT_TO_CONTROL_CENTER[source_mode]
    reason = f"经营模式 Agent 未返回 DECIDED，使用 advert_config.operatingMode 兜底：{source_name}"
    raw_analysis["operatingMode"] = {
        "sourceSystem": "advert-agent-decision",
        "sourceTool": "erp_listing_advert_agent_config",
        "sourceType": "ADVERT_CONFIG_FALLBACK",
        "sourceModeName": source_name,
        "sourceModeCode": source_mode,
        "controlCenterBusinessModel": business_model,
        "operatingModeAgentStatus": current.decision_status if current else "NO_RECORD",
        "fallbackReason": reason,
    }
    return unit.model_copy(update={
        "listing": unit.listing.model_copy(update={"business_model": business_model}),
        "proposal": unit.proposal.model_copy(update={
            "recommended_business_model": business_model,
            "mode_reason_summary": reason,
            "raw_analysis": raw_analysis,
        }),
    })


def apply_current_operating_mode(
    unit: PatrolUnitPayload,
    current: CurrentOperatingMode | None,
) -> PatrolUnitPayload:
    if current is not None and current.parent_unit_contribution is not None:
        sales_quantity = unit.operating_metric.sales_quantity
        if sales_quantity is not None:
            unit_contribution = float(current.parent_unit_contribution)
            contribution_profit = unit_contribution * sales_quantity
            raw_analysis = dict(unit.proposal.raw_analysis or {})
            raw_analysis["operatingMetricContribution"] = {
                "source": "operating_mode_agent",
                "unitContribution": unit_contribution,
                "salesQuantity": sales_quantity,
                "contributionProfit": contribution_profit,
                "currency": current.currency or "CNY",
            }
            unit = unit.model_copy(update={
                "operating_metric": unit.operating_metric.model_copy(
                    update={"contribution_profit": contribution_profit}
                ),
                "proposal": unit.proposal.model_copy(
                    update={"raw_analysis": raw_analysis}
                ),
            })
    if current is None or current.decision_status != "DECIDED":
        return unit  # 不做回落，经营模式 Agent 无 DECIDED 结果时直接返回原 unit
    source_mode = current.recommended_mode_code
    if source_mode is None:
        raise ValueError("DECIDED operating mode has no mode code")
    business_model = MODE_AGENT_TO_CONTROL_CENTER[source_mode]
    for existing in (
        unit.listing.business_model,
        unit.proposal.recommended_business_model,
    ):
        if existing is not None and existing != business_model:
            raise ValueError("operating mode conflicts with an existing control-center value")
    reason = current.explanation.strip()
    if not reason:
        raise ValueError("DECIDED operating mode requires a non-empty explanation")
    raw_analysis = dict(unit.proposal.raw_analysis or {})
    raw_analysis["operatingMode"] = {
        "sourceSystem": "amazon-operating-mode-agent",
        "sourceTool": "get_current_operating_mode",
        "sourceModeCode": source_mode,
        "controlCenterBusinessModel": business_model,
        "decisionStatus": current.decision_status,
        "explanation": reason,
    }
    return unit.model_copy(
        update={
            "listing": unit.listing.model_copy(
                update={"business_model": business_model}
            ),
            "proposal": unit.proposal.model_copy(
                update={
                    "recommended_business_model": business_model,
                    "mode_confidence": None,
                    "mode_reason_summary": reason,
                    "raw_analysis": raw_analysis,
                }
            ),
        }
    )


def attach_operating_mode_error(
    unit: PatrolUnitPayload,
    *,
    error_code: str,
    message: str,
    source_tool: str = "start_operating_mode_evaluation_job",
) -> PatrolUnitPayload:
    """Attach an operating-mode-agent evaluation failure to a patrol unit payload.

    The failure is recorded both in ``rawAnalysis.operatingModeError`` and as a
    ``dataGaps`` entry so the control center can see why authoritative operating
    mode data is missing even when the advert-config fallback was applied.
    """
    raw_analysis = dict(unit.proposal.raw_analysis or {})
    raw_analysis["operatingModeError"] = {
        "sourceSystem": "amazon-operating-mode-agent",
        "sourceTool": source_tool,
        "errorCode": error_code,
        "errorMessage": message,
    }
    data_gaps = list(unit.proposal.data_gaps or [])
    gap = {
        "code": error_code or "OPERATING_MODE_AGENT_ERROR",
        "field": "operatingMode",
        "impact": "经营模式 Agent 评估失败，缺少权威经营模式",
        "repairAction": "经营模式 Agent 侧修复同步抓取/判断链路后重新评估",
        "blocking": False,
        "sourceTool": source_tool,
    }
    if gap not in data_gaps:
        data_gaps.append(gap)
    return unit.model_copy(
        update={
            "proposal": unit.proposal.model_copy(
                update={
                    "raw_analysis": raw_analysis,
                    "data_gaps": data_gaps,
                }
            )
        }
    )


class SubmitPatrolBatchRequest(ControlCenterModel):
    patrol_batch_no: str = Field(min_length=1)
    units: list[PatrolUnitPayload] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _reject_duplicates(self) -> SubmitPatrolBatchRequest:
        keys = [unit.business_key for unit in self.units]
        if len(keys) != len(set(keys)):
            raise ValueError("units must not contain duplicate business keys")
        return self


class PatrolUnitReceipt(ControlCenterModel):
    shop_id: str
    parent_asin: str
    parent_seller_sku: str
    status: Literal["SUCCESS", "PARTIAL_SUCCESS", "FAILED"]
    code: str
    listing_id: str | None = None
    proposal_id: str | None = None
    message: str


class SubmitPatrolBatchResult(ControlCenterModel):
    patrol_batch_no: str
    received_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    partial_success_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    results: list[PatrolUnitReceipt]

    @model_validator(mode="after")
    def _validate_counts(self) -> SubmitPatrolBatchResult:
        total = self.success_count + self.partial_success_count + self.failed_count
        if self.received_count != total or self.received_count != len(self.results):
            raise ValueError("batch receipt counts do not match results")
        statuses = Counter(item.status for item in self.results)
        expected = {
            "SUCCESS": self.success_count,
            "PARTIAL_SUCCESS": self.partial_success_count,
            "FAILED": self.failed_count,
        }
        if any(statuses.get(status, 0) != count for status, count in expected.items()):
            raise ValueError("batch receipt status counts do not match results")
        keys = [(item.shop_id, item.parent_asin, item.parent_seller_sku) for item in self.results]
        if len(keys) != len(set(keys)):
            raise ValueError("batch receipt results must not contain duplicate business keys")
        return self


class ReviewCriterion(ControlCenterModel):
    criterion_id: str = Field(min_length=1)
    metric_path: str = Field(min_length=1)
    operator: Literal["GTE", "LTE", "EQ"]
    target: float | int | str | bool


class ReviewRequest(ControlCenterModel):
    contract_version: Literal["amazon_ops.patrol_review_request.v1"] = (
        "amazon_ops.patrol_review_request.v1"
    )
    review_request_id: str = Field(min_length=1, max_length=128)
    review_round: int = Field(ge=1)
    patrol_batch_no: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    baseline_snapshot_id: str = Field(pattern=r"^fs_[0-9a-f]{24}$")
    shop_id: int = Field(ge=1)
    shop_account: str = Field(min_length=1)
    site_code: str = Field(min_length=1)
    parent_asin: str = Field(min_length=1)
    parent_seller_sku: str = Field(min_length=1, max_length=128)
    approved_plan: dict[str, Any]
    execution_receipts: list[dict[str, Any]] = Field(default_factory=list)
    success_criteria: list[ReviewCriterion] = Field(default_factory=list)
    failure_criteria: list[ReviewCriterion] = Field(default_factory=list)
    requested_at: datetime

    @property
    def binding(self) -> OperatingUnitBinding:
        return OperatingUnitBinding(
            shop_id=self.shop_id,
            shop_account=self.shop_account,
            site_code=self.site_code,
            parent_asin=self.parent_asin,
            parent_seller_sku=self.parent_seller_sku,
        )

    def request_hash(self) -> str:
        return sha256_json(
            self.model_dump(mode="json", by_alias=True, exclude={"requested_at"})
        )


class CriterionResult(ControlCenterModel):
    criterion_id: str
    metric_path: str
    operator: Literal["GTE", "LTE", "EQ"]
    target: float | int | str | bool
    actual: Any = None
    evaluation_status: Literal["MET", "NOT_MET", "NOT_EVALUATED"]
    missing_reason: str | None
    met: bool | None

    @model_validator(mode="after")
    def _validate_evaluation(self) -> CriterionResult:
        expected = {
            "MET": True,
            "NOT_MET": False,
            "NOT_EVALUATED": None,
        }[self.evaluation_status]
        if self.met is not expected:
            raise ValueError("met must match evaluationStatus")
        if self.evaluation_status == "NOT_EVALUATED":
            if self.actual is not None or not self.missing_reason:
                raise ValueError(
                    "NOT_EVALUATED requires actual=null and a missingReason"
                )
        elif self.missing_reason is not None:
            raise ValueError("evaluated criteria must use missingReason=null")
        return self


class ReviewResult(ControlCenterModel):
    contract_version: Literal["amazon_ops.patrol_review_result.v1"] = (
        "amazon_ops.patrol_review_result.v1"
    )
    review_request_id: str
    review_round: int
    patrol_batch_no: str
    proposal_id: str
    baseline_snapshot_id: str
    current_fact_snapshot: OperatingFactSnapshot
    owner_user_name: str | None = Field(default=None, min_length=1)
    outcome_status: Literal["SUCCESS", "PARTIAL_SUCCESS", "FAILED", "INCONCLUSIVE"]
    success_results: list[CriterionResult]
    failure_results: list[CriterionResult]
    data_gaps: list[DataGap]
    execution_deviations: list[dict[str, Any]]
    requires_new_patrol: bool
    next_recommendation: Literal["CLOSE_CYCLE", "START_NEW_PATROL_CYCLE", "REPAIR_DATA"]
    reviewed_at: datetime
