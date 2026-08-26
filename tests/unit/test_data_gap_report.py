from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa

from integrations.repositories.tables import (
    metadata,
    patrol_signal,
    patrol_signal_occurrence,
)
from scripts.report_real_patrol_data_gaps import _signals


def _signal_values(
    signal_id: str,
    issue_code: str,
    *,
    last_detected_at: datetime,
    run_id: str,
) -> dict:
    return {
        "signal_id": signal_id,
        "dedup_key": signal_id.ljust(64, "0")[:64],
        "operating_unit_id": "ou_0123456789abcdef01234567",
        "issue_code": issue_code,
        "child_scope_key": "PARENT",
        "signal_type": "ANOMALY",
        "severity": "S1",
        "signal_state": "NEW",
        "action_timing_status": "DUE_SOON",
        "economic_window": "UNASSESSED",
        "constraint_flags_json": [],
        "execution_readiness": "READY_HUMAN",
        "diagnosis_json": {},
        "signal_payload_json": {},
        "first_detected_at": last_detected_at,
        "last_detected_at": last_detected_at,
        "last_scan_run_id": run_id,
        "recurrence_count": 0,
        "consecutive_miss_count": 0,
        "version": 1,
    }


def test_report_only_includes_current_detected_occurrences():
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    run_id = "pr_report0123456789abcdef012"
    detected_at = datetime(2026, 8, 4, 1, 0)
    rescan_at = datetime(2026, 8, 5, 1, 0)
    with engine.begin() as connection:
        connection.execute(sa.insert(patrol_signal), [
            _signal_values(
                "is_current0123456789abcdef01",
                "CURRENT_ISSUE",
                last_detected_at=rescan_at,
                run_id=run_id,
            ),
            _signal_values(
                "is_missed0123456789abcdef012",
                "MISSED_ISSUE",
                last_detected_at=detected_at,
                run_id=run_id,
            ),
        ])
        connection.execute(sa.insert(patrol_signal_occurrence), [
            {
                "occurrence_id": "oc-current",
                "signal_id": "is_current0123456789abcdef01",
                "run_id": run_id,
                "occurrence_type": "DETECTED",
                "evidence_refs_json": [],
                "details_json": {},
                "occurred_at": rescan_at,
            },
            {
                "occurrence_id": "oc-missed",
                "signal_id": "is_missed0123456789abcdef012",
                "run_id": run_id,
                "occurrence_type": "MISSED",
                "evidence_refs_json": [],
                "details_json": {},
                "occurred_at": rescan_at,
            },
        ])

        assert [item["issue_code"] for item in _signals(connection, run_id)] == [
            "CURRENT_ISSUE"
        ]
