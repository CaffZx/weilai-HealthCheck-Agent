"""统一枚举 — 交接包机器契约的 Python 镜像。

【权威来源】本文件所有取值逐字镜像自：
  contracts/operating-policy-registry.v1.json
  contracts/capability-registry.v1.json
  contracts/inspection-signal.v2.schema.json
  contracts/handoff-directive.v2.schema.json
  contracts/decision-review.v1.schema.json

【维护铁律】
  1. 本文件不是"设计"，是"抄写"。改任何取值必须先改 contracts/ 下的注册表，
     再同步本文件，最后跑 tests/contract/test_enum_mirror.py 验证集合相等。
  2. 禁止在本文件新增契约里没有的取值。巡检 Agent 不拥有规则定义权
     （Ontology A12：Agent MUST_NOT_PUBLISH CanonicalRule）。
  3. 已退役枚举（P0/P1/P2 经营优先级、T0-T3 权重、综合执行分）不得出现在本文件，
     也不得以任何别名形式复活（inspection-signal v2 已删除 operating_priority /
     priority_basis / score 三个字段，契约一致性测试会直接拦截）。
"""
from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------------------
# 契约版本常量（各 schema 的 contract_version const）
# ---------------------------------------------------------------------------
CONTRACT_VERSION_OPERATING_UNIT = "amazon_ops.v2"
CONTRACT_VERSION_INSPECTION = "amazon_ops.inspection.v2"
CONTRACT_VERSION_HANDOFF = "amazon_ops.handoff.v2"
CONTRACT_VERSION_APPROVED_PLAN = "amazon_ops.approved_plan.v1"
CONTRACT_VERSION_DECISION_REVIEW = "amazon_ops.decision_review.v1"
CONTRACT_VERSION_RULE_CHANGE_PROPOSAL = "amazon_ops.rule_change_proposal.v1"
CONTRACT_VERSION_INITIALIZATION = "amazon_ops.initialization.v2"

# 巡检 Agent 与中控之间的私有合同版本（不在交接包 contracts/ 内，由本项目导出）
CONTRACT_VERSION_PATROL_PACKAGE = "amazon_ops.patrol_package.v1"
CONTRACT_VERSION_MODE_DECISION = "amazon_ops.mode_decision.v1"
CONTRACT_VERSION_ADVERTISING_PROPOSAL = "amazon_ops.advertising_proposal.v1"
CONTRACT_VERSION_INSPECTION_RUN = "amazon_ops.inspection_run.v1"
CONTRACT_VERSION_INSPECTION_RESULT = "amazon_ops.inspection_result.v1"
CONTRACT_VERSION_INSPECTION_FEEDBACK = "amazon_ops.inspection_feedback.v1"


# ---------------------------------------------------------------------------
# A. 经营模式与变体
# ---------------------------------------------------------------------------
class OperatingModeCode(StrEnum):
    """经营模式（7 个）。来源：operating-policy-registry.v1.json#modes[].code"""

    NEW_PRODUCT_VALIDATION = "NEW_PRODUCT_VALIDATION"      # 新品验证
    IMMEDIATE_EXIT = "IMMEDIATE_EXIT"                      # 立即退出
    CONTROLLED_CLEARANCE = "CONTROLLED_CLEARANCE"          # 控制清货
    TIME_BOXED_REPAIR = "TIME_BOXED_REPAIR"                # 限时修复
    STABLE_OPERATION = "STABLE_OPERATION"                  # 稳定经营
    ACTIVE_ADVANCE = "ACTIVE_ADVANCE"                      # 积极推进
    PROFIT_HARVEST = "PROFIT_HARVEST"                      # 获取利润


MODE_NAMES_ZH: dict[str, str] = {
    OperatingModeCode.NEW_PRODUCT_VALIDATION: "新品验证",
    OperatingModeCode.IMMEDIATE_EXIT: "立即退出",
    OperatingModeCode.CONTROLLED_CLEARANCE: "控制清货",
    OperatingModeCode.TIME_BOXED_REPAIR: "限时修复",
    OperatingModeCode.STABLE_OPERATION: "稳定经营",
    OperatingModeCode.ACTIVE_ADVANCE: "积极推进",
    OperatingModeCode.PROFIT_HARVEST: "获取利润",
}


