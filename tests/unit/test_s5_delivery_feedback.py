from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from core.enums import (
    DeliveryStatus,
    FeedbackInboxStatus,
    InspectionRunStatus,
    SignalState,
    TriggerType,
)
from core.inspection_run import InspectionRun
from core.result_envelope import (
    InspectionFeedbackEvent,
    InspectionResultAck,
    InspectionResultEnvelope,
)
from integrations.feedback import FeedbackInboxService, FollowUpPolicy
from integrations.repositories.tables import metadata, patrol_delivery_outbox
from integrations.result_delivery import (
    ClaimedDelivery,
    MySqlResultDeliveryWorker,
    ResultAckInvalid,
)
from integrations.rollout import RolloutPolicy


def make_envelope() -> InspectionResultEnvelope:
    started_at = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
    run = InspectionRun(
        run_id="pr_0123456789abcdef01234567",
        job_id="job-1",
        batch_id="batch-1",
        request_id="request-1",
        operating_unit_id="ou_0123456789abcdef01234567",
        trigger_type=TriggerType.DAILY_SCHEDULE,
        status=InspectionRunStatus.COMPLETED,
        rule_versions={"R2": "2.0", "R3": "2.0", "R7": "2.0"},
        fact_snapshot_id="fs_0123456789abcdef01234567",
        signal_count=0,
        data_gap_count=0,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
    )
    return InspectionResultEnvelope(
        run=run,
        producer_versions={"business-inspection": "2.0.0"},
        generated_at=run.finished_at,
    )


def make_event(event_type: str, **overrides) -> InspectionFeedbackEvent:
    values = {
        "event_id": "feedback-1",
        "signal_id": "is_0123456789abcdef01234567",
        "signal_version": 3,
        "event_type": event_type,
        "source_system": "control-center",
        "occurred_at": datetime(2026, 7, 31, 3, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return InspectionFeedbackEvent(**values)


def signal_row(state: SignalState = SignalState.IN_PROGRESS) -> dict:
    return {
        "signal_state": state.value,
        "version": 3,
        "issue_code": "库存不足",
        "signal_payload_json": {"signal_state": state.value},
        "next_inspection_at": None,
    }


def test_business_ack_must_cover_every_result_object():
    envelope = make_envelope()
    delivery = ClaimedDelivery(
        outbox_id="ob_0123456789abcdef01234567",
        aggregate_id=envelope.run.run_id,
        payload_hash=envelope.content_hash(),
        payload=envelope.model_dump(mode="json"),
        retry_count=0,
        max_retry=10,
    )
    ack = InspectionResultAck(
        event_id=delivery.outbox_id,
        accepted=True,
        accepted_object_ids=[envelope.run.run_id],
        duplicate=False,
        received_at=datetime.now(UTC),
    )
    MySqlResultDeliveryWorker._validate_ack(delivery, envelope, ack)

    incomplete = ack.model_copy(update={"accepted_object_ids": ["another-object"]})
    with pytest.raises(ResultAckInvalid, match="every signal"):
        MySqlResultDeliveryWorker._validate_ack(delivery, envelope, incomplete)


def test_http_2xx_style_ack_without_object_ids_is_invalid():
    with pytest.raises(ValueError, match="accepted_object_ids"):
        InspectionResultAck(
            event_id="ob_0123456789abcdef01234567",
            accepted=True,
            accepted_object_ids=[],
            duplicate=False,
            received_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_delivery_worker_never_claims_suppressed_history():
    envelope = make_envelope()
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(patrol_delivery_outbox).values(
                outbox_id="ob_suppressed_history",
                aggregate_type="INSPECTION_RUN",
                aggregate_id=envelope.run.run_id,
                aggregate_version=1,
                payload_hash=envelope.content_hash(),
                payload_json=envelope.model_dump(mode="json"),
                status=DeliveryStatus.SUPPRESSED.value,
                retry_count=0,
                max_retry=10,
            )
        )

    class FailIfCalledSink:
        async def deliver(self, **kwargs):
            raise AssertionError("SUPPRESSED outbox must never be delivered")

    worker = MySqlResultDeliveryWorker(
        engine=engine,
        sink=FailIfCalledSink(),
        worker_id="delivery-test",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    result = await worker.process_once()

    assert result.outbox_id is None
    assert result.status is None


@pytest.mark.asyncio
async def test_internal_delivery_worker_does_not_claim_pending_history():
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)

    class FailIfCalledSink:
        async def deliver(self, **kwargs):
            raise AssertionError("INTERNAL_ONLY must not deliver")

    worker = MySqlResultDeliveryWorker(
        engine=engine,
        sink=FailIfCalledSink(),
        worker_id="delivery-internal",
        rollout_policy=RolloutPolicy(stage="INTERNAL_ONLY"),
    )

    result = await worker.process_once()

    assert result.outbox_id is None
    assert result.status is None


def test_handled_moves_to_awaiting_rescan_using_r7():
    service = FeedbackInboxService(
        engine=None,
        policy=FollowUpPolicy({"库存不足": 30}, default_days=3),
    )
    event = make_event("HANDLED")
    result, updates = service._evaluate(event, signal_row())
    assert result.accepted
    assert result.status is FeedbackInboxStatus.APPLIED
    assert result.signal_state == SignalState.AWAITING_RESCAN.value
    assert result.signal_version == 4
    assert result.next_inspection_at == event.occurred_at + timedelta(days=30)
    assert updates["signal_state"] == SignalState.AWAITING_RESCAN.value
    assert updates["version"] == 4


def test_feedback_rejects_stale_version_and_illegal_transition():
    service = FeedbackInboxService(None, FollowUpPolicy({}, default_days=3))
    stale, updates = service._evaluate(
        make_event("HANDLED", signal_version=2),
        signal_row(),
    )
    assert not stale.accepted
    assert stale.error_code == "SIGNAL_VERSION_CONFLICT"
    assert updates == {}

    illegal, updates = service._evaluate(
        make_event("HANDLING_STARTED"),
        signal_row(SignalState.OBSERVING),
    )
    assert not illegal.accepted
    assert illegal.error_code == "INVALID_STATE_TRANSITION"
    assert updates == {}


def test_terminal_feedback_requires_reason_code():
    service = FeedbackInboxService(None, FollowUpPolicy({}, default_days=3))
    result, updates = service._evaluate(
        make_event("FALSE_POSITIVE"),
        signal_row(SignalState.NEW),
    )
    assert not result.accepted
    assert result.error_code == "REASON_CODE_REQUIRED"
    assert updates == {}


def test_action_receipt_does_not_change_signal_state_or_version():
    service = FeedbackInboxService(None, FollowUpPolicy({}, default_days=3))
    result, updates = service._evaluate(
        make_event(
            "ACTION_RECEIPT_AVAILABLE",
            action_receipt_ref="receipt-1",
        ),
        signal_row(SignalState.IN_PROGRESS),
    )
    assert result.accepted
    assert result.signal_state == SignalState.IN_PROGRESS.value
    assert result.signal_version == 3
    assert result.action_receipt_ref == "receipt-1"
    assert updates == {}
