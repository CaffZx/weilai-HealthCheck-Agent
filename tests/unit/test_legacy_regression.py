"""现有 R2/R3/R7 规则回归。

改造方案 v1.0 §26.4：现有异常检测用例必须继续通过。

现有项目没有 pytest 套件，只有三个 `跑内置算例()` 自检函数（靠 YAML 里的
`单元测试算例` 段驱动）。这里把它们包成 pytest 用例，让 CI 能跑、能报红。

同时验证适配器把统一事实快照翻译成旧规则输入后，九类异常仍然能被检出。
"""
from __future__ import annotations

import sys
from datetime import timedelta
from typing import Any

import pytest

from clients.mcp_client import FakeMcpClient, McpToolResult, request_hash
from core.enums import Severity
from facts.collector import FactCollector
from facts.normalizer import FactNormalizer
from facts.quality import FactQualityService
from facts.snapshot import FactSnapshotBuilder
from inspector.legacy_rule_adapter import LegacyRuleAdapter
from tests.conftest import TODAY, collect_facts_with_children, healthy_mcp_payload

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# 旧引擎内置算例
# ---------------------------------------------------------------------------
def test_anomaly_detector_builtin_cases():
    from inspector.engine import anomaly_detector as R2

    results = R2.跑内置算例()
    failed = [(name, detail) for name, ok, detail in results if not ok]
    assert not failed, f"R2 内置算例失败：{failed}"
    assert len(results) > 0


def test_severity_grader_builtin_cases():
    from inspector.engine import severity_grader as R3

    results = R3.跑内置算例()
    failed = [(name, detail) for name, ok, detail in results if not ok]
    assert not failed, f"R3 内置算例失败：{failed}"
    assert len(results) > 0


def test_rule_yaml_versions_are_present():
    """规则版本号必须存在 —— 提交包要靠它做溯源。"""
    from inspector.engine import anomaly_detector as R2
    from inspector.engine import severity_grader as R3

    assert R2.加载R2().get("版本")
    assert R3.加载参数().get("版本")


def test_review_schedule_rule_version_and_issue_mapping():
    """R7 必须可追溯，且同一异常只能命中一个观察窗口。"""
    from data.review_schedule import load_rules

    rules = load_rules()
    assert rules.get("version")
    assert rules.get("default", {}).get("days") == 3

    issue_days: dict[str, int] = {}
    for rule in rules.get("rules") or []:
        days = int(rule["days"])
        for issue in rule.get("issues") or []:
            assert issue not in issue_days, f"R7 异常重复映射：{issue}"
            issue_days[issue] = days

    assert issue_days["链接不可售"] == 1
    assert issue_days["销量异常"] == 3
    assert issue_days["链接转化率异常下降"] == 7
    assert issue_days["近期集中差评"] == 14
    assert issue_days["库存不足"] == 30


@pytest.mark.parametrize(
    ("issue", "expected_days"),
    [
        ("链接不可售", 1),
        ("销量异常", 3),
        ("链接转化率异常下降", 7),
        ("近期集中差评", 14),
        ("库存不足", 30),
    ],
)
def test_review_schedule_resolves_key_windows(issue, expected_days):
    from data.review_schedule import resolve_schedule

    result = resolve_schedule({"问题点位": issue}, today=TODAY)
    assert result["days"] == expected_days
    assert result["next_inspection_at"] == (TODAY + timedelta(days=expected_days)).isoformat()


# ---------------------------------------------------------------------------
# 适配器驱动旧规则
# ---------------------------------------------------------------------------
def build_snapshot(payload: dict, binding, unit):
    fake = FakeMcpClient(responses=payload)
    raw = collect_facts_with_children(FactCollector(fake), binding)
    normalized, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, normalized)
    return FactSnapshotBuilder().build(unit, normalized, refs, gaps)