class OperatingModeVariantCode(StrEnum):
    """经营模式变体（16 个）。IMMEDIATE_EXIT 无变体，恒为 None。"""

    PROBE_VALIDATION = "PROBE_VALIDATION"
    STANDARD_VALIDATION = "STANDARD_VALIDATION"
    PRIORITY_VALIDATION = "PRIORITY_VALIDATION"
    VALUE_PRESERVING = "VALUE_PRESERVING"
    BALANCED_EXIT = "BALANCED_EXIT"
    ACCELERATED_EXIT = "ACCELERATED_EXIT"
    LIGHT_CORRECTION = "LIGHT_CORRECTION"
    FOCUSED_REPAIR = "FOCUSED_REPAIR"
    LAST_CHANCE_REPAIR = "LAST_CHANCE_REPAIR"
    LOW_TOUCH_MAINTAIN = "LOW_TOUCH_MAINTAIN"
    STANDARD_MAINTAIN = "STANDARD_MAINTAIN"
    CONTROLLED_SCALE = "CONTROLLED_SCALE"
    PRIORITY_SCALE = "PRIORITY_SCALE"
    EFFICIENCY_YIELD = "EFFICIENCY_YIELD"
    MARGIN_YIELD = "MARGIN_YIELD"
    CAPITAL_YIELD = "CAPITAL_YIELD"


MODE_VARIANTS: dict[str, tuple[str, ...]] = {
    OperatingModeCode.NEW_PRODUCT_VALIDATION: (
        OperatingModeVariantCode.PROBE_VALIDATION,
        OperatingModeVariantCode.STANDARD_VALIDATION,
        OperatingModeVariantCode.PRIORITY_VALIDATION,
    ),
    OperatingModeCode.IMMEDIATE_EXIT: (),
    OperatingModeCode.CONTROLLED_CLEARANCE: (
        OperatingModeVariantCode.VALUE_PRESERVING,
        OperatingModeVariantCode.BALANCED_EXIT,
        OperatingModeVariantCode.ACCELERATED_EXIT,
    ),
    OperatingModeCode.TIME_BOXED_REPAIR: (
        OperatingModeVariantCode.LIGHT_CORRECTION,
        OperatingModeVariantCode.FOCUSED_REPAIR,
        OperatingModeVariantCode.LAST_CHANCE_REPAIR,
    ),
    OperatingModeCode.STABLE_OPERATION: (
        OperatingModeVariantCode.LOW_TOUCH_MAINTAIN,
        OperatingModeVariantCode.STANDARD_MAINTAIN,
    ),
    OperatingModeCode.ACTIVE_ADVANCE: (
        OperatingModeVariantCode.CONTROLLED_SCALE,
        OperatingModeVariantCode.PRIORITY_SCALE,
    ),
    OperatingModeCode.PROFIT_HARVEST: (
        OperatingModeVariantCode.EFFICIENCY_YIELD,
        OperatingModeVariantCode.MARGIN_YIELD,
        OperatingModeVariantCode.CAPITAL_YIELD,
    ),
}

#: 停止补货只能落到这两个模式（Ontology A3 / governance.stop_replenishment_requires_clearance_or_exit）
EXIT_ORIENTED_MODES: frozenset[str] = frozenset({
    OperatingModeCode.CONTROLLED_CLEARANCE,
    OperatingModeCode.IMMEDIATE_EXIT,
})


class ReplenishmentPolicy(StrEnum):
    CONTINUE = "CONTINUE"
    PAUSE = "PAUSE"
    STOP = "STOP"


class ChildInventoryDisposition(StrEnum):
    KEEP_ACTIVE = "KEEP_ACTIVE"
    HOLD_AND_OBSERVE = "HOLD_AND_OBSERVE"
    CONTROLLED_CLEARANCE = "CONTROLLED_CLEARANCE"
    IMMEDIATE_EXIT = "IMMEDIATE_EXIT"


class SeasonalOperatingPosture(StrEnum):
    PRE_SEASON_BUILD = "PRE_SEASON_BUILD"
    IN_SEASON_EXECUTE = "IN_SEASON_EXECUTE"
    POST_SEASON_EXIT = "POST_SEASON_EXIT"
    OFF_SEASON_HOLD = "OFF_SEASON_HOLD"
    NON_SEASONAL = "NON_SEASONAL"
    UNDETERMINED = "UNDETERMINED"


class NewProductValidationStage(StrEnum):
    ASIN_READY = "ASIN_READY"
    LAUNCH_READY = "LAUNCH_READY"
    TRAFFIC_VALIDATION = "TRAFFIC_VALIDATION"
    CONVERSION_VALIDATION = "CONVERSION_VALIDATION"
    UNIT_ECONOMICS_VALIDATION = "UNIT_ECONOMICS_VALIDATION"
    DECISION_READY = "DECISION_READY"


