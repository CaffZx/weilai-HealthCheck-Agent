"""巡检点位 ↔ 契约 issue_code 映射。

inspection-signal.v2 的 `issue_code` 必须匹配 `^[A-Z][A-Z0-9_]{2,63}$`，
而现有 R2/R3 规则用的是中文点位名。这份映射是唯一的翻译表。

【维护约定】
  - 中文点位名是规则 YAML 的主键，不改；issue_code 是对外契约，也不改。
  - 新增点位必须同时登记两边，否则 signal_builder 会抛错（不做兜底猜测）。
  - 交接路由（next_capability / reason_code）按异常大类确定，
    来源：设计_v1.3/08 §2.3 与 knowledge/06 §5.3 的路由表。
"""
from __future__ import annotations

from core.enums import CapabilityCode, PermittedNextStep, ReasonCode, RoutingMode

#: 中文点位 → (issue_code, 大类编号, 大类名)
POINT_CODES: dict[str, tuple[str, str, str]] = {
    # 2.1 链接状态
    "链接不可售": ("LISTING_NOT_SELLABLE", "2.1", "链接状态"),
    "变体掉线": ("VARIANT_DETACHED", "2.1", "链接状态"),
    "父子体关系异常": ("PARENT_CHILD_RELATION_BROKEN", "2.1", "链接状态"),
    "Buy Box丢失": ("BUY_BOX_LOST", "2.1", "链接状态"),
    # 2.2 内容完整性
    "主图异常": ("MAIN_IMAGE_ABNORMAL", "2.2", "内容完整性"),
    "标题异常": ("TITLE_ABNORMAL", "2.2", "内容完整性"),
    "五点异常": ("BULLET_POINTS_ABNORMAL", "2.2", "内容完整性"),
    "图片异常": ("GALLERY_IMAGE_ABNORMAL", "2.2", "内容完整性"),
    "A+异常": ("APLUS_CONTENT_ABNORMAL", "2.2", "内容完整性"),
    # 2.3 价格与促销
    "变体价差异常": ("VARIANT_PRICE_GAP", "2.3", "价格与促销"),
    "促销异常": ("PROMOTION_ABNORMAL", "2.3", "价格与促销"),
    # 2.4 库存与可售
    "FBA可售库存为0": ("FBA_AVAILABLE_ZERO", "2.4", "库存与可售"),
    "库存不足": ("INVENTORY_SHORTAGE", "2.4", "库存与可售"),
    "库存积压": ("INVENTORY_OVERSTOCK", "2.4", "库存与可售"),
    "滞销异常": ("SLOW_MOVING_INVENTORY", "2.4", "库存与可售"),
    # 2.5 交易表现
    "销量异常": ("SALES_DECLINE", "2.5", "交易表现"),
    "销量增长提示": ("SALES_GROWTH_SIGNAL", "2.5", "交易表现"),
    "ACOS异常": ("ACOS_ABNORMAL", "2.5", "交易表现"),
    "广告花费异常": ("AD_SPEND_ABNORMAL", "2.5", "交易表现"),
    "目标偏离": ("TARGET_DEVIATION", "2.5", "交易表现"),
    "自然流量异常": ("NATURAL_TRAFFIC_ABNORMAL", "2.5", "交易表现"),
    "卡位异常": ("KEYWORD_RANK_ABNORMAL", "2.5", "交易表现"),
    "放量未执行": ("SCALE_UP_NOT_EXECUTED", "2.5", "交易表现"),
    "链接转化率异常下降": ("CONVERSION_RATE_DROP", "2.5", "交易表现"),
    # 2.6 合规与属性
    "合规异常": ("COMPLIANCE_ISSUE", "2.6", "合规与属性"),
    "类目异常": ("CATEGORY_MISMATCH", "2.6", "合规与属性"),
    "属性信息异常": ("ATTRIBUTE_MISMATCH", "2.6", "合规与属性"),
    # 2.7 售后
    "评分异常": ("RATING_ABNORMAL", "2.7", "售后"),
    "退款异常": ("REFUND_RATE_ABNORMAL", "2.7", "售后"),
    "近期集中差评": ("CONCENTRATED_NEGATIVE_REVIEWS", "2.7", "售后"),
    # 配置类（不是异常，是"判不了"）
    "目标配置待补": ("TARGET_CONFIGURATION_MISSING", "0.0", "配置缺失"),
}

