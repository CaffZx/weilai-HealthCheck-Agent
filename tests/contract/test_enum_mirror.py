"""契约枚举镜像测试。

core/enums.py 是 contracts/upstream/ 下注册表的抄写件。
本测试保证抄写没有走样 —— 集合相等，多一个少一个都算失败。

这是 CI 必跑项。谁在 enums.py 里加了契约没有的取值，这里立刻红。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.enums import (
    DECISION_GATE_ORDER,
    GOVERNANCE,
    MODE_VARIANTS,
    ActionTimingStatus,
    AllowedActionDomain,
    ApprovalPath,
    CapabilityCode,
    ChildExceptionType,
    ConstraintFlag,
    EconomicExposureWindow,
    ExecutionReadiness,
    ExecutionStatus,
    GateStatus,
    GuardrailResultStatus,
    HumanAttention,
    HumanGate,
    OperatingModeCode,
    OperatingModeVariantCode,
    OutcomeStatus,
    PermittedNextStep,
    ReasonCode,
    ReplenishmentPolicy,
    RoutingMode,
    SeasonalOperatingPosture,
    Severity,
    SignalState,
    SignalType,
)

pytestmark = pytest.mark.contract

UPSTREAM = Path(__file__).resolve().parent.parent.parent / "contracts" / "upstream"


def load(name: str) -> dict:
    with (UPSTREAM / name).open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def policy() -> dict:
    return load("operating-policy-registry.v1.json")


@pytest.fixture(scope="module")
def capability() -> dict:
    return load("capability-registry.v1.json")


@pytest.fixture(scope="module")
def inspection_v2() -> dict:
    return load("inspection-signal.v2.schema.json")


@pytest.fixture(scope="module")
def handoff_v2() -> dict:
    return load("handoff-directive.v2.schema.json")


@pytest.fixture(scope="module")
def decision_review() -> dict:
    return load("decision-review.v1.schema.json")


def values(enum_cls) -> set[str]:
    return {member.value for member in enum_cls}


# ---------------------------------------------------------------------------
# 经营模式
# ---------------------------------------------------------------------------
def test_operating_modes_mirror_policy_registry(policy):
    assert values(OperatingModeCode) == {m["code"] for m in policy["modes"]}


def test_mode_variants_mirror_policy_registry(policy):
    upstream = {m["code"]: tuple(m["variants"]) for m in policy["modes"]}
    ours = {code: tuple(variants) for code, variants in MODE_VARIANTS.items()}
    assert ours == upstream


def test_all_variants_are_declared(policy):
    upstream = {v for m in policy["modes"] for v in m["variants"]}
    assert values(OperatingModeVariantCode) == upstream


def test_immediate_exit_has_no_variant():
    assert MODE_VARIANTS[OperatingModeCode.IMMEDIATE_EXIT] == ()


# ---------------------------------------------------------------------------
# 队列字段与状态
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("enum_cls", "registry_key"),
    [
        (ReplenishmentPolicy, "replenishment_policies"),
        (SeasonalOperatingPosture, "seasonal_operating_postures"),
        (ActionTimingStatus, "action_timing_statuses"),
        (ExecutionReadiness, "execution_readiness_statuses"),
        (ConstraintFlag, "constraint_flags"),
        (ExecutionStatus, "execution_statuses"),
        (OutcomeStatus, "outcome_statuses"),
        (HumanGate, "human_gates"),
        (ApprovalPath, "approval_paths"),
    ],
)
def test_shared_enums_mirror_policy_registry(policy, enum_cls, registry_key):
    assert values(enum_cls) == set(policy[registry_key])


def test_decision_gate_order_is_exact(policy):
    # 顺序敏感：G0→G8 不得重排。
    assert list(DECISION_GATE_ORDER) == policy["decision_gates"]


def test_governance_flags_mirror_registry(policy):
    for key, expected in policy["governance"].items():
        assert GOVERNANCE[key] == expected, f"governance.{key} 与注册表不一致"


def test_composite_score_is_forbidden(policy):
    assert policy["governance"]["routine_queue_uses_composite_score"] is False
    assert GOVERNANCE["routine_queue_uses_composite_score"] is False


def test_advertising_is_frozen(policy):
    assert policy["governance"]["advertising_agent_rollout_status"] == "FROZEN"


# ---------------------------------------------------------------------------
# 能力与交接
# ---------------------------------------------------------------------------
def test_capabilities_mirror_registry(capability):
    assert values(CapabilityCode) == {c["code"] for c in capability["capabilities"]}


def test_reason_codes_mirror_registry(capability):
    assert values(ReasonCode) == set(capability["reason_codes"])


@pytest.mark.parametrize(
    ("enum_cls", "registry_key"),
    [
        (HumanAttention, "human_attention_codes"),
        (PermittedNextStep, "permitted_next_steps"),
        (RoutingMode, "routing_modes"),
        (ActionTimingStatus, "action_timing_status_codes"),
    ],
)
def test_handoff_enums_mirror_capability_registry(capability, enum_cls, registry_key):
    assert values(enum_cls) == set(capability[registry_key])


def test_handoff_v2_enums_match_our_enums(handoff_v2):
    props = handoff_v2["properties"]
    assert values(CapabilityCode) == set(props["next_capability"]["enum"])
    assert values(ReasonCode) == set(props["reason_code"]["enum"])
    assert values(HumanAttention) == set(props["human_attention"]["enum"])
    assert values(PermittedNextStep) == set(props["permitted_next_step"]["enum"])
    assert values(RoutingMode) == set(props["routing_mode"]["enum"])


def test_advertising_capability_is_frozen_in_registry(capability):
    entry = next(
        c for c in capability["capabilities"]
        if c["code"] == CapabilityCode.ADVERTISING_DECISION_EXECUTION.value
    )
    assert entry["rollout_status"] == "FROZEN_FOR_CURRENT_MIGRATION"


# ---------------------------------------------------------------------------
# 巡检信号
# ---------------------------------------------------------------------------
def test_inspection_signal_enums_match(inspection_v2):
    props = inspection_v2["properties"]
    assert values(Severity) == set(props["severity"]["enum"])
    assert values(SignalState) == set(props["signal_state"]["enum"])
    assert values(SignalType) == set(props["signal_type"]["enum"])
    assert values(ExecutionReadiness) == set(props["execution_readiness"]["enum"])
    assert values(ConstraintFlag) == set(props["constraint_flags"]["items"]["enum"])
    assert values(EconomicExposureWindow) == set(
        props["economic_exposure"]["properties"]["window"]["enum"]
    )
    assert values(ChildExceptionType) == set(
        props["child_scope"]["properties"]["exception_type"]["enum"]
    )


def test_severity_uses_contract_value_not_plan_value():
    """改造方案写的是 DATA_GAP，契约是 DATA_INSUFFICIENT。以契约为准。"""
    assert "DATA_INSUFFICIENT" in values(Severity)
    assert "DATA_GAP" not in values(Severity)


def test_retired_priority_fields_are_absent(inspection_v2):
    """v2 删除了综合执行分和 P0/P1/P2，任何形式都不得复活。"""
    props = inspection_v2["properties"]
    for retired in ("operating_priority", "priority_basis", "score"):
        assert retired not in props

    from core.contracts import InspectionSignal

    ours = set(InspectionSignal.model_fields)
    for retired in ("operating_priority", "priority_basis", "score", "operating_score"):
        assert retired not in ours


def test_our_inspection_signal_fields_match_contract(inspection_v2):
    from core.contracts import InspectionSignal

    contract_fields = set(inspection_v2["properties"])
    our_fields = set(InspectionSignal.model_fields)
    assert our_fields == contract_fields, (
        f"多出：{our_fields - contract_fields}；缺少：{contract_fields - our_fields}"
    )


def test_our_inspection_signal_required_matches(inspection_v2):
    from core.contracts import InspectionSignal

    contract_required = set(inspection_v2["required"])
    our_required = {
        name for name, field in InspectionSignal.model_fields.items() if field.is_required()
    }
    # contract_version 在我们这里有默认值（const），不需要调用方传。
    assert contract_required - our_required <= {"contract_version"}


# ---------------------------------------------------------------------------
# 复盘
# ---------------------------------------------------------------------------
def test_decision_review_enums_match(decision_review):
    props = decision_review["properties"]
    assert values(ExecutionStatus) == set(props["execution_status"]["enum"])
    assert values(OutcomeStatus) == set(props["outcome_status"]["enum"])
    assert values(GuardrailResultStatus) == set(
        props["parent_guardrail_results"]["items"]["properties"]["status"]["enum"]
    )
    child_outcomes = set(
        props["child_scope_results"]["items"]["properties"]["outcome_status"]["enum"]
    )
    from core.enums import CHILD_OUTCOME_STATUSES

    assert {v.value for v in CHILD_OUTCOME_STATUSES} == child_outcomes


def test_gate_status_matches_initialization_contract():
    init = load("operating-unit-initialization.v2.schema.json")
    upstream = set(init["$defs"]["gateResultBase"]["properties"]["status"]["enum"])
    assert values(GateStatus) == upstream


def test_allowed_action_domains_match_plan_contract():
    plan = load("approved-operating-plan.v1.schema.json")
    upstream = set(plan["properties"]["allowed_action_domains"]["items"]["enum"])
    assert values(AllowedActionDomain) == upstream
