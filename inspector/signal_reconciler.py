from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from core.contracts import InspectionSignal
from core.enums import SignalState, SignalType
from integrations.repositories.patrol import _child_scope_key

ACTIVE_STATES = {
    SignalState.NEW,
    SignalState.PENDING_CONFIRMATION,
    SignalState.IN_PROGRESS,
    SignalState.OBSERVING,
    SignalState.LONG_TERM_FOLLOW_UP,
    SignalState.AWAITING_RESCAN,
}

CLOSED_STATES = {
    SignalState.RESOLVED,
    SignalState.FALSE_POSITIVE,
    SignalState.IGNORED,
    SignalState.INTERRUPTED,
}


class SignalMutationKind(StrEnum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"


@dataclass(frozen=True, slots=True)
class StoredSignal:
    signal_id: str
    issue_code: str
    child_scope_key: str
    signal_state: SignalState
    first_detected_at: datetime
    last_detected_at: datetime
    recurrence_count: int
    consecutive_miss_count: int
    version: int
    payload: InspectionSignal

    @property
    def business_key(self) -> tuple[str, str]:
        return self.issue_code, self.child_scope_key


@dataclass(frozen=True, slots=True)
class SignalMutation:
    kind: SignalMutationKind
    signal_id: str
    occurrence_type: str
    signal_state: SignalState
    recurrence_count: int
    consecutive_miss_count: int
    version: int
    candidate: InspectionSignal | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    signals: tuple[InspectionSignal, ...]
    mutations: tuple[SignalMutation, ...]


class SignalReconciler:
    def reconcile(
        self,
        *,
        existing: list[StoredSignal],
        candidates: list[InspectionSignal],
        facts_complete: bool,
        occurred_at: datetime,
        scan_run_id: str,
    ) -> ReconciliationResult:
        existing_by_key = {item.business_key: item for item in existing}
        if len(existing_by_key) != len(existing):
            raise ValueError("stored signals contain duplicate business keys")
        candidate_by_key = {
            (item.issue_code, _child_scope_key(item)): item for item in candidates
        }
        if len(candidate_by_key) != len(candidates):
            raise ValueError("candidate signals contain duplicate business keys")

        output_signals: list[InspectionSignal] = []
        mutations: list[SignalMutation] = []

        for key in sorted(candidate_by_key):
            candidate = candidate_by_key[key]
            stored = existing_by_key.get(key)
            if stored is None:
                signal = candidate.model_copy(
                    update={
                        "signal_state": SignalState.NEW,
                        "recurrence_count": 0,
                        "first_detected_at": candidate.first_detected_at,
                        "last_detected_at": occurred_at,
                        "last_scan_run_id": scan_run_id,
                    }
                )
                output_signals.append(signal)
                mutations.append(
                    SignalMutation(
                        kind=SignalMutationKind.INSERT,
                        signal_id=signal.signal_id,
                        occurrence_type=(
                            "DATA_GAP"
                            if signal.signal_type is SignalType.DATA_QUALITY
                            else "DETECTED"
                        ),
                        signal_state=SignalState.NEW,
                        recurrence_count=0,
                        consecutive_miss_count=0,
                        version=1,
                        candidate=signal,
                        occurred_at=occurred_at,
                    )
                )
                continue

            recurrence = stored.signal_state in CLOSED_STATES
            next_state = SignalState.NEW if recurrence else stored.signal_state
            recurrence_count = stored.recurrence_count + (1 if recurrence else 0)
            signal = candidate.model_copy(
                update={
                    "signal_id": stored.signal_id,
                    "signal_state": next_state,
                    "first_detected_at": stored.first_detected_at,
                    "last_detected_at": occurred_at,
                    "last_scan_run_id": scan_run_id,
                    "recurrence_count": recurrence_count,
                    "signal_type": (
                        candidate.signal_type
                        if candidate.signal_type is SignalType.DATA_QUALITY
                        else (
                            SignalType.RECURRENCE
                            if recurrence
                            else candidate.signal_type
                        )
                    ),
                }
            )
            output_signals.append(signal)
            mutations.append(
                SignalMutation(
                    kind=SignalMutationKind.UPDATE,
                    signal_id=stored.signal_id,
                    occurrence_type=(
                        "RECURRED"
                        if recurrence
                        else (
                            "DATA_GAP"
                            if signal.signal_type is SignalType.DATA_QUALITY
                            else "DETECTED"
                        )
                    ),
                    signal_state=next_state,
                    recurrence_count=recurrence_count,
                    consecutive_miss_count=0,
                    version=stored.version + 1,
                    candidate=signal,
                    occurred_at=occurred_at,
                )
            )

        for key in sorted(existing_by_key.keys() - candidate_by_key.keys()):
            stored = existing_by_key[key]
            if stored.signal_state not in ACTIVE_STATES:
                continue
            if stored.payload.signal_type is SignalType.DATA_QUALITY:
                signal = stored.payload.model_copy(
                    update={
                        "signal_state": SignalState.RESOLVED,
                        "signal_type": SignalType.RECOVERY,
                        "last_scan_run_id": scan_run_id,
                    }
                )
                mutations.append(
                    SignalMutation(
                        kind=SignalMutationKind.UPDATE,
                        signal_id=stored.signal_id,
                        occurrence_type="RECOVERED",
                        signal_state=SignalState.RESOLVED,
                        recurrence_count=stored.recurrence_count,
                        consecutive_miss_count=stored.consecutive_miss_count + 1,
                        version=stored.version + 1,
                        candidate=signal,
                        occurred_at=occurred_at,
                    )
                )
                continue
            if not facts_complete:
                signal = stored.payload.model_copy(
                    update={"last_scan_run_id": scan_run_id}
                )
                mutations.append(
                    SignalMutation(
                        kind=SignalMutationKind.UPDATE,
                        signal_id=stored.signal_id,
                        occurrence_type="DATA_GAP",
                        signal_state=stored.signal_state,
                        recurrence_count=stored.recurrence_count,
                        consecutive_miss_count=stored.consecutive_miss_count,
                        version=stored.version + 1,
                        candidate=signal,
                        occurred_at=occurred_at,
                    )
                )
                continue
            miss_count = stored.consecutive_miss_count + 1
            recovered = miss_count >= 2
            signal = stored.payload.model_copy(
                update={
                    "signal_state": (
                        SignalState.RESOLVED if recovered else stored.signal_state
                    ),
                    "signal_type": (
                        SignalType.RECOVERY if recovered else stored.payload.signal_type
                    ),
                    "last_scan_run_id": scan_run_id,
                }
            )
            mutations.append(
                SignalMutation(
                    kind=SignalMutationKind.UPDATE,
                    signal_id=stored.signal_id,
                    occurrence_type="RECOVERED" if recovered else "MISSED",
                    signal_state=(
                        SignalState.RESOLVED if recovered else stored.signal_state
                    ),
                    recurrence_count=stored.recurrence_count,
                    consecutive_miss_count=miss_count,
                    version=stored.version + 1,
                    candidate=signal,
                    occurred_at=occurred_at,
                )
            )

        return ReconciliationResult(
            signals=tuple(
                mutation.candidate
                for mutation in mutations
                if mutation.candidate is not None
            ),
            mutations=tuple(mutations),
        )
