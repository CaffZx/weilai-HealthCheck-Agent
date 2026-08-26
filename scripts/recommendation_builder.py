"""按异常类型生成具体处置建议（替代写死的"仅人工复核"模板）。

输入：anomalies 列表，每项含：
  - issue_code: 内部点位码（如 INVENTORY_OVERSTOCK / SALES_DECLINE）
  - diagnosis.summary: 判定过程人话（带数据，如"可售天数 97（A:90-120天），未来覆盖 0.7 月"）
输出：多条建议用"；"拼接；无异常或全未知点位 → 兜底模板。

数字来源：从判定过程（summary）按点位正则提取，填入模板 {n}/{m} 占位；
提取不到时自动去掉对应片段，只保留动作文案。
"""
from __future__ import annotations

import re

# 按内部点位码的处置建议模板。
# 占位符 {n} {m} 依次填充从判定过程提取的数字；提取不到则删除含占位符的片段。
RECOMMENDATION_TEMPLATES: dict[str, str] = {
    # -- 链接/前台 --
    "LISTING_NOT_SELLABLE": "核实链接不可售原因（库存/合规/图片），必要时开 Case 恢复上架",
    "VARIANT_DETACHED": "检查变体挂接，重新关联子体到正确父体",
    "PARENT_CHILD_RELATION_BROKEN": "检查父-子关系配置，修复变体关联",
    "BUY_BOX_LOST": "检查价格竞争力与跟卖情况，恢复 Buy Box",
    # -- 内容完整性 --
    "MAIN_IMAGE_ABNORMAL": "重新上传合规主图（建议≥1000px、白底、无文字水印）",
    "GALLERY_IMAGE_ABNORMAL": "补充副图至≥3张，展示多角度/细节/尺码（当前{n}张）",
    "TITLE_ABNORMAL": "修正标题格式与关键词，避免堆砌或违规词",
    "BULLET_POINTS_ABNORMAL": "补齐五点至≥5条（当前{n}条）：卖点/规格/适用场景",
    "APLUS_CONTENT_ABNORMAL": "修复/补充 A+ 内容，提升转化",
    # -- 价格/促销 --
    "VARIANT_PRICE_GAP": "检查变体间价差，避免异常价格波动",
    "PROMOTION_ABNORMAL": "核对促销登记：ERP未登记但前台有活动，确认登记或处理平台自动促销",
    # -- 交易表现 --
    "SALES_DECLINE": "排查销量下滑原因：对比广告/竞品/价格，考虑促销或调整预算（近3日销量{n}）",
    "CONVERSION_RATE_DROP": "优化转化：详情页/价格/评价（转化率{n}%）",
    "ACOS_ABNORMAL": "优化广告：降低无效词出价、加否定词（ACOS {n}%）",
    "AD_SPEND_ABNORMAL": "调整预算/出价（日花费${n}超预算${m}）",
    "TARGET_DEVIATION": "复盘目标与经营策略：连续{n}天未达日均目标",
    "NATURAL_TRAFFIC_ABNORMAL": "排查自然流量下滑：关键词排名/竞品/类目变动",
    "KEYWORD_RANK_ABNORMAL": "检查关键词排名变化，调整广告与优化策略",
    "SCALE_UP_NOT_EXECUTED": "按经营策略执行放量计划，复核执行状态",
    # -- 库存 --
    "INVENTORY_SHORTAGE": "及时补货/调拨：FBA 可售 {n} 件不足，防断货",
    "INVENTORY_OVERSTOCK": "促销清库存：可售天数{n}天超阈值，考虑折扣/站内活动，控制补货",
    "SLOW_MOVING_INVENTORY": "清理滞销：日均销量{n}过低，评估降价/弃置",
    "FBA_AVAILABLE_ZERO": "立即补货/检查在售状态：FBA 可售为 0",
    # -- 合规/属性 --
    "CATEGORY_MISMATCH": "核对类目归属，确认是否被错误归类",
    "ATTRIBUTE_MISMATCH": "核对属性信息，修正不一致项",
    "COMPLIANCE_ISSUE": "核查合规问题，按平台要求整改",
    # -- 售后 --
    "CONCENTRATED_NEGATIVE_REVIEWS": "关注集中差评：排查质量/描述偏差，及时回复与改进",
    "RATING_ABNORMAL": "提升评分：优化质量、回复差评（当前{n}星）",
    "REFUND_RATE_ABNORMAL": "排查退款原因：质量/尺码/描述偏差（16周退款率 {n}%）",
}

# 数字提取规则：issue_code → 正则列表（按序匹配，取第一个命中组）。
# 正则与 severity_grader 的"判定过程"生成格式对齐。
_EXTRACTION_PATTERNS: dict[str, list[str]] = {
    "GALLERY_IMAGE_ABNORMAL": [r"副图\s*(\d+)\s*张", r"(\d+)\s*张"],
    "BULLET_POINTS_ABNORMAL": [r"五点项数\s*(\d+)", r"五点\s*(\d+)/"],
    "SALES_DECLINE": [r"日均销量\s*([\d.]+)"],
    "CONVERSION_RATE_DROP": [r"转化率\s*([\d.]+)%"],
    "ACOS_ABNORMAL": [r"ACOS\s*([\d.]+)%", r"acos\s*([\d.]+)%"],
    "AD_SPEND_ABNORMAL": [r"日均花费\s*\$?([\d.]+)"],
    "TARGET_DEVIATION": [r"连续\s*(\d+)\s*天"],
    "INVENTORY_SHORTAGE": [r"FBA可售库存\s*([\d.]+)"],
    "INVENTORY_OVERSTOCK": [r"可售天数\s*(\d+)"],
    "SLOW_MOVING_INVENTORY": [r"日均\s*([\d.]+)", r"消化周期\s*([\d.]+)\s*天"],
    "RATING_ABNORMAL": [r"当前评分\s*([\d.]+)", r"评分\s*([\d.]+)"],
    "REFUND_RATE_ABNORMAL": [r"退款率\s*([\d.]+)%"],
}