class ExitRoute(StrEnum):
    LIQUIDATE = "LIQUIDATE"
    REMOVE = "REMOVE"
    RETURN = "RETURN"
    DISPOSE = "DISPOSE"
    TRANSFER_CHANNEL = "TRANSFER_CHANNEL"


# ---------------------------------------------------------------------------
# B. 严重度 / 时点 / 经济暴露
# ---------------------------------------------------------------------------
class Severity(StrEnum):
    """inspection-signal.v2#severity。

    注意：改造方案 v1.0 §6 写的是 DATA_GAP，但机器契约取值是 DATA_INSUFFICIENT。
    以契约为准（见 docs/05-与交接包契约的偏差说明.md 偏差 D-01）。
    """

    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


class ActionTimingStatus(StrEnum):
    OVERDUE = "OVERDUE"
    DUE_TODAY = "DUE_TODAY"
    DUE_SOON = "DUE_SOON"
    SCHEDULED = "SCHEDULED"
    MONITOR_ONLY = "MONITOR_ONLY"


class EconomicExposureWindow(StrEnum):
    PER_DAY = "PER_DAY"
    CURRENT_CYCLE = "CURRENT_CYCLE"
    OPPORTUNITY_WINDOW = "OPPORTUNITY_WINDOW"
    UNASSESSED = "UNASSESSED"


class ConstraintFlag(StrEnum):
    NEGATIVE_CASH_RECOVERY = "NEGATIVE_CASH_RECOVERY"
    STOCKOUT_RISK = "STOCKOUT_RISK"
    CLEARANCE_DEADLINE = "CLEARANCE_DEADLINE"
    SEASONAL_WINDOW_CLOSING = "SEASONAL_WINDOW_CLOSING"
    REPAIR_WINDOW_ENDING = "REPAIR_WINDOW_ENDING"
    GROWTH_STOP_LOSS = "GROWTH_STOP_LOSS"
    PROTECTION_FLOOR_BREACHED = "PROTECTION_FLOOR_BREACHED"
    CORE_CHILD_AT_RISK = "CORE_CHILD_AT_RISK"
    DATA_UNTRUSTED = "DATA_UNTRUSTED"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"


class ExecutionReadiness(StrEnum):
    READY_AUTO = "READY_AUTO"
    READY_HUMAN = "READY_HUMAN"
    BLOCKED_DATA = "BLOCKED_DATA"
    BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"
    MONITORING = "MONITORING"


# ---------------------------------------------------------------------------
# C. 人机分工
# ---------------------------------------------------------------------------
class HumanGate(StrEnum):
    H0 = "H0"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"


class HumanAttention(StrEnum):
    NONE = "NONE"
    REVIEW_RECOMMENDED = "REVIEW_RECOMMENDED"
    DECISION_REQUIRED = "DECISION_REQUIRED"


class PermittedNextStep(StrEnum):
    SUPPLY_EVIDENCE = "SUPPLY_EVIDENCE"
    PRODUCE_RECOMMENDATION = "PRODUCE_RECOMMENDATION"
    PROPOSE_ACTION = "PROPOSE_ACTION"
    EXECUTE_APPROVED_ACTION = "EXECUTE_APPROVED_ACTION"
    VERIFY_EXECUTION = "VERIFY_EXECUTION"
    HUMAN_DECISION = "HUMAN_DECISION"


class RoutingMode(StrEnum):
    HUMAN_ONLY = "HUMAN_ONLY"
    HUMAN_CONFIRM_THEN_ROUTER = "HUMAN_CONFIRM_THEN_ROUTER"
    AUTO_ROUTER = "AUTO_ROUTER"


class ApprovalPath(StrEnum):
    """注意：operating-policy-registry 有 4 值（含 PREAUTHORIZED），
    operating-unit-initialization.v2 的 approval_path 只允许 3 值。
    本枚举取注册表全集，写初始化对象时需自行排除 PREAUTHORIZED。"""

    AUTO_LIFECYCLE = "AUTO_LIFECYCLE"
    PREAUTHORIZED = "PREAUTHORIZED"
    BATCH_APPROVAL = "BATCH_APPROVAL"
    INDIVIDUAL_APPROVAL = "INDIVIDUAL_APPROVAL"


