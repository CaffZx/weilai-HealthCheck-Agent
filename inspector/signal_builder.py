"""AnomalySignal → 契约版 InspectionSignal v2。

把规则命中的"异常事实"升级成中控可消费的巡检信号：补齐四个确定性队列字段
（action_timing_status / economic_exposure / constraint_flags /
execution_readiness）和交接指令。

【严重度与任务排序必须分离】（knowledge/06 §5.2）
  severity 只描述异常事实的严重程度；排序由队列字段确定性计算。
  LLM 不得参与其中任何一个字段。

【不得复活综合分】
  这里没有、也不允许有任何"总分""权重和"字段。排序是多键比较，不是加权求和。
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from core.contracts import (
    AnomalySignal,
    ChildScope,
    DataGap,
    Diagnosis,
    EconomicExposure,
    HandoffDirective,
    InspectionSignal,
    OperatingFactSnapshot,
    OperatingUnitRef,
)
from core.enums import (
    ActionTimingStatus,
    ChildExceptionType,
    ConstraintFlag,
    EconomicExposureWindow,
    ExecutionReadiness,
    HumanAttention,
    Severity,
    SignalState,
    SignalType,
)
from inspector.issue_codes import routing_for

logger = logging.getLogger(__name__)

MONEY_QUANTUM = Decimal("0.0001")
CHILD_ASIN_FIELD_PATTERN = re.compile(r"\[([A-Z0-9]{8,32})\]$")


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)

_POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "queue_policy.yaml"
_policy_cache: dict[str, Any] | None = None


def load_queue_policy(force_reload: bool = False) -> dict[str, Any]:
    global _policy_cache
    if _policy_cache is None or force_reload:
        with _POLICY_PATH.open(encoding="utf-8") as handle:
            _policy_cache = yaml.safe_load(handle) or {}
    return _policy_cache


#: 点位 → 子体例外类型（child_scope.exception_type）
CHILD_EXCEPTION_BY_POINT: dict[str, ChildExceptionType] = {
    "FBA可售库存为0": ChildExceptionType.OUT_OF_STOCK,
    "库存不足": ChildExceptionType.LOW_STOCK,
    "滞销异常": ChildExceptionType.SLOW_MOVING,
    "库存积压": ChildExceptionType.SLOW_MOVING,
    "变体价差异常": ChildExceptionType.PRICE,
    "促销异常": ChildExceptionType.PRICE,
    "变体掉线": ChildExceptionType.VARIANT_RELATION,
    "父子体关系异常": ChildExceptionType.VARIANT_RELATION,
}

#: 点位 → 约束标记
CONSTRAINT_BY_POINT: dict[str, list[ConstraintFlag]] = {
    "FBA可售库存为0": [ConstraintFlag.STOCKOUT_RISK],
    "库存不足": [ConstraintFlag.STOCKOUT_RISK],
    "库存积压": [ConstraintFlag.CLEARANCE_DEADLINE],
    "滞销异常": [ConstraintFlag.CLEARANCE_DEADLINE],
    "链接不可售": [ConstraintFlag.CORE_CHILD_AT_RISK],
    "变体掉线": [ConstraintFlag.CORE_CHILD_AT_RISK],
    "合规异常": [ConstraintFlag.HUMAN_APPROVAL_REQUIRED],
}


class SignalBuilder:
    def __init__(
        self,
        *,
        producer_version: str,
        policy: dict[str, Any] | None = None,
    ) -> None:
        self.producer_version = producer_version
        self.policy = policy or load_queue_policy()

    # -- 主入口 ------------------------------------------------------------
    def build(
        self,
        *,
        anomaly: AnomalySignal,
        snapshot: OperatingFactSnapshot,
        scan_run_id: str,
        initialization_ready: bool = True,
        recurrence_count: int = 0,
        first_detected_at: datetime | None = None,
    ) -> InspectionSignal:
        now = datetime.now(UTC)
        unit = snapshot.operating_unit
        severity = Severity(anomaly.severity)

        timing = self._timing(severity)
        exposure = self._exposure(anomaly, snapshot, now)
        constraints = self._constraints(anomaly, snapshot)
        readiness = self._readiness(anomaly, snapshot, initialization_ready)

        return InspectionSignal(
            signal_id=anomaly.signal_id,
            operating_unit_ref=unit,
            child_scope=self._child_scope(anomaly),
            signal_type=SignalType(anomaly.signal_type),
            issue_code=anomaly.issue_code,
            severity=severity,
            signal_state=SignalState(anomaly.lifecycle_status),
            action_timing_status=timing,
            economic_exposure=exposure,
            constraint_flags=constraints,
            execution_readiness=readiness,
            first_detected_at=first_detected_at or now,
            last_detected_at=now,
            last_scan_run_id=scan_run_id,
            recurrence_count=recurrence_count,
            evidence_refs=list(anomaly.evidence_refs) or [snapshot.snapshot_id],
            diagnosis=self._diagnosis(anomaly, snapshot),
            handoff_directive=self._handoff(anomaly, unit, timing, scan_run_id),
            valid_until=now + timedelta(
                hours=int(self.policy["signal_validity_hours"][severity.value])
            ),
        )

    def build_data_gap_signal(
        self,
        *,
        gap: DataGap,
        snapshot: OperatingFactSnapshot,
        scan_run_id: str,
    ) -> InspectionSignal:
        """把数据缺口表达成 DATA_QUALITY 信号，交给 DATA_REPAIR 能力。"""
        now = datetime.now(UTC)
        unit = snapshot.operating_unit
        routing = dict(routing_for("目标配置待补"))
        routing["reason_code"] = routing["reason_code"]
        child_match = CHILD_ASIN_FIELD_PATTERN.search(gap.field)
        child_scope = (
            ChildScope(
                child_asins=[child_match.group(1)],
                exception_type=ChildExceptionType.OTHER,
            )
            if child_match
            else None
        )

        return InspectionSignal(
            signal_id=f"is_{uuid4().hex[:24]}",
            operating_unit_ref=unit,
            child_scope=child_scope,
            signal_type=SignalType.DATA_QUALITY,
            issue_code=gap.code,
            severity=Severity.DATA_INSUFFICIENT,
            signal_state=SignalState.NEW,
            action_timing_status=(
                ActionTimingStatus.DUE_TODAY if gap.blocking else ActionTimingStatus.SCHEDULED
            ),
            economic_exposure=EconomicExposure(
                currency=None,
                amount=None,
                window=EconomicExposureWindow.UNASSESSED,
                calculation_basis="数据缺口本身不产生直接经济暴露",
                as_of=now,
            ),
            constraint_flags=[ConstraintFlag.DATA_UNTRUSTED] if gap.blocking else [],
            execution_readiness=ExecutionReadiness.BLOCKED_DATA,
            first_detected_at=now,
            last_detected_at=now,
            last_scan_run_id=scan_run_id,
            recurrence_count=0,
            evidence_refs=[snapshot.snapshot_id],
            diagnosis=Diagnosis(
                summary=gap.impact,
                known_causes=[f"数据源缺失：{gap.source_tool or gap.field}"],
                unknowns=["缺口修复前无法确认该维度是否真的存在异常"],
                confidence=1.0,
            ),
            handoff_directive=HandoffDirective(
                handoff_id=f"hd_{uuid4().hex[:24]}",
                source_result_ref=f"{scan_run_id}:{gap.code}",
                target_ref=unit,
                next_capability=routing["next_capability"],
                reason_code=routing["reason_code"],
                requested_outcome=gap.repair_action,
                required_input_refs=[snapshot.snapshot_id],
                missing_input_codes=[gap.code],
                action_timing_status=(
                    ActionTimingStatus.DUE_TODAY if gap.blocking else ActionTimingStatus.SCHEDULED
                ),
                human_attention=(
                    HumanAttention.DECISION_REQUIRED if gap.blocking
                    else HumanAttention.REVIEW_RECOMMENDED
                ),
                permitted_next_step=routing["permitted_next_step"],
                routing_mode=routing["routing_mode"],
                expires_at=None,
                producer_versions={"business-inspection": self.producer_version},
            ),
        )

    # -- 队列字段 ----------------------------------------------------------
    def _timing(self, severity: Severity) -> ActionTimingStatus:
        return ActionTimingStatus(self.policy["severity_to_timing"][severity.value])

    def _constraints(
        self,
        anomaly: AnomalySignal,
        snapshot: OperatingFactSnapshot,
    ) -> list[ConstraintFlag]:
        flags: set[ConstraintFlag] = set(CONSTRAINT_BY_POINT.get(anomaly.point_code, []))

        if snapshot.is_blocking or Severity(anomaly.severity) is Severity.DATA_INSUFFICIENT:
            flags.add(ConstraintFlag.DATA_UNTRUSTED)

        # 净现金回收为负 → 必然立即退出（Ontology A4），这是最高优先级约束。
        recovery = (snapshot.profit or {}).get("best_feasible_net_cash_recovery")
        if recovery is not None and recovery < 0:
            flags.add(ConstraintFlag.NEGATIVE_CASH_RECOVERY)

        if Severity(anomaly.severity) is Severity.S0:
            flags.add(ConstraintFlag.HUMAN_APPROVAL_REQUIRED)

        return sorted(flags, key=lambda f: f.value)

    def _exposure(
        self,
        anomaly: AnomalySignal,
        snapshot: OperatingFactSnapshot,
        now: datetime,
    ) -> EconomicExposure:
        config = self.policy["economic_exposure"]
        sales = snapshot.sales or {}
        profit = snapshot.profit or {}
        inventory = snapshot.inventory or {}
        currency = profit.get("currency")

        point = anomaly.point_code

        if point in {"FBA可售库存为0", "库存不足"}:
            daily_units = sales.get("avg_daily_units_7d")
            unit_contribution = profit.get("unit_contribution")
            amount = (
                money(daily_units) * money(unit_contribution)
                if daily_units is not None and unit_contribution is not None
                else None
            )
            return self._exposure_or_unassessed(
                amount, currency, config["stockout"], now,
            )

        if point in {"库存积压", "滞销异常"}:
            fee = inventory.get("aged_storage_fee_monthly")
            return self._exposure_or_unassessed(
                fee, currency, config["overstock"], now,
            )

        if point == "ACOS异常":
            spend = profit.get("ad_spend_3d_avg")
            actual = profit.get("acos_3d")
            target = profit.get("target_acos")
            amount = None
            if all(v is not None for v in (spend, actual, target)) and actual > 0:
                spend_value = money(spend)
                actual_value = Decimal(str(actual))
                target_value = Decimal(str(target))
                amount = spend_value * max(
                    Decimal("0"), (actual_value - target_value) / actual_value
                )
            return self._exposure_or_unassessed(amount, currency, config["acos"], now)

        return EconomicExposure(
            currency=None,
            amount=None,
            window=EconomicExposureWindow.UNASSESSED,
            calculation_basis=config["default"]["basis"],
            as_of=now,
        )

    @staticmethod
    def _exposure_or_unassessed(
        amount: float | None,
        currency: str | None,
        config: dict[str, Any],
        now: datetime,
    ) -> EconomicExposure:
        """算不出金额时老实标 UNASSESSED —— 不是 0。

        0 会让排序把它当成"没有损失"沉到底部，而事实是"我们不知道"。
        """
        if amount is None:
            return EconomicExposure(
                currency=None,
                amount=None,
                window=EconomicExposureWindow.UNASSESSED,
                calculation_basis=f"{config['basis']}（缺少输入，未能计算）",
                as_of=now,
            )
        return EconomicExposure(
            currency=currency,
            amount=amount,
            window=EconomicExposureWindow(config["window"]),
            calculation_basis=config["basis"],
            as_of=now,
        )

    def _readiness(
        self,
        anomaly: AnomalySignal,
        snapshot: OperatingFactSnapshot,
        initialization_ready: bool,
    ) -> ExecutionReadiness:
        # 初始化未完成 → BLOCKED_DEPENDENCY（交接包 09 §6）
        if not initialization_ready:
            return ExecutionReadiness.BLOCKED_DEPENDENCY
        if snapshot.is_blocking or Severity(anomaly.severity) is Severity.DATA_INSUFFICIENT:
            return ExecutionReadiness.BLOCKED_DATA
        # 巡检不产生自动执行动作，一律等人。
        return ExecutionReadiness.READY_HUMAN

    # -- 诊断与交接 --------------------------------------------------------
    @staticmethod
    def _child_scope(anomaly: AnomalySignal) -> ChildScope | None:
        if not anomaly.child_asins:
            return None
        return ChildScope(
            child_asins=list(anomaly.child_asins),
            exception_type=CHILD_EXCEPTION_BY_POINT.get(
                anomaly.point_code, ChildExceptionType.OTHER,
            ),
        )

    @staticmethod
    def _diagnosis(anomaly: AnomalySignal, snapshot: OperatingFactSnapshot) -> Diagnosis:
        unknowns: list[str] = []
        if snapshot.data_gaps:
            unknowns.append(
                f"存在 {len(snapshot.data_gaps)} 项数据缺口，可能影响归因准确性"
            )
        if Severity(anomaly.severity) is Severity.DATA_INSUFFICIENT:
            unknowns.append("样本或配置不足，本条只做观察，不作为判定依据")

        # 置信度只反映"规则命中的确定性"，不是"原因判断的确定性"。
        confidence = 0.3 if Severity(anomaly.severity) is Severity.DATA_INSUFFICIENT else 0.9
        return Diagnosis(
            summary=anomaly.description or anomaly.point_code,
            known_causes=[anomaly.reason] if anomaly.reason else [],
            unknowns=unknowns,
            confidence=confidence,
        )

    def _handoff(
        self,
        anomaly: AnomalySignal,
        unit: OperatingUnitRef,
        timing: ActionTimingStatus,
        scan_run_id: str,
    ) -> HandoffDirective:
        routing = routing_for(anomaly.point_code)
        severity = Severity(anomaly.severity)

        if severity is Severity.S0:
            attention = HumanAttention.DECISION_REQUIRED
        elif severity is Severity.DATA_INSUFFICIENT:
            attention = HumanAttention.NONE
        else:
            attention = HumanAttention.REVIEW_RECOMMENDED

        return HandoffDirective(
            handoff_id=f"hd_{uuid4().hex[:24]}",
            source_result_ref=f"{scan_run_id}:{anomaly.signal_id}",
            target_ref=unit,
            next_capability=routing["next_capability"],
            reason_code=routing["reason_code"],
            requested_outcome=str(routing["requested_outcome"]),
            required_input_refs=list(anomaly.evidence_refs),
            missing_input_codes=[],
            action_timing_status=timing,
            human_attention=attention,
            permitted_next_step=routing["permitted_next_step"],
            routing_mode=routing["routing_mode"],
            expires_at=None,
            producer_versions={"business-inspection": self.producer_version},
        )

    # -- 排序 --------------------------------------------------------------
    def sort_key(self, signal: InspectionSignal) -> tuple:
        """确定性排序键。交接包 05 §5.2 的五段式，最后用 signal_id 打破并列。"""
        constraint_rank = self.policy["constraint_rank"]
        timing_rank = self.policy["timing_rank"]
        readiness_rank = self.policy["readiness_rank"]

        best_constraint = min(
            (constraint_rank.get(flag, 99) for flag in signal.constraint_flags),
            default=constraint_rank["_无约束"],
        )
        exposure = signal.economic_exposure.amount
        # 未评估的暴露排在有金额的后面，而不是当成 0 混在中间。
        exposure_key = -exposure if exposure is not None else float("inf")

        return (
            best_constraint,
            timing_rank[signal.action_timing_status],
            exposure_key,
            readiness_rank[signal.execution_readiness],
            signal.signal_id,
        )

    def sort(self, signals: list[InspectionSignal]) -> list[InspectionSignal]:
        return sorted(signals, key=self.sort_key)
