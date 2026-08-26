from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import httpx

import clients.operating_mode_mcp as operating_mode_mcp
from clients.operating_mode_mcp import (
    CurrentOperatingMode,
    OperatingModeEvaluationFailure,
    OperatingModeEvaluationJob,
    OperatingModeEvaluationJobStarted,
    OperatingModeMcpClient,
)
from core.control_center_contracts import (
    PatrolUnitPayload,
    apply_current_operating_mode,
)
from core.errors import AgentOperatingUnitMismatch, ContractInvalid, ModeAgentUnavailable
from scripts.mock_operating_mode_mcp import load_lookup_records

ROOT = Path(__file__).resolve().parent.parent.parent
MOCK_PATH = ROOT / "contracts/examples/operating-mode-mcp.v1.mock.json"


def current_payload(**overrides):
    payload = {
        "shop_id": "1622",
        "parent_asin": "B0TEST123",
        "parent_seller_sku": "PSKU-001",
        "decision_status": "DECIDED",
        "recommended_mode_code": "ACTIVE_ADVANCE",
        "recommended_mode": "积极推进",
        "explanation": "当前处于增长阶段",
    }
    payload.update(overrides)
    return payload


def make_patrol_unit() -> PatrolUnitPayload:
    from tests.unit.test_control_center_review import make_patrol_unit_payload

    return PatrolUnitPayload.model_validate(make_patrol_unit_payload())


def operating_mode_mock() -> dict:
    return json.loads(MOCK_PATH.read_text(encoding="utf-8"))


def test_operating_mode_mock_is_explicitly_simulated_and_loadable():
    payload = operating_mode_mock()
    records = load_lookup_records(MOCK_PATH)

    assert payload["contractVersion"] == "1.0"
    assert payload["isSimulated"] is True
    assert len(records) == 5
    assert records[("99004004", "B0EXAMPLE0", "DEMO-NONE-P")] is None


@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_mode"),
    [
        ("decided", "DECIDED", "ACTIVE_ADVANCE"),
        ("blocked", "BLOCKED", None),
        ("needsHumanReview", "NEEDS_HUMAN_REVIEW", None),
    ],
)
def test_operating_mode_mock_lookup_results_match_runtime_contract(
    scenario,
    expected_status,
    expected_mode,
):
    result = operating_mode_mock()["getCurrentOperatingMode"][scenario]["result"]

    current = CurrentOperatingMode.model_validate(result)

    assert current.decision_status == expected_status
    assert current.recommended_mode_code == expected_mode


def test_operating_mode_mock_decided_result_maps_to_control_center_contract():
    result = operating_mode_mock()["getCurrentOperatingMode"]["decided"]["result"]
    current = CurrentOperatingMode.model_validate(result)
    unit = make_patrol_unit()
    unit.listing.business_model = None
    unit.proposal.recommended_business_model = None
    unit.proposal.mode_reason_summary = None

    enriched = apply_current_operating_mode(unit, current)

    assert enriched.listing.business_model == "CONTROLLED_GROWTH"
    assert enriched.proposal.recommended_business_model == "CONTROLLED_GROWTH"
    assert enriched.proposal.mode_confidence is None
    assert enriched.proposal.raw_analysis["operatingMode"] == {
        "sourceSystem": "amazon-operating-mode-agent",
        "sourceTool": "get_current_operating_mode",
        "sourceModeCode": "ACTIVE_ADVANCE",
        "controlCenterBusinessModel": "CONTROLLED_GROWTH",
        "decisionStatus": "DECIDED",
        "explanation": "模拟判断：增长信号、利润率与库存覆盖满足积极推进条件。",
    }


@pytest.mark.parametrize("scenario", ["blocked", "needsHumanReview"])
def test_operating_mode_mock_undecided_results_do_not_fill_control_center_mode(scenario):
    result = operating_mode_mock()["getCurrentOperatingMode"][scenario]["result"]
    current = CurrentOperatingMode.model_validate(result)
    unit = make_patrol_unit()
    unit.listing.business_model = None
    unit.proposal.recommended_business_model = None

    assert apply_current_operating_mode(unit, current) is unit


