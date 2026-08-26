from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
import sqlalchemy as sa
from sqlalchemy import Engine

from core.enums import DeliveryStatus
from core.result_envelope import InspectionResultAck, InspectionResultEnvelope
from integrations.repositories.patrol import _utc_naive
from integrations.repositories.tables import patrol_delivery_outbox, patrol_job, patrol_run
from integrations.rollout import RolloutPolicy


class ResultDeliveryError(RuntimeError):
    pass


class ResultAckInvalid(ResultDeliveryError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    outbox_id: str
    aggregate_id: str
    payload_hash: str
    payload: dict[str, Any]
    retry_count: int
    max_retry: int


@dataclass(frozen=True, slots=True)
class DeliveryAttemptResult:
    outbox_id: str | None
    status: DeliveryStatus | None
    retry_count: int = 0
    error: str | None = None


class ResultSinkPort(Protocol):
    async def deliver(
        self,
        *,
        outbox_id: str,
        payload_hash: str,
        payload: dict[str, Any],
    ) -> InspectionResultAck: ...


class HttpInspectionResultSink:
    def __init__(
        self,
        endpoint: str,
        token: str = "",
        *,
        timeout_seconds: float = 10,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not endpoint.strip():
            raise ValueError("inspection result endpoint is required")
        self.endpoint = endpoint
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def aclose(self) -> None:
        if self._owns_client and not self.client.is_closed:
            await self.client.aclose()

    async def deliver(
        self,
        *,
        outbox_id: str,
        payload_hash: str,
        payload: dict[str, Any],
    ) -> InspectionResultAck:
        try:
            response = await self.client.post(
                self.endpoint,
                json=payload,
                headers={
                    **self.headers,
                    "Idempotency-Key": outbox_id,
                    "X-Content-Hash": payload_hash,
                },
            )
            response.raise_for_status()
            return InspectionResultAck.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ResultDeliveryError(type(exc).__name__) from exc


class MySqlResultDeliveryWorker:
    def __init__(
        self,
        *,
        engine: Engine,
        sink: ResultSinkPort,
        worker_id: str,
        base_backoff_seconds: int = 30,
        stale_after: timedelta = timedelta(minutes=5),
        rollout_policy: RolloutPolicy | None = None,
    ) -> None:
        self.engine = engine
        self.sink = sink
        self.worker_id = worker_id
        self.base_backoff_seconds = base_backoff_seconds
        self.stale_after = stale_after
        self.rollout_policy = rollout_policy or RolloutPolicy()

    def claim(self, *, now: datetime | None = None) -> ClaimedDelivery | None:
        if not self.rollout_policy.result_delivery_enabled:
            return None
        current = now or datetime.now(UTC)
        stale_before = current - self.stale_after
        with self.engine.begin() as connection:
            conditions = [
                patrol_delivery_outbox.c.aggregate_type == "INSPECTION_RUN",
                patrol_delivery_outbox.c.status == DeliveryStatus.PENDING.value,
                sa.or_(
                    patrol_delivery_outbox.c.next_retry_at.is_(None),
                    patrol_delivery_outbox.c.next_retry_at <= _utc_naive(current),
                ),
                sa.or_(
                    patrol_delivery_outbox.c.locked_by.is_(None),
                    patrol_delivery_outbox.c.locked_at < _utc_naive(stale_before),
                ),
            ]
            statement = sa.select(patrol_delivery_outbox)
            allowed_shop_ids = self.rollout_policy.allowed_shop_ids
            if allowed_shop_ids is not None:
                if not allowed_shop_ids:
                    return None
                statement = statement.select_from(
                    patrol_delivery_outbox.join(
                        patrol_run,
                        patrol_run.c.run_id == patrol_delivery_outbox.c.aggregate_id,
                    ).join(patrol_job, patrol_job.c.job_id == patrol_run.c.job_id)
                )
                conditions.append(patrol_job.c.shop_id.in_(allowed_shop_ids))
            row = connection.execute(
                statement
                .where(*conditions)
                .order_by(
                    patrol_delivery_outbox.c.created_at,
                    patrol_delivery_outbox.c.outbox_id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            ).mappings().one_or_none()
            if row is None:
                return None
            payload = dict(row["payload_json"])
            try:
                stored_envelope = InspectionResultEnvelope.model_validate(payload)
                hash_matches = stored_envelope.content_hash() == row["payload_hash"]
            except ValueError:
                hash_matches = False
            if not hash_matches:
                self._mark_dead_in_connection(
                    connection,
                    row["outbox_id"],
                    "stored payload hash mismatch",
                    int(row["retry_count"]),
                    current,
                )
                return None
            connection.execute(
                sa.update(patrol_delivery_outbox)
                .where(patrol_delivery_outbox.c.outbox_id == row["outbox_id"])
                .values(
                    locked_by=self.worker_id,
                    locked_at=_utc_naive(current),
                )
            )
            return ClaimedDelivery(
                outbox_id=row["outbox_id"],
                aggregate_id=row["aggregate_id"],
                payload_hash=row["payload_hash"],
                payload=payload,
                retry_count=int(row["retry_count"]),
                max_retry=int(row["max_retry"]),
            )

    async def process_once(
        self, *, now: datetime | None = None
    ) -> DeliveryAttemptResult:
        current = now or datetime.now(UTC)
        delivery = self.claim(now=current)
        if delivery is None:
            return DeliveryAttemptResult(outbox_id=None, status=None)
        try:
            envelope = InspectionResultEnvelope.model_validate(delivery.payload)
            ack = await self.sink.deliver(
                outbox_id=delivery.outbox_id,
                payload_hash=delivery.payload_hash,
                payload=delivery.payload,
            )
            self._validate_ack(delivery, envelope, ack)
        except Exception as exc:
            return self._record_failure(delivery, exc, current)
        self._record_success(delivery, ack, current)
        return DeliveryAttemptResult(
            outbox_id=delivery.outbox_id,
            status=DeliveryStatus.DELIVERED,
            retry_count=delivery.retry_count,
        )

    @staticmethod
    def _validate_ack(
        delivery: ClaimedDelivery,
        envelope: InspectionResultEnvelope,
        ack: InspectionResultAck,
    ) -> None:
        if ack.event_id != delivery.outbox_id:
            raise ResultAckInvalid("ACK event_id does not match outbox_id")
        if not ack.accepted:
            raise ResultAckInvalid("receiver rejected the inspection result")
        accepted_ids = set(ack.accepted_object_ids)
        expected_ids = {
            envelope.run.run_id,
            *(signal.signal_id for signal in envelope.signals),
            *(item.handoff_id for item in envelope.handoff_directives),
        }
        if not expected_ids.issubset(accepted_ids):
            raise ResultAckInvalid("ACK does not cover the run and every signal")

    def _record_success(
        self,
        delivery: ClaimedDelivery,
        ack: InspectionResultAck,
        now: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            updated = connection.execute(
                sa.update(patrol_delivery_outbox)
                .where(
                    patrol_delivery_outbox.c.outbox_id == delivery.outbox_id,
                    patrol_delivery_outbox.c.status == DeliveryStatus.PENDING.value,
                    patrol_delivery_outbox.c.locked_by == self.worker_id,
                    patrol_delivery_outbox.c.payload_hash == delivery.payload_hash,
                )
                .values(
                    status=DeliveryStatus.DELIVERED.value,
                    locked_by=None,
                    locked_at=None,
                    last_error=None,
                    delivery_ack_json=ack.model_dump(mode="json"),
                    published_at=_utc_naive(now),
                )
            )
            if updated.rowcount != 1:
                raise ResultDeliveryError("outbox ownership changed before ACK commit")

    def _record_failure(
        self,
        delivery: ClaimedDelivery,
        error: Exception,
        now: datetime,
    ) -> DeliveryAttemptResult:
        retry_count = delivery.retry_count + 1
        dead = retry_count >= delivery.max_retry
        message = type(error).__name__[:2000]
        with self.engine.begin() as connection:
            if dead:
                self._mark_dead_in_connection(
                    connection,
                    delivery.outbox_id,
                    message,
                    retry_count,
                    now,
                    worker_id=self.worker_id,
                )
            else:
                delay = self.base_backoff_seconds * (2 ** (retry_count - 1))
                connection.execute(
                    sa.update(patrol_delivery_outbox)
                    .where(
                        patrol_delivery_outbox.c.outbox_id == delivery.outbox_id,
                        patrol_delivery_outbox.c.status == DeliveryStatus.PENDING.value,
                        patrol_delivery_outbox.c.locked_by == self.worker_id,
                    )
                    .values(
                        retry_count=retry_count,
                        next_retry_at=_utc_naive(now + timedelta(seconds=delay)),
                        locked_by=None,
                        locked_at=None,
                        last_error=message,
                    )
                )
        return DeliveryAttemptResult(
            outbox_id=delivery.outbox_id,
            status=DeliveryStatus.DEAD if dead else DeliveryStatus.PENDING,
            retry_count=retry_count,
            error=message,
        )

    @staticmethod
    def _mark_dead_in_connection(
        connection,
        outbox_id: str,
        error: str,
        retry_count: int,
        now: datetime,
        *,
        worker_id: str | None = None,
    ) -> None:
        conditions = [patrol_delivery_outbox.c.outbox_id == outbox_id]
        if worker_id is not None:
            conditions.append(patrol_delivery_outbox.c.locked_by == worker_id)
        connection.execute(
            sa.update(patrol_delivery_outbox)
            .where(*conditions)
            .values(
                status=DeliveryStatus.DEAD.value,
                retry_count=retry_count,
                next_retry_at=None,
                locked_by=None,
                locked_at=None,
                last_error=error[:2000],
                published_at=None,
            )
        )
