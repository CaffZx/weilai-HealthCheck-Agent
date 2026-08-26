from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from core.contracts import (
    HandoffDirective,
    InspectionSignal,
    StrictModel,
    sha256_json,
)
from core.enums import (
    CONTRACT_VERSION_INSPECTION_FEEDBACK,
    CONTRACT_VERSION_INSPECTION_RESULT,
    FeedbackEventType,
)
from core.inspection_run import InspectionRun


class InspectionResultEnvelope(StrictModel):
    contract_version: Literal["amazon_ops.inspection_result.v1"] = (
        CONTRACT_VERSION_INSPECTION_RESULT
    )
    run: InspectionRun
    signals: list[InspectionSignal] = Field(default_factory=list)
    handoff_directives: list[HandoffDirective] = Field(default_factory=list)
    producer_versions: dict[str, str] = Field(min_length=1)
    generated_at: datetime

    @model_validator(mode="after")
    def _validate_references(self) -> InspectionResultEnvelope:
        if self.run.signal_count != len(self.signals):
            raise ValueError("run.signal_count must equal the number of signals")
        signal_ids = [signal.signal_id for signal in self.signals]
        if len(set(signal_ids)) != len(signal_ids):
            raise ValueError("signals must not contain duplicate signal_id values")
        unit_ids = {
            signal.operating_unit_ref.operating_unit_id for signal in self.signals
        }
        if unit_ids - {self.run.operating_unit_id}:
            raise ValueError("all signals must belong to the run operating unit")
        if any(signal.last_scan_run_id != self.run.run_id for signal in self.signals):
            raise ValueError("all signals must reference the envelope run")
        embedded_handoffs = {
            signal.handoff_directive.handoff_id for signal in self.signals
        }
        explicit_handoffs = {item.handoff_id for item in self.handoff_directives}
        if len(explicit_handoffs) != len(self.handoff_directives):
            raise ValueError("handoff_directives must not contain duplicates")
        if explicit_handoffs != embedded_handoffs:
            raise ValueError("handoff_directives must match signal handoffs exactly")
        return self

    def content_hash(self) -> str:
        return sha256_json(
            self.model_dump(mode="json", exclude={"generated_at"})
        ).removeprefix("sha256:")


class InspectionFeedbackEvent(StrictModel):
    contract_version: Literal["amazon_ops.inspection_feedback.v1"] = (
        CONTRACT_VERSION_INSPECTION_FEEDBACK
    )
    event_id: str = Field(min_length=1, max_length=128)
    signal_id: str = Field(pattern=r"^is_[0-9a-f]{24}$")
    signal_version: int = Field(ge=1)
    event_type: FeedbackEventType
    source_system: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    action_receipt_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def payload_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json")).removeprefix("sha256:")


class RejectedObject(StrictModel):
    object_id: str = Field(min_length=1, max_length=128)
    reason_code: str = Field(min_length=1, max_length=64)
    message: str | None = Field(default=None, max_length=2000)


class InspectionResultAck(StrictModel):
    event_id: str = Field(min_length=1, max_length=128)
    accepted: bool
    accepted_object_ids: list[str] = Field(default_factory=list)
    rejected_objects: list[RejectedObject] = Field(default_factory=list)
    duplicate: bool
    received_at: datetime

    @model_validator(mode="after")
    def _validate_acceptance(self) -> InspectionResultAck:
        if len(set(self.accepted_object_ids)) != len(self.accepted_object_ids):
            raise ValueError("accepted_object_ids must be unique")
        rejected_ids = [item.object_id for item in self.rejected_objects]
        if len(set(rejected_ids)) != len(rejected_ids):
            raise ValueError("rejected_objects must have unique object_id values")
        if self.accepted and self.rejected_objects:
            raise ValueError("accepted ACK must not contain rejected_objects")
        if self.accepted and not self.accepted_object_ids:
            raise ValueError("accepted ACK requires accepted_object_ids")
        return self