def test_operating_mode_evaluation_mock_preserves_request_and_snapshot_identity():
    evaluation = operating_mode_mock()["evaluateOperatingMode"]
    request = evaluation["arguments"]["request"]
    decided = evaluation["decidedResult"]

    assert decided["request_id"] == request["request_id"]
    assert decided["trace_id"] == request["trace_id"]
    assert decided["operating_unit_id"] == request["operating_unit"]["operating_unit_id"]
    assert decided["fact_snapshot_id"] == request["fact_snapshot"]["snapshot_id"]
    assert decided["decision_status"] == "DECIDED"
    assert decided["recommended_mode_code"] == "ACTIVE_ADVANCE"


def test_control_center_mode_adapter_maps_authoritative_active_advance():
    unit = make_patrol_unit()
    unit.listing.business_model = None
    unit.proposal.recommended_business_model = None
    unit.proposal.mode_reason_summary = None
    current = CurrentOperatingMode.model_validate(current_payload())

    enriched = apply_current_operating_mode(unit, current)

    assert enriched.listing.business_model == "CONTROLLED_GROWTH"
    assert enriched.proposal.recommended_business_model == "CONTROLLED_GROWTH"
    assert enriched.proposal.mode_confidence is None
    assert enriched.proposal.mode_reason_summary == "当前处于增长阶段"
    assert enriched.proposal.raw_analysis["operatingMode"]["sourceModeCode"] == (
        "ACTIVE_ADVANCE"
    )
    assert unit.listing.business_model is None


def test_control_center_mode_adapter_overrides_contribution_profit_from_agent():
    unit = make_patrol_unit()
    current = CurrentOperatingMode.model_validate(
        current_payload(parent_unit_contribution=4.5, currency="CNY")
    )

    enriched = apply_current_operating_mode(unit, current)

    expected = 4.5 * unit.operating_metric.sales_quantity
    assert enriched.operating_metric.contribution_profit == expected
    assert enriched.proposal.raw_analysis["operatingMetricContribution"] == {
        "source": "operating_mode_agent",
        "unitContribution": 4.5,
        "salesQuantity": unit.operating_metric.sales_quantity,
        "contributionProfit": expected,
        "currency": "CNY",
    }


@pytest.mark.parametrize("status", ["BLOCKED", "NEEDS_HUMAN_REVIEW"])
def test_control_center_mode_adapter_does_not_invent_undecided_mode(status):
    unit = make_patrol_unit()
    current = CurrentOperatingMode.model_validate(
        current_payload(
            decision_status=status,
            recommended_mode_code=None,
            recommended_mode=None,
        )
    )

    assert apply_current_operating_mode(unit, current) is unit
    assert apply_current_operating_mode(unit, None) is unit


def test_control_center_mode_adapter_no_fallback_when_undecided():
    """经营模式 Agent 未返回 DECIDED 时，不做回落，直接返回原 unit。"""
    unit = make_patrol_unit()
    unit.listing.business_model = None
    unit.proposal.recommended_business_model = None
    unit.proposal.raw_analysis = {"operatingModeCandidate": {
        "sourceTool": "erp_listing_advert_agent_config",
        "sourceModeName": "稳定经营",
    }}
    current = CurrentOperatingMode.model_validate(
        current_payload(
            decision_status="BLOCKED",
            recommended_mode_code=None,
            recommended_mode=None,
        )
    )

    enriched = apply_current_operating_mode(unit, current)

    # 不做回落，business_model 保持 None
    assert enriched.listing.business_model is None
    assert enriched.proposal.recommended_business_model is None


def test_control_center_mode_adapter_rejects_conflicting_existing_mode():
    unit = make_patrol_unit()
    unit.listing.business_model = "STABLE_OPERATION"
    current = CurrentOperatingMode.model_validate(current_payload())

    with pytest.raises(ValueError, match="conflicts"):
        apply_current_operating_mode(unit, current)


@pytest.mark.asyncio
async def test_mode_mcp_client_calls_read_only_tool_and_validates_key(monkeypatch, binding):
    calls = []

    @asynccontextmanager
    async def fake_transport(url, *, http_client):
        calls.append(("connect", url, http_client))
        yield (object(), object())

    class FakeSession:
        def __init__(self, *args):
            del args

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            calls.append(("initialize",))

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {"structuredContent": {"result": current_payload()}}

    monkeypatch.setattr(operating_mode_mcp, "streamable_http_client", fake_transport)
    monkeypatch.setattr(operating_mode_mcp, "ClientSession", FakeSession)
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    try:
        result = await client.get_current_operating_mode(binding)
    finally:
        await client.aclose()

    assert result is not None
    assert result.recommended_mode_code == "ACTIVE_ADVANCE"
    assert calls[-1] == (
        "get_current_operating_mode",
        {
            "shop_id": "1622",
            "parent_asin": "B0TEST123",
            "parent_seller_sku": "PSKU-001",
        },
    )


