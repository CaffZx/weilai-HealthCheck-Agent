from __future__ import annotations

from datetime import date, datetime

import pytest
import sqlalchemy as sa

from clients.operating_mode_mcp import CurrentOperatingMode
from inspector.issue_codes import POINT_CODES
from scripts.submit_real_patrol_to_control_center import (
    ISSUE_CODE_MAP,
    UnitNotSubmittable,
    _anomalies_and_details,
    _build_unit,
    _fact_snapshot,
    _latest_sources,
)


def _snapshot() -> dict:
    return {
        "snapshot_id": "fs_0123456789abcdef01234567",
        "run_id": "run-1",
        "operating_unit_id": "ou_0123456789abcdef01234567",
        "as_of": date(2026, 8, 5),
        "quality_status": "PARTIAL",
        "completeness_score": 80,
        "content_hash": "0" * 64,
        "data_gaps_json": [],
        "normalized_summary_json": {
            "identity": {"sellable_child_count": 1, "owner_user_ids": [35]},
            "sales": {
                "daily_rows": [
                    {
                        "stat_date": "2026-08-04",
                        "revenue": 10,
                        "units": 2,
                        "orders": None,
                        "ad_cost": 1,
                        "ad_sales": None,
                        "ad_sale_num": None,
                        "ad_click": None,
                        "ad_impressions": None,
                        "gross_margin": None,
                        "gross_profit_amount": -2.0,
                    }
                ]
            },
            "traffic": {
                "yesterday_date": "2026-08-04",
                "yesterday_ad_orders": 4,
            },
            "profit": {"unit_contribution": 2},
            "inventory": {
                "fba_available": 3,
                "fba_inbound": None,
                "fba_reserved": None,
            },
            "price": {},
        },
    }


def _source() -> dict:
    return {
        "shop_id": 1,
        "site_code": "AMAZON_US",
        "parent_asin": "B0TEST",
        "parent_seller_sku": "SKU-P",
        "payload_json": {"binding": {}},
        "scope_json": {},
        "result_ref": "run-1",
    }


def _signal(issue_code: str = "MAIN_IMAGE_ABNORMAL") -> dict:
    return {
        "signal_id": "is_0123456789abcdef01234567",
        "signal_type": "ANOMALY",
        "issue_code": issue_code,
        "severity": "S1",
        "signal_payload_json": {
            "diagnosis": {"summary": "主图异常", "known_causes": []},
            "evidence_refs": ["fact:1"],
        },
        "last_detected_at": datetime(2026, 8, 5, 1, 2, 3),
    }


def test_all_control_center_representable_issue_codes_are_mapped():
    internal_codes = {item[0] for item in POINT_CODES.values()}
    intentionally_not_anomalies = {
        "SALES_GROWTH_SIGNAL",
        "TARGET_CONFIGURATION_MISSING",
    }

    assert internal_codes - intentionally_not_anomalies == set(ISSUE_CODE_MAP)


def test_unknown_issue_code_is_not_silently_mapped_to_target_deviation():
    with pytest.raises(UnitNotSubmittable, match="unsupported issueCode=UNKNOWN_CODE"):
        _anomalies_and_details(
            [_signal("UNKNOWN_CODE")],
            _snapshot(),
            "ou_0123456789abcdef01234567",
        )


def test_data_gap_without_real_anomaly_builds_data_quality_anomaly():
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["sales"]["monthly_target"] = 300
    snapshot["normalized_summary_json"]["sales"]["daily_target_orders"] = 10
    anomalies, details = _anomalies_and_details(
        [],
        snapshot,
        "ou_0123456789abcdef01234567",
    )

    assert anomalies[0]["anomalyCode"] == "TARGET_DEVIATION"
    assert "数据缺口" in anomalies[0]["summary"]
    assert details[0]["proposedValue"]["action"] == "REPAIR_DATA_AND_REVIEW"


