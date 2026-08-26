from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from core.enums import DeliveryStatus, SignalState
from core.result_envelope import InspectionFeedbackEvent, InspectionResultAck
from integrations.database import create_database_engine
from integrations.feedback import FeedbackInboxService, FollowUpPolicy
from integrations.repositories import PatrolUnitOfWork
from integrations.result_delivery import MySqlResultDeliveryWorker, ResultDeliveryError
from integrations.rollout import RolloutPolicy
from scripts.verify_mysql_repository import identifiers, insert_prerequisites, make_objects


class RecordingSink:
    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.fail = True

    async def deliver(self, *, outbox_id, payload_hash, payload):
        self.payloads.append(payload)
        if self.fail:
            raise ResultDeliveryError("verification outage")
        object_ids = [payload["run"]["run_id"]]
        object_ids.extend(signal["signal_id"] for signal in payload["signals"])
        object_ids.extend(item["handoff_id"] for item in payload["handoff_directives"])
        return InspectionResultAck(
            event_id=outbox_id,
            accepted=True,
            accepted_object_ids=object_ids,
            duplicate=False,
            received_at=datetime.now(UTC),
        )


def cleanup(engine, ids, envelope) -> None:
    signal_ids = [signal.signal_id for signal in envelope.signals]
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM t_patrol_feedback_inbox WHERE signal_id=:signal_id"),
            {"signal_id": signal_ids[0]},
        )
        connection.execute(
            text("DELETE FROM t_patrol_delivery_outbox WHERE aggregate_id=:run_id"),
            {"run_id": envelope.run.run_id},
        )
        connection.execute(
            text("DELETE FROM t_patrol_signal_occurrence WHERE signal_id=:signal_id"),
            {"signal_id": signal_ids[0]},
        )
        connection.execute(
            text("DELETE FROM t_patrol_signal WHERE signal_id=:signal_id"),
            {"signal_id": signal_ids[0]},
        )
        connection.execute(
            text("DELETE FROM t_patrol_fact_snapshot WHERE run_id=:run_id"),
            {"run_id": envelope.run.run_id},
        )
        connection.execute(
            text("DELETE FROM t_patrol_run WHERE run_id=:run_id"),
            {"run_id": envelope.run.run_id},
        )
        connection.execute(
            text("DELETE FROM t_patrol_job WHERE job_id=:job_id"),
            {"job_id": ids["job_id"]},
        )
        connection.execute(
            text("DELETE FROM t_patrol_batch WHERE batch_id=:batch_id"),
            {"batch_id": ids["batch_id"]},
        )


