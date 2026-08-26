from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

import clients.control_center_mcp as control_center_mcp
from clients.control_center_mcp import (
    ControlCenterContractError,
    ControlCenterPatrolMcpClient,
)
from core.control_center_contracts import (
    SubmitPatrolBatchRequest,
    SubmitPatrolBatchResult,
)
from core.enums import DeliveryStatus
from core.errors import ControlCenterUnavailable, IdempotencyConflict, ModeAgentUnavailable
from integrations.control_center_delivery import (
    AGGREGATE_TYPE,
    ControlCenterBatchPublisher,
    MySqlControlCenterDeliveryWorker,
    _payload_hash,
    enqueue_control_center_batch,
)
from integrations.repositories.tables import metadata, patrol_delivery_outbox
from integrations.result_delivery import MySqlResultDeliveryWorker
from integrations.rollout import RolloutPolicy

ROOT = Path(__file__).resolve().parents[2]


def make_request() -> SubmitPatrolBatchRequest:
    payload = json.loads(
        (ROOT / "contracts/examples/control-center-submit-patrol-batch.v2.example.json")
        .read_text(encoding="utf-8")
    )
    return SubmitPatrolBatchRequest.model_validate(payload)


def make_receipt(
    request: SubmitPatrolBatchRequest,
    *,
    status: str = "SUCCESS",
    code: str = "OK",
) -> SubmitPatrolBatchResult:
    unit = request.units[0]
    counts = {
        "SUCCESS": (1, 0, 0),
        "PARTIAL_SUCCESS": (0, 1, 0),
        "FAILED": (0, 0, 1),
    }
    success, partial, failed = counts[status]
    return SubmitPatrolBatchResult.model_validate({
        "patrolBatchNo": request.patrol_batch_no,
        "receivedCount": 1,
        "successCount": success,
        "partialSuccessCount": partial,
        "failedCount": failed,
        "results": [{
            "shopId": unit.listing.shop_id,
            "parentAsin": unit.listing.parent_asin,
            "parentSellerSku": unit.listing.parent_seller_sku,
            "status": status,
            "code": code,
            "message": "accepted",
        }],
    })


def make_engine() -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    return engine


def outbox_row(engine: sa.Engine) -> dict:
    with engine.connect() as connection:
        return dict(connection.execute(sa.select(patrol_delivery_outbox)).mappings().one())


def test_control_center_publisher_is_idempotent_and_keeps_transaction_usable():
    engine = make_engine()
    request = make_request()
    changed = request.model_copy(deep=True)
    changed.units[0].proposal.problem_summary = "different analysis"

    with engine.begin() as connection:
        first = enqueue_control_center_batch(
            connection,
            request,
            delivery_status=DeliveryStatus.PENDING,
        )
        assert enqueue_control_center_batch(
            connection,
            request,
            delivery_status=DeliveryStatus.PENDING,
        ) == first
        with pytest.raises(IdempotencyConflict):
            enqueue_control_center_batch(
                connection,
                changed,
                delivery_status=DeliveryStatus.PENDING,
            )
        assert connection.execute(sa.select(sa.literal(1))).scalar_one() == 1

    assert outbox_row(engine)["outbox_id"] == first


def test_control_center_payload_hash_ignores_json_float_rendering_noise():
    request = make_request()
    request.units[0].operating_metric.sales_amount = 121.44999999999999
    normalized = request.model_copy(deep=True)
    normalized.units[0].operating_metric.sales_amount = 121.45

    assert _payload_hash(request) == _payload_hash(normalized)


@pytest.mark.asyncio
async def test_control_center_publisher_enriches_mode_before_freezing_outbox():
    engine = make_engine()
    request = make_request()
    request.units[0].listing.business_model = None
    request.units[0].proposal.recommended_business_model = None
    request.units[0].proposal.mode_confidence = None
    request.units[0].proposal.mode_reason_summary = None
    # get_evaluate_by_key 需要非空 shop_account
    request.units[0].listing.shop_account = "test-shop-account"

    class ModeClient:
        async def get_evaluate_by_key(self, **key):
            assert key == {
                "shop_id": "SIM-SHOP-1622",
                "shop_account": "test-shop-account",
                "site_code": "Amazon_US",
                "parent_asin": "B0EXAMPLE0",
                "parent_seller_sku": "DEMO-JUICER-P",
            }
            from clients.operating_mode_mcp import CurrentOperatingMode

            return CurrentOperatingMode.model_validate({
                "shop_id": "SIM-SHOP-1622",
                "parent_asin": "B0EXAMPLE0",
                "parent_seller_sku": "DEMO-JUICER-P",
                "decision_status": "DECIDED",
                "recommended_mode_code": "ACTIVE_ADVANCE",
                "recommended_mode": "积极推进",
                "explanation": "权威经营模式说明",
            })

    publisher = ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
        operating_mode_client=ModeClient(),
        operating_mode_lookup_enabled=True,
    )

    await publisher.enqueue(request)

    payload = outbox_row(engine)["payload_json"]
    unit = payload["units"][0]
    assert unit["listing"]["businessModel"] == "CONTROLLED_GROWTH"
    assert unit["proposal"]["recommendedBusinessModel"] == "CONTROLLED_GROWTH"
    assert unit["proposal"]["modeConfidence"] is None
    assert unit["proposal"]["rawAnalysis"]["operatingMode"]["sourceModeCode"] == (
        "ACTIVE_ADVANCE"
    )


