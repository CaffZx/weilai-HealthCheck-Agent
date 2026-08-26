from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from core.contracts import OperatingUnitId, StrictModel
from core.enums import (
    CONTRACT_VERSION_INSPECTION_RUN,
    InspectionRunStatus,
    TriggerType,
)

TERMINAL_RUN_STATUSES = {
    InspectionRunStatus.COMPLETED,
    InspectionRunStatus.COMPLETED_WITH_GAPS,
    InspectionRunStatus.BLOCKED,
    InspectionRunStatus.FAILED,
}


class InspectionRun(StrictModel):
    contract_version: Literal["amazon_ops.inspection_run.v1"] = (
        CONTRACT_VERSION_INSPECTION_RUN
    )
    run_id: str = Field(pattern=r"^pr_[0-9a-f]{24}$")
    job_id: str = Field(min_length=1, max_length=64)
    batch_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=191)
    operating_unit_id: OperatingUnitId
    trigger_type: TriggerType
    status: InspectionRunStatus
    rule_versions: dict[str, str] = Field(min_length=1)
    fact_snapshot_id: str | None = Field(default=None, pattern=r"^fs_[0-9a-f]{24}$")
    signal_count: int = Field(default=0, ge=0)
    data_gap_count: int = Field(default=0, ge=0)
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = None

    @model_validator(mode="after")
    def _validate_terminal_state(self) -> InspectionRun:
        is_terminal = self.status in TERMINAL_RUN_STATUSES
        if is_terminal and self.finished_at is None:
            raise ValueError("terminal run status requires finished_at")
        if not is_terminal and self.finished_at is not None:
            raise ValueError("non-terminal run status must not carry finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.status is InspectionRunStatus.FAILED and not self.error_code:
            raise ValueError("FAILED run requires error_code")
        if self.status is not InspectionRunStatus.FAILED and (
            self.error_code is not None or self.error_message is not None
        ):
            raise ValueError("only FAILED run may carry error details")
        if self.status in {
            InspectionRunStatus.COMPLETED,
            InspectionRunStatus.COMPLETED_WITH_GAPS,
        } and self.fact_snapshot_id is None:
            raise ValueError("completed run requires fact_snapshot_id")
        return self