class AsinAwareFakeMcpClient:
    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]],
        asin_responses: dict[tuple[str, str], list[dict[str, Any]] | Exception],
    ) -> None:
        self.responses = responses
        self.asin_responses = asin_responses

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
    ) -> McpToolResult:
        asin = str(arguments.get("asin") or "").upper()
        response = self.asin_responses.get(
            (tool_name, asin), self.responses.get(tool_name, [])
        )
        if isinstance(response, Exception):
            raise response
        return McpToolResult(
            tool_name=tool_name,
            data=response,
            raw={"structuredContent": {"data": response}},
            request_hash=request_hash(tool_name, arguments),
        )


def child_detail(
    *,
    asin: str = "B0CHILD01",
    parent_asin: str = "B0TEST123",
    in_stock: bool = True,
    main_image: str | None = "https://example.test/main.jpg",
    gallery_count: int = 7,
    has_aplus: bool = True,
    color: str = "Red",
    size: str = "M",
    buy_box_seller_id: str | None = None,
    features: list[str] | None = None,
) -> list[dict[str, Any]]:
    result = {
        "asin": asin,
        "linkStatus": {
            "inStock": in_stock,
            "hasCart": True,
            "buyBoxSellerId": buy_box_seller_id,
        },
        "images": {
            "main": main_image,
            "all": [f"https://example.test/{index}.jpg" for index in range(gallery_count)],
        },
        "aplus": {"hasAplus": has_aplus},
        "bonus": {"parentAsin": parent_asin, "color": color, "size": size},
    }
    if features is not None:
        result["features"] = features
    return [result]


def build_extended_snapshot(
    payload: dict,
    binding,
    unit,
    *,
    child_details: dict[str, list[dict[str, Any]] | Exception] | None = None,
):
    details = child_details or {"B0CHILD01": child_detail()}
    asin_responses = {
        ("erp_asin_full_detail", asin): response
        for asin, response in details.items()
    }
    fake = AsinAwareFakeMcpClient(payload, asin_responses)
    raw = collect_facts_with_children(FactCollector(fake), binding)
    normalized, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, normalized)
    snapshot = FactSnapshotBuilder().build(unit, normalized, refs, gaps)
    return snapshot, normalized


def points_from(signals) -> set[str]:
    return {s.point_code for s in signals}


def test_healthy_product_has_no_s0(binding, unit):
    snapshot = build_snapshot(healthy_mcp_payload(), binding, unit)
    signals = LegacyRuleAdapter().inspect(snapshot)
    s0 = [s for s in signals if Severity(s.severity) is Severity.S0]
    assert not s0, f"健康产品不应有 S0：{[s.point_code for s in s0]}"


def test_runtime_rule_adapter_does_not_load_sqlite_modules(binding, unit):
    snapshot = build_snapshot(healthy_mcp_payload(), binding, unit)
    forbidden = {
        "sqlite3",
        "data.local_store",
        "integrations.db",
        "inspector.inspector_main",
        "inspector.scheduler.daily_monitor",
    }
    loaded_before = forbidden & set(sys.modules)

    LegacyRuleAdapter().inspect(snapshot)

    assert (forbidden & set(sys.modules)) == loaded_before


def test_zero_inventory_is_detected(binding, unit):
    payload = healthy_mcp_payload()
    payload["erp_listing_stock_alert"][0].update({
        "canSaleNum": 0, "asin": "B0CHILD01", "sellerSku": "SKU-1",
    })
    snapshot = build_snapshot(payload, binding, unit)
    signals = LegacyRuleAdapter().inspect(snapshot)
    assert "FBA可售库存为0" in points_from(signals)


def test_low_inventory_is_detected(binding, unit):
    payload = healthy_mcp_payload()
    payload["erp_listing_stock_alert"][0].update({
        "canSaleNum": 10, "asin": "B0CHILD01", "sellerSku": "SKU-1",
    })  # 日均 12 → 不足 1 天
    snapshot = build_snapshot(payload, binding, unit)
    signals = LegacyRuleAdapter().inspect(snapshot)
    assert "库存不足" in points_from(signals)