# ---------------------------------------------------------------------------
# D. 状态类
# ---------------------------------------------------------------------------
class SignalType(StrEnum):
    ANOMALY = "ANOMALY"
    OPPORTUNITY = "OPPORTUNITY"
    RECOVERY = "RECOVERY"
    RECURRENCE = "RECURRENCE"
    DATA_QUALITY = "DATA_QUALITY"
    OBSERVATION_DUE = "OBSERVATION_DUE"


class SignalState(StrEnum):
    NEW = "NEW"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    IN_PROGRESS = "IN_PROGRESS"
    OBSERVING = "OBSERVING"
    LONG_TERM_FOLLOW_UP = "LONG_TERM_FOLLOW_UP"
    AWAITING_RESCAN = "AWAITING_RESCAN"
    RESOLVED = "RESOLVED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    IGNORED = "IGNORED"
    INTERRUPTED = "INTERRUPTED"


class ChildExceptionType(StrEnum):
    OUT_OF_STOCK = "OUT_OF_STOCK"
    LOW_STOCK = "LOW_STOCK"
    SLOW_MOVING = "SLOW_MOVING"
    PRICE = "PRICE"
    COLOR_KEYWORD = "COLOR_KEYWORD"
    SIZE_KEYWORD = "SIZE_KEYWORD"
    VARIANT_RELATION = "VARIANT_RELATION"
    OTHER = "OTHER"


class ExecutionStatus(StrEnum):
    PLANNED = "PLANNED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OutcomeStatus(StrEnum):
    NOT_DUE = "NOT_DUE"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    ADVERSE_EFFECT = "ADVERSE_EFFECT"


#: 子体级 outcome 只允许这 5 个（decision-review.v1#child_scope_results[].outcome_status）
CHILD_OUTCOME_STATUSES: frozenset[str] = frozenset({
    OutcomeStatus.SUCCESS,
    OutcomeStatus.PARTIAL_SUCCESS,
    OutcomeStatus.FAILED,
    OutcomeStatus.INCONCLUSIVE,
    OutcomeStatus.ADVERSE_EFFECT,
})

#: 自动判定允许的 outcome（decision-review.v1 allOf[0]，排除 NOT_DUE / READY_FOR_REVIEW / INCONCLUSIVE）
AUTO_JUDGEABLE_OUTCOMES: frozenset[str] = frozenset({
    OutcomeStatus.SUCCESS,
    OutcomeStatus.PARTIAL_SUCCESS,
    OutcomeStatus.FAILED,
    OutcomeStatus.ADVERSE_EFFECT,
})


class GuardrailResultStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    BLOCKED = "BLOCKED"


class DecisionGateCode(StrEnum):
    """G0-G8，顺序敏感，不得重排（一致性测试 test_initialization_rejects_reordered_decision_gates）。"""

    G0_EVIDENCE_TRUST = "G0_EVIDENCE_TRUST"
    G1_DECISION_SCOPE = "G1_DECISION_SCOPE"
    G2_CASH_FEASIBILITY = "G2_CASH_FEASIBILITY"
    G3_REPLENISHMENT_ELIGIBILITY = "G3_REPLENISHMENT_ELIGIBILITY"
    G4_DEMAND_VALIDATION = "G4_DEMAND_VALIDATION"
    G5_REPAIRABILITY = "G5_REPAIRABILITY"
    G6_INCREMENTAL_OPPORTUNITY = "G6_INCREMENTAL_OPPORTUNITY"
    G7_PROFIT_HARVEST_FIT = "G7_PROFIT_HARVEST_FIT"
    G8_STABLE_OPERATION = "G8_STABLE_OPERATION"


DECISION_GATE_ORDER: tuple[str, ...] = tuple(code.value for code in DecisionGateCode)