FALLBACK_RECOMMENDATION = "仅提交人工复核建议，禁止中控或下游自动执行"

# 清理未填充占位符的片段：如"（当前{n}张）"、"（近3日销量{n}）"
_UNFILLED_PLACEHOLDER_RE = re.compile(
    r"[（(][^（()）]*\{[a-z]\}[^（()）]*[）)]|[（(][^（()）]*\{[a-z]\}[^（()）]*"
)


def _extract_numbers(issue_code: str, summary: str) -> list[str]:
    """从判定过程按点位规则提取数字，按出现顺序返回。"""
    patterns = _EXTRACTION_PATTERNS.get(issue_code) or []
    hits: list[str] = []
    for pattern in patterns:
        match = re.search(pattern, summary or "")
        if match:
            hits.append(match.group(1))
            if len(hits) >= 2:  # 最多两个占位符 {n}/{m}
                break
    return hits


def _render_template(template: str, numbers: list[str]) -> str:
    """把数字依次填入 {n}/{m}；未填的占位符片段直接删除。"""
    rendered = template
    for index, value in enumerate(numbers):
        marker = "{%s}" % chr(ord("n") + index)
        if marker in rendered:
            rendered = rendered.replace(marker, value)
    rendered = _UNFILLED_PLACEHOLDER_RE.sub("", rendered).strip()
    if "{" in rendered:
        # 兜底：无括号包裹的残留占位符（如"可覆盖天数{n}天"），删除从最近标点到占位符结尾的片段
        rendered = re.sub(r"[^，。；、：:；]*\{[a-z]\}[^，。；、：:；]*", "", rendered)
        rendered = re.sub(r"[：:，,；;]{2,}", "：", rendered).strip("：:，,；; ")
    return rendered


def _render_one(anomaly: dict) -> str | None:
    """生成单条异常的具体处置建议；无对应模板/渲染为空 → None。"""
    issue_code = anomaly.get("issue_code")
    template = RECOMMENDATION_TEMPLATES.get(issue_code or "")
    if template is None:
        return None
    summary = anomaly.get("summary") or ""
    numbers = _extract_numbers(issue_code, summary)
    rendered = _render_template(template, numbers)
    return rendered or None


def build_recommendations(anomalies: list[dict]) -> str:
    """按异常类型生成具体处置建议；多条用"；"拼接；空/全未知 → 兜底。"""
    if not anomalies:
        return FALLBACK_RECOMMENDATION
    parts = [rendered for rendered in (_render_one(a) for a in anomalies) if rendered]
    return "；".join(parts) if parts else FALLBACK_RECOMMENDATION


def build_recommendation_single(anomaly: dict) -> str:
    """单条异常的具体处置建议（供 proposal.details[].reason 使用）；无 → 兜底人工复核。"""
    rendered = _render_one(anomaly)
    return rendered or "仅提交人工复核建议，禁止中控或下游自动执行"


# 当前值提取规则：issue_code → (标签, 正则)。用于 details.currentValue（中控"当前"字段）。
CURRENT_VALUE_RULES: dict[str, tuple[str, str]] = {
    "RATING_ABNORMAL": ("当前评分", r"当前评分\s*([\d.]+)"),
    "REFUND_RATE_ABNORMAL": ("16周退款率", r"退款率\s*([\d.]+)%"),
    "INVENTORY_OVERSTOCK": ("可售天数", r"可售天数\s*(\d+)"),
    "INVENTORY_SHORTAGE": ("FBA可售库存", r"FBA可售库存\s*([\d.]+)"),
    "SLOW_MOVING_INVENTORY": ("日均销量", r"日均\s*([\d.]+)"),
    "ACOS_ABNORMAL": ("ACOS", r"ACOS\s*([\d.]+)%"),
    "AD_SPEND_ABNORMAL": ("日均花费", r"日均花费\s*\$?([\d.]+)"),
    "SALES_DECLINE": ("近3日日均销量", r"日均销量\s*([\d.]+)"),
    "BULLET_POINTS_ABNORMAL": ("五点项数", r"五点项数\s*(\d+)"),
    "GALLERY_IMAGE_ABNORMAL": ("副图数量", r"副图数量\s*(\d+)"),
    "CONVERSION_RATE_DROP": ("转化率", r"转化率\s*([\d.]+)%"),
    "TARGET_DEVIATION": ("连续未达标天数", r"连续\s*(\d+)\s*天"),
}


def build_current_value(issue_code: str, summary: str) -> dict:
    """从判定过程提取该异常的当前值（供 details.currentValue，中控"当前"字段）。

    有提取规则的填具体键值（如 {"当前评分": "4.0"}）；无规则/提取不到 → 兜底判定过程原文，
    保证所有异常明细都有"当前值"，不再显示"未提供"。
    """
    rule = CURRENT_VALUE_RULES.get(issue_code or "")
    if rule:
        label, pattern = rule
        match = re.search(pattern, summary or "")
        if match:
            return {label: match.group(1)}
    return {"data": summary or ""}