@pytest.mark.asyncio
async def test_control_center_publisher_does_not_enqueue_when_mode_lookup_fails():
    engine = make_engine()
    request = make_request()
    request.units[0].listing.shop_account = "test-shop-account"

    class ModeClient:
        async def get_evaluate_by_key(self, **key):
            del key
            raise ModeAgentUnavailable("mode service unavailable")

    publisher = ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
        operating_mode_client=ModeClient(),
        operating_mode_lookup_enabled=True,
    )

    await publisher.enqueue(request)

    with engine.connect() as connection:
        assert connection.execute(
            sa.select(sa.func.count()).select_from(patrol_delivery_outbox)
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_control_center_publisher_attaches_operating_mode_failure():
    engine = make_engine()
    request = make_request()
    request.units[0].listing.business_model = None
    request.units[0].proposal.recommended_business_model = None
    request.units[0].proposal.mode_confidence = None
    request.units[0].proposal.mode_reason_summary = None
    request.units[0].listing.shop_account = "test-shop-account"

    class ModeClient:
        async def get_evaluate_by_key(self, **key):
            del key
            raise RuntimeError("同步抓取或判断发生未预期错误")

    publisher = ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
        operating_mode_client=ModeClient(),
        operating_mode_lookup_enabled=True,
    )

    await publisher.enqueue(request)

    payload = outbox_row(engine)["payload_json"]
    unit = payload["units"][0]
    error = unit["proposal"]["rawAnalysis"]["operatingModeError"]
    assert error["errorCode"] == "MODE_AGENT_ERROR"
    assert "未预期错误" in error["errorMessage"]
    gaps = unit["proposal"]["dataGaps"]
    assert any(gap.get("code") == "MODE_AGENT_ERROR" for gap in gaps)


@pytest.mark.asyncio
async def test_control_center_publisher_suppresses_internal_only_batches():
    engine = make_engine()
    publisher = ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="INTERNAL_ONLY"),
    )

    await publisher.enqueue(make_request())

    assert outbox_row(engine)["status"] == DeliveryStatus.SUPPRESSED.value