# ---------------------------------------------------------------------------
# E. 能力与交接
# ---------------------------------------------------------------------------
class CapabilityCode(StrEnum):
    """capability-registry.v1.json#capabilities[].code，同时是 handoff v2 的 next_capability。"""

    OPERATING_FACT = "OPERATING_FACT"
    MARKET_RELATION = "MARKET_RELATION"
    MARKET_RESEARCH = "MARKET_RESEARCH"
    BUSINESS_INSPECTION = "BUSINESS_INSPECTION"
    COST_PROFIT_EVIDENCE = "COST_PROFIT_EVIDENCE"
    OPERATING_DECISION = "OPERATING_DECISION"
    ADVERTISING_DECISION_EXECUTION = "ADVERTISING_DECISION_EXECUTION"
    PRICE_PROMOTION = "PRICE_PROMOTION"
    INVENTORY_PLANNING = "INVENTORY_PLANNING"
    LISTING_EXECUTION = "LISTING_EXECUTION"
    PORTFOLIO_COORDINATION = "PORTFOLIO_COORDINATION"
    OUTCOME_REVIEW = "OUTCOME_REVIEW"
    DATA_REPAIR = "DATA_REPAIR"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class ReasonCode(StrEnum):
    """34 个原因码，必须与 capability-registry.v1.json#reason_codes 逐字相等。"""

    IDENTITY_REVIEW_REQUIRED = "IDENTITY_REVIEW_REQUIRED"
    MARKET_EVIDENCE_REQUIRED = "MARKET_EVIDENCE_REQUIRED"
    SEASON_REBASE_REQUIRED = "SEASON_REBASE_REQUIRED"
    COST_EVIDENCE_REQUIRED = "COST_EVIDENCE_REQUIRED"
    PROFIT_RECONCILIATION_REQUIRED = "PROFIT_RECONCILIATION_REQUIRED"
    MODE_REVIEW_REQUIRED = "MODE_REVIEW_REQUIRED"
    ADVERTISING_ACTION_REQUIRED = "ADVERTISING_ACTION_REQUIRED"
    PRICE_ACTION_REQUIRED = "PRICE_ACTION_REQUIRED"
    INVENTORY_ACTION_REQUIRED = "INVENTORY_ACTION_REQUIRED"
    LISTING_ACTION_REQUIRED = "LISTING_ACTION_REQUIRED"
    EXECUTION_VERIFICATION_REQUIRED = "EXECUTION_VERIFICATION_REQUIRED"
    OBSERVATION_DUE = "OBSERVATION_DUE"
    OUTCOME_READY = "OUTCOME_READY"
    ADVERSE_EFFECT_DETECTED = "ADVERSE_EFFECT_DETECTED"
    RULE_CHANGE_PROPOSED = "RULE_CHANGE_PROPOSED"
    ENVELOPE_NEAR_LIMIT = "ENVELOPE_NEAR_LIMIT"
    ENVELOPE_EXCEEDED = "ENVELOPE_EXCEEDED"
    CHILD_EXCEPTION_REQUIRED = "CHILD_EXCEPTION_REQUIRED"
    PORTFOLIO_COORDINATION_REQUIRED = "PORTFOLIO_COORDINATION_REQUIRED"
    INSPECTION_SIGNAL_DETECTED = "INSPECTION_SIGNAL_DETECTED"
    REQUIRED_CONFIGURATION_MISSING = "REQUIRED_CONFIGURATION_MISSING"
    MARKET_CONTEXT_READY = "MARKET_CONTEXT_READY"
    NEW_PRODUCT_PRIOR_READY = "NEW_PRODUCT_PRIOR_READY"
    MATERIAL_MARKET_CHANGE = "MATERIAL_MARKET_CHANGE"
    PURE_OFF_SEASON_CONFIRMED = "PURE_OFF_SEASON_CONFIRMED"
    CLEARANCE_WINDOW_UPDATED = "CLEARANCE_WINDOW_UPDATED"
    PRICE_STRUCTURE_UPDATED = "PRICE_STRUCTURE_UPDATED"
    COMPETITIVENESS_CHANGED = "COMPETITIVENESS_CHANGED"
    MARKET_EVIDENCE_BLOCKED = "MARKET_EVIDENCE_BLOCKED"
    HUMAN_BOUNDARY_DECISION_REQUIRED = "HUMAN_BOUNDARY_DECISION_REQUIRED"
    OPERATING_UNIT_INITIALIZATION_REQUIRED = "OPERATING_UNIT_INITIALIZATION_REQUIRED"
    INITIALIZATION_RECOMMENDATION_READY = "INITIALIZATION_RECOMMENDATION_READY"
    BATCH_APPROVAL_REQUIRED = "BATCH_APPROVAL_REQUIRED"
    INDIVIDUAL_EXIT_APPROVAL_REQUIRED = "INDIVIDUAL_EXIT_APPROVAL_REQUIRED"


class AllowedActionDomain(StrEnum):
    PRICE = "PRICE"
    PROMOTION = "PROMOTION"
    INVENTORY = "INVENTORY"
    LISTING = "LISTING"
    ADVERTISING = "ADVERTISING"
    MANUAL = "MANUAL"


