from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from core.enums import Severity, SignalState, SignalType
from inspector.signal_builder import SignalBuilder
from inspector.signal_reconciler import SignalMutationKind, SignalReconciler, StoredSignal
from tests.unit.test_signal_builder import make_anomaly, make_snapshot


def candidate(
    unit,
    *,
    issue="INVENTORY_SHORTAGE",
    severity=Severity.S1,
):
    return SignalBuilder(producer_version="test").build(
        anomaly=make_anomaly("库存不足", issue, severity),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_0123456789abcdef01234567",
    )


def stored_from(signal, **overrides):
    values = {
        "signal_id": f"is_{uuid4().hex[:24]}",
        "issue_code": signal.issue_code,
        "child_scope_key": "",
        "signal_state": SignalState.NEW,
        "first_detected_at": signal.first_detected_at - timedelta(days=1),
        "last_detected_at": signal.last_detected_at - timedelta(days=1),
        "recurrence_count": 0,
        "consecutive_miss_count": 0,
        "version": 1,
        "payload": signal,
    }
    values.update(overrides)
    return StoredSignal(**values)


def test_first_hit_creates_new_signal(unit):
    signal = candidate(unit)
    result = SignalReconciler().reconcile(
        existing=[],
        candidates=[signal],
        facts_complete=True,
        occurred_at=datetime.now(UTC),
        scan_run_id="pr_0123456789abcdef01234567",
    )
    mutation = result.mutations[0]
    assert mutation.kind is SignalMutationKind.INSERT
    assert mutation.occurrence_type == "DETECTED"
    assert mutation.signal_state is SignalState.NEW
    assert mutation.recurrence_count == 0


def test_active_hit_is_continuing_not_recurrence(unit):
    signal = candidate(unit)
    existing = stored_from(
        signal,
        signal_state=SignalState.IN_PROGRESS,
        consecutive_miss_count=1,
    )
    result = SignalReconciler().reconcile(
        existing=[existing],
        candidates=[signal],
        facts_complete=True,
        occurred_at=datetime.now(UTC),
        scan_run_id="pr_0123456789abcdef01234567",
    )
    mutation = result.mutations[0]
    assert mutation.occurrence_type == "DETECTED"
    assert mutation.signal_state is SignalState.IN_PROGRESS
    assert mutation.consecutive_miss_count == 0


def test_resolved_hit_is_recurrence(unit):
    signal = candidate(unit)
    existing = stored_from(
        signal,
        signal_state=SignalState.RESOLVED,
        recurrence_count=2,
        version=4,
    )
    result = SignalReconciler().reconcile(
        existing=[existing],
        candidates=[signal],
        facts_complete=True,
        occurred_at=datetime.now(UTC),
        scan_run_id="pr_0123456789abcdef01234567",
    )
    mutation = result.mutations[0]
    assert mutation.occurrence_type == "RECURRED"
    assert mutation.signal_state is SignalState.NEW
    assert mutation.recurrence_count == 3
    assert mutation.version == 5


def test_resolved_data_quality_hit_keeps_data_quality_type(unit):
    signal = candidate(
        unit,
        issue="MCP_STOCK_UNAVAILABLE",
        severity=Severity.DATA_INSUFFICIENT,
    ).model_copy(update={"signal_type": SignalType.DATA_QUALITY})
    existing = stored_from(signal, signal_state=SignalState.RESOLVED)

    result = SignalReconciler().reconcile(
        existing=[existing],
        candidates=[signal],
        facts_complete=False,
        occurred_at=datetime.now(UTC),
        scan_run_id="pr_0123456789abcdef01234567",
    )

    assert result.mutations[0].occurrence_type == "RECURRED"
    assert result.signals[0].signal_type is SignalType.DATA_QUALITY


def test_two_complete_misses_are_required_for_recovery(unit):
    signal = candidate(unit)
    first = stored_from(signal, consecutive_miss_count=0)
    first_result = SignalReconciler().reconcile(
        existing=[first],
        candidates=[],
        facts_complete=True,
        occurred_at=datetime.now(UTC),
        scan_run_id="pr_0123456789abcdef01234567",
    ).mutations[0]
    assert first_result.occurrence_type == "MISSED"
    assert first_result.signal_state is SignalState.NEW
    assert first_result.consecutive_miss_count == 1

    second = stored_from(signal, consecutive_miss_count=1, version=2)
    second_result = SignalReconciler().reconcile(
        existing=[second],
        candidates=[],
        facts_complete=True,
        occurred_at=datetime.now(UTC),
        scan_run_id="pr_0123456789abcdef01234567",
    ).mutations[0]
    assert second_result.occurrence_type == "RECOVERED"
    assert second_result.signal_state is SignalState.RESOLVED
    assert second_result.consecutive_miss_count == 2


def test_data_gap_does_not_increment_miss_count(unit):
    signal = candidate(unit)
    existing = stored_from(signal, consecutive_miss_count=1)
    mutation = SignalReconciler().reconcile(
        existing=[existing],
        candidates=[],
        facts_complete=False,
        occurred_at=datetime.now(UTC),
        scan_run_id="pr_0123456789abcdef01234567",
    ).mutations[0]
    assert mutation.occurrence_type == "DATA_GAP"
    assert mutation.signal_state is SignalState.NEW
    assert mutation.consecutive_miss_count == 1


def test_disappeared_data_quality_signal_recovers_immediately(unit):
    signal = candidate(
        unit,
        issue="MCP_STOCK_UNAVAILABLE",
        severity=Severity.DATA_INSUFFICIENT,
    ).model_copy(update={"signal_type": SignalType.DATA_QUALITY})
    existing = stored_from(signal, consecutive_miss_count=0)

    mutation = SignalReconciler().reconcile(
        existing=[existing],
        candidates=[],
        facts_complete=False,
        occurred_at=datetime.now(UTC),
        scan_run_id="pr_0123456789abcdef01234567",
    ).mutations[0]

    assert mutation.occurrence_type == "RECOVERED"
    assert mutation.signal_state is SignalState.RESOLVED
    assert mutation.consecutive_miss_count == 1


def test_closed_non_hit_is_unchanged(unit):
    signal = candidate(unit)
    existing = stored_from(signal, signal_state=SignalState.RESOLVED)
    result = SignalReconciler().reconcile(
        existing=[existing],
        candidates=[],
        facts_complete=True,
        occurred_at=datetime.now(UTC),
        scan_run_id="pr_0123456789abcdef01234567",
    )
    assert result.mutations == ()