def test_parent_inventory_summary_is_not_projected_to_a_child(binding, unit):
    payload = healthy_mcp_payload()
    payload["erp_listing_stock_alert"][0]["canSaleNum"] = 0
    snapshot = build_snapshot(payload, binding, unit)
    signals = LegacyRuleAdapter().inspect(snapshot)

    assert snapshot.inventory["stock_scope"] == "PARENT_SUMMARY"
    assert "FBA可售库存为0" not in points_from(signals)


def test_missing_bullet_points_is_detected(binding, unit, monday):
    payload = healthy_mcp_payload()
    child = payload["erp_listing_product_info"][0]
    for index in range(2, 6):
        child[f"fiveBulletPoint{index}"] = ""
    # 前台五点（features）只有 1 条 → 前台口径命中（前台不可得才不判，这里前台可得）
    snapshot, _ = build_extended_snapshot(
        payload, binding, unit,
        child_details={"B0CHILD01": child_detail(features=["仅 1 条五点"])},
    )
    signals = LegacyRuleAdapter().inspect(snapshot)
    assert "五点异常" in points_from(signals)


def test_child_asin_is_carried_into_signal(binding, unit, monday):
    payload = healthy_mcp_payload()
    child = payload["erp_listing_product_info"][0]
    for index in range(2, 6):
        child[f"fiveBulletPoint{index}"] = ""
    snapshot, _ = build_extended_snapshot(
        payload, binding, unit,
        child_details={"B0CHILD01": child_detail(features=["仅 1 条五点"])},
    )
    signal = next(
        s for s in LegacyRuleAdapter().inspect(snapshot) if s.point_code == "五点异常"
    )
    # 变体级异常已提升到经营单元级，命中子体收进 child_asins
    assert signal.target_type == "PARENT_ASIN"
    assert signal.target_id == "B0TEST123"
    assert signal.child_asins == ["B0CHILD01"]


def test_child_scope_signals_merge_into_one_parent_signal():
    from core.contracts import AnomalySignal
    from core.enums import SignalState, SignalType
    from inspector.legacy_rule_adapter import _merge_child_scope_signals

    signals = [
        AnomalySignal(
            signal_id="is_0123456789abcdef01234567", category="内容完整性", point_code="A+异常",
            issue_code="APLUS_CONTENT_ABNORMAL", target_type="CHILD_ASIN",
            target_id="B0CHILD01", child_asins=["B0CHILD01"], severity=Severity.S1,
            signal_type=SignalType.ANOMALY, description="A+异常", reason="A+未创建",
            evidence_refs=["fs_1"], lifecycle_status=SignalState.NEW,
        ),
        AnomalySignal(
            signal_id="is_0123456789abcdef01234568", category="内容完整性", point_code="A+异常",
            issue_code="APLUS_CONTENT_ABNORMAL", target_type="CHILD_ASIN",
            target_id="B0CHILD02", child_asins=["B0CHILD02"], severity=Severity.S1,
            signal_type=SignalType.ANOMALY, description="A+异常", reason="A+未创建",
            evidence_refs=["fs_1"], lifecycle_status=SignalState.NEW,
        ),
    ]

    merged = _merge_child_scope_signals(signals, "B0PARENT")

    assert len(merged) == 1
    assert merged[0].point_code == "A+异常"
    assert merged[0].target_type == "PARENT_ASIN"
    assert merged[0].target_id == "B0PARENT"
    assert merged[0].child_asins == ["B0CHILD01", "B0CHILD02"]


