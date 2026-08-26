"""合同实例校验：我们产出的对象必须能通过交接包的 JSON Schema。

方案 §26.2 要求三方共享同一份版本化 Schema。
本测试用**上游 Schema** 校验**我们序列化出来的实例** —— 这是"共享"的实际含义。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from core.contracts import (
    ChildScope,
    Diagnosis,
    EconomicExposure,
    HandoffDirective,
    InspectionSignal,
    OperatingUnitRef,
)
from core.enums import (
    ActionTimingStatus,
    CapabilityCode,
    ChildExceptionType,
    ConstraintFlag,
    EconomicExposureWindow,
    ExecutionReadiness,
    HumanAttention,
    PermittedNextStep,
    ReasonCode,
    RoutingMode,
    Severity,
    SignalState,
    SignalType,
)

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent.parent
UPSTREAM = ROOT / "contracts" / "upstream"
EXPORTED = ROOT / "contracts"


@pytest.fixture(scope="module")
def registry() -> Registry:
    """把 upstream 下所有 schema 按 $id 注册，让相对 $ref 能解析。"""
    resources = []
    for path in UPSTREAM.glob("*.schema.json"):
        with path.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        resource = Resource.from_contents(schema)
        resources.append((schema["$id"], resource))
        # 相对文件名也注册一份，兼容 "handoff-directive.v2.schema.json" 这种 $ref
        resources.append((path.name, resource))
    return Registry().with_resources(resources)


def validator_for(name: str, registry: Registry) -> Draft202012Validator:
    with (UPSTREAM / name).open(encoding="utf-8") as handle:
        schema = json.load(handle)
    return Draft202012Validator(schema, registry=registry)


@pytest.fixture
def sample_unit() -> OperatingUnitRef:
    return OperatingUnitRef.derive(
        shop_id=1622,
        site_code="US",
        parent_asin="B0TEST123",
        parent_seller_sku="PSKU-1",
    )


@pytest.fixture
def sample_handoff(sample_unit: OperatingUnitRef) -> HandoffDirective:
    return HandoffDirective(
        handoff_id=f"hd_{uuid4().hex[:24]}",
        source_result_ref="pr_abc:is_def",
        target_ref=sample_unit,
        next_capability=CapabilityCode.INVENTORY_PLANNING,
        reason_code=ReasonCode.INVENTORY_ACTION_REQUIRED,
        requested_outcome="保护库存并避免继续放大断货风险",
        required_input_refs=["fs_0123456789abcdef01234567"],
        missing_input_codes=[],
        action_timing_status=ActionTimingStatus.DUE_TODAY,
        human_attention=HumanAttention.REVIEW_RECOMMENDED,
        permitted_next_step=PermittedNextStep.PRODUCE_RECOMMENDATION,
        routing_mode=RoutingMode.HUMAN_ONLY,
        expires_at=None,
        producer_versions={"business-inspection": "2.0.0"},
    )


@pytest.fixture
def sample_signal(
    sample_unit: OperatingUnitRef, sample_handoff: HandoffDirective
) -> InspectionSignal:
    now = datetime.now(UTC)
    return InspectionSignal(
        signal_id=f"is_{uuid4().hex[:24]}",
        operating_unit_ref=sample_unit,
        child_scope=ChildScope(
            child_asins=["B0CHILD01"],
            exception_type=ChildExceptionType.LOW_STOCK,
        ),
        signal_type=SignalType.ANOMALY,
        issue_code="INVENTORY_SHORTAGE",
        severity=Severity.S0,
        signal_state=SignalState.NEW,
        action_timing_status=ActionTimingStatus.DUE_TODAY,
        economic_exposure=EconomicExposure(
            currency="USD",
            amount=50.4,
            window=EconomicExposureWindow.PER_DAY,
            calculation_basis="日均销量 × 单位贡献",
            as_of=now,
        ),
        constraint_flags=[ConstraintFlag.STOCKOUT_RISK],
        execution_readiness=ExecutionReadiness.READY_HUMAN,
        first_detected_at=now,
        last_detected_at=now,
        last_scan_run_id="pr_0123456789abcdef01234567",
        recurrence_count=0,
        evidence_refs=["fs_0123456789abcdef01234567"],
        diagnosis=Diagnosis(
            summary="FBA 可售库存不足以覆盖未来 14 天预计消耗",
            known_causes=["近 7 天日均销量 12，可售库存 5"],
            unknowns=[],
            confidence=0.9,
        ),
        handoff_directive=sample_handoff,
        valid_until=None,
    )


# ---------------------------------------------------------------------------
# 上游 Schema 校验我们的实例
# ---------------------------------------------------------------------------
def test_operating_unit_ref_passes_upstream_schema(sample_unit, registry):
    validator = validator_for("operating-unit-ref.v2.schema.json", registry)
    validator.validate(sample_unit.model_dump(mode="json"))


def test_handoff_directive_passes_upstream_schema(sample_handoff, registry):
    validator = validator_for("handoff-directive.v2.schema.json", registry)
    validator.validate(sample_handoff.model_dump(mode="json"))


def test_inspection_signal_passes_upstream_schema(sample_signal, registry):
    validator = validator_for("inspection-signal.v2.schema.json", registry)
    validator.validate(sample_signal.model_dump(mode="json"))


def test_inspection_signal_without_child_scope_passes(sample_signal, registry):
    validator = validator_for("inspection-signal.v2.schema.json", registry)
    payload = sample_signal.model_copy(update={"child_scope": None}).model_dump(mode="json")
    validator.validate(payload)


def test_data_gap_signal_passes_upstream_schema(registry, sample_unit):
    """数据缺口信号也必须是合法的 InspectionSignal，不能是自造结构。"""
    from core.contracts import DataGap, OperatingFactSnapshot, sha256_json
    from core.enums import DataQualityStatus
    from inspector.signal_builder import SignalBuilder

    snapshot = OperatingFactSnapshot(
        snapshot_id=f"fs_{uuid4().hex[:24]}",
        operating_unit=sample_unit,
        as_of_time=datetime.now(UTC),
        content_hash=sha256_json({}),
        quality_status=DataQualityStatus.PARTIAL,
        completeness_score=0.8,
    )
    gap = DataGap(
        code="ADVERTISING_TARGET_CONFIG_MISSING",
        field="profit.target_acos",
        impact="广告类异常只能出配置待补提示",
        repair_action="在 ERP 补齐广告目标配置",
        blocking=False,
    )
    signal = SignalBuilder(producer_version="2.0.0").build_data_gap_signal(
        gap=gap,
        snapshot=snapshot,
        scan_run_id="pr_0123456789abcdef01234567",
    )
    validator = validator_for("inspection-signal.v2.schema.json", registry)
    validator.validate(signal.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Schema 违规必须被拒绝（证明校验真的在起作用）
# ---------------------------------------------------------------------------
def test_invalid_signal_id_is_rejected(sample_signal, registry):
    from jsonschema.exceptions import ValidationError

    validator = validator_for("inspection-signal.v2.schema.json", registry)
    payload = sample_signal.model_dump(mode="json")
    payload["signal_id"] = "bad-id"
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_extra_field_is_rejected(sample_signal, registry):
    """additionalProperties=false —— 偷偷加字段绕过契约会被拦。"""
    from jsonschema.exceptions import ValidationError

    validator = validator_for("inspection-signal.v2.schema.json", registry)
    payload = sample_signal.model_dump(mode="json")
    payload["operating_priority"] = "P0"
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_pydantic_forbids_extra_fields(sample_unit):
    from pydantic import ValidationError

    payload = sample_unit.model_dump(mode="json")
    payload["shop_account"] = "sneaky"
    with pytest.raises(ValidationError):
        OperatingUnitRef.model_validate(payload)


# ---------------------------------------------------------------------------
# 导出的共享 Schema
# ---------------------------------------------------------------------------
def test_exported_schemas_are_up_to_date():
    """代码改了 Schema 没重导 → CI 红。"""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "scripts.export_contracts", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "filename",
    [
        "inspection-run.v1.schema.json",
        "inspection-result-envelope.v1.schema.json",
        "inspection-feedback-event.v1.schema.json",
        "inspection-result-ack.v1.schema.json",
        "operating-fact-snapshot.v1.schema.json",
        "inspection-signal.v2.projection.json",
        "control-center-submit-patrol-batch.v2.schema.json",
        "control-center-submit-patrol-batch-result.v1.schema.json",
        "patrol-review-request.v1.schema.json",
        "patrol-review-result.v1.schema.json",
    ],
)
def test_exported_schema_is_valid_draft_2020_12(filename):
    with (EXPORTED / filename).open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith(filename)


def test_mcp_input_acceptance_sample_schema_is_valid():
    filename = "mcp-input-acceptance-sample.v1.schema.json"
    with (EXPORTED / filename).open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith(filename)


def test_control_center_v2_example_matches_model_and_schema():
    from core.control_center_contracts import SubmitPatrolBatchRequest

    example_name = "control-center-submit-patrol-batch.v2.example.json"
    schema_name = "control-center-submit-patrol-batch.v2.schema.json"
    with (EXPORTED / "examples" / example_name).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    with (EXPORTED / schema_name).open(encoding="utf-8") as handle:
        schema = json.load(handle)

    request = SubmitPatrolBatchRequest.model_validate(payload)
    Draft202012Validator(schema).validate(request.model_dump(mode="json", by_alias=True))

    unit = request.units[0]
    assert unit.listing.shop_id.startswith("SIM-")
    assert unit.listing.owner_user_id.startswith("SIM-")
    assert {snapshot.snapshot_type for snapshot in unit.fact_snapshots} >= {
        "SALES",
        "ADVERTISING",
        "PROFIT",
        "INVENTORY",
    }
    anomaly_uids = {anomaly.anomaly_uid for anomaly in unit.proposal.anomalies}
    assert anomaly_uids
    assert all(detail.anomaly_uid in anomaly_uids for detail in unit.proposal.details)
    assert unit.proposal.raw_analysis["isSimulated"] is True
    assert "token" not in json.dumps(payload, ensure_ascii=False).casefold()


def test_mcp_input_acceptance_example_remains_accepted():
    from scripts.validate_mcp_input_sample import validate_file

    result = validate_file(EXPORTED / "examples" / "mcp-input-acceptance-sample.v1.example.json")
    assert result["status"] == "ACCEPTED_WITH_GAPS"
    assert result["blocking_gap_count"] == 0
    assert all(result["critical_field_coverage"].values())
    assert result["errors"] == []


def test_operating_unit_id_matches_official_vectors():
    """身份派生算法必须和交接包给的测试向量逐字一致。"""
    with (UPSTREAM / "operating-unit-ref.v2.vectors.json").open(encoding="utf-8") as handle:
        vectors = json.load(handle)

    for case in vectors.get("valid", []):
        given = case["input"]
        expected = case["normalized"]
        ref = OperatingUnitRef.derive(
            shop_id=given["shop_id"],
            site_code=given["site_code"],
            parent_asin=given["parent_asin"],
            parent_seller_sku=given.get("parent_seller_sku"),
        )
        assert ref.operating_unit_id == expected["operating_unit_id"]
        assert ref.site_code == expected["site_code"]
        assert ref.parent_asin == expected["parent_asin"]
        assert ref.parent_seller_sku == expected.get("parent_seller_sku")


def test_operating_unit_id_uses_only_control_center_business_key():
    us = OperatingUnitRef.derive(
        shop_id=1622,
        site_code="US",
        parent_asin="B0TEST",
        parent_seller_sku="SKU-001",
    )
    uk = OperatingUnitRef.derive(
        shop_id=1622,
        site_code="UK",
        parent_asin="B0TEST",
        parent_seller_sku="SKU-001",
    )
    another_sku = OperatingUnitRef.derive(
        shop_id=1622,
        site_code="US",
        parent_asin="B0TEST",
        parent_seller_sku="SKU-002",
    )
    assert us.operating_unit_id == uk.operating_unit_id
    assert us.operating_unit_id != another_sku.operating_unit_id


def test_invalid_identity_vectors_are_rejected():
    from core.operating_unit import OperatingUnitIdentityError

    with (UPSTREAM / "operating-unit-ref.v2.vectors.json").open(encoding="utf-8") as handle:
        vectors = json.load(handle)

    for case in vectors.get("invalid", []):
        given = case["input"]
        with pytest.raises((OperatingUnitIdentityError, ValueError)):
            OperatingUnitRef.derive(
                shop_id=given.get("shop_id"),
                site_code=given.get("site_code"),
                parent_asin=given.get("parent_asin"),
                parent_seller_sku=given.get("parent_seller_sku"),
            )
