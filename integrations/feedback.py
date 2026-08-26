from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
import yaml
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from core.enums import (
    FeedbackEventType,
    FeedbackInboxStatus,
    SignalState,
)
from core.result_envelope import InspectionFeedbackEvent
from inspector.signal_reconciler import ACTIVE_STATES
from integrations.repositories.patrol import _utc_naive
from integrations.repositories.tables import (
    patrol_feedback_inbox,
    patrol_signal,
    patrol_signal_occurrence,
)


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    event_id: str
    accepted: bool
    duplicate: bool
    status: FeedbackInboxStatus
    signal_id: str
    previous_state: str | None
    signal_state: str | None
    signal_version: int | None
    next_inspection_at: datetime | None = None
    action_receipt_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "status": self.status.value,
            "signal_id": self.signal_id,
            "previous_state": self.previous_state,
            "signal_state": self.signal_state,
            "signal_version": self.signal_version,
            "next_inspection_at": (
                self.next_inspection_at.isoformat() if self.next_inspection_at else None
            ),
            "action_receipt_ref": self.action_receipt_ref,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any], *, duplicate: bool) -> FeedbackResult:
        next_at = payload.get("next_inspection_at")
        return cls(
            event_id=payload["event_id"],
            accepted=bool(payload["accepted"]),
            duplicate=duplicate,
            status=FeedbackInboxStatus(payload["status"]),
            signal_id=payload["signal_id"],
            previous_state=payload.get("previous_state"),
            signal_state=payload.get("signal_state"),
            signal_version=payload.get("signal_version"),
            next_inspection_at=datetime.fromisoformat(next_at) if next_at else None,
            action_receipt_ref=payload.get("action_receipt_ref"),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
        )


class FollowUpPolicy:
    def __init__(self, issue_days: dict[str, int], default_days: int = 3) -> None:
        self.issue_days = issue_days
        self.default_days = default_days

    @classmethod
    def load(cls, path: Path) -> FollowUpPolicy:
        with path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        default_days = int((config.get("default") or {}).get("days", 3))
        issue_days: dict[str, int] = {}
        for rule in config.get("rules") or []:
            days = int(rule["days"])
            for issue in rule.get("issues") or []:
                issue_days[str(issue)] = days
        return cls(issue_days, default_days)

    def next_inspection_at(self, issue_code: str, occurred_at: datetime) -> datetime:
        return occurred_at + timedelta(
            days=self.issue_days.get(issue_code, self.default_days)
        )


TRANSITIONS: dict[FeedbackEventType, SignalState | None] = {
    FeedbackEventType.HANDLING_STARTED: SignalState.IN_PROGRESS,
    FeedbackEventType.HANDLED: SignalState.AWAITING_RESCAN,
    FeedbackEventType.OBSERVE_REQUESTED: SignalState.OBSERVING,
    FeedbackEventType.IGNORED: SignalState.IGNORED,
    FeedbackEventType.FALSE_POSITIVE: SignalState.FALSE_POSITIVE,
    FeedbackEventType.ACTION_RECEIPT_AVAILABLE: None,
    FeedbackEventType.UNIT_ARCHIVED: SignalState.INTERRUPTED,
}

ALLOWED_SOURCE_STATES: dict[FeedbackEventType, set[SignalState]] = {
    FeedbackEventType.HANDLING_STARTED: {
        SignalState.NEW,
        SignalState.PENDING_CONFIRMATION,
    },
    FeedbackEventType.HANDLED: {
        SignalState.NEW,
        SignalState.PENDING_CONFIRMATION,
        SignalState.IN_PROGRESS,
        SignalState.OBSERVING,
        SignalState.LONG_TERM_FOLLOW_UP,
    },
    FeedbackEventType.OBSERVE_REQUESTED: {
        SignalState.NEW,
        SignalState.PENDING_CONFIRMATION,
        SignalState.IN_PROGRESS,
        SignalState.LONG_TERM_FOLLOW_UP,
        SignalState.AWAITING_RESCAN,
    },
    FeedbackEventType.IGNORED: set(ACTIVE_STATES),
    FeedbackEventType.FALSE_POSITIVE: set(ACTIVE_STATES),
    FeedbackEventType.ACTION_RECEIPT_AVAILABLE: set(ACTIVE_STATES),
    FeedbackEventType.UNIT_ARCHIVED: set(ACTIVE_STATES),
}