@pytest.mark.parametrize(
    ("detail_overrides", "expected_point"),
    [
        # 主图不可得（main_image=None，爬虫空）不再报"主图缺失"——与五点/标题
        # "前台不可得不判"原则一致，新语义由 test_child_detail_unavailable 覆盖。
        ({"gallery_count": 2}, "图片异常"),
        ({"has_aplus": False}, "A+异常"),
        ({"in_stock": False}, "链接不可售"),
        ({"color": "Blue"}, "属性信息异常"),
    ],
)
def test_child_detail_anomalies_are_detected(
    binding, unit, detail_overrides, expected_point, monday
):
    snapshot, _ = build_extended_snapshot(
        healthy_mcp_payload(),
        binding,
        unit,
        child_details={"B0CHILD01": child_detail(**detail_overrides)},
    )

    assert expected_point in points_from(LegacyRuleAdapter().inspect(snapshot))


def test_category_mismatch_is_detected(binding, unit, monday):
    payload = healthy_mcp_payload()
    payload["erp_listing_price_promotion_analysis"][0]["categoryName"] = "Shoes"
    snapshot, _ = build_extended_snapshot(payload, binding, unit)
    child = snapshot.identity["children"][0]
    snapshot = snapshot.model_copy(update={
        "identity": {
            **snapshot.identity,
            "children": [{
                **child,
                "category_baseline": {
                    "category_path": "Apparel",
                    "status": "CONFIRMED",
                    "source_tool": "erp_listing_price_promotion_analysis",
                },
            }],
        }
    })

    assert "类目异常" in points_from(LegacyRuleAdapter().inspect(snapshot))


def test_pending_category_baseline_never_creates_anomaly(binding, unit):
    payload = healthy_mcp_payload()
    payload["erp_listing_price_promotion_analysis"][0]["categoryName"] = "Shoes"
    snapshot, normalized = build_extended_snapshot(payload, binding, unit)
    child = snapshot.identity["children"][0]
    snapshot = snapshot.model_copy(update={
        "identity": {
            **snapshot.identity,
            "children": [{
                **child,
                "category_baseline": {
                    "category_path": "Apparel",
                    "status": "PENDING_CONFIRMATION",
                    "source_tool": "erp_listing_price_promotion_analysis",
                },
            }],
        }
    })

    assert "类目异常" not in points_from(LegacyRuleAdapter().inspect(snapshot))
    assert "类目异常" in LegacyRuleAdapter().unavailable_points({
        **normalized,
        "identity": snapshot.identity,
    })


def test_compliance_issue_is_detected(binding, unit):
    payload = healthy_mcp_payload()
    payload["erp_amazon_account_health_issue"] = [{
        "asin": "B0CHILD01",
        "issues": [{"reason": "restricted product policy"}],
    }]
    snapshot, _ = build_extended_snapshot(payload, binding, unit)

    assert "合规异常" in points_from(LegacyRuleAdapter().inspect(snapshot))


def test_configured_promotion_missing_on_storefront_is_detected(binding, unit, monday):
    payload = healthy_mcp_payload()
    payload["erp_listing_platform_activity"] = [{
        "sellerSku": "SKU-1",
        "activityName": "Test promotion",
        "activityType": "COUPON",
        "recordDate": TODAY.isoformat(),
    }]
    snapshot, _ = build_extended_snapshot(payload, binding, unit)

    assert "促销异常" in points_from(LegacyRuleAdapter().inspect(snapshot))


def test_expired_platform_activity_does_not_create_promotion_anomaly(binding, unit):
    payload = healthy_mcp_payload()
    payload["erp_listing_platform_activity"] = [{
        "sellerSku": "SKU-1",
        "activityName": "Historical promotion",
        "activityTypeDicValue": "Coupon",
        "recordDate": "2026-02-01",
        "startActivityDate": "2026-02-01",
        "endActivityDate": "2026-02-28",
        "completionDate": "2026-02-20",
    }]
    snapshot, normalized = build_extended_snapshot(payload, binding, unit)

    assert normalized["price"]["platform_activities"] == []
    assert normalized["price"]["platform_activity_history"]
    assert "促销异常" not in points_from(LegacyRuleAdapter().inspect(snapshot))