def test_missing_monthly_target_does_not_forge_target_deviation_anomaly():
    with pytest.raises(UnitNotSubmittable, match="monthly target not configured"):
        _anomalies_and_details(
            [],
            _snapshot(),
            "ou_0123456789abcdef01234567",
        )


def test_required_metric_absence_raises_unit_not_submittable():
    """必填字段缺失时抛出 UnitNotSubmittable，不再兜底填0。"""
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["sales"]["daily_rows"][0]["revenue"] = None

    with pytest.raises(UnitNotSubmittable):
        _build_unit(_source(), snapshot, [_signal()])


def test_operating_mode_agent_unit_contribution_has_priority():
    current = CurrentOperatingMode(
        shop_id="1",
        parent_asin="B0TEST",
        parent_seller_sku="SKU-P",
        decision_status="DECIDED",
        recommended_mode_code="STABLE_OPERATION",
        recommended_mode="稳定经营",
        explanation="稳定经营",
        parent_unit_contribution=7.5,
        currency="CNY",
    )

    unit = _build_unit(_source(), _snapshot(), [_signal()], current)

    assert unit["operatingMetric"]["contributionProfit"] == 15
    assert unit["proposal"]["rawAnalysis"]["operatingMetricContribution"] == {
        "source": "operating_mode_agent",
        "unitContribution": 7.5,
        "salesQuantity": 2,
        "contributionProfit": 15.0,
        "currency": "CNY",
    }


def test_operating_mode_agent_unit_contribution_unblocks_missing_erp_value():
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["profit"]["unit_contribution"] = None
    current = CurrentOperatingMode(
        shop_id="1",
        parent_asin="B0TEST",
        parent_seller_sku="SKU-P",
        decision_status="BLOCKED",
        recommended_mode_code=None,
        recommended_mode=None,
        explanation="经营模式证据不足",
        parent_unit_contribution=3,
        currency="CNY",
    )

    unit = _build_unit(_source(), snapshot, [_signal()], current)

    assert unit["operatingMetric"]["contributionProfit"] == 6


def test_missing_unit_contribution_raises_when_no_fallback():
    """无经营模式Agent且无 gross_profit_amount 时抛出 UnitNotSubmittable。"""
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["profit"]["unit_contribution"] = None
    snapshot["normalized_summary_json"]["sales"]["daily_rows"][0]["gross_profit_amount"] = None
    # daily_row 没有 gross_profit_amount，所以无法 fallback

    with pytest.raises(UnitNotSubmittable):
        _build_unit(_source(), snapshot, [])


def test_missing_unit_contribution_falls_back_to_gross_profit_amount():
    """无经营模式Agent但有 gross_profit_amount 时使用同源兜底。"""
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["profit"]["unit_contribution"] = None
    snapshot["normalized_summary_json"]["sales"]["daily_rows"][0]["gross_profit_amount"] = -5.0

    unit = _build_unit(_source(), snapshot, [])

    assert unit["operatingMetric"]["contributionProfit"] == pytest.approx(-5.0)
    assert unit["proposal"]["rawAnalysis"]["operatingMetricContribution"]["source"] == (
        "erp_listing_gross_profit_history.grossProfitAmount"
    )


def test_optional_metric_absence_remains_explicit_null():
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["traffic"]["yesterday_ad_orders"] = None
    unit = _build_unit(_source(), snapshot, [_signal()])

    assert unit["listing"]["siteCode"] == "Amazon_US"
    assert unit["operatingMetric"]["orderQuantity"] is None
    assert unit["operatingMetric"]["adClick"] is None
    assert unit["operatingMetric"]["adImpressions"] is None
    assert unit["operatingMetric"]["adSalesAmount"] is None
    assert unit["proposal"]["rawAnalysis"]["fieldCoverage"]["operatingMetric"][
        "optionalNullFields"
    ] == [
        "adClick",
        "adImpressions",
        "adOrder",
        "adSalesAmount",
        "orderQuantity",
        "profitRate",
    ]