@pytest.mark.asyncio
async def test_mode_mcp_client_accepts_no_current_record(monkeypatch, binding):
    @asynccontextmanager
    async def fake_transport(url, *, http_client):
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
            return {"structuredContent": {"result": None}}

    monkeypatch.setattr(operating_mode_mcp, "streamable_http_client", fake_transport)
    monkeypatch.setattr(operating_mode_mcp, "ClientSession", FakeSession)
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    try:
        assert await client.get_current_operating_mode(binding) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mode_mcp_client_batch_aligns_found_and_missing_in_request_order(monkeypatch):
    calls = []

    @asynccontextmanager
    async def fake_transport(url, *, http_client):
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
            calls.append((name, arguments))
            return {"structuredContent": {
                "requested_count": 2,
                "found_count": 1,
                "missing_count": 1,
                "results": [current_payload()],
                "missing_operating_units": [{
                    "shop_id": "1596",
                    "parent_asin": "B0MISSING1",
                    "parent_seller_sku": "SKU-MISSING",
                }],
            }}

    monkeypatch.setattr(operating_mode_mcp, "streamable_http_client", fake_transport)
    monkeypatch.setattr(operating_mode_mcp, "ClientSession", FakeSession)
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    try:
        results = await client.get_current_operating_modes([
            {
                "shop_id": "1596",
                "parent_asin": "B0MISSING1",
                "parent_seller_sku": "SKU-MISSING",
            },
            {
                "shop_id": "1622",
                "parent_asin": "B0TEST123",
                "parent_seller_sku": "PSKU-001",
            },
        ])
    finally:
        await client.aclose()

    assert results[0] is None
    assert results[1] is not None
    assert calls[0][0] == "get_current_operating_modes"
    assert len(calls[0][1]["operating_units"]) == 2


@pytest.mark.asyncio
async def test_mode_mcp_client_batch_rejects_inconsistent_counts(monkeypatch):
    @asynccontextmanager
    async def fake_transport(url, *, http_client):
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
            return {"structuredContent": {
                "requested_count": 2,
                "found_count": 1,
                "missing_count": 0,
                "results": [current_payload()],
                "missing_operating_units": [],
            }}

    monkeypatch.setattr(operating_mode_mcp, "streamable_http_client", fake_transport)
    monkeypatch.setattr(operating_mode_mcp, "ClientSession", FakeSession)
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    try:
        with pytest.raises(ContractInvalid, match="counts are inconsistent"):
            await client.get_current_operating_modes([
                {"shop_id": "1622", "parent_asin": "B0TEST123", "parent_seller_sku": "PSKU-001"},
                {"shop_id": "1596", "parent_asin": "B0MISSING1", "parent_seller_sku": "SKU-MISSING"},
            ])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mode_mcp_client_batch_rejects_mismatched_business_key(monkeypatch):
    @asynccontextmanager
    async def fake_transport(url, *, http_client):
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
            return {"structuredContent": {
                "requested_count": 1,
                "found_count": 1,
                "missing_count": 0,
                "results": [current_payload(parent_asin="B0DIFFERENT")],
                "missing_operating_units": [],
            }}

    monkeypatch.setattr(operating_mode_mcp, "streamable_http_client", fake_transport)
    monkeypatch.setattr(operating_mode_mcp, "ClientSession", FakeSession)
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    try:
        with pytest.raises(AgentOperatingUnitMismatch):
            await client.get_current_operating_modes([
                {"shop_id": "1622", "parent_asin": "B0TEST123", "parent_seller_sku": "PSKU-001"}
            ])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mode_mcp_client_evaluates_missing_then_queries_again(monkeypatch):
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    missing = {
        "shop_id": "1596",
        "parent_asin": "B0MISSING1",
        "parent_seller_sku": "SKU-MISSING",
    }
    existing = current_payload()
    evaluated = current_payload(**missing, recommended_mode_code="STABLE_OPERATION")
    queries = []
    started = []

    async def query(units):
        queries.append(units)
        if len(queries) == 1:
            return [CurrentOperatingMode.model_validate(existing), None]
        return [CurrentOperatingMode.model_validate(evaluated)]

    async def start(units):
        started.append(units)
        return OperatingModeEvaluationJobStarted(
            job_id="JOB-1",
            status="PENDING",
            requested_count=1,
            succeeded_count=0,
            failed_count=0,
        )

    async def wait(job_id, *, expected_units):
        assert job_id == "JOB-1"
        assert expected_units == [missing]
        return OperatingModeEvaluationJob(
            job_id=job_id,
            status="SUCCEEDED",
            requested_count=1,
            succeeded_count=1,
            failed_count=0,
            results=[CurrentOperatingMode.model_validate(evaluated)],
        )

    monkeypatch.setattr(client, "get_current_operating_modes", query)
    monkeypatch.setattr(client, "start_operating_mode_evaluation_job", start)
    monkeypatch.setattr(client, "_wait_for_evaluation_job", wait)
    try:
        results = await client.get_or_evaluate_operating_modes([
            {
                "shop_id": 1622,
                "parent_asin": "b0test123",
                "parent_seller_sku": "PSKU-001",
            },
            missing,
        ])
    finally:
        await client.aclose()

    assert len(queries) == 2
    assert queries[1] == [missing]
    assert started == [[missing]]
    assert results[0].recommended_mode_code == "ACTIVE_ADVANCE"
    assert results[1].recommended_mode_code == "STABLE_OPERATION"