def test_unevaluated_storefront_promotion_is_only_a_coverage_gap(binding, unit):
    payload = healthy_mcp_payload()
    payload["erp_listing_platform_activity"] = [{
        "sellerSku": "SKU-1",
        "activityName": "Test promotion",
        "activityType": "COUPON",
        "recordDate": TODAY.isoformat(),
    }]
    client = AsinAwareFakeMcpClient(
        payload,
        {
            ("erp_asin_full_detail", "B0CHILD01"): child_detail(),
            ("erp_listing_price_promotion_analysis", "B0CHILD01"): RuntimeError(
                "price promotion unavailable"
            ),
        },
    )
    raw = collect_facts_with_children(FactCollector(client), binding)
    normalized, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    snapshot = FactSnapshotBuilder().build(
        unit,
        normalized,
        refs,
        FactQualityService().check(binding, raw, normalized),
    )
    adapter = LegacyRuleAdapter()

    assert "促销异常" not in points_from(adapter.inspect(snapshot))
    assert "促销异常" in adapter.unavailable_points(normalized)


def test_variant_price_gap_is_detected(binding, unit, monday):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"].append({
        **payload["erp_listing_product_info"][0],
        "asin": "B0CHILD02",
        "sellerSku": "SKU-2",
        "productColor": "Blue",
        "finenessDesc": "SecondaryColor",
    })
    details = {
        "B0CHILD01": child_detail(),
        "B0CHILD02": child_detail(asin="B0CHILD02", color="Blue"),
    }
    payload["erp_listing_price_promotion_analysis"] = [{
        "asin": "B0CHILD01",
        "price": "$19.90",
        "categoryName": "Apparel",
    }]
    client = AsinAwareFakeMcpClient(
        payload,
        {
            **{("erp_asin_full_detail", asin): value for asin, value in details.items()},
            ("erp_listing_price_promotion_analysis", "B0CHILD02"): [{
                "asin": "B0CHILD02", "price": "$29.90", "categoryName": "Apparel",
            }],
        },
    )
    raw = collect_facts_with_children(FactCollector(client), binding)
    normalized, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    snapshot = FactSnapshotBuilder().build(
        unit,
        normalized,
        refs,
        FactQualityService().check(binding, raw, normalized),
    )

    assert "变体价差异常" in points_from(LegacyRuleAdapter().inspect(snapshot))


@pytest.mark.skip(
    reason="recent_reviews 子体工具已禁用（评论爬虫 180s 长尾瓶颈），"
    "'近期集中差评'异常点暂不可判；恢复差评异常时从 DISABLED_EXTENSION_TOOLS 移除即可"
)
def test_recent_concentrated_negative_reviews_are_detected(binding, unit):
    payload = healthy_mcp_payload()
    payload["erp_listing_review_analysis"] = [{
        "asin": "B0CHILD01",
        "reviews": [
            {
                "rating": 1,
                "date": (TODAY - timedelta(days=offset + 1)).isoformat(),
                "content": f"Poor quality broken item {offset}",
            }
            for offset in range(3)
        ],
    }]
    snapshot, _ = build_extended_snapshot(payload, binding, unit)

    assert "近期集中差评" in points_from(LegacyRuleAdapter().inspect(snapshot))


def test_keyword_rank_decline_is_detected(binding, unit):
    payload = healthy_mcp_payload()
    payload["erp_listing_asin_keyword_rank_history"] = [{
        "asin": "B0CHILD01",
        "keyword": "test keyword",
        "siteCode": "US",
        "createTime": (TODAY - timedelta(days=offset)).isoformat(),
        "crawNatureRank": 100 if offset < 3 else 20,
    } for offset in range(14)]
    snapshot, _ = build_extended_snapshot(payload, binding, unit)

    assert "卡位异常" in points_from(LegacyRuleAdapter().inspect(snapshot))