def test_catalog_owner_fallback_is_used_when_snapshot_owner_is_absent():
    source = _source()
    source["_cached_image_url"] = (
        "https://m.media-amazon.com/images/I/real-product.jpg"
    )
    source["binding_owner_user_ids"] = "[35]"
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["identity"]["owner_user_ids"] = []
    snapshot["normalized_summary_json"]["identity"]["storefront"] = {
        "main_image_url": None,
    }

    unit = _build_unit(source, snapshot, [_signal()])

    assert unit["listing"]["productPicUrl"].endswith("real-product.jpg")
    assert unit["listing"]["ownerUserId"] == "35"
    assert unit["proposal"]["rawAnalysis"]["ownerUserIds"] == [35]
    assert (
        unit["proposal"]["rawAnalysis"]["ownerSource"]
        == "operating_unit_catalog_fallback"
    )


def test_new_product_info_owner_is_used_when_mysql_enrichment_is_absent():
    source = _source()
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["identity"]["owner_user_ids"] = [35]

    unit = _build_unit(source, snapshot, [_signal()])

    assert unit["listing"]["ownerUserId"] == "35"
    assert unit["proposal"]["rawAnalysis"]["ownerUserIds"] == [35]
    assert unit["proposal"]["rawAnalysis"]["ownerSource"] == "product_info"


def test_operating_tags_are_mapped_without_guessing_unknown_values():
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["profit"]["operating_tags"] = {
        "product_position": "重点产品",
        "product_stage": "推进期",
        "season_type": "未知季节",
    }

    unit = _build_unit(_source(), snapshot, [_signal()])

    assert unit["tags"] == {
        "productLevel": "P1_PRODUCT",
        "productStage": "PUSHING",
    }
    assert unit["proposal"]["rawAnalysis"]["unmappedOperatingTags"] == {
        "season_type": "未知季节"
    }


def test_new_advert_config_tags_are_mapped_for_control_center():
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["profit"]["operating_tags"] = {
        "product_position": "常规产品",
        "product_stage": "推进期",
        "season_type": "大旺季",
        "operating_mode": "限时修复",
        "advert_purposes": "转化型",
        "target_keyword_types": "长尾词",
        "advert_direction_types": "优化 ACOS、平衡维持",
        "day_range": "7天",
    }

    unit = _build_unit(_source(), snapshot, [_signal()])

    assert unit["tags"] == {
        "productLevel": "P2_PRODUCT",
        "productStage": "PUSHING",
        "seasonStage": "PEAK",
        "adPurposes": ["CONVERSION"],
        "targetKeywordStrategies": ["LONG_TAIL"],
        "adDirections": ["OPTIMIZE_ACOS", "BALANCE_MAINTAIN"],
    }
    assert unit["proposal"]["rawAnalysis"]["unmappedOperatingTags"] == {}


def test_advert_config_tags_priority_over_parent_extension():
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["identity"]["parent_extension"] = {
        "progress_preparation_code": "COMPLETION_PERIOD",
        "seasonality_code": "LATE_PEAK_SEASON",
    }
    snapshot["normalized_summary_json"]["profit"]["operating_tags"] = {
        "product_stage": "测试期",
        "season_type": "淡季",
    }

    unit = _build_unit(_source(), snapshot, [_signal()])

    # 广告配置优先，parent_extension 只在广告配置缺失时兜底
    assert unit["tags"] == {
        "productStage": "TESTING",
        "seasonStage": "OFF_SEASON",
    }


def test_parent_extension_fills_tags_when_advert_config_missing():
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["identity"]["parent_extension"] = {
        "progress_preparation_code": "COMPLETION_PERIOD",
        "seasonality_code": "LATE_PEAK_SEASON",
    }

    unit = _build_unit(_source(), snapshot, [_signal()])

    assert unit["tags"] == {
        "productStage": "HARVESTING",
        "seasonStage": "LATE_PEAK",
    }