async def verify() -> None:
    engine = create_database_engine()
    ids = identifiers()
    unit, snapshot, started, envelope = make_objects(ids)
    try:
        with PatrolUnitOfWork(engine) as work:
            assert work.connection is not None and work.repository is not None
            insert_prerequisites(work.connection, ids, unit)
            work.repository.create_run(started)
            outbox_id = work.repository.persist_result(envelope, snapshot)
            work.commit()

        sink = RecordingSink()
        blocked_worker = MySqlResultDeliveryWorker(
            engine=engine,
            sink=sink,
            worker_id="verify-s5-blocked",
            rollout_policy=RolloutPolicy(
                stage="CANARY",
                allowlisted_shop_ids=[unit.shop_id + 1],
            ),
        )
        blocked = await blocked_worker.process_once()
        assert blocked.outbox_id is None
        assert sink.payloads == []
        with engine.connect() as connection:
            blocked_row = connection.execute(
                text(
                    "SELECT status,locked_by FROM t_patrol_delivery_outbox "
                    "WHERE outbox_id=:outbox_id"
                ),
                {"outbox_id": outbox_id},
            ).mappings().one()
            assert blocked_row["status"] == DeliveryStatus.PENDING.value
            assert blocked_row["locked_by"] is None

        worker = MySqlResultDeliveryWorker(
            engine=engine,
            sink=sink,
            worker_id="verify-s5",
            base_backoff_seconds=1,
            rollout_policy=RolloutPolicy(
                stage="CANARY",
                allowlisted_shop_ids=[unit.shop_id],
            ),
        )
        failed = await worker.process_once()
        assert failed.status is DeliveryStatus.PENDING
        assert failed.retry_count == 1
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT status,retry_count,payload_hash,payload_json "
                    "FROM t_patrol_delivery_outbox WHERE outbox_id=:outbox_id"
                ),
                {"outbox_id": outbox_id},
            ).mappings().one()
            assert row["status"] == DeliveryStatus.PENDING.value
            assert row["retry_count"] == 1
            assert row["payload_hash"] == envelope.content_hash()
            connection.execute(
                text(
                    "UPDATE t_patrol_delivery_outbox SET next_retry_at=NULL "
                    "WHERE outbox_id=:outbox_id"
                ),
                {"outbox_id": outbox_id},
            )

        sink.fail = False
        delivered = await worker.process_once()
        assert delivered.status is DeliveryStatus.DELIVERED
        assert sink.payloads[0] == sink.payloads[1]
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT status,delivery_ack_json,payload_hash "
                    "FROM t_patrol_delivery_outbox WHERE outbox_id=:outbox_id"
                ),
                {"outbox_id": outbox_id},
            ).mappings().one()
            assert row["status"] == DeliveryStatus.DELIVERED.value
            ack_json = row["delivery_ack_json"]
            if isinstance(ack_json, str):
                ack_json = json.loads(ack_json)
            assert ack_json["event_id"] == outbox_id
            assert row["payload_hash"] == envelope.content_hash()

        signal = envelope.signals[0]
        inbox = FeedbackInboxService(engine, FollowUpPolicy({}, default_days=3))
        handling = InspectionFeedbackEvent(
            event_id=f"feedback-{uuid4().hex}",
            signal_id=signal.signal_id,
            signal_version=1,
            event_type="HANDLING_STARTED",
            source_system="verify-control-center",
            occurred_at=datetime.now(UTC),
        )
        first = inbox.consume(handling)
        assert first.accepted and not first.duplicate
        duplicate = inbox.consume(handling)
        assert duplicate.accepted and duplicate.duplicate
        assert duplicate.signal_version == first.signal_version == 2

        handled_at = datetime.now(UTC)
        handled = InspectionFeedbackEvent(
            event_id=f"feedback-{uuid4().hex}",
            signal_id=signal.signal_id,
            signal_version=2,
            event_type="HANDLED",
            source_system="verify-control-center",
            occurred_at=handled_at,
        )
        result = inbox.consume(handled)
        assert result.accepted
        assert result.signal_state == SignalState.AWAITING_RESCAN.value
        assert result.next_inspection_at == handled_at + timedelta(days=3)
        stale = inbox.consume(
            InspectionFeedbackEvent(
                event_id=f"feedback-{uuid4().hex}",
                signal_id=signal.signal_id,
                signal_version=2,
                event_type="HANDLED",
                source_system="verify-control-center",
                occurred_at=datetime.now(UTC),
            )
        )
        assert not stale.accepted
        assert stale.error_code == "SIGNAL_VERSION_CONFLICT"
        with engine.connect() as connection:
            state = connection.execute(
                text(
                    "SELECT signal_state,version,next_inspection_at "
                    "FROM t_patrol_signal WHERE signal_id=:signal_id"
                ),
                {"signal_id": signal.signal_id},
            ).mappings().one()
            assert state["signal_state"] == SignalState.AWAITING_RESCAN.value
            assert state["version"] == 3
            assert state["next_inspection_at"] is not None
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_patrol_feedback_inbox "
                    "WHERE signal_id=:signal_id"
                ),
                {"signal_id": signal.signal_id},
            ).scalar_one() == 3
        print(
            "S5 canary allowlist, outbox retry, immutable payload, business ACK, "
            "feedback idempotency, version guard, and due-rescan verification passed"
        )
    finally:
        cleanup(engine, ids, envelope)
        engine.dispose()


def main() -> None:
    asyncio.run(verify())


if __name__ == "__main__":
    main()