def test_failed_extension_is_a_gap_not_a_business_anomaly(binding, unit):
    snapshot, normalized = build_extended_snapshot(
        healthy_mcp_payload(),
        binding,
        unit,
        child_details={"B0CHILD01": RuntimeError("detail unavailable")},
    )
    points = points_from(LegacyRuleAdapter().inspect(snapshot))
    unavailable = LegacyRuleAdapter().unavailable_points(normalized)

    assert {"主图异常", "图片异常", "A+异常"}.isdisjoint(points)
    assert {"主图异常", "图片异常", "A+异常"}.issubset(unavailable)
    assert "链接不可售" not in unavailable
    assert "链接不可售" not in points
    assert "Buy Box丢失" in unavailable


def test_buy_box_is_evaluated_only_with_own_seller_id(binding, unit, monday):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"][0]["SELLING_PARTNER_ID"] = "A1OWNSELLER"
    snapshot, normalized = build_extended_snapshot(
        payload,
        binding,
        unit,
        child_details={
            "B0CHILD01": child_detail(buy_box_seller_id="A1OTHERSELLER")
        },
    )
    snapshot = snapshot.model_copy(update={
        "identity": {
            **snapshot.identity,
            "own_seller_id": "A1OWNSELLER",
            "children": [
                {
                    **snapshot.identity["children"][0],
                    "frontend": {
                        **snapshot.identity["children"][0]["frontend"],
                        "buy_box_owned": False,
                    },
                }
            ],
        }
    })

    assert "Buy Box丢失" in points_from(LegacyRuleAdapter().inspect(snapshot))
    assert "Buy Box丢失" not in LegacyRuleAdapter().unavailable_points({
        **normalized,
        "identity": snapshot.identity,
    })


def test_every_signal_has_a_contract_issue_code(binding, unit):
    import re

    payload = healthy_mcp_payload()
    payload["erp_listing_stock_alert"][0]["canSaleNum"] = 0
    snapshot = build_snapshot(payload, binding, unit)
    pattern = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
    for signal in LegacyRuleAdapter().inspect(snapshot):
        assert pattern.fullmatch(signal.issue_code), signal.issue_code


def test_every_signal_references_the_snapshot(binding, unit):
    snapshot = build_snapshot(healthy_mcp_payload(), binding, unit)
    for signal in LegacyRuleAdapter().inspect(snapshot):
        assert snapshot.snapshot_id in signal.evidence_refs


def test_rating_anomaly_fires_when_below_target(binding, unit):
    snapshot = build_snapshot(healthy_mcp_payload(), binding, unit)
    snapshot = snapshot.model_copy(update={
        "quality": {
            **(snapshot.quality or {}),
            "star_rating": 4.2,
            "target_star_rating": 4.5,
        },
    })
    signals = LegacyRuleAdapter().inspect(snapshot)
    rating = [s for s in signals if s.point_code == "评分异常"]
    assert len(rating) == 1
    assert rating[0].issue_code == "RATING_ABNORMAL"
    assert Severity(rating[0].severity) is Severity.S1


def test_rating_anomaly_skipped_when_star_rating_missing(binding, unit):
    snapshot = build_snapshot(healthy_mcp_payload(), binding, unit)
    snapshot = snapshot.model_copy(update={
        "quality": {
            **(snapshot.quality or {}),
            "star_rating": None,
            "target_star_rating": 4.5,
        },
    })
    signals = LegacyRuleAdapter().inspect(snapshot)
    assert "评分异常" not in {s.point_code for s in signals}


def test_rating_anomaly_skipped_when_meets_target(binding, unit):
    snapshot = build_snapshot(healthy_mcp_payload(), binding, unit)
    snapshot = snapshot.model_copy(update={
        "quality": {
            **(snapshot.quality or {}),
            "star_rating": 4.6,
            "target_star_rating": 4.5,
        },
    })
    signals = LegacyRuleAdapter().inspect(snapshot)
    assert "评分异常" not in {s.point_code for s in signals}


