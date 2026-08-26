"""巡检 Agent 当前运行所需的严格数据模型。"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

from core.enums import (
    CONTRACT_VERSION_HANDOFF,
    CONTRACT_VERSION_INSPECTION,
    CONTRACT_VERSION_OPERATING_UNIT,
    ActionTimingStatus,
    CapabilityCode,
    ChildExceptionType,
    ConstraintFlag,
    DataQualityStatus,
    EconomicExposureWindow,
    ExecutionReadiness,
    HumanAttention,
    PermittedNextStep,
    ReasonCode,
    RoutingMode,
    Severity,
    SignalState,
    SignalType,
)
from core.operating_unit import derive_operating_unit_id, normalize_identity


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_json(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


SignalId = Annotated[str, Field(pattern=r"^is_[0-9a-f]{24}$")]
HandoffId = Annotated[str, Field(pattern=r"^hd_[0-9a-f]{24}$")]
OperatingUnitId = Annotated[str, Field(pattern=r"^ou_[0-9a-f]{24}$")]
IssueCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")]
MissingInputCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")]


class OperatingUnitRef(StrictModel):
    """由店铺、父 ASIN 和父 Seller SKU 唯一派生的经营单元身份。"""

    contract_version: Literal["amazon_ops.v2"] = CONTRACT_VERSION_OPERATING_UNIT
    operating_unit_id: OperatingUnitId
    shop_id: int = Field(ge=1)
    site_code: str = Field(pattern=r"^AMAZON_[A-Z0-9_-]{2,24}$")
    parent_asin: str = Field(min_length=1, max_length=32)
    parent_seller_sku: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _check_derived_id(self) -> OperatingUnitRef:
        expected = derive_operating_unit_id(
            self.shop_id,
            self.parent_asin,
            self.parent_seller_sku,
        )
        if self.operating_unit_id != expected:
            raise ValueError(
                "operating_unit_id must be derived from shop_id, parent_asin and "
                "parent_seller_sku; "
                f"expected {expected}"
            )
        return self

    @classmethod
    def derive(
        cls,
        *,
        shop_id: Any,
        site_code: Any,
        parent_asin: Any,
        parent_seller_sku: Any = None,
    ) -> OperatingUnitRef:
        shop_id_norm, site_norm, asin_norm, sku_norm = normalize_identity(
            shop_id,
            site_code,
            parent_asin,
            parent_seller_sku,
        )
        return cls(
            operating_unit_id=derive_operating_unit_id(shop_id_norm, asin_norm, sku_norm),
            shop_id=shop_id_norm,
            site_code=site_norm,
            parent_asin=asin_norm,
            parent_seller_sku=sku_norm,
        )


class HandoffDirective(StrictModel):
    contract_version: Literal["amazon_ops.handoff.v2"] = CONTRACT_VERSION_HANDOFF
    handoff_id: HandoffId
    source_result_ref: str = Field(min_length=1)
    target_ref: OperatingUnitRef
    next_capability: CapabilityCode
    reason_code: ReasonCode
    requested_outcome: str = Field(min_length=1)
    required_input_refs: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    missing_input_codes: list[MissingInputCode] = Field(default_factory=list)
    action_timing_status: ActionTimingStatus
    human_attention: HumanAttention
    permitted_next_step: PermittedNextStep
    routing_mode: RoutingMode
    expires_at: datetime | None = None
    producer_versions: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _advertising_is_human_only(self) -> HandoffDirective:
        if (
            self.next_capability == CapabilityCode.ADVERTISING_DECISION_EXECUTION
            and self.routing_mode != RoutingMode.HUMAN_ONLY
        ):
            raise ValueError(
                "advertising capability is FROZEN: routing_mode must be HUMAN_ONLY"
            )
        return self


class ChildScope(StrictModel):
    child_asins: list[str] = Field(min_length=1)
    exception_type: ChildExceptionType


class EconomicExposure(StrictModel):
    currency: str | None = None
    amount: Annotated[
        Decimal,
        Field(max_digits=18, decimal_places=4),
        PlainSerializer(lambda value: float(value), return_type=float, when_used="json"),
    ] | None = None
    window: EconomicExposureWindow
    calculation_basis: str
    as_of: datetime

    @field_validator("amount", mode="before")
    @classmethod
    def _quantize_amount(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @model_validator(mode="after")
    def _null_amount_is_unassessed(self) -> EconomicExposure:
        if self.amount is None and self.window != EconomicExposureWindow.UNASSESSED:
            raise ValueError("amount=None requires window=UNASSESSED (do not fabricate zero)")
        return self


class Diagnosis(StrictModel):
    summary: str
    known_causes: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class InspectionSignal(StrictModel):
    contract_version: Literal["amazon_ops.inspection.v2"] = CONTRACT_VERSION_INSPECTION
    signal_id: SignalId
    operating_unit_ref: OperatingUnitRef
    child_scope: ChildScope | None = None
    signal_type: SignalType
    issue_code: IssueCode
    severity: Severity
    signal_state: SignalState
    action_timing_status: ActionTimingStatus
    economic_exposure: EconomicExposure
    constraint_flags: list[ConstraintFlag]
    execution_readiness: ExecutionReadiness
    first_detected_at: datetime
    last_detected_at: datetime
    last_scan_run_id: str = Field(min_length=1)
    recurrence_count: int = Field(ge=0)
    evidence_refs: list[str] = Field(min_length=1)
    diagnosis: Diagnosis
    handoff_directive: HandoffDirective
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def _check_references(self) -> InspectionSignal:
        if len(set(self.constraint_flags)) != len(self.constraint_flags):
            raise ValueError("constraint_flags must be unique")
        if self.handoff_directive.target_ref.operating_unit_id != (
            self.operating_unit_ref.operating_unit_id
        ):
            raise ValueError("handoff_directive.target_ref must match the signal operating unit")
        return self


class SourceRef(StrictModel):
    tool_name: str
    request_hash: str
    fetched_at: datetime
    window_start: date | None = None
    window_end: date | None = None
    content_hash: str
    quality_status: DataQualityStatus = DataQualityStatus.COMPLETE
    latency_ms: int | None = None
    warning: str | None = None


class DataGap(StrictModel):
    code: IssueCode
    field: str
    impact: str
    repair_action: str
    blocking: bool
    source_tool: str | None = None


class OperatingFactSnapshot(StrictModel):
    snapshot_id: str = Field(pattern=r"^fs_[0-9a-f]{24}$")
    version: int = Field(default=1, ge=1)
    operating_unit: OperatingUnitRef
    as_of_time: datetime
    content_hash: str
    quality_status: DataQualityStatus
    completeness_score: float = Field(ge=0, le=1)
    identity: dict[str, Any] = Field(default_factory=dict)
    sales: dict[str, Any] = Field(default_factory=dict)
    traffic: dict[str, Any] = Field(default_factory=dict)
    profit: dict[str, Any] = Field(default_factory=dict)
    inventory: dict[str, Any] = Field(default_factory=dict)
    price: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    execution_history: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[SourceRef] = Field(default_factory=list)
    data_gaps: list[DataGap] = Field(default_factory=list)

    @property
    def is_blocking(self) -> bool:
        return self.quality_status in {
            DataQualityStatus.INSUFFICIENT,
            DataQualityStatus.FAILED,
        }

    @property
    def blocking_gap_codes(self) -> list[str]:
        return sorted({gap.code for gap in self.data_gaps if gap.blocking})


class AnomalySignal(StrictModel):
    """R2/R3 命中后、转换为共享 InspectionSignal 前的内部表示。"""

    signal_id: SignalId
    category: str
    point_code: str
    issue_code: IssueCode
    target_type: Literal["PARENT_ASIN", "CHILD_ASIN"]
    target_id: str
    child_asins: list[str] = Field(default_factory=list)
    severity: Severity
    signal_type: SignalType = SignalType.ANOMALY
    description: str
    reason: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(min_length=1)
    lifecycle_status: SignalState = SignalState.NEW
    detected_by: Literal["R2", "R3", "QUALITY"] = "R2"


__all__ = [
    "AnomalySignal",
    "ChildScope",
    "DataGap",
    "Diagnosis",
    "EconomicExposure",
    "HandoffDirective",
    "InspectionSignal",
    "OperatingFactSnapshot",
    "OperatingUnitRef",
    "SourceRef",
    "StrictModel",
    "canonical_json",
    "new_id",
    "sha256_json",
    "utc_now",
]