#: 大类 → 交接路由。来源：设计_v1.3/08 §2.3 巡检交接路由表。
#:
#: 巡检"可以给"的是保护性动作与证据，"不得给"的是具体经营动作数值。
#: 因此 permitted_next_step 一律止步于 PRODUCE_RECOMMENDATION / SUPPLY_EVIDENCE，
#: 绝不出现 EXECUTE_APPROVED_ACTION。
CATEGORY_ROUTING: dict[str, dict[str, object]] = {
    "库存与可售": {
        "next_capability": CapabilityCode.INVENTORY_PLANNING,
        "reason_code": ReasonCode.INVENTORY_ACTION_REQUIRED,
        "permitted_next_step": PermittedNextStep.PRODUCE_RECOMMENDATION,
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "保护库存并避免继续放大断货或积压风险",
    },
    "价格与促销": {
        "next_capability": CapabilityCode.PRICE_PROMOTION,
        "reason_code": ReasonCode.PRICE_ACTION_REQUIRED,
        "permitted_next_step": PermittedNextStep.PRODUCE_RECOMMENDATION,
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "暂停未经确认的价格变更并给出价格域方案",
    },
    "内容完整性": {
        "next_capability": CapabilityCode.LISTING_EXECUTION,
        "reason_code": ReasonCode.LISTING_ACTION_REQUIRED,
        "permitted_next_step": PermittedNextStep.PRODUCE_RECOMMENDATION,
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "保留证据并阻止错误内容继续发布",
    },
    "链接状态": {
        "next_capability": CapabilityCode.LISTING_EXECUTION,
        "reason_code": ReasonCode.LISTING_ACTION_REQUIRED,
        "permitted_next_step": PermittedNextStep.PRODUCE_RECOMMENDATION,
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "恢复链接可售状态并保留失效证据",
    },
    "交易表现": {
        "next_capability": CapabilityCode.OPERATING_DECISION,
        "reason_code": ReasonCode.MODE_REVIEW_REQUIRED,
        "permitted_next_step": PermittedNextStep.PRODUCE_RECOMMENDATION,
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "维持现状并补齐原因证据，由经营决策判断模式是否需要变化",
    },
    "售后": {
        "next_capability": CapabilityCode.OPERATING_DECISION,
        "reason_code": ReasonCode.MODE_REVIEW_REQUIRED,
        "permitted_next_step": PermittedNextStep.PRODUCE_RECOMMENDATION,
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "补齐售后原因证据并评估是否影响当前经营模式",
    },
    "合规与属性": {
        "next_capability": CapabilityCode.HUMAN_REVIEW,
        "reason_code": ReasonCode.HUMAN_BOUNDARY_DECISION_REQUIRED,
        "permitted_next_step": PermittedNextStep.HUMAN_DECISION,
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "保留证据并阻断高风险自动动作，由人工判定处置方式",
    },
    "配置缺失": {
        "next_capability": CapabilityCode.DATA_REPAIR,
        "reason_code": ReasonCode.REQUIRED_CONFIGURATION_MISSING,
        "permitted_next_step": PermittedNextStep.SUPPLY_EVIDENCE,
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "补齐缺失的目标配置后重跑巡检",
    },
    "数据缺口": {
        "next_capability": CapabilityCode.DATA_REPAIR,
        "reason_code": ReasonCode.REQUIRED_CONFIGURATION_MISSING,
        "permitted_next_step": PermittedNextStep.SUPPLY_EVIDENCE,
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "修复数据缺口，阻断依赖该事实的自动动作",
    },
    "广告": {
        "next_capability": CapabilityCode.ADVERTISING_DECISION_EXECUTION,
        "reason_code": ReasonCode.ADVERTISING_ACTION_REQUIRED,
        "permitted_next_step": PermittedNextStep.PRODUCE_RECOMMENDATION,
        # 广告能力冻结期间必须人工消费（Ontology A13）
        "routing_mode": RoutingMode.HUMAN_ONLY,
        "requested_outcome": "提示广告侧复核，巡检不给预算、竞价或关键词动作",
    },
}

#: 广告相关点位单独路由到广告能力（仍然是人工交接）。
ADVERTISING_POINTS: frozenset[str] = frozenset({
    "ACOS异常",
    "广告花费异常",
    "放量未执行",
})


class UnknownIssuePoint(KeyError):
    """点位未登记。不猜、不兜底，直接报错让人来补映射表。"""


def issue_code_for(point: str) -> str:
    try:
        return POINT_CODES[point][0]
    except KeyError as exc:
        raise UnknownIssuePoint(
            f"点位 {point!r} 未登记 issue_code，请在 inspector/issue_codes.py 补充映射"
        ) from exc


def category_for(point: str) -> tuple[str, str]:
    """返回 (大类编号, 大类名)，例如 ('2.4', '库存与可售')。"""
    try:
        _, number, name = POINT_CODES[point]
    except KeyError as exc:
        raise UnknownIssuePoint(
            f"点位 {point!r} 未登记异常大类，请在 inspector/issue_codes.py 补充映射"
        ) from exc
    return number, name


def routing_for(point: str) -> dict[str, object]:
    """按点位取交接路由。广告类点位优先走广告路由。"""
    if point in ADVERTISING_POINTS:
        return dict(CATEGORY_ROUTING["广告"])
    _, category = category_for(point)
    return dict(CATEGORY_ROUTING[category])