@pytest.mark.asyncio
async def test_control_center_worker_delivers_partial_success_without_retry():
    engine = make_engine()
    request = make_request()
    await ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
    ).enqueue(request)

    class PartialClient:
        async def submit_patrol_batch(self, submitted):
            assert submitted == request
            return make_receipt(
                request,
                status="PARTIAL_SUCCESS",
                code="PROPOSAL_LOCKED",
            )

    worker = MySqlControlCenterDeliveryWorker(
        engine=engine,
        client=PartialClient(),
        worker_id="control-center-test",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    result = await worker.process_once(now=datetime(2026, 8, 4, tzinfo=UTC))

    assert result.status is DeliveryStatus.DELIVERED
    row = outbox_row(engine)
    assert row["status"] == DeliveryStatus.DELIVERED.value
    assert row["delivery_ack_json"]["results"][0]["code"] == "PROPOSAL_LOCKED"


@pytest.mark.asyncio
async def test_control_center_worker_marks_business_rejection_dead():
    engine = make_engine()
    request = make_request()
    await ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
    ).enqueue(request)

    class RejectingClient:
        async def submit_patrol_batch(self, submitted):
            assert submitted == request
            return make_receipt(request, status="FAILED", code="INVALID_OPERATING_METRIC")

    worker = MySqlControlCenterDeliveryWorker(
        engine=engine,
        client=RejectingClient(),
        worker_id="control-center-test",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    result = await worker.process_once(now=datetime(2026, 8, 4, tzinfo=UTC))

    assert result.status is DeliveryStatus.DEAD
    assert result.error == "ContractInvalid"
    assert outbox_row(engine)["status"] == DeliveryStatus.DEAD.value


@pytest.mark.asyncio
async def test_control_center_worker_retries_the_frozen_payload():
    engine = make_engine()
    request = make_request()
    await ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
    ).enqueue(request)
    original = outbox_row(engine)

    class UnavailableClient:
        async def submit_patrol_batch(self, submitted):
            assert submitted.model_dump(mode="json", by_alias=True) == original["payload_json"]
            raise ControlCenterUnavailable("unavailable")

    worker = MySqlControlCenterDeliveryWorker(
        engine=engine,
        client=UnavailableClient(),
        worker_id="control-center-test",
        base_backoff_seconds=1,
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    result = await worker.process_once(now=datetime(2026, 8, 4, tzinfo=UTC))

    current = outbox_row(engine)
    assert result.status is DeliveryStatus.PENDING
    assert current["retry_count"] == 1
    assert current["payload_hash"] == original["payload_hash"]
    assert current["payload_json"] == original["payload_json"]


@pytest.mark.asyncio
async def test_control_center_worker_times_out_and_clears_lock_for_retry():
    engine = make_engine()
    request = make_request()
    await ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
    ).enqueue(request)

    class SlowClient:
        async def submit_patrol_batch(self, submitted):
            del submitted
            await asyncio.sleep(1)
            raise AssertionError("timeout should occur before the response")

    worker = MySqlControlCenterDeliveryWorker(
        engine=engine,
        client=SlowClient(),
        worker_id="control-center-timeout",
        base_backoff_seconds=1,
        request_timeout_seconds=0.01,
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    result = await worker.process_once(now=datetime(2026, 8, 4, tzinfo=UTC))

    assert result.status is DeliveryStatus.PENDING
    row = outbox_row(engine)
    assert row["retry_count"] == 1
    assert row["locked_by"] is None
    assert row["locked_at"] is None
    assert row["last_error"] == "ControlCenterUnavailable"


@pytest.mark.asyncio
async def test_control_center_worker_releases_lock_when_cancelled():
    engine = make_engine()
    request = make_request()
    await ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
    ).enqueue(request)

    class CancelledClient:
        async def submit_patrol_batch(self, submitted):
            del submitted
            raise asyncio.CancelledError

    worker = MySqlControlCenterDeliveryWorker(
        engine=engine,
        client=CancelledClient(),
        worker_id="control-center-cancelled",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.process_once()

    row = outbox_row(engine)
    assert row["status"] == DeliveryStatus.PENDING.value
    assert row["locked_by"] is None
    assert row["locked_at"] is None


@pytest.mark.asyncio
async def test_delivery_workers_do_not_cross_consume_aggregate_types():
    engine = make_engine()
    request = make_request()
    with engine.begin() as connection:
        connection.execute(sa.insert(patrol_delivery_outbox).values(
            outbox_id="ob_inspection",
            aggregate_type="INSPECTION_RUN",
            aggregate_id="pr_0123456789abcdef01234567",
            aggregate_version=1,
            payload_hash="0" * 64,
            payload_json={"not": "a control center batch"},
            status=DeliveryStatus.PENDING.value,
            retry_count=0,
            max_retry=10,
        ))

    class FailClient:
        async def submit_patrol_batch(self, submitted):
            raise AssertionError("inspection outbox must not reach control center")

    control_worker = MySqlControlCenterDeliveryWorker(
        engine=engine,
        client=FailClient(),
        worker_id="control-center-test",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    assert (await control_worker.process_once()).outbox_id is None

    with engine.begin() as connection:
        enqueue_control_center_batch(
            connection,
            request,
            delivery_status=DeliveryStatus.PENDING,
        )

    class FailSink:
        async def deliver(self, **kwargs):
            raise AssertionError("control center outbox must not reach result sink")

    result_worker = MySqlResultDeliveryWorker(
        engine=engine,
        sink=FailSink(),
        worker_id="result-test",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    await result_worker.process_once()
    with engine.connect() as connection:
        control_status = connection.execute(
            sa.select(patrol_delivery_outbox.c.status).where(
                patrol_delivery_outbox.c.aggregate_type == AGGREGATE_TYPE
            )
        ).scalar_one()
    assert control_status == DeliveryStatus.PENDING.value


@pytest.mark.asyncio
async def test_control_center_worker_only_claims_allowed_batch_outboxes():
    engine = make_engine()
    first = make_request()
    second = first.model_copy(update={"patrol_batch_no": "PT-SECOND"}, deep=True)
    publisher = ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    first_id = await publisher.enqueue(first)
    second_id = await publisher.enqueue(second)

    class Client:
        async def submit_patrol_batch(self, submitted):
            assert submitted.patrol_batch_no == "PT-SECOND"
            return make_receipt(submitted)

    worker = MySqlControlCenterDeliveryWorker(
        engine=engine,
        client=Client(),
        worker_id="scoped-control-center-test",
        rollout_policy=RolloutPolicy(stage="FULL"),
        allowed_outbox_ids=frozenset({second_id}),
    )

    result = await worker.process_once()

    assert result.outbox_id == second_id
    with engine.connect() as connection:
        statuses = dict(connection.execute(sa.select(
            patrol_delivery_outbox.c.outbox_id,
            patrol_delivery_outbox.c.status,
        )).all())
    assert statuses[first_id] == DeliveryStatus.PENDING.value
    assert statuses[second_id] == DeliveryStatus.DELIVERED.value


@pytest.mark.asyncio
async def test_control_center_sdk_calls_tool_and_validates_receipt(monkeypatch):
    request = make_request()
    receipt = make_receipt(request)
    calls = []

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client):
        calls.append(("connect", url, http_client))
        yield (object(), object())

    class FakeSession:
        def __init__(self, read_stream, write_stream):
            del read_stream, write_stream

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            calls.append(("initialize",))

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {"structuredContent": receipt.model_dump(mode="json", by_alias=True)}

    monkeypatch.setattr(
        control_center_mcp,
        "streamable_http_client",
        fake_streamable_http_client,
    )
    monkeypatch.setattr(control_center_mcp, "ClientSession", FakeSession)
    client = ControlCenterPatrolMcpClient("https://control-center.test/mcp", "token")
    try:
        result = await client.submit_patrol_batch(request)
    finally:
        await client.aclose()

    assert result == receipt
    assert calls[-1] == (
        "submit_patrol_batch",
        request.model_dump(mode="json", by_alias=True),
    )


@pytest.mark.asyncio
async def test_control_center_sdk_rejects_another_batch(monkeypatch):
    request = make_request()
    receipt = make_receipt(request).model_copy(update={"patrol_batch_no": "another-batch"})

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client):
        del url, http_client
        yield (object(), object())

    class FakeSession:
        def __init__(self, *args):
            del args

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            del name, arguments
            return {"structuredContent": receipt.model_dump(mode="json", by_alias=True)}

    monkeypatch.setattr(
        control_center_mcp,
        "streamable_http_client",
        fake_streamable_http_client,
    )
    monkeypatch.setattr(control_center_mcp, "ClientSession", FakeSession)
    client = ControlCenterPatrolMcpClient("https://control-center.test/mcp", "token")
    try:
        with pytest.raises(ControlCenterContractError, match="another patrol batch"):
            await client.submit_patrol_batch(request)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_control_center_sdk_rejects_different_business_keys(monkeypatch):
    request = make_request()
    receipt = make_receipt(request)
    changed = receipt.results[0].model_copy(update={"parent_asin": "B0DIFFERENT"})
    receipt = receipt.model_copy(update={"results": [changed]})

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client):
        del url, http_client
        yield (object(), object())

    class FakeSession:
        def __init__(self, *args):
            del args

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            del name, arguments
            return {"structuredContent": receipt.model_dump(mode="json", by_alias=True)}

    monkeypatch.setattr(
        control_center_mcp,
        "streamable_http_client",
        fake_streamable_http_client,
    )
    monkeypatch.setattr(control_center_mcp, "ClientSession", FakeSession)
    client = ControlCenterPatrolMcpClient("https://control-center.test/mcp", "token")
    try:
        with pytest.raises(ControlCenterContractError, match="business keys"):
            await client.submit_patrol_batch(request)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_control_center_sdk_normalizes_transport_exception_group(monkeypatch):
    request = make_request()

    @asynccontextmanager
    async def failing_transport(url, *, http_client):
        del url, http_client
        raise ExceptionGroup("cleanup", [RuntimeError("connection closed")])
        yield

    monkeypatch.setattr(control_center_mcp, "streamable_http_client", failing_transport)
    client = ControlCenterPatrolMcpClient("https://control-center.test/mcp", "token")
    try:
        with pytest.raises(ControlCenterUnavailable):
            await client.submit_patrol_batch(request)
    finally:
        await client.aclose()