class FeedbackInboxService:
    def __init__(self, engine: Engine, policy: FollowUpPolicy) -> None:
        self.engine = engine
        self.policy = policy

    def consume(self, event: InspectionFeedbackEvent) -> FeedbackResult:
        try:
            return self._consume_once(event)
        except IntegrityError:
            with self.engine.connect() as connection:
                existing = connection.execute(
                    sa.select(patrol_feedback_inbox).where(
                        patrol_feedback_inbox.c.event_id == event.event_id
                    )
                ).mappings().one_or_none()
            if existing is None:
                raise
            if existing["payload_hash"] != event.payload_hash():
                return self._idempotency_conflict(event)
            return FeedbackResult.from_json(existing["result_json"], duplicate=True)

    def _consume_once(self, event: InspectionFeedbackEvent) -> FeedbackResult:
        with self.engine.begin() as connection:
            existing = connection.execute(
                sa.select(patrol_feedback_inbox).where(
                    patrol_feedback_inbox.c.event_id == event.event_id
                ).with_for_update()
            ).mappings().one_or_none()
            if existing is not None:
                if existing["payload_hash"] != event.payload_hash():
                    return self._idempotency_conflict(event)
                return FeedbackResult.from_json(existing["result_json"], duplicate=True)

            signal = connection.execute(
                sa.select(patrol_signal)
                .where(patrol_signal.c.signal_id == event.signal_id)
                .with_for_update()
            ).mappings().one_or_none()
            if signal is None:
                result = FeedbackResult(
                    event_id=event.event_id,
                    accepted=False,
                    duplicate=False,
                    status=FeedbackInboxStatus.REJECTED,
                    signal_id=event.signal_id,
                    previous_state=None,
                    signal_state=None,
                    signal_version=None,
                    error_code="SIGNAL_NOT_FOUND",
                    error_message="signal does not exist",
                )
                updates: dict[str, Any] = {}
            else:
                result, updates = self._evaluate(event, signal)
            stored = result.to_json()
            connection.execute(
                sa.insert(patrol_feedback_inbox).values(
                        event_id=event.event_id,
                        signal_id=event.signal_id,
                        signal_version=event.signal_version,
                        event_type=event.event_type.value,
                        source_system=event.source_system,
                        occurred_at=_utc_naive(event.occurred_at),
                        payload_hash=event.payload_hash(),
                        payload_json=event.model_dump(mode="json"),
                        status=result.status.value,
                        result_json=stored,
                        error_message=result.error_message,
                        received_at=_utc_naive(datetime.now(UTC)),
                        applied_at=(
                            _utc_naive(datetime.now(UTC)) if result.accepted else None
                        ),
                )
            )
            if result.accepted and updates:
                updated = connection.execute(
                    sa.update(patrol_signal)
                    .where(
                        patrol_signal.c.signal_id == event.signal_id,
                        patrol_signal.c.version == event.signal_version,
                    )
                    .values(**updates)
                )
                if updated.rowcount != 1:
                    raise RuntimeError("signal version changed while applying feedback")
                connection.execute(
                    sa.insert(patrol_signal_occurrence).values(
                        occurrence_id=f"oc_{uuid4().hex[:24]}",
                        signal_id=event.signal_id,
                        run_id=signal["last_scan_run_id"],
                        snapshot_id=None,
                        occurrence_type=event.event_type.value,
                        severity=signal["severity"],
                        evidence_refs_json=(
                            [event.action_receipt_ref] if event.action_receipt_ref else []
                        ),
                        details_json={
                            "source_event_id": event.event_id,
                            "source_system": event.source_system,
                            "previous_state": result.previous_state,
                            "signal_state": result.signal_state,
                            "reason_code": event.payload.get("reason_code"),
                            "signal_version": result.signal_version,
                        },
                        occurred_at=_utc_naive(event.occurred_at),
                    )
                )
            return result

    def _evaluate(self, event, signal) -> tuple[FeedbackResult, dict[str, Any]]:
        current_state = SignalState(signal["signal_state"])
        current_version = int(signal["version"])
        if event.signal_version != current_version:
            return self._rejected(
                event,
                current_state,
                current_version,
                "SIGNAL_VERSION_CONFLICT",
                "feedback signal_version is not current",
            ), {}

        target = TRANSITIONS[event.event_type]
        if event.event_type is FeedbackEventType.ACTION_RECEIPT_AVAILABLE:
            if not event.action_receipt_ref:
                return self._rejected(
                    event,
                    current_state,
                    current_version,
                    "ACTION_RECEIPT_REQUIRED",
                    "ACTION_RECEIPT_AVAILABLE requires action_receipt_ref",
                ), {}
            return FeedbackResult(
                event_id=event.event_id,
                accepted=True,
                duplicate=False,
                status=FeedbackInboxStatus.APPLIED,
                signal_id=event.signal_id,
                previous_state=current_state.value,
                signal_state=current_state.value,
                signal_version=current_version,
                next_inspection_at=self._aware(signal["next_inspection_at"]),
                action_receipt_ref=event.action_receipt_ref,
            ), {}

        if current_state not in ALLOWED_SOURCE_STATES[event.event_type]:
            return self._rejected(
                event,
                current_state,
                current_version,
                "INVALID_STATE_TRANSITION",
                f"cannot apply {event.event_type.value} to {current_state.value}",
            ), {}
        if event.event_type in {
            FeedbackEventType.IGNORED,
            FeedbackEventType.FALSE_POSITIVE,
            FeedbackEventType.UNIT_ARCHIVED,
        } and not str(event.payload.get("reason_code") or "").strip():
            return self._rejected(
                event,
                current_state,
                current_version,
                "REASON_CODE_REQUIRED",
                f"{event.event_type.value} requires payload.reason_code",
            ), {}

        try:
            next_at = self._next_at(event, signal) if target in {
                SignalState.AWAITING_RESCAN,
                SignalState.OBSERVING,
            } else None
        except (TypeError, ValueError) as exc:
            return self._rejected(
                event,
                current_state,
                current_version,
                "INVALID_RESCAN_TIME",
                str(exc),
            ), {}
        payload = dict(signal["signal_payload_json"])
        payload["signal_state"] = target.value
        updates = {
            "signal_state": target.value,
            "signal_payload_json": payload,
            "next_inspection_at": _utc_naive(next_at),
            "version": current_version + 1,
        }
        return FeedbackResult(
            event_id=event.event_id,
            accepted=True,
            duplicate=False,
            status=FeedbackInboxStatus.APPLIED,
            signal_id=event.signal_id,
            previous_state=current_state.value,
            signal_state=target.value,
            signal_version=current_version + 1,
            next_inspection_at=next_at,
            action_receipt_ref=event.action_receipt_ref,
        ), updates

    def _next_at(self, event, signal) -> datetime:
        supplied = event.payload.get("next_inspection_at")
        if supplied:
            parsed = datetime.fromisoformat(str(supplied).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("next_inspection_at must include timezone")
            if parsed <= event.occurred_at:
                raise ValueError("next_inspection_at must be after occurred_at")
            return parsed.astimezone(UTC)
        return self.policy.next_inspection_at(signal["issue_code"], event.occurred_at)

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None

    @staticmethod
    def _rejected(event, state, version, code, message) -> FeedbackResult:
        return FeedbackResult(
            event_id=event.event_id,
            accepted=False,
            duplicate=False,
            status=FeedbackInboxStatus.REJECTED,
            signal_id=event.signal_id,
            previous_state=state.value,
            signal_state=state.value,
            signal_version=version,
            error_code=code,
            error_message=message,
        )

    @staticmethod
    def _idempotency_conflict(event) -> FeedbackResult:
        return FeedbackResult(
            event_id=event.event_id,
            accepted=False,
            duplicate=True,
            status=FeedbackInboxStatus.REJECTED,
            signal_id=event.signal_id,
            previous_state=None,
            signal_state=None,
            signal_version=None,
            error_code="IDEMPOTENCY_CONFLICT",
            error_message="event_id was reused with different content",
        )
