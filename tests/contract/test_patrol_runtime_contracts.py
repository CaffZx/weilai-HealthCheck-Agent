from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.contracts import EconomicExposure
from core.enums import (
    EconomicExposureWindow,
    InspectionRunStatus,
    TriggerType,
)
from core.inspection_run import InspectionRun
from core.result_envelope import InspectionResultEnvelope


def make_run(**overrides) -> InspectionRun:
    started_at = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
    values = {
        "run_id": "pr_0123456789abcdef01234567",
        "job_id": "job-1",
        "batch_id": "batch-1",
        "request_id": "request-1",
        "operating_unit_id": "ou_0123456789abcdef01234567",
        "trigger_type": TriggerType.DAILY_SCHEDULE,
        "status": InspectionRunStatus.COMPLETED,
        "rule_versions": {"R2": "2.0", "R3": "2.0"},
        "fact_snapshot_id": "fs_0123456789abcdef01234567",
        "signal_count": 0,
        "data_gap_count": 0,
        "started_at": started_at,
        "finished_at": started_at + timedelta(seconds=1),
    }
    values.update(overrides)
    return InspectionRun(**values)


def test_completed_run_requires_snapshot_and_finish_time():
    with pytest.raises(ValidationError, match="finished_at"):
        make_run(finished_at=None)
    with pytest.raises(ValidationError, match="fact_snapshot_id"):
        make_run(fact_snapshot_id=None)


def test_failed_run_requires_error_code():
    with pytest.raises(ValidationError, match="error_code"):
        make_run(
            status=InspectionRunStatus.FAILED,
            fact_snapshot_id=None,
            error_code=None,
        )


def test_money_is_decimal_and_rounds_half_up_to_four_places():
    exposure = EconomicExposure(
        currency="USD",
        amount="1.23455",
        window=EconomicExposureWindow.PER_DAY,
        calculation_basis="verification",
        as_of=datetime.now(UTC),
    )
    assert exposure.amount == Decimal("1.2346")
    assert exposure.model_dump(mode="json")["amount"] == 1.2346


def test_result_envelope_contains_only_patrol_outputs():
    run = make_run()
    InspectionResultEnvelope(
        run=run,
        signals=[],
        handoff_directives=[],
        producer_versions={"business-inspection": "2.0.0"},
        generated_at=run.finished_at,
    )
    fields = set(InspectionResultEnvelope.model_fields)
    assert fields == {
        "contract_version",
        "run",
        "signals",
        "handoff_directives",
        "producer_versions",
        "generated_at",
    }
    assert "decision_proposal" not in fields
    assert "mode_decision" not in fields
    assert "advertising_proposal" not in fields


def test_result_envelope_rejects_signal_count_mismatch():
    with pytest.raises(ValidationError, match="signal_count"):
        InspectionResultEnvelope(
            run=make_run(signal_count=1),
            signals=[],
            handoff_directives=[],
            producer_versions={"business-inspection": "2.0.0"},
            generated_at=datetime.now(UTC),
        )