# ---------------------------------------------------------------------------
# F. 巡检 Agent 私有运行态枚举（不进入跨服务契约）
# ---------------------------------------------------------------------------
class RunStatus(StrEnum):
    """巡检运行状态（改造方案 v1.0 §6）。仅用于内部日志与任务表。"""

    RECEIVED = "RECEIVED"
    COLLECTING_FACTS = "COLLECTING_FACTS"
    ANALYZING_ANOMALIES = "ANALYZING_ANOMALIES"
    EVALUATING_MODE = "EVALUATING_MODE"
    EVALUATING_ADVERTISING = "EVALUATING_ADVERTISING"
    BUILDING_PROPOSAL = "BUILDING_PROPOSAL"
    SUBMITTING = "SUBMITTING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class InspectionRunStatus(StrEnum):
    """最终巡检主链路状态；旧 ``RunStatus`` 保留到 S3 断链。"""

    RECEIVED = "RECEIVED"
    COLLECTING_FACTS = "COLLECTING_FACTS"
    VALIDATING_FACTS = "VALIDATING_FACTS"
    ANALYZING_ANOMALIES = "ANALYZING_ANOMALIES"
    RECONCILING_SIGNALS = "RECONCILING_SIGNALS"
    PERSISTING = "PERSISTING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_GAPS = "COMPLETED_WITH_GAPS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class DataQualityStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    FAILED = "FAILED"


class AnalysisStatus(StrEnum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_GAPS = "COMPLETED_WITH_GAPS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class ModeDecisionStatus(StrEnum):
    DECIDED = "DECIDED"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


class TriggerType(StrEnum):
    DAILY_SCHEDULE = "DAILY_SCHEDULE"
    MANUAL = "MANUAL"
    CONTROL_CENTER = "CONTROL_CENTER"
    OBSERVATION_DUE = "OBSERVATION_DUE"
    DATA_REPAIRED = "DATA_REPAIRED"


class BatchStatus(StrEnum):
    CREATED = "CREATED"
    ENQUEUING = "ENQUEUING"
    RUNNING = "RUNNING"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD = "DEAD"
    ARCHIVED = "ARCHIVED"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SUPPRESSED = "SUPPRESSED"
    DELIVERED = "DELIVERED"
    DEAD = "DEAD"


class FeedbackEventType(StrEnum):
    HANDLING_STARTED = "HANDLING_STARTED"
    HANDLED = "HANDLED"
    OBSERVE_REQUESTED = "OBSERVE_REQUESTED"
    IGNORED = "IGNORED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    ACTION_RECEIPT_AVAILABLE = "ACTION_RECEIPT_AVAILABLE"
    UNIT_ARCHIVED = "UNIT_ARCHIVED"


class FeedbackInboxStatus(StrEnum):
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


class AdvertisingRolloutStatus(StrEnum):
    """广告 Agent 灰度状态。

    交接包 operating-policy-registry.v1.json#governance.advertising_agent_rollout_status
    当前值为 FROZEN，capability-registry 为 FROZEN_FOR_CURRENT_MIGRATION。
    FROZEN 时巡检 Agent 只产出人工交接，不自动调用广告 Agent。
    """

    FROZEN = "FROZEN"
    ENABLED = "ENABLED"


# ---------------------------------------------------------------------------
# G. 治理常量
# ---------------------------------------------------------------------------
LEGACY_LABEL_POLICY = "READ_ONLY_EXCLUDED_FROM_REASONING"

#: 切换后不得进入推理、规则计算、任务排序或动作授权的旧字段（设计_v1.3/09 §1）
LEGACY_FIELDS_EXCLUDED_FROM_REASONING: frozenset[str] = frozenset({
    "product_position",
    "product_stage",
    "season_type",
    "综合执行分",
    "产品执行分数",
    "单异常执行分数",
    "执行优先级",
    "经营优先级",
    "operating_priority",
    "priority_basis",
})

GOVERNANCE: dict[str, object] = {
    "one_active_parent_mode_per_operating_unit": True,
    "child_asin_never_owns_parent_mode": True,
    "stop_replenishment_requires_clearance_or_exit": True,
    "negative_best_feasible_cash_recovery_requires_exit": True,
    "routine_queue_uses_composite_score": False,
    "cases_accumulate_automatically": True,
    "agents_may_change_canonical_rules": False,
    "advertising_agent_rollout_status": AdvertisingRolloutStatus.FROZEN.value,
}