def test_current_day_placeholder_is_replaced_by_latest_complete_prior_day():
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["sales"]["daily_rows"] = [
        {
            "stat_date": "2026-08-05",
            "revenue": 0,
            "units": 0,
            "orders": 0,
            "ad_cost": 0,
        },
        {
            "stat_date": "2026-08-04",
            "revenue": 217.7,
            "units": 30,
            "orders": 29,
            "ad_cost": 25.02,
            "ad_sales": 48.93,
            "ad_sale_num": 7,
            "ad_click": 123,
            "ad_impressions": 4567,
            "gross_profit_amount": 50.0,
        },
    ]

    unit = _build_unit(_source(), snapshot, [_signal()])

    assert unit["operatingMetric"]["metricDate"] == "2026-08-04"
    assert unit["operatingMetric"]["salesAmount"] == 217.7
    assert unit["operatingMetric"]["salesQuantity"] == 30
    assert unit["operatingMetric"]["orderQuantity"] == 29
    assert unit["operatingMetric"]["adCost"] == 25.02
    assert unit["operatingMetric"]["adClick"] == 123
    assert unit["operatingMetric"]["adImpressions"] == 4567
    assert unit["operatingMetric"]["adOrder"] == 7


def test_ad_order_uses_daily_sale_num_from_gross_profit():
    """adOrder 现在从 gross_profit_history 同源 ad_sale_num 取，不再跨工具。"""
    snapshot = _snapshot()
    snapshot["normalized_summary_json"]["sales"]["daily_rows"][0]["ad_sale_num"] = 3

    unit = _build_unit(_source(), snapshot, [_signal()])

    assert unit["operatingMetric"]["metricDate"] == "2026-08-04"
    assert unit["operatingMetric"]["adOrder"] == 3


def test_ad_order_is_none_when_ad_sale_num_missing():
    """ad_sale_num 缺失时 adOrder 为 None，不跨工具取。"""
    snapshot = _snapshot()

    unit = _build_unit(_source(), snapshot, [_signal()])

    assert unit["operatingMetric"]["adOrder"] is None


def test_fact_completeness_is_converted_to_control_center_percentage():
    fact = _fact_snapshot(_snapshot(), "SALES", {"revenue": 10})

    assert fact["completenessScore"] == 80


def test_fact_snapshot_window_ends_on_last_complete_day():
    fact = _fact_snapshot(_snapshot(), "SALES", {"revenue": 10})

    assert fact["windowStart"] == "2026-07-06T00:00:00+00:00"
    assert fact["windowEnd"] == "2026-08-04T23:59:59.999999+00:00"


def test_latest_sources_validates_all_keys_then_returns_successful_results():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE t_patrol_batch (batch_id TEXT PRIMARY KEY, scope_json TEXT)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE t_patrol_job (
                job_id TEXT PRIMARY KEY,batch_id TEXT,shop_id INTEGER,site_code TEXT,
                parent_asin TEXT,parent_seller_sku TEXT,payload_json TEXT,result_ref TEXT,
                status TEXT,created_at TEXT
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO t_patrol_batch VALUES ('batch-1','{}')"
        )
        connection.exec_driver_sql(
            """
            INSERT INTO t_patrol_job VALUES
            ('job-1','batch-1',1,'AMAZON_US','B0ONE','SKU-1','{}','run-1',
             'SUCCEEDED','2026-08-06 01:00:00'),
            ('job-2','batch-1',2,'AMAZON_US','B0TWO','SKU-2','{}',NULL,
             'DEAD','2026-08-06 01:00:00')
            """
        )

        rows = _latest_sources(
            connection,
            source_batch_ids=["batch-1"],
            expected_count=2,
            probe=False,
        )

    assert [(row["parent_asin"], row["result_ref"]) for row in rows] == [
        ("B0ONE", "run-1")
    ]