@pytest.mark.asyncio
async def test_mode_mcp_client_does_not_retrigger_missing_during_cooldown(monkeypatch):
    client = OperatingModeMcpClient(
        "https://mode.test/mcp",
        "token",
        evaluation_cooldown_seconds=60,
    )
    missing = {
        "shop_id": "1596",
        "parent_asin": "B0MISSING1",
        "parent_seller_sku": "SKU-MISSING",
    }
    starts = 0

    async def query(units):
        return [None for _ in units]

    async def start(units):
        nonlocal starts
        starts += 1
        return OperatingModeEvaluationJobStarted(
            job_id="JOB-1",
            status="PENDING",
            requested_count=len(units),
            succeeded_count=0,
            failed_count=0,
        )

    async def wait(job_id, *, expected_units):
        return OperatingModeEvaluationJob(
            job_id=job_id,
            status="FAILED",
            requested_count=1,
            succeeded_count=0,
            failed_count=1,
            failures=[OperatingModeEvaluationFailure(
                **expected_units[0],
                error_code="REQUIRED_UPSTREAM_DATA_MISSING",
                message="missing upstream data",
            )],
        )

    monkeypatch.setattr(client, "get_current_operating_modes", query)
    monkeypatch.setattr(client, "start_operating_mode_evaluation_job", start)
    monkeypatch.setattr(client, "_wait_for_evaluation_job", wait)
    try:
        assert await client.get_or_evaluate_operating_modes([missing]) == [None]
        assert await client.get_or_evaluate_operating_modes([missing]) == [None]
    finally:
        await client.aclose()

    assert starts == 1


@pytest.mark.asyncio
async def test_mode_mcp_client_splits_evaluation_jobs_at_one_hundred(monkeypatch):
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    units = [
        {
            "shop_id": "1596",
            "parent_asin": f"B0MISSING{index:04d}",
            "parent_seller_sku": f"SKU-{index:04d}",
        }
        for index in range(101)
    ]
    chunks = []

    async def query(requested):
        return [None for _ in requested]

    async def start(chunk):
        chunks.append(chunk)
        return OperatingModeEvaluationJobStarted(
            job_id=f"JOB-{len(chunks)}",
            status="PENDING",
            requested_count=len(chunk),
            succeeded_count=0,
            failed_count=0,
        )

    async def wait(job_id, *, expected_units):
        failures = [
            OperatingModeEvaluationFailure(
                **item,
                error_code="REQUIRED_UPSTREAM_DATA_MISSING",
                message="missing upstream data",
            )
            for item in expected_units
        ]
        return OperatingModeEvaluationJob(
            job_id=job_id,
            status="FAILED",
            requested_count=len(expected_units),
            succeeded_count=0,
            failed_count=len(expected_units),
            failures=failures,
        )

    monkeypatch.setattr(client, "get_current_operating_modes", query)
    monkeypatch.setattr(client, "start_operating_mode_evaluation_job", start)
    monkeypatch.setattr(client, "_wait_for_evaluation_job", wait)
    try:
        assert await client.get_or_evaluate_operating_modes(units) == [None] * 101
    finally:
        await client.aclose()

    assert [len(chunk) for chunk in chunks] == [100, 1]