def test_target_deviation_missing_goal_is_tip_not_anomaly(binding, unit):
    snapshot = build_snapshot(healthy_mcp_payload(), binding, unit)
    snapshot = snapshot.model_copy(update={
        "sales": {
            **(snapshot.sales or {}),
            "daily_target_orders": None,
            "monthly_target": None,
            "avg_daily_orders_3d": 5.0,
        },
    })
    signals = LegacyRuleAdapter().inspect(snapshot)
    assert "目标偏离" not in {
        s.point_code for s in signals if s.signal_type.value == "ANOMALY"
    }
    tips = [s for s in signals if s.point_code == "目标配置待补"]
    assert len(tips) == 1
    assert tips[0].signal_type.value == "DATA_QUALITY"
    assert Severity(tips[0].severity) is Severity.DATA_INSUFFICIENT


def test_target_deviation_fires_when_below_target(binding, unit):
    snapshot = build_snapshot(healthy_mcp_payload(), binding, unit)
    snapshot = snapshot.model_copy(update={
        "sales": {
            **(snapshot.sales or {}),
            "daily_target_orders": 10.0,
            "monthly_target": 310.0,
            "avg_daily_orders_3d": 4.0,  # 40% → S1
            "daily_units_7d": [4, 4, 4, 4, 4, 4, 4],
            "monthly_completion_rate": None,
            "month_time_progress": None,
        },
    })
    signals = LegacyRuleAdapter().inspect(snapshot)
    hits = [s for s in signals if s.point_code == "目标偏离" and s.signal_type.value == "ANOMALY"]
    assert len(hits) == 1
    assert hits[0].issue_code == "TARGET_DEVIATION"


def test_stale_sales_downgrades_performance_signals(binding, unit):
    """销量数据过期 → 时序类判定降级为观察，不出正式严重度。"""
    from datetime import timedelta

    payload = healthy_mcp_payload()
    old = (TODAY - timedelta(days=15)).isoformat()
    for row in payload["erp_listing_gross_profit_history"]:
        row["statDate"] = old
    snapshot = build_snapshot(payload, binding, unit)
    signals = LegacyRuleAdapter().inspect(snapshot)
    performance = [s for s in signals if s.detected_by == "R3"]
    assert all(
        Severity(s.severity) is Severity.DATA_INSUFFICIENT for s in performance
    ), [(s.point_code, s.severity) for s in performance]


def test_unavailable_points_are_declared_not_silently_skipped(binding, unit):
    """父级详情不能冒充完整子体覆盖，未覆盖点位须显式登记。"""
    adapter = LegacyRuleAdapter()
    unavailable = adapter.unavailable_points()
    assert "主图异常" in unavailable
    assert "图片异常" in unavailable
    assert "A+异常" in unavailable
    assert "Buy Box丢失" in unavailable
    # 卡位异常已停用（keyword_rank_enabled=false），不再登记为数据源不可用。
    assert "卡位异常" not in unavailable

    gap = adapter.coverage_gap()
    assert gap is not None
    assert gap.code == "INSPECTION_POINT_COVERAGE_LIMITED"
    assert gap.blocking is False


def test_storefront_facts_available_clears_the_gap():
    adapter = LegacyRuleAdapter(
        storefront_facts_available=True, keyword_facts_available=True,
    )
    assert adapter.unavailable_points() == []
    assert adapter.coverage_gap() is None


def test_retired_score_calculator_is_not_used():
    """R4 综合执行分已退役，适配器不得调用它。"""
    import inspect

    from inspector import legacy_rule_adapter

    source = inspect.getsource(legacy_rule_adapter)
    assert "score_calculator" not in source
    assert "产品执行分数" not in source
    assert "执行优先级" not in source
