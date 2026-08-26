"""现有 R2/R3 规则适配器。

改造方案 v1.0 §12。

【设计立场】
  不重写 anomaly_detector.py 和 severity_grader.py。这两个文件是过去半年
  业务校准的沉淀，重写等于把校准结果清零。适配器只做一件事：
  把统一事实快照翻译成这两个引擎期望的输入形状，再把它们的输出翻译成
  契约化的 AnomalySignal。

【第一阶段纪律】
  只做适配，**不动 R2/R3 的业务阈值**。边接新流程边改阈值，出了问题
  分不清是流程问题还是阈值问题。阈值调整走 rule-change-proposal（H3）。

【明确的能力边界】
  扩展 MCP 已覆盖前台页面、价格促销、评论、合规和关键词排名。工具未成功或
  关键字段缺失时由 FactQualityService 显式登记缺口，不把 None 当成"没问题"。
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any
from uuid import uuid4

from datetime import datetime
from core.contracts import AnomalySignal, DataGap, OperatingFactSnapshot
from core.enums import Severity, SignalState, SignalType
from core.errors import PatrolRuleFailed
from inspector.engine import anomaly_detector as R2
from inspector.engine import severity_grader as R3
from inspector.issue_codes import issue_code_for

logger = logging.getLogger(__name__)

#: 旧严重度字符串 → 契约 Severity。
#: 注意 "配置待补"/"数据不足" 都落到 DATA_INSUFFICIENT（契约取值，不是方案里的 DATA_GAP）。
SEVERITY_MAP: dict[str, Severity] = {
    "S0": Severity.S0,
    "S1": Severity.S1,
    "S2": Severity.S2,
    "配置待补": Severity.DATA_INSUFFICIENT,
    "数据不足": Severity.DATA_INSUFFICIENT,
}

#: 依赖前台页面快照或关键词数据源的点位。MCP 本轮不覆盖，显式登记为不可判。
class RuntimeAggregate:
    """MySQL/MCP 快照的纯规则输入；不导入旧缓存或数据库模块。"""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    def to_ACOS异常_kwargs(self) -> dict:
        return {
            "目标ACOS": self.目标ACOS,
            "近3天平均ACOS": self.近3天平均ACOS,
            "连续天数": self.ACOS连续超标天数,
            "近3天累计广告花费": self.近3天累计广告花费,
            "近7天日均花费": self.近7天日均花费,
            "近3天累计点击": self.近3天累计点击,
            "零销售花费天数": self.ACOS零销售花费天数,
        }

    def to_广告花费异常_kwargs(self) -> dict:
        return {
            "目标每日预算": self.目标每日预算,
            "近3天平均花费": self.近3天平均花费,
            "连续天数": self.花费连续偏离天数,
        }

    def to_销量类异常_kwargs(self) -> dict:
        return {
            "过去7天日均销量": self.过去7天日均销量,
            "近3天日均销量": self.近3天日均销量,
            "连续天数": self.销量连续下降天数,
            "近7天逐日单量": self.近7天逐日单量,
        }

    def to_目标偏离_kwargs(self) -> dict:
        return {
            "近3天日均单量": self.近3天日均单量,
            "日均目标单量": self.日均目标单量,
            "近7天逐日单量": self.近7天逐日单量,
            "月累计完成率": self.月累计完成率,
            "月时间进度": self.月时间进度,
        }

    def to_自然流量异常_kwargs(self) -> dict:
        return {
            "近3天平均自然占比": self.近3天平均自然占比,
            "近7天平均自然占比": self.近7天平均自然占比,
            "近3天自然订单数": self.近3天自然订单数,
            "近7天自然订单数": self.近7天自然订单数,
            "近7天逐日自然占比": self.近7天逐日自然占比,
            "前7天平均自然占比": self.前7天平均自然占比,
        }

    def to_卡位异常_kwargs(self) -> dict:
        return {
            "近3天平均卡位": self.近3天平均卡位,
            "近7天平均卡位": self.近7天平均卡位,
            "近7天逐日卡位": self.近7天逐日卡位,
            "前7天平均卡位": self.前7天平均卡位,
        }

    def to_链接转化率异常_kwargs(self) -> dict:
        return {
            "本周转化率": self.本周转化率,
            "上周转化率": self.上周转化率,
            "类目平均转化率": self.类目平均转化率,
            "本周会话": self.本周会话,
            "上周会话": self.上周会话,
            "本周订单商品总数": self.本周订单商品总数,
            "上周订单商品总数": self.上周订单商品总数,
        }

    def to_退款异常_kwargs(self) -> dict:
        return {
            "十六周退款率": self.十六周退款率,
            "三十二周退款率": self.三十二周退款率,
            "类目平均退换货率": self.类目平均退换货率,
            "本周订单商品总数": self.本周订单商品总数,
            "上周订单商品总数": self.上周订单商品总数,
        }

    def to_库存积压_kwargs(self) -> dict:
        return {
            "可售库存": self.可售库存,
            "近30天日均销量": self.近30天日均销量,
            "未来N月目标累计_覆盖月数": self.未来N月目标累计_覆盖月数,
            "是否新品观察期": self.是否新品观察期,
            "有效销售天数": self.有效销售天数,
        }

    def to_放量未执行_kwargs(self) -> dict:
        return {
            "近7天日均销量": self.过去7天日均销量,
            "日均目标单量": self.日均目标单量,
            "可售库存": self.可售库存,
            "近7天平均ACOS": self.近7天平均ACOS,
            "目标ACOS": self.目标ACOS,
            "近3天平均花费": self.近3天平均花费,
            "近7天平均花费": self.近7天平均花费,
            "近7天平均自然占比": self.近7天平均自然占比,
            "前7天平均自然占比": self.前7天平均自然占比,
            "近3天平均自然占比": self.近3天平均自然占比,
        }

    def to_滞销异常_kwargs(self) -> dict:
        return {
            "有滞销库存": self.有滞销库存,
            "命中子ASIN滞销库存": self.命中子ASIN滞销库存,
            "命中子ASIN近30天日均销量": self.命中子ASIN近30天日均销量,
            "命中子ASIN近30天累计订单": self.命中子ASIN近30天累计订单,
            "命中子ASIN销量覆盖天数": self.命中子ASIN销量覆盖天数,
            "命中子ASIN销量数据截止日": self.命中子ASIN销量数据截止日,
            "父ASIN汇总单月超龄仓租费用": self.父ASIN汇总单月超龄仓租费用,
        }

    def to_评分异常_kwargs(self) -> dict:
        return {
            "当前评分": self.当前评分,
            "近7天平均评分": self.近7天平均评分,
            "目标评分": self.目标评分,
        }


def build_aggregate(snapshot: OperatingFactSnapshot) -> Any:
    """FactSnapshot → R2/R3 规则所需聚合输入。

    字段对应关系写在这里，是"新事实"与"旧规则"之间唯一的翻译点。
    """
    sales = snapshot.sales or {}
    traffic = snapshot.traffic or {}
    profit = snapshot.profit or {}
    inventory = snapshot.inventory or {}
    quality = snapshot.quality or {}
    unit = snapshot.operating_unit

    slow_children = inventory.get("slow_moving_children") or []
    hit_child = slow_children[0] if slow_children else {}

    return RuntimeAggregate(
        父ASIN=unit.parent_asin,
        店铺账号=str((snapshot.identity or {}).get("shop_account") or ""),
        站点=unit.site_code,
        # 销量
        近3天日均销量=sales.get("avg_daily_units_3d"),
        过去7天日均销量=sales.get("avg_daily_units_7d"),
        近30天日均销量=sales.get("avg_daily_units_30d"),
        近7天逐日单量=list(sales.get("daily_units_7d") or []),
        销量连续下降天数=int(sales.get("consecutive_decline_days") or 0),
        # ACOS
        近7天平均ACOS=profit.get("acos_7d"),
        近3天平均ACOS=profit.get("acos_3d"),
        近3天累计广告花费=profit.get("ad_spend_3d_total"),
        近7天日均花费=profit.get("ad_spend_7d_avg"),
        近3天累计点击=profit.get("ad_clicks_3d_total"),
        ACOS连续超标天数=int(profit.get("acos_over_target_days") or 0),
        ACOS零销售花费天数=int(profit.get("ad_zero_sale_days") or 0),
        # 广告花费
        近7天平均花费=profit.get("ad_spend_7d_avg"),
        近3天平均花费=profit.get("ad_spend_3d_avg"),
        花费连续偏离天数=int(profit.get("spend_deviation_days") or 0),
        # 目标偏离
        近3天日均单量=sales.get("avg_daily_orders_3d"),
        日均目标单量=sales.get("daily_target_orders"),
        月累计完成率=sales.get("monthly_completion_rate"),
        月时间进度=sales.get("month_time_progress"),
        # 自然流量
        近3天平均自然占比=traffic.get("natural_ratio_3d"),
        近7天平均自然占比=traffic.get("natural_ratio_7d"),
        前7天平均自然占比=traffic.get("natural_ratio_prev_7d"),
        近3天自然订单数=traffic.get("natural_orders_3d"),
        近7天自然订单数=traffic.get("natural_orders_7d"),
        近7天逐日自然占比=list(traffic.get("daily_natural_ratio_7d") or []),
        # 卡位
        近3天平均卡位=traffic.get("keyword_rank_3d"),
        近7天平均卡位=traffic.get("keyword_rank_7d"),
        前7天平均卡位=traffic.get("keyword_rank_prev_7d"),
        近7天逐日卡位=list(traffic.get("daily_keyword_rank_7d") or []),
        # 转化
        本周转化率=traffic.get("conversion_this_week") or quality.get("conversion_rate"),
        上周转化率=traffic.get("conversion_last_week"),
        类目平均转化率=quality.get("category_conversion_rate"),
        本周会话=traffic.get("sessions_this_week"),
        上周会话=traffic.get("sessions_last_week"),
        本周订单商品总数=traffic.get("ordered_items_this_week"),
        上周订单商品总数=traffic.get("ordered_items_last_week"),
        # 评分
        当前评分=quality.get("star_rating"),
        近7天平均评分=None,
        目标评分=quality.get("target_star_rating"),
        # 退款（ERP 无"本周/上周"退款率，只有 16周/32周；改判长趋势，不再硬拿 16周冒充"本周"）
        十六周退款率=quality.get("refund_rate_16w"),
        三十二周退款率=quality.get("refund_rate_32w"),
        类目平均退换货率=quality.get("category_refund_rate"),
        # 广告目标
        目标ACOS=profit.get("target_acos"),
        目标每日预算=profit.get("target_daily_budget"),
        # 库存
        可售库存=inventory.get("fba_available"),
        未来N月目标累计_覆盖月数=inventory.get("target_coverage_months"),
        是否新品观察期=False,
        有效销售天数=int(sales.get("effective_days") or 0),
        # 滞销
        有滞销库存=bool(inventory.get("has_aged_inventory")),
        命中子ASIN=hit_child.get("child_asin"),
        命中子ASIN滞销库存=hit_child.get("aged_quantity"),
        命中子ASIN近30天日均销量=None,
        命中子ASIN近30天累计订单=None,
        命中子ASIN销量覆盖天数=None,
        命中子ASIN销量数据截止日=None,
        父ASIN汇总单月超龄仓租费用=inventory.get("aged_storage_fee_monthly"),
        # 元信息
        数据窗口_天数=int(sales.get("row_count") or 0),
        数据窗口_起=None,
        数据窗口_止=sales.get("latest_stat_date"),
        销售数据年龄天数=sales.get("data_age_days"),
        销售数据时效=sales.get("freshness") or "NO_DATA",
        缺失字段=[gap.field for gap in snapshot.data_gaps],
    )


def build_legacy_snapshot_dict(snapshot: OperatingFactSnapshot) -> dict[str, Any]:
    """FactSnapshot → `_detect_with_local_data` 期望的 `快照数据` dict。

    没有对应数据源的键给空容器（不是 None），让旧代码的 `or {}` / `or []`
    路径正常走过，同时通过 unavailable_points 显式说明"这些点位本轮判不了"。
    """
    identity = snapshot.identity or {}
    inventory = snapshot.inventory or {}
    price = snapshot.price or {}
    quality = snapshot.quality or {}

    children: list[dict[str, Any]] = []
    for child in identity.get("children") or []:
        bullets = child.get("bullet_points") or [None] * 5
        row: dict[str, Any] = {
            "asin": child.get("child_asin"),
            "sellerSku": child.get("seller_sku"),
            "status": child.get("status"),
            "productName": child.get("title"),
            "parentAsin": child.get("parent_asin"),
            "variationThemeName": child.get("variation_theme"),
            "productColor": child.get("color"),
            "productSize": child.get("size"),
            "lastCategory": child.get("category_name"),
            "categoryId": child.get("category_id"),
            "categoryBaseline": child.get("category_baseline") or {},
            "frontend": child.get("frontend") or {},
            "pricePromotion": child.get("price_promotion") or {},
            "complianceIssues": child.get("compliance_issues") or [],
            "complianceEvaluated": child.get("compliance_evaluated") is True,
        }
        for index in range(5):
            row[f"fiveBulletPoint{index + 1}"] = bullets[index] if index < len(bullets) else None
        importance = child.get("importance")
        if importance:
            row["finenessDesc"] = importance
        children.append(row)

    return {
        "listing": {"评论数": quality.get("review_count")},
        "stock": {
            "FBA可售库存": inventory.get("fba_available"),
            "scope": inventory.get("stock_scope"),
            "child_asin": inventory.get("stock_child_asin"),
        },
        "tags": {},
        "product_info": {
            "标题": (children[0].get("productName") if children else None),
            "fetched_at": snapshot.as_of_time.isoformat(),
        },
        "product_info_children": children,
        "page": identity.get("storefront") or {},
        "page_history": [],
        "price_promo": price.get("children_prices") or [],
        "price_history": [],
        "inspection": {
            "recent_negative_reviews": quality.get("recent_negative_reviews") or [],
            "review_count": quality.get("review_count"),
            "recent_reviews_evaluated": quality.get("recent_reviews_evaluated") is True,
        },
        "platform_activities": price.get("platform_activities") or [],
        "platform_activity_history": price.get("platform_activity_history") or [],
        "platform_activity_evaluated": price.get("platform_activity_evaluated") is True,
        "inventory_cost": inventory.get("slow_moving_children") or [],
    }


def _frontend_display_status(frontend: dict[str, Any], *, evaluated: bool) -> str | None:
    """前台展示状态。inStock=false 且仍可加购/持有购物车时不当成不可展示。"""
    if not evaluated:
        return None
    if frontend.get("in_stock") is not False:
        return "可展示"
    if frontend.get("has_cart") is True or frontend.get("buy_box_owned") is True:
        return "可展示"
    return "不可展示"


def _active_children(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    children: dict[str, dict[str, Any]] = {}
    for row in rows:
        child_asin = str(row.get("asin") or "").strip().upper()
        if not child_asin or str(row.get("status") or "").strip() == "不可售":
            continue
        children.setdefault(child_asin, row)
    return children


def _normalized_category(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    return "".join(character for character in text if character.isalnum()) or None


def _erp_attributes(child: dict[str, Any]) -> dict[str, Any]:
    return {
        "color": child.get("productColor"),
        "size": child.get("productSize"),
    }


def _frontend_attributes(
    frontend: dict[str, Any], child: dict[str, Any]
) -> dict[str, Any] | None:
    if frontend.get("evaluated") is not True:
        return None
    attributes = frontend.get("attributes") or {}
    # P0-1 修复：上游 child_detail 对部分子体返回空结构（bonus 为空），
    # 此时 attributes 全为 None/空——属于"前台未采集"，不是"Listing 缺属性"。
    # 若前台没有任何属性值，返回 None（detect 侧数据不足不判，避免误报属性缺失）。
    if not attributes or not any(
        value not in (None, "") for value in attributes.values()
    ):
        return None
    erp = _erp_attributes(child)
    # 只保留"前台有值 且 ERP 有值"的属性参与比较；
    # 前台值为空的键不参与（既不算缺失、也不与 ERP 基准比不一致），消除双计数误报。
    return {
        key: attributes.get(key)
        for key, value in erp.items()
        if value not in (None, "") and attributes.get(key) not in (None, "")
    } or None


def _compliance_summary(issues: list[dict[str, Any]]) -> str | None:
    values = [
        str(issue.get("reason") or issue.get("message") or "").strip()
        for issue in issues
    ]
    text = "；".join(value for value in values if value)
    return text[:500] or None


def _child_fact_evaluated(
    facts: dict[str, Any], child_asin: str, domain: str
) -> bool:
    child = next(
        (
            row for row in facts.get("product_info_children") or []
            if row.get("asin") == child_asin
        ),
        {},
    )
    return bool(child.get(f"{domain}Evaluated"))


def _reviews_evaluated(facts: dict[str, Any]) -> bool:
    return bool((facts.get("inspection") or {}).get("recent_reviews_evaluated"))


def _concentrated_reviews(
    reviews: list[dict[str, Any]], ratings_count: int | None
) -> dict[str, Any] | None:
    if not reviews:
        return None
    categories = Counter(
        review.get("category") for review in reviews
        if review.get("category") not in (None, "其他")
    )
    category, count = categories.most_common(1)[0] if categories else (None, 0)
    ratio = count / len(reviews)
    concentrated = len(reviews) >= 3 and category is not None and ratio >= 0.60
    relative = bool(ratings_count and len(reviews) > ratings_count * 0.20)
    if not concentrated and not relative:
        return None
    return {
        "category": category,
        "ratio": ratio,
        "condition": "低星集中同类问题" if concentrated else "近期负面反馈占比过高",
    }


def _detect_runtime_facts(
    facts: dict[str, Any],
    aggregate: RuntimeAggregate,
    r2_config: dict,
    r3_config: dict,
    *,
    compliance_points_enabled: bool = True,
) -> list[R2.命中异常]:
    hits: list[R2.命中异常] = []
    stock = facts.get("stock") or {}
    # ERP 当前无 ASIN/SKU 的库存响应只代表父体汇总，不能投射成子体断货。
    inventory_results = ()
    if stock.get("scope") == "CHILD":
        # 子体重要性（主要色/次要色/长尾色）用于缺货严重度分级（_stockout_severity）。
        _stock_child = {
            (c.get("child_asin") or c.get("asin") or ""): c
            for c in facts.get("product_info_children") or []
        }
        _stock_importance = (
            _stock_child.get(stock.get("child_asin") or "", {}) or {}
        ).get("finenessDesc")
        inventory_results = (
            R2.detect_FBA可售库存为0(
                FBA可售库存=stock.get("FBA可售库存"),
                子体ASIN=stock.get("child_asin"),
                变体重要性=_stock_importance,
                r2=r2_config,
                r3=r3_config,
            ),
            R2.detect_库存不足(
                FBA可售库存=stock.get("FBA可售库存"),
                过去7天日均销量=aggregate.过去7天日均销量,
                子体ASIN=stock.get("child_asin"),
                变体重要性=_stock_importance,
                r2=r2_config,
                r3=r3_config,
            ),
        )
    for result in inventory_results:
        if isinstance(result, R2.命中异常):
            hits.append(result)
    # 合规异常（account_health，ERP 工具）非爬虫点位，每天判定。
    # 临时开关（feature_flags.compliance_enabled）：关闭时跳过合规异常判定。
    if compliance_points_enabled:
        for child_asin, child in _active_children(
            facts.get("product_info_children") or []
        ).items():
            compliance_result = R2.detect_合规异常(
                平台合规状态=(
                    "违规" if child.get("complianceIssues") else "正常"
                    if _child_fact_evaluated(facts, child_asin, "compliance") else None
                ),
                合规提示=_compliance_summary(child.get("complianceIssues") or []),
                子体ASIN=child_asin,
                变体重要性=child.get("finenessDesc"),
                r3=r3_config,
            )
            if isinstance(compliance_result, R2.命中异常):
                hits.append(compliance_result)
    # 2026-08-21 方案：爬虫类点位一周一判（仅周一判定），其余天只判库存等 ERP 点位。
    if datetime.now().weekday() == 0:
        hits.extend(_detect_crawler_points(facts, aggregate, r2_config, r3_config))
    return hits


def _detect_crawler_points(
    facts: dict[str, Any],
    aggregate: RuntimeAggregate,
    r2_config: dict,
    r3_config: dict,
) -> list[R2.命中异常]:
    """2026-08-21 方案：爬虫类前台点位（主图/图片/A+/五点/标题/属性/链接/BuyBox/价差/促销/差评）
    一周一判——仅周一（weekday==0）判定；其余天不判不投。"""
    hits: list[R2.命中异常] = []
    children = _active_children(facts.get("product_info_children") or [])
    relations: dict[str, bool] = {}
    for child_asin, child in children.items():
        importance = child.get("finenessDesc")
        frontend = child.get("frontend") or {}
        frontend_evaluated = frontend.get("evaluated") is True
        if frontend_evaluated and frontend.get("parent_asin"):
            relations[child_asin] = frontend["parent_asin"] == aggregate.父ASIN
        # P1-1 修复：五点判定只认"前台口径"（child_detail.features）。
        # 前台已采集（list）→ 用前台计数；前台未采集（None）→ 不判（数据缺口），
        # 不再回退 ERP 冒充前台数据，避免 ERP 同步不全被误判为前台五点缺失。
        frontend_bullets = frontend.get("bullet_points")
        if frontend_bullets is None:
            bullet_count = None
            bullet_source = "前台"
        else:
            bullet_count = sum(1 for b in frontend_bullets if str(b).strip())
            bullet_source = "前台"
        for result in (
            R2.detect_五点异常(
                五点内容项数=bullet_count,
                审核状态=None,
                子体ASIN=child_asin,
                子体SKU=child.get("sellerSku"),
                变体重要性=importance,
                数据源=bullet_source,
                r2=r2_config,
                r3=r3_config,
            ),
            # 标题异常：改用爬虫前台标题（bonus.title，normalizer frontend.title）。
            # 前台不可得（None）→ 不判（数据缺口），不再用 ERP productName 冒充前台标题。
            R2.detect_标题异常(
                标题字段=frontend.get("title"),
                审核状态=None,
                ERP标题基准=None,
                子体ASIN=child_asin,
                子体SKU=child.get("sellerSku"),
                变体重要性=importance,
                r3=r3_config,
            ) if frontend.get("title") else None,
            R2.detect_链接不可售(
                后台可售状态=child.get("status"),
                前台展示状态=_frontend_display_status(
                    frontend, evaluated=frontend_evaluated
                ),
                子体ASIN=child_asin,
                变体重要性=importance,
                r3=r3_config,
            ),
            R2.detect_BuyBox丢失(
                BuyBox状态=(
                    "拥有" if frontend.get("buy_box_owned") is True
                    else "丢失" if frontend.get("buy_box_owned") is False
                    else None
                ),
                子体ASIN=child_asin,
                变体重要性=importance,
                r3=r3_config,
            ),
            (
                R2.detect_主图异常(
                    主图字段=frontend.get("main_image_url"),
                    审核状态=None,
                    前台展示=("正常" if frontend_evaluated else None),
                    子体ASIN=child_asin,
                    变体重要性=importance,
                    r2=r2_config,
                    r3=r3_config,
                )
                if frontend.get("main_image_url")
                else None
            ),
            R2.detect_图片异常(
                副图数量=(
                    max(int(frontend["gallery_count"]) - 1, 0)
                    if frontend_evaluated and frontend.get("gallery_count") is not None
                    else None
                ),
                审核状态=None,
                前台展示=("正常" if frontend_evaluated else None),
                子体ASIN=child_asin,
                变体重要性=importance,
                r2=r2_config,
                r3=r3_config,
            ),
            R2.detect_A加异常(
                A加内容状态=(
                    "已创建" if frontend.get("aplus_present") is True
                    else "缺失" if frontend.get("aplus_present") is False
                    else None
                ),
                审核状态=None,
                前台展示=(
                    "已展示" if frontend.get("aplus_present") is True
                    else "未展示" if frontend.get("aplus_present") is False
                    else None
                ),
                子体ASIN=child_asin,
                变体重要性=importance,
                r3=r3_config,
            ),
            R2.detect_类目异常(
                类目路径=(
                    _normalized_category(frontend.get("category_name"))
                    if frontend_evaluated else None
                ),
                ERP类目基准=(
                    _normalized_category(
                        (child.get("categoryBaseline") or {}).get("category_path")
                    )
                    if (child.get("categoryBaseline") or {}).get("status")
                    == "CONFIRMED"
                    else None
                ),
                命中层级="变体级",
                子体ASIN=child_asin,
                变体重要性=importance,
                r3=r3_config,
            ),
            R2.detect_属性信息异常(
                属性字段=_frontend_attributes(frontend, child),
                ERP属性基准=_erp_attributes(child),
                命中层级="变体级",
                子体ASIN=child_asin,
                变体重要性=importance,
                r3=r3_config,
            ),
        ):
            if isinstance(result, R2.命中异常):
                hits.append(result)


    # P0-2 修复：只保留"前台价"（source=frontend）参与价差比较。
    # ERP 回退价（source=erp，或旧代码的回退标记 erp_listing_product_info）
    # 与前台价口径不同（促销/含税/划线），混算会让价差假性放大 → 剔除。
    prices = [
        row for row in facts.get("price_promo") or []
        if row.get("price") is not None
        and row.get("source") not in ("erp", "erp_listing_product_info")
    ]
    primary = [
        row for row in prices
        if (children.get(row.get("child_asin")) or {}).get("finenessDesc") == "主要色"
    ]
    # 主要色价格缺失时不静默回退全部（基准不稳），直接置空走"数据不足"观察类。
    baseline = min((row["price"] for row in primary), default=None)
    activities = facts.get("platform_activities") or []
    for row in prices:
        child = children.get(row.get("child_asin")) or {}
        result = R2.detect_变体价差异常(
            基准价=baseline,
            对比子体={
                "变体重要性": child.get("finenessDesc"),
                "分类": "尺码" if child.get("productSize") else "颜色",
                "到手价": row.get("price"),
                "ASIN": row.get("child_asin"),
            },
            # P0-2 修复：运营动作豁免从"单元有任意活动"改为"该子体自身有前台促销"，
            # 避免存在活动配置就一刀切豁免全部价差（漏报真实价差异常）。
            运营动作导致=bool(
                row.get("promotions") or row.get("coupon") or row.get("discount_types")
            ),
            r2=r2_config,
            r3=r3_config,
        )
        if isinstance(result, R2.命中异常):
            hits.append(result)

    # P1-2 修复：前台促销展示只在"前台价格数据已采集"（source=frontend 的行）上判定。
    # 全部为 ERP 回退价时，前台促销域未采集，coupon/promotions 全为 null 属于数据缺失，
    # 不能据此判"未展示"（否则 ERP 有活动但前台促销数据缺失会被误报）。此时传 None，
    # detect_促销异常 走"促销相关字段均缺失"观察类，不产出 ANOMALY。
    frontend_prices = [
        row for row in prices
        if row.get("source") not in ("erp", "erp_listing_product_info")
    ]
    promotion = next(iter(frontend_prices), {})
    # 三态 ERP 活动配置：True=有活动 / False=已采集且明确无活动 / None=活动数据缺失（不判反向）
    activities_evaluated = facts.get("platform_activity_evaluated") is True
    erp_activity_state = (
        True if activities else (False if activities_evaluated else None)
    )
    promotion_result = R2.detect_促销异常(
        ERP活动配置存在=erp_activity_state,
        # "已展示"覆盖 coupon/promotions/discount_types + 划线价/省幅（划线价折扣也是前台促销）
        前台促销展示=(
            "已展示" if any(
                row.get("coupon") or row.get("promotions") or row.get("discount_types")
                or row.get("strikethrough_price") or row.get("savings_percentage")
                for row in frontend_prices
            ) else "未展示" if frontend_prices else None
        ),
        活动审核状态=None,
        前台售价=promotion.get("price"),
        划线价=promotion.get("strikethrough_price"),
        r3=r3_config,
    )
    if isinstance(promotion_result, R2.命中异常):
        hits.append(promotion_result)

    negative_reviews = (facts.get("inspection") or {}).get("recent_negative_reviews") or []
    concentrated = _concentrated_reviews(
        negative_reviews, (facts.get("inspection") or {}).get("review_count")
    )
    review_result = R2.detect_近期集中差评(
        近期低星评论数=len(negative_reviews) if _reviews_evaluated(facts) else None,
        同类问题=(concentrated or {}).get("category"),
        同类问题占比=(concentrated or {}).get("ratio"),
        现有评分数=(facts.get("inspection") or {}).get("review_count"),
        触发条件=(concentrated or {}).get("condition"),
        评论样本=negative_reviews,
        r3=r3_config,
    )
    if isinstance(review_result, R2.命中异常):
        hits.append(review_result)
    return hits


def _grade_runtime_performance(
    aggregate: RuntimeAggregate,
    r3_config: dict,
    coverage: dict[str, str],
    *,
    keyword_rank_enabled: bool = True,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def collect(result: Any, point: str, *, child_asin: str | None = None) -> None:
        actual_point = getattr(result, "问题点位", None) or point
        if isinstance(result, R3.严重度判定结果) and result.严重度:
            coverage[actual_point] = "EVALUATED"
            results.append({
                "问题点位": actual_point,
                "严重度": result.严重度,
                "分档名称": result.分档名称,
                "判定过程": result.判定过程,
                "命中值": result.命中值,
                "数据快照": list(getattr(result, "数据快照", []) or []),
                "命中变体": child_asin or "",
            })
        elif isinstance(result, R3.观察类结果):
            missing = set(result.缺失字段 or [])
            # 目标偏离缺目标：只出「未设置目标」提示，不产业务异常。
            if point == "目标偏离" and missing & {"日均目标单量", "有效目标"}:
                coverage[actual_point] = "CONFIG_REQUIRED"
                results.append({
                    "问题点位": "目标配置待补",
                    "严重度": "配置待补",
                    "分档名称": "未设置目标",
                    "判定过程": result.原因 or "未设置目标，不判定目标偏离",
                    "命中值": None,
                    "数据快照": [],
                    "缺失目标": sorted(missing & {"日均目标单量", "有效目标"}),
                    "命中变体": child_asin or "",
                })
            else:
                coverage[actual_point] = (
                    "CONFIG_REQUIRED"
                    if point in {"ACOS异常", "广告花费异常"}
                    and missing & {"目标ACOS", "目标每日预算"}
                    else "INSUFFICIENT"
                )
        else:
            coverage[actual_point] = "EVALUATED"

    calls = [
        # 2026-08-21 方案：ACOS异常/广告花费异常 暂停判定（不查不判不投）。
        # 采集保留（business_report 同时服务销量/退款/转化率等点位）。
        (R3.判销量类异常, aggregate.to_销量类异常_kwargs(), "销量异常", None),
        (R3.判目标偏离, aggregate.to_目标偏离_kwargs(), "目标偏离", None),
        (R3.判自然流量异常, aggregate.to_自然流量异常_kwargs(), "自然流量异常", None),
        (R3.判退款异常, aggregate.to_退款异常_kwargs(), "退款异常", None),
        (R3.判库存积压, aggregate.to_库存积压_kwargs(), "库存积压", None),
        (R3.判放量未执行, aggregate.to_放量未执行_kwargs(), "放量未执行", None),
        (
            R3.判链接转化率异常,
            aggregate.to_链接转化率异常_kwargs(),
            "链接转化率异常下降",
            None,
        ),
        (R3.判评分异常, aggregate.to_评分异常_kwargs(), "评分异常", None),
    ]
    # 滞销异常（正常判定，不受卡位开关影响）
    calls.append(
        (R3.判滞销异常, aggregate.to_滞销异常_kwargs(), "滞销异常", aggregate.命中子ASIN)
    )
    # 临时开关（feature_flags.keyword_rank_enabled）：关闭时跳过卡位异常判定。
    if keyword_rank_enabled:
        calls.append(
            (R3.判卡位异常, aggregate.to_卡位异常_kwargs(), "卡位异常", None)
        )
    for function, arguments, point, child_asin in calls:
        collect(function(**arguments, 参数=r3_config), point, child_asin=child_asin)
    return results


def _merge_child_scope_signals(
    signals: list[AnomalySignal],
    parent_asin: str,
) -> list[AnomalySignal]:
    """把同一异常点位的多条子 ASIN 信号合并为一条经营单元级信号。

    变体级异常（属性信息异常、A+异常、五点异常等）此前按子 ASIN 逐条输出，
    一个父体有多少子体就刷多少条。这里按 point_code 归并：命中子体收进
    ``child_asins``，信号本身提升到经营单元（父 ASIN）层级，只保留一条。
    """
    merged: dict[str, AnomalySignal] = {}
    order: list[str] = []
    for signal in signals:
        existing = merged.get(signal.point_code)
        if existing is None:
            if signal.child_asins:
                signal = signal.model_copy(update={
                    "signal_id": f"is_{uuid4().hex[:24]}",
                    "target_type": "PARENT_ASIN",
                    "target_id": parent_asin,
                })
            merged[signal.point_code] = signal
            order.append(signal.point_code)
            continue
        child_asins = list(dict.fromkeys([*existing.child_asins, *signal.child_asins]))
        merged[signal.point_code] = existing.model_copy(update={
            "signal_id": f"is_{uuid4().hex[:24]}",
            "target_type": "PARENT_ASIN",
            "target_id": parent_asin,
            "child_asins": child_asins,
        })
    return [merged[code] for code in order]


class LegacyRuleAdapter:
    """驱动现有 R2/R3 规则，输出契约化异常信号。"""

    def __init__(
        self,
        r2_config: dict | None = None,
        r3_config: dict | None = None,
        *,
        storefront_facts_available: bool = False,
        keyword_facts_available: bool = False,
        child_points_enabled: bool = True,
        keyword_rank_points_enabled: bool = True,
        compliance_points_enabled: bool = True,
    ) -> None:
        self.r2_config = r2_config if r2_config is not None else R2.加载R2()
        self.r3_config = r3_config if r3_config is not None else R3.加载参数()
        self.storefront_facts_available = storefront_facts_available
        self.keyword_facts_available = keyword_facts_available
        # 临时开关（feature_flags.keyword_rank_enabled）：关闭时跳过卡位异常判定。
        self.keyword_rank_points_enabled = keyword_rank_points_enabled
        # 临时开关（feature_flags.compliance_enabled）：关闭时跳过合规异常判定。
        self.compliance_points_enabled = compliance_points_enabled
        # 临时开关（config/settings.yaml feature_flags.child_fact_enabled）：
        # 关闭时跳过全部变体级（子 ASIN 层）点位判定，只保留父体链接级表现型。
        self.child_points_enabled = child_points_enabled

    # -- 主入口 ------------------------------------------------------------
    def inspect(self, snapshot: OperatingFactSnapshot) -> list[AnomalySignal]:
        """跑完 R2 + R3，返回统一的异常信号列表。"""
        try:
            legacy_snapshot = build_legacy_snapshot_dict(snapshot)
            aggregate = build_aggregate(snapshot)
        except Exception as exc:
            raise PatrolRuleFailed(
                f"failed to adapt fact snapshot for legacy rules: {type(exc).__name__}: {exc}"
            ) from exc

        signals: list[AnomalySignal] = []
        signals.extend(self._run_phenomenon(snapshot, legacy_snapshot, aggregate))
        signals.extend(self._run_performance(snapshot, aggregate))
        unavailable = set(self._unavailable_points_from_sections(
            identity=snapshot.identity,
            price=snapshot.price,
            quality=snapshot.quality,
            traffic=snapshot.traffic,
        ))
        filtered = [signal for signal in signals if signal.point_code not in unavailable]
        return _merge_child_scope_signals(filtered, snapshot.operating_unit.parent_asin)

    def unavailable_points(
        self, normalized: dict[str, Any] | None = None
    ) -> list[str]:
        """本轮事实源覆盖不到、因此不参与判定的点位。"""
        if normalized is None:
            if self.storefront_facts_available and self.keyword_facts_available:
                return []
            return sorted({
                "主图异常", "图片异常", "A+异常", "Buy Box丢失", "促销异常",
                "变体价差异常", "近期集中差评", "类目异常",
                "属性信息异常", "链接不可售",
            })

        return self._unavailable_points_from_sections(
            identity=normalized.get("identity") or {},
            price=normalized.get("price") or {},
            quality=normalized.get("quality") or {},
            traffic=normalized.get("traffic") or {},
        )

    @staticmethod
    def _unavailable_points_from_sections(
        *,
        identity: dict[str, Any],
        price: dict[str, Any],
        quality: dict[str, Any],
        traffic: dict[str, Any],
    ) -> list[str]:
        """按归一化事实的实际覆盖情况计算本轮不可判点位。"""
        children = identity.get("children") or []
        frontend_ready = bool(children) and all(
            (child.get("frontend") or {}).get("evaluated") is True for child in children
        )
        price_ready = bool(children) and all(
            (child.get("price_promotion") or {}).get("evaluated") is True
            for child in children
        )
        category_ready = price_ready and all(
            (child.get("frontend") or {}).get("category_name")
            and (child.get("category_baseline") or {}).get("status") == "CONFIRMED"
            for child in children
        )
        relation_ready = frontend_ready and all(
            (child.get("frontend") or {}).get("parent_asin")
            for child in children
        )
        compliance_ready = bool(children) and all(
            child.get("compliance_evaluated") is True for child in children
        )

        points: set[str] = set()
        if not children or any(
            (child.get("frontend") or {}).get("buy_box_owned") is None
            for child in children
        ):
            points.add("Buy Box丢失")
        if not frontend_ready:
            points |= {
                "主图异常", "图片异常", "A+异常", "属性信息异常",
            }
        if not relation_ready:
            points |= {"父子体关系异常", "变体掉线"}
        if not price_ready:
            points.add("变体价差异常")
        if not category_ready:
            points.add("类目异常")
        if not (
            price_ready and price.get("platform_activity_evaluated") is True
        ):
            points.add("促销异常")
        if quality.get("recent_reviews_evaluated") is not True:
            points.add("近期集中差评")
        if not compliance_ready:
            points.add("合规异常")
        if not traffic.get("keyword_rank_series"):
            points.add("卡位异常")
        return sorted(points)

    def coverage_gap(
        self, normalized: dict[str, Any] | None = None
    ) -> DataGap | None:
        """把"点位判不了"表达成一条数据缺口，而不是悄悄跳过。"""
        points = self.unavailable_points(normalized)
        if not points:
            return None
        return DataGap(
            code="INSPECTION_POINT_COVERAGE_LIMITED",
            field="inspection.points",
            impact=(
                f"{len(points)} 个点位缺少事实源，本轮未判定："
                f"{'、'.join(points)}"
            ),
            repair_action=(
                "重试对应 AZListing 扩展工具；Buy Box 另需冻结本店 Seller ID 对照"
            ),
            blocking=False,
        )

    # -- R2 现象即原因型 ----------------------------------------------------
    def _run_phenomenon(
        self,
        snapshot: OperatingFactSnapshot,
        legacy_snapshot: dict[str, Any],
        aggregate: Any,
    ) -> list[AnomalySignal]:
        # 临时开关：子 ASIN 巡检关闭时，变体级现象型点位（链接不可售、Buy Box、
        # 主图/五点/A+、变体价差、FBA=0、库存不足、合规、父子关系等）整组跳过。
        if not self.child_points_enabled:
            return []
        try:
            hits = _detect_runtime_facts(
                legacy_snapshot, aggregate, self.r2_config, self.r3_config,
                compliance_points_enabled=self.compliance_points_enabled,
            )
        except Exception as exc:
            raise PatrolRuleFailed(
                f"R2 phenomenon detection failed: {type(exc).__name__}: {exc}"
            ) from exc

        signals: list[AnomalySignal] = []
        for hit in hits:
            point = getattr(hit, "问题点位", None)
            if not point:
                continue
            severity = SEVERITY_MAP.get(
                getattr(hit, "默认严重度", None) or "", Severity.DATA_INSUFFICIENT,
            )
            child = getattr(hit, "命中变体", None)
            signals.append(AnomalySignal(
                signal_id=f"is_{uuid4().hex[:24]}",
                category=self._category(point),
                point_code=point,
                issue_code=issue_code_for(point),
                target_type="CHILD_ASIN" if child else "PARENT_ASIN",
                target_id=child or snapshot.operating_unit.parent_asin,
                child_asins=[child] if child else [],
                severity=severity,
                signal_type=SignalType.ANOMALY,
                description=getattr(hit, "命中依据", "") or point,
                reason=getattr(hit, "命中依据", "") or "",
                metrics={
                    "触发字段": getattr(hit, "触发字段", {}) or {},
                    "作用层级": getattr(hit, "作用层级", None),
                    "变体重要性": getattr(hit, "变体重要性", None),
                },
                evidence_refs=[snapshot.snapshot_id],
                lifecycle_status=SignalState.NEW,
                detected_by="R2",
            ))
        return signals

    # -- R3 表现型 ----------------------------------------------------------
    def _run_performance(
        self,
        snapshot: OperatingFactSnapshot,
        aggregate: Any,
    ) -> list[AnomalySignal]:
        coverage: dict[str, str] = {}
        try:
            results = _grade_runtime_performance(
                aggregate,
                self.r3_config,
                coverage,
                keyword_rank_enabled=self.keyword_rank_points_enabled,
            )
        except Exception as exc:
            raise PatrolRuleFailed(
                f"R3 severity grading failed: {type(exc).__name__}: {exc}"
            ) from exc

        # 销量数据过期时，依赖时序的点位判定不可信，整组降级为观察。
        stale = (snapshot.sales or {}).get("freshness") == "STALE"

        signals: list[AnomalySignal] = []
        for hit in results:
            point = hit.get("问题点位")
            if not point:
                continue
            raw_severity = hit.get("严重度")
            severity = SEVERITY_MAP.get(raw_severity or "", Severity.DATA_INSUFFICIENT)
            if stale:
                severity = Severity.DATA_INSUFFICIENT
            child = hit.get("命中变体") or None
            is_config = point == "目标配置待补" or raw_severity == "配置待补"

            signals.append(AnomalySignal(
                signal_id=f"is_{uuid4().hex[:24]}",
                category=self._category(point),
                point_code=point,
                issue_code=issue_code_for(point),
                target_type="CHILD_ASIN" if child else "PARENT_ASIN",
                target_id=child or snapshot.operating_unit.parent_asin,
                child_asins=[child] if child else [],
                severity=severity,
                signal_type=SignalType.DATA_QUALITY if is_config else SignalType.ANOMALY,
                description=hit.get("判定过程") or hit.get("分档名称") or point,
                reason=hit.get("判定过程") or "",
                metrics={
                    "命中值": hit.get("命中值"),
                    "分档名称": hit.get("分档名称"),
                    "数据快照": hit.get("数据快照") or [],
                    "缺失目标": hit.get("缺失目标"),
                    "关联现象": hit.get("关联现象"),
                    "销量数据过期降级": stale,
                },
                evidence_refs=[snapshot.snapshot_id],
                lifecycle_status=SignalState.NEW,
                detected_by="R3",
            ))

        self.last_coverage = coverage
        return signals

    # -- 工具 --------------------------------------------------------------
    @staticmethod
    def _category(point: str) -> str:
        from inspector.issue_codes import category_for

        number, name = category_for(point)
        return f"{number} {name}" if number != "0.0" else name