@pytest.mark.asyncio
async def test_mode_mcp_client_rejects_wrong_business_key(monkeypatch, binding):
    @asynccontextmanager
    async def fake_transport(url, *, http_client):
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
            return {
                "structuredContent": {
                    "result": current_payload(parent_asin="B0DIFFERENT")
                }
            }

    monkeypatch.setattr(operating_mode_mcp, "streamable_http_client", fake_transport)
    monkeypatch.setattr(operating_mode_mcp, "ClientSession", FakeSession)
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    try:
        with pytest.raises(AgentOperatingUnitMismatch):
            await client.get_current_operating_mode(binding)
    finally:
        await client.aclose()


def test_mode_mcp_client_requires_token():
    with pytest.raises(ValueError, match="token"):
        OperatingModeMcpClient("https://mode.test/mcp", "")


@pytest.mark.asyncio
async def test_mode_rest_client_queries_current_decision(binding):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/get-evaluate")
        assert json.loads(request.content) == {
            "shop_id": "1622",
            "parent_asin": "B0TEST123",
            "parent_seller_sku": "PSKU-001",
        }
        return httpx.Response(200, json=current_payload())

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OperatingModeMcpClient(
        "https://mode.test/api/v1/mode-decisions/get-evaluate",
        "token",
        client=http_client,
    )
    try:
        current = await client.get_current_operating_mode(binding)
        assert current is not None
        assert current.recommended_mode_code == "ACTIVE_ADVANCE"
    finally:
        await http_client.aclose()


def test_decided_mode_requires_code():
    with pytest.raises(ValueError, match="recommended_mode_code"):
        CurrentOperatingMode.model_validate(
            current_payload(recommended_mode_code=None)
        )


@pytest.mark.asyncio
async def test_mode_mcp_get_evaluate_uses_updated_single_unit_tool(monkeypatch, binding):
    @asynccontextmanager
    async def fake_transport(url, *, http_client):
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
            assert name == "get_evaluate"
            assert arguments == {
                "shop_id": "1622",
                "shop_account": "test-shop-account",
                "site_code": "AMAZON_US",
                "parent_asin": "B0TEST123",
                "parent_seller_sku": "PSKU-001",
            }
            return {"structuredContent": current_payload()}

    monkeypatch.setattr(operating_mode_mcp, "streamable_http_client", fake_transport)
    monkeypatch.setattr(operating_mode_mcp, "ClientSession", FakeSession)
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    try:
        current = await client.get_evaluate(binding)
        assert current.recommended_mode_code == "ACTIVE_ADVANCE"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mode_mcp_client_normalizes_transport_failure(monkeypatch, binding):
    @asynccontextmanager
    async def failing_transport(url, *, http_client):
        del url, http_client
        raise ExceptionGroup("transport", [RuntimeError("connection closed")])
        yield

    monkeypatch.setattr(operating_mode_mcp, "streamable_http_client", failing_transport)
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    try:
        with pytest.raises(ModeAgentUnavailable):
            await client.get_current_operating_mode(binding)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mode_mcp_client_rejects_invalid_structured_result(monkeypatch, binding):
    @asynccontextmanager
    async def fake_transport(url, *, http_client):
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
            return {"structuredContent": {"result": {"shop_id": "1622"}}}

    monkeypatch.setattr(operating_mode_mcp, "streamable_http_client", fake_transport)
    monkeypatch.setattr(operating_mode_mcp, "ClientSession", FakeSession)
    client = OperatingModeMcpClient("https://mode.test/mcp", "token")
    try:
        with pytest.raises(ContractInvalid, match="response is invalid"):
            await client.get_current_operating_mode(binding)
    finally:
        await client.aclose()
