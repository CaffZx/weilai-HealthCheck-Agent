"""当前信号字段与确定性排序单元测试。"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from core.contracts import AnomalySignal, DataGap, OperatingFactSnapshot, sha256_json
from core.enums import (
    ActionTimingStatus,
    CapabilityCode,
    ConstraintFlag,
    DataQualityStatus,
    EconomicExposureWindow,
    ExecutionReadiness,
    RoutingMode,
    Severity,
    SignalState,
)
from inspector.signal_builder import SignalBuilder


def make_snapshot(unit, **kwargs) -> OperatingFactSnapshot:
    defaults = {
        "inventory": {"inventory_days_of_supply": 75.0, "aged_storage_fee_monthly": None},
        "sales": {"avg_daily_units_7d": 12.0, "freshness": "FRESH"},
        "profit": {
            "unit_contribution": 4.2,
            "best_feasible_net_cash_recovery": 5000.0,
            "currency": "USD",
            "acos_3d": 0.3,
            "target_acos": 0.25,
            "ad_spend_3d_avg": 30.0,
        },
    }
    defaults.update(kwargs)
    return OperatingFactSnapshot(
        snapshot_id=f"fs_{uuid4().hex[:24]}",
        operating_unit=unit,
        as_of_time=datetime.now(UTC),
        content_hash=sha256_json({"x": 1}),
        quality_status=defaults.pop("quality_status", DataQualityStatus.COMPLETE),
        completeness_score=defaults.pop("completeness_score", 1.0),
        data_gaps=defaults.pop("data_gaps", []),
        **defaults,
    )


def make_anomaly(point: str, issue: str, severity: Severity, child: str | None = None):
    return AnomalySignal(
        signal_id=f"is_{uuid4().hex[:24]}",
        category="2.4 库存与可售",
        point_code=point,
        issue_code=issue,
        target_type="CHILD_ASIN" if child else "PARENT_ASIN",
        target_id=child or "B0TEST123",
        child_asins=[child] if child else [],
        severity=severity,
        description=point,
        reason="rule hit",
        evidence_refs=["fs_x"],
        lifecycle_status=SignalState.NEW,
    )


@pytest.fixture
def builder() -> SignalBuilder:
    return SignalBuilder(producer_version="test/2.0.0")


def test_s0_maps_to_due_today(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("FBA可售库存为0", "FBA_AVAILABLE_ZERO", Severity.S0),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    assert signal.action_timing_status == ActionTimingStatus.DUE_TODAY
    assert ConstraintFlag.STOCKOUT_RISK in signal.constraint_flags
    assert ConstraintFlag.HUMAN_APPROVAL_REQUIRED in signal.constraint_flags


def test_data_insufficient_maps_to_monitor_only(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("退款异常", "REFUND_RATE_ABNORMAL", Severity.DATA_INSUFFICIENT),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    assert signal.action_timing_status == ActionTimingStatus.MONITOR_ONLY
    assert ConstraintFlag.DATA_UNTRUSTED in signal.constraint_flags
    assert signal.execution_readiness == ExecutionReadiness.BLOCKED_DATA


def test_child_specific_data_gap_has_distinct_child_scope(builder, unit):
    signal = builder.build_data_gap_signal(
        gap=DataGap(
            code="MCP_KEYWORD_RANK_UNAVAILABLE",
            field="traffic.keyword_rank_series[B0CHILD01]",
            impact="卡位异常点位不可判",
            repair_action="重试关键词排名工具",
            blocking=False,
            source_tool="erp_listing_asin_keyword_rank_history",
        ),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_0123456789abcdef01234567",
    )

    assert signal.child_scope is not None
    assert signal.child_scope.child_asins == ["B0CHILD01"]


def test_negative_cash_recovery_flags_exit_constraint(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("库存积压", "INVENTORY_OVERSTOCK", Severity.S1),
        snapshot=make_snapshot(unit, profit={"best_feasible_net_cash_recovery": -100.0}),
        scan_run_id="pr_x",
    )
    assert ConstraintFlag.NEGATIVE_CASH_RECOVERY in signal.constraint_flags


def test_stockout_exposure_is_computed(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("库存不足", "INVENTORY_SHORTAGE", Severity.S1),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    assert signal.economic_exposure.amount == Decimal("50.4000")
    assert signal.economic_exposure.window == EconomicExposureWindow.PER_DAY
    assert signal.economic_exposure.currency == "USD"


def test_unassessed_exposure_is_null_not_zero(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("库存不足", "INVENTORY_SHORTAGE", Severity.S1),
        snapshot=make_snapshot(unit, profit={"unit_contribution": None}),
        scan_run_id="pr_x",
    )
    assert signal.economic_exposure.amount is None
    assert signal.economic_exposure.window == EconomicExposureWindow.UNASSESSED


def test_initialization_not_ready_blocks_dependency(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("库存不足", "INVENTORY_SHORTAGE", Severity.S1),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
        initialization_ready=False,
    )
    assert signal.execution_readiness == ExecutionReadiness.BLOCKED_DEPENDENCY


def test_readiness_never_ready_auto(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("库存不足", "INVENTORY_SHORTAGE", Severity.S1),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    assert signal.execution_readiness == ExecutionReadiness.READY_HUMAN


def test_advertising_handoff_is_human_only(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("ACOS异常", "ACOS_ABNORMAL", Severity.S1),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    assert signal.handoff_directive.next_capability == CapabilityCode.ADVERTISING_DECISION_EXECUTION
    assert signal.handoff_directive.routing_mode == RoutingMode.HUMAN_ONLY


def test_inventory_signal_routes_to_inventory_planning(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("库存不足", "INVENTORY_SHORTAGE", Severity.S1),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    assert signal.handoff_directive.next_capability == CapabilityCode.INVENTORY_PLANNING


def test_compliance_signal_requires_human_decision(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("合规异常", "COMPLIANCE_ISSUE", Severity.S0),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    assert signal.handoff_directive.next_capability == CapabilityCode.HUMAN_REVIEW
    assert signal.handoff_directive.human_attention == "DECISION_REQUIRED"


def test_child_scope_is_attached(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("库存不足", "INVENTORY_SHORTAGE", Severity.S1, child="B0CHILD01"),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    assert signal.child_scope.child_asins == ["B0CHILD01"]
    assert signal.child_scope.exception_type == "LOW_STOCK"


def test_sort_puts_hard_constraints_first(builder, unit):
    low_priority = builder.build(
        anomaly=make_anomaly("退款异常", "REFUND_RATE_ABNORMAL", Severity.S2),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    cash_negative = builder.build(
        anomaly=make_anomaly("库存积压", "INVENTORY_OVERSTOCK", Severity.S2),
        snapshot=make_snapshot(unit, profit={"best_feasible_net_cash_recovery": -50.0}),
        scan_run_id="pr_x",
    )
    assert builder.sort([low_priority, cash_negative])[0].issue_code == "INVENTORY_OVERSTOCK"


def test_sort_is_stable_and_replayable(builder, unit):
    snapshot = make_snapshot(unit)
    signals = [
        builder.build(
            anomaly=make_anomaly(point, code, severity),
            snapshot=snapshot,
            scan_run_id="pr_x",
        )
        for point, code, severity in [
            ("退款异常", "REFUND_RATE_ABNORMAL", Severity.S2),
            ("库存不足", "INVENTORY_SHORTAGE", Severity.S1),
            ("链接不可售", "LISTING_NOT_SELLABLE", Severity.S0),
        ]
    ]
    first = [signal.signal_id for signal in builder.sort(signals)]
    second = [signal.signal_id for signal in builder.sort(list(reversed(signals)))]
    assert first == second


def test_unassessed_exposure_sorts_after_known_exposure(builder, unit):
    known = builder.build(
        anomaly=make_anomaly("库存不足", "INVENTORY_SHORTAGE", Severity.S1),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    unknown = builder.build(
        anomaly=make_anomaly("FBA可售库存为0", "FBA_AVAILABLE_ZERO", Severity.S1),
        snapshot=make_snapshot(unit, profit={"unit_contribution": None}),
        scan_run_id="pr_x",
    )
    assert builder.sort([unknown, known])[0].economic_exposure.amount is not None


def test_no_composite_score_field_exists(builder, unit):
    signal = builder.build(
        anomaly=make_anomaly("库存不足", "INVENTORY_SHORTAGE", Severity.S1),
        snapshot=make_snapshot(unit),
        scan_run_id="pr_x",
    )
    payload = signal.model_dump(mode="json")
    for banned in ("score", "operating_priority", "priority_basis", "综合执行分"):
        assert banned not in payload
