"""事实层单元测试。

覆盖方案 §26.1 的：MCP 返回封套解析、空数据与 warn、事实归一化、快照哈希稳定性。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from clients.azlisting_contract import (
    AZLISTING_EXTERNAL_CONTRACT_STATUS,
    AZLISTING_FACT_CONTRACTS,
    AZLISTING_INTERNAL_CONTRACT_VERSION,
)
from clients.mcp_client import (
    AzListingMcpClient,
    FakeMcpClient,
    McpToolResult,
    parse_tool_result,
    request_hash,
)
from core.enums import DataQualityStatus
from core.errors import (
    McpContractInvalid,
    McpError,
    McpPermissionDenied,
    McpRateLimited,
    McpToolNotAllowed,
)
from facts.collector import (
    CORE_KEYS,
    KEYWORD_RANK_LOOKBACK_DAYS,
    TOOL_NAMES,
    FactCollector,
)
from facts.normalizer import FactNormalizer
from facts.quality import FactQualityService
from facts.snapshot import FactSnapshotBuilder
from facts.supplemental import StreamableSupplementalAdapter
from tests.conftest import TODAY, collect_facts_with_children, healthy_mcp_payload


# ---------------------------------------------------------------------------
# MCP 封套解析
# ---------------------------------------------------------------------------
def test_parses_structured_content():
    result = parse_tool_result("t", {"structuredContent": {"data": [{"a": 1}]}})
    assert result.data == [{"a": 1}]


def test_parses_snake_case_structured_content():
    result = parse_tool_result("t", {"structured_content": {"data": [{"a": 1}]}})
    assert result.data == [{"a": 1}]


def test_parses_text_content_array():
    result = parse_tool_result("t", {"content": [{"type": "text", "text": '[{"a": 1}]'}]})
    assert result.data == [{"a": 1}]


def test_parses_text_content_wrapped_data():
    result = parse_tool_result("t", {"content": [{"type": "text", "text": '{"data": [{"a": 1}]}'}]})
    assert result.data == [{"a": 1}]


def test_parses_nested_text_content_wrapped_single_data_object():
    result = parse_tool_result(
        "pangolinfo_api_sync_Extract",
        {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "content": [{
                        "type": "text",
                        "text": json.dumps({"success": True, "data": {"results": []}}),
                    }],
                }),
            }],
        },
    )

    assert result.data == [{"results": []}]


def test_parses_single_object_into_list():
    result = parse_tool_result("t", {"content": [{"type": "text", "text": '{"a": 1}'}]})
    assert result.data == [{"a": 1}]


def test_empty_data_is_legal_not_an_error():
    """空数组是合法结果（例如没有库存告警），不是异常。"""
    result = parse_tool_result("t", {"structuredContent": {"data": []}})
    assert result.data == []
    assert result.is_empty is True


def test_mcp_result_recursively_removes_credentials_and_masks_secret_text():
    result = McpToolResult(
        tool_name="shop_query",
        data=[
            {
                "account": "shop_us",
                "password": "plain-text",
                "nested": {
                    "thirdAzSpApiAccessToken": "access-token",
                    "safe": "Bearer should-not-survive",
                },
            }
        ],
        raw={
            "structuredContent": {
                "data": [{"appSecret": "secret", "siteCode": "US"}],
            },
            "content": [
                {
                    "type": "text",
                    "text": '{"thirdAzSpApiAccessToken":"secret","siteCode":"US"}',
                }
            ],
        },
        warning="api_key=should-not-survive token=sk-starsrock-secretvalue1234567890",
    )

    assert result.data == [
        {
            "account": "shop_us",
            "nested": {"safe": "Bearer [REDACTED]"},
        }
    ]
    assert result.raw == {
        "structuredContent": {"data": [{"siteCode": "US"}]},
        "content": [{"type": "text", "text": '{"siteCode":"US"}'}],
    }
    assert result.warning == "api_key=[REDACTED] token=[REDACTED]"


def test_mcp_redaction_preserves_normal_seller_sku_values():
    result = McpToolResult(
        tool_name="listing_query",
        data=[{"sellerSku": "SK-HW02907-05-US", "parentAsin": "B0TEST1234"}],
        raw={},
    )

    assert result.data == [
        {
            "sellerSku": "SK-HW02907-05-US",
            "parentAsin": "B0TEST1234",
        }
    ]


def test_mcp_redaction_preserves_safe_json_string_formatting():
    params_json = '{"shopAccount": "shop_us", "parentSellerSku": "PSKU-001"}'
    result = McpToolResult(
        tool_name="product_info",
        data=[{"paramsJson": params_json}],
        raw={},
    )

    assert result.data == [{"paramsJson": params_json}]


def test_is_error_raises():
    with pytest.raises(McpError):
        parse_tool_result("t", {"isError": True})


def test_snake_case_is_error_raises_contract_error():
    with pytest.raises(McpContractInvalid):
        parse_tool_result(
            "t",
            {
                "is_error": True,
                "content": [{"type": "text", "text": "Missing required param: paramsJson"}],
            },
        )


def test_argument_error_is_non_retryable_contract_error():
    with pytest.raises(McpContractInvalid):
        parse_tool_result(
            "t",
            {"isError": True, "content": [{"type": "text", "text": "INVALID_ARGUMENT"}]},
        )


def test_rate_limit_error_is_retryable():
    with pytest.raises(McpRateLimited):
        parse_tool_result(
            "t",
            {"isError": True, "content": [{"type": "text", "text": "429 rate_limit"}]},
        )


def test_missing_data_array_raises():
    with pytest.raises(McpError, match="no data array"):
        parse_tool_result("t", {"content": []})


def test_structured_count_mismatch_is_contract_error():
    with pytest.raises(McpContractInvalid, match="count does not match"):
        parse_tool_result(
            "t",
            {"structuredContent": {"data": [{"a": 1}], "count": 2}},
        )


def test_parses_nested_gateway_envelope():
    result = parse_tool_result(
        "t",
        {
            "structuredContent": {
                "data": [{"structuredContent": {"data": [{"records": [{"asin": "B0TEST"}]}]}}]
            }
        },
    )
    assert result.data == [{"records": [{"asin": "B0TEST"}]}]


def test_nested_gateway_permission_error_is_non_retryable():
    with pytest.raises(McpPermissionDenied) as caught:
        parse_tool_result(
            "t",
            {
                "structuredContent": {
                    "data": [
                        {
                            "isError": True,
                            "content": [{"type": "text", "text": "无权调用工具: t"}],
                        }
                    ]
                }
            },
        )
    assert caught.value.retryable is False


def test_warning_is_captured():
    result = parse_tool_result("t", {"structuredContent": {"data": [], "warning": "部分数据延迟"}})
    assert result.warning == "部分数据延迟"


def test_request_hash_is_order_independent():
    a = request_hash("tool", {"b": 2, "a": 1})
    b = request_hash("tool", {"a": 1, "b": 2})
    assert a == b


def test_content_hash_changes_with_data():
    one = McpToolResult("t", [{"a": 1}], {})
    two = McpToolResult("t", [{"a": 2}], {})
    assert one.content_hash != two.content_hash


@pytest.mark.asyncio
async def test_client_does_not_retry_non_retryable_contract_error():
    class ContractFailureClient(AzListingMcpClient):
        calls = 0

        async def _call_once(self, tool_name, arguments):
            self.calls += 1
            raise McpContractInvalid("invalid arguments", tool_name=tool_name)

    client = ContractFailureClient(
        "https://mcp.example.test",
        "",
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    with pytest.raises(McpContractInvalid):
        await client.call_tool("t", {})
    assert client.calls == 1


@pytest.mark.asyncio
async def test_client_unwraps_non_retryable_mcp_error_from_exception_group():
    class PermissionFailureClient(AzListingMcpClient):
        calls = 0

        async def _call_once(self, tool_name, arguments):
            self.calls += 1
            raise ExceptionGroup(
                "session cleanup",
                [McpPermissionDenied("denied", tool_name=tool_name)],
            )

    client = PermissionFailureClient(
        "https://mcp.example.test",
        "",
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    with pytest.raises(McpPermissionDenied):
        await client.call_tool("t", {})
    assert client.calls == 1


@pytest.mark.asyncio
async def test_client_retries_rate_limit_then_succeeds():
    class RateLimitedClient(AzListingMcpClient):
        calls = 0

        async def _call_once(self, tool_name, arguments):
            self.calls += 1
            if self.calls == 1:
                raise McpRateLimited("limited", tool_name=tool_name)
            return McpToolResult(tool_name, [{"ok": True}], {})

    client = RateLimitedClient(
        "https://mcp.example.test",
        "",
        max_attempts=2,
        retry_backoff_seconds=0,
    )
    result = await client.call_tool("t", {})
    assert result.data == [{"ok": True}]
    assert client.calls == 2


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"concurrency": 0}, "concurrency"),
    ],
)
def test_client_rejects_invalid_runtime_limits(kwargs, message):
    with pytest.raises(ValueError, match=message):
        AzListingMcpClient("https://mcp.example.test", "", **kwargs)


def test_mcp_client_rejects_tool_outside_audited_allowlist_without_transport_call():
    client = AzListingMcpClient(
        "https://mcp.example.test",
        "",
        allowed_tools=frozenset({"safe_tool"}),
    )

    with pytest.raises(McpToolNotAllowed) as caught:
        asyncio.run(client.call_tool("submit_or_modify", {}))

    assert caught.value.retryable is False
    assert caught.value.http_status == 403
    assert client._http_client is None


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def test_complete_day_windows_end_before_inspection_date():
    inspection_date = date(2026, 8, 5)

    assert FactCollector.window_for(inspection_date, 30) == (
        date(2026, 7, 6),
        date(2026, 8, 4),
    )
    assert FactCollector.window_for(
        inspection_date,
        KEYWORD_RANK_LOOKBACK_DAYS,
    ) == (date(2026, 7, 22), date(2026, 8, 4))


def test_complete_day_window_rejects_non_positive_lookback():
    with pytest.raises(ValueError, match="lookback_days must be positive"):
        FactCollector.window_for(date(2026, 8, 5), 0)


def test_collector_uses_complete_day_windows_for_mcp_calls(binding, fake_mcp):
    collect_facts_with_children(
        FactCollector(fake_mcp, disabled_fact_keys=frozenset()),
        binding,
        date(2026, 8, 5),
    )
    calls = dict(fake_mcp.calls)

    for tool_name in (
        TOOL_NAMES["gross_profit"],
        "erp_amazon_account_health_issue",
    ):
        assert calls[tool_name]["startDate"] == "2026-07-06"
        assert calls[tool_name]["endDate"] == "2026-08-04"
    assert calls[TOOL_NAMES["natural_ad_flow"]]["startDate"] == "2026-08-04"
    assert calls[TOOL_NAMES["natural_ad_flow"]]["endDate"] == "2026-08-04"

    keyword_arguments = calls["erp_listing_asin_keyword_rank_history"]
    assert keyword_arguments["startDate"] == "2026-07-22"
    assert keyword_arguments["endDate"] == "2026-08-04"


def test_product_info_replaces_az_extend_detail_call(binding):
    primary = FakeMcpClient(responses=healthy_mcp_payload())
    collector = FactCollector(primary)

    calls = collector.build_calls(binding, date(2026, 8, 5))

    assert "supplemental_az_extend_detail" not in calls
    assert {tool_name for tool_name, _ in calls.values()}.isdisjoint({"az_extend_detail"})


def test_product_info_uses_deployed_mcp_schema_with_documented_payload(binding):
    calls = FactCollector(FakeMcpClient(responses={})).build_calls(
        binding, date(2026, 8, 5)
    )

    tool_name, arguments = calls["product_info"]
    assert tool_name == "erp_listing_product_info"
    assert json.loads(arguments["paramsJson"]) == {
        "shopAccount": binding.shop_account,
        "parentAsin": binding.parent_asin,
        "parentSellerSku": binding.parent_seller_sku,
        "qryFiveBulletPoint": True,
    }


def test_build_calls_request_shape_matches_contract(binding):
    """合同即单一真相源门禁：build_calls 产出字段必须落在合同声明范围内。

    对每个事实工具，含 zip 与不含 zip 两种站点配置都要满足：
      request_fields ⊆ 实际参数键 ⊆ request_fields ∪ optional_request_fields。
    未来谁改工具参数却漏改合同（或反之），这条测试立即失败。
    """
    for storefront_zip_codes in ({}, {"AMAZON_US": "10001"}):
        calls = FactCollector(
            FakeMcpClient(responses={}),
            storefront_zip_codes=storefront_zip_codes,
        ).build_calls(binding, date(2026, 8, 5))
        for key, contract in AZLISTING_FACT_CONTRACTS.items():
            tool_name, arguments = calls[key]
            assert tool_name == contract.tool_name
            required = set(contract.request_fields)
            allowed = required | set(contract.optional_request_fields)
            produced = set(arguments)
            assert required <= produced, f"{key}: 缺少合同必需字段 {required - produced}"
            assert produced <= allowed, f"{key}: 产出合同未声明字段 {produced - allowed}"


def test_monthly_goal_uses_only_erp_source(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_monthly_goal"][0]["monthlyGoals"] = [
        {"everyMonthStr": "2026年07月", "estimatedMonthOrderNum": 372}
    ]
    primary = FakeMcpClient(responses=payload)
    collector = FactCollector(primary)

    raw = asyncio.run(collector.collect(binding, TODAY))
    normalized, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["sales"]["monthly_target"] == 372
    assert normalized["sales"]["monthly_target_source"] == "erp_listing_monthly_goal"
    assert normalized["sales"]["monthly_goals"] == [
        {"month": "2026-07", "target_orders": 372.0, "source": "erp_listing_monthly_goal"},
    ]
    assert normalized["identity"]["own_seller_id"] == "A1OWNSELLER"
    assert "sales_performance" not in {ref.tool_name for ref in refs}
    assert "az_extend_detail" not in {ref.tool_name for ref in refs}


def test_monthly_goal_stays_missing_when_erp_source_is_empty(binding):
    payload = healthy_mcp_payload(erp_listing_monthly_goal=[])
    primary = FakeMcpClient(responses=payload)
    collector = FactCollector(primary)

    raw = asyncio.run(collector.collect(binding, TODAY))
    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["sales"]["monthly_target"] is None
    assert normalized["sales"]["monthly_target_source"] is None


def test_conflicting_product_info_seller_ids_are_rejected(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"].append({
        **payload["erp_listing_product_info"][0],
        "asin": "B0CHILD02",
        "sellerSku": "SKU-2",
        "ASIN": "B0CHILD02",
        "SELLER_SKU": "SKU-2",
        "SELLING_PARTNER_ID": "A1OTHERSELLER",
    })
    raw = asyncio.run(
        FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY)
    )

    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, normalized)

    assert normalized["identity"]["own_seller_id"] is None
    assert "identity.own_seller_id.conflict" in normalized["_meta"]["unresolved_fields"]
    assert "OWN_SELLER_ID_MISSING" in {gap.code for gap in gaps}

def test_collector_returns_exception_instead_of_swallowing(binding):
    fake = FakeMcpClient(
        responses=healthy_mcp_payload(),
        failures={"erp_listing_stock_alert": McpError("boom")},
    )
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    assert isinstance(raw["stock"], Exception)
    assert isinstance(raw["product_info"], McpToolResult)


def test_collector_calls_every_declared_tool(binding, fake_mcp):
    # 父体由 collect() 采集，子体扩展工具由 build_child_calls 采集；
    # collect_facts_with_children 显式装配两段，等价于生产的父体+异步子体合并。
    collect_facts_with_children(
        FactCollector(fake_mcp, disabled_fact_keys=frozenset()), binding
    )
    called = {name for name, _ in fake_mcp.calls}
    assert called == {
        *TOOL_NAMES.values(),
        "erp_listing_price_promotion_analysis",
        "erp_listing_review_analysis",
        "erp_amazon_account_health_issue",
        "erp_listing_asin_keyword_rank_history",
    }


def test_natural_ad_flow_is_enabled_for_yesterday_by_default(binding, fake_mcp):
    raw = asyncio.run(FactCollector(fake_mcp).collect(binding, TODAY))
    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert any(
        tool_name == TOOL_NAMES["natural_ad_flow"]
        for tool_name, _ in fake_mcp.calls
    )
    assert {
        key: normalized["traffic"][key]
        for key in ("yesterday_date", "yesterday_ad_orders")
    } == {
        "yesterday_date": (TODAY - timedelta(days=1)).isoformat(),
        "yesterday_ad_orders": 4,
    }
    assert "natural_ad_flow" not in normalized["_meta"]["disabled_fact_keys"]


def test_collector_passes_shop_account_not_shop_id(binding, fake_mcp):
    asyncio.run(FactCollector(fake_mcp).collect(binding, TODAY))
    stock_args = next(args for name, args in fake_mcp.calls if name == "erp_listing_stock_alert")
    assert stock_args["shopAccount"] == binding.shop_account


def test_collector_uses_documented_storefront_parameters(binding, fake_mcp):
    asyncio.run(FactCollector(fake_mcp).collect(binding, TODAY))
    args = next(args for name, args in fake_mcp.calls if name == "erp_asin_full_detail")
    assert args == {
        "asin": binding.parent_asin,
        "siteCode": "US",
        "includeReviews": "false",
    }


def test_collector_adds_configured_storefront_zip_code(binding, fake_mcp):
    collector = FactCollector(
        fake_mcp,
        storefront_zip_codes={"AMAZON_US": "10001"},
    )
    asyncio.run(collector.collect(binding, TODAY))
    args = next(args for name, args in fake_mcp.calls if name == "erp_asin_full_detail")
    assert args["zipCode"] == "10001"


def test_normalizes_documented_storefront_detail(binding):
    payload = healthy_mcp_payload()
    payload["erp_asin_full_detail"] = [
        {
            "asin": binding.parent_asin,
            "siteCode": "US",
            "linkStatus": {
                "inStock": True,
                "price": "$19.90",
                "hasCart": True,
                "buyBoxOwner": "Example Seller",
                "coupon": "10%",
                "strikethroughPrice": "$24.90",
            },
            "images": {"main": "https://example.test/main.jpg", "all": ["a", "b"]},
            "aplus": {"hasAplus": True, "descriptionBlocks": [{"type": "image"}]},
            "reviews": {"lowStarRecentCount": 2},
            "bonus": {"title": "Documented Product", "star": 4.4, "ratingsNum": 123},
        }
    ]
    raw = asyncio.run(
        FactCollector(
            FakeMcpClient(responses=payload), disabled_fact_keys=frozenset()
        ).collect(binding, TODAY)
    )
    norm, _ = FactNormalizer(disabled_fact_keys=frozenset()).normalize(
        binding, raw, as_of=TODAY
    )
    storefront = norm["identity"]["storefront"]
    assert storefront["main_image_url"] is None
    assert storefront["main_image_source"] is None
    assert storefront["gallery_count"] == 2
    assert storefront["aplus_present"] is True
    assert norm["price"]["current_price"] == pytest.approx(19.9)
    assert norm["quality"]["star_rating"] == pytest.approx(4.4)
    assert norm["quality"]["review_count"] == 123


def test_listing_basic_info_supplements_missing_rating_and_review_count(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"][0].update({
        "flowInlet": "是",
        "picUrl": "https://m.media-amazon.com/images/I/traffic-entry.jpg",
    })
    payload["erp_asin_full_detail"] = [{
        "asin": binding.parent_asin,
        "siteCode": "US",
        "reviews": [],
    }]
    supplemental = StreamableSupplementalAdapter(
        FakeMcpClient(responses={
            "listing_basic_info": [{"星级": 4.7, "评论数": 456}],
        }),
        enabled=True,
    )

    raw = asyncio.run(FactCollector(
        FakeMcpClient(responses=payload),
        supplemental_adapter=supplemental,
    ).collect(binding, TODAY))
    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["identity"]["storefront"]["main_image_url"].endswith(
        "traffic-entry.jpg"
    )
    assert normalized["quality"]["star_rating"] == pytest.approx(4.7)
    assert normalized["quality"]["review_count"] == 456
    assert normalized["_meta"]["field_lineage"]["quality.star_rating"] == "星级"
    assert normalized["_meta"]["field_lineage"]["quality.review_count"] == "评论数"


def test_storefront_rating_takes_priority_over_listing_basic_info(binding):
    payload = healthy_mcp_payload()
    payload["erp_asin_full_detail"] = [{
        "asin": binding.parent_asin,
        "bonus": {"star": 4.8, "ratingsNum": 999},
    }]
    supplemental = StreamableSupplementalAdapter(
        FakeMcpClient(responses={
            "listing_basic_info": [{"星级": 4.1, "评论数": 100}],
        }),
        enabled=True,
    )

    raw = asyncio.run(FactCollector(
        FakeMcpClient(responses=payload),
        supplemental_adapter=supplemental,
    ).collect(binding, TODAY))
    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["quality"]["star_rating"] == pytest.approx(4.8)
    assert normalized["quality"]["review_count"] == 999
    assert normalized["_meta"]["field_lineage"]["quality.star_rating"] == "star_rating"
    assert normalized["_meta"]["field_lineage"]["quality.review_count"] == "review_count"


def test_listing_basic_info_failure_is_non_blocking_gap(binding):
    payload = healthy_mcp_payload()
    payload["erp_asin_full_detail"] = [{
        "asin": binding.parent_asin,
        "siteCode": "US",
        "reviews": [],
    }]
    supplemental = StreamableSupplementalAdapter(
        FakeMcpClient(
            responses={},
            failures={"listing_basic_info": RuntimeError("basic info unavailable")},
        ),
        enabled=True,
    )
    raw = asyncio.run(FactCollector(
        FakeMcpClient(responses=payload),
        supplemental_adapter=supplemental,
    ).collect(binding, TODAY))
    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, normalized)

    gap = next(item for item in gaps if item.code == "MCP_LISTING_BASIC_INFO_UNAVAILABLE")
    assert gap.blocking is False




def test_core_keys_are_the_blocking_set():
    assert CORE_KEYS == {"product_info", "listing_identity", "stock", "gross_profit"}


def test_normalizes_documented_advert_config_and_refund_rate(binding):
    payload = healthy_mcp_payload()
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    assert norm["profit"]["target_acos"] == pytest.approx(0.25)
    assert norm["profit"]["target_daily_budget"] == pytest.approx(40.0)
    assert norm["profit"]["operating_tags"] == {
        "product_position": "重点产品",
        "product_stage": "推进期",
        "season_type": "旺季前期",
        "operating_mode": "积极推进",
        "advert_purposes": "扩大销量、提升排名",
        "target_keyword_types": "核心词、长尾词",
        "advert_direction_types": "推进自然位",
        "day_range": "30",
    }
    assert norm["quality"]["refund_rate_16w"] == pytest.approx(0.05)
    assert norm["quality"]["refund_rate_32w"] == pytest.approx(0.05)


def test_normalizes_parent_extension_for_control_center_tags(binding):
    payload = healthy_mcp_payload()
    payload["erp_parent_listing_product_info"] = [{
        "progressPreparationCode": "PROGRESS_PERIOD",
        "seasonalityCode": "BIG_PEAK_SEASON",
    }]
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    assert norm["identity"]["parent_extension"] == {
        "progress_preparation_code": "PROGRESS_PERIOD",
        "progress_preparation": None,
        "seasonality_code": "BIG_PEAK_SEASON",
        "seasonality": None,
    }


@pytest.mark.parametrize("target_acos", [45, "45", "45%", 0.45])
def test_normalizes_target_acos_percent_and_ratio_forms(binding, target_acos):
    payload = healthy_mcp_payload()
    payload["erp_listing_advert_agent_config"][0]["targetAcosSuggest"] = target_acos
    raw = asyncio.run(
        FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY)
    )

    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["profit"]["target_acos"] == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
@pytest.fixture
def normalized(binding, fake_mcp):
    raw = asyncio.run(FactCollector(fake_mcp).collect(binding, TODAY))
    normalized, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    return raw, normalized, refs


def test_normalizer_separates_inspection_date_from_data_cutoff(binding, fake_mcp):
    inspection_date = date(2026, 8, 5)
    raw = asyncio.run(FactCollector(fake_mcp).collect(binding, inspection_date))

    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=inspection_date)

    assert normalized["_meta"]["inspection_date"] == "2026-08-05"
    assert normalized["_meta"]["as_of"] == "2026-08-04"
    assert normalized["_meta"]["window_start"] == "2026-07-06"
    assert normalized["_meta"]["window_end"] == "2026-08-04"


def test_normalizes_children(normalized):
    _, norm, _ = normalized
    children = norm["identity"]["children"]
    assert len(children) == 1
    assert children[0]["child_asin"] == "B0CHILD01"
    assert children[0]["bullet_count"] == 5
    assert children[0]["importance"] == "主要色"
    assert children[0]["sellable"] is True
    assert children[0]["principal_user_id"] == 35
    assert children[0]["principal_user_name"] == "Operator A"


def test_product_info_supplies_main_image_and_all_owners(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"][0]["picUrl"] = (
        "https://m.media-amazon.com/images/I/product-main.jpg"
    )
    payload["erp_listing_product_info"][0]["flowInlet"] = "是"
    payload["erp_listing_product_info"].append({
        **payload["erp_listing_product_info"][0],
        "asin": "B0CHILD02",
        "sellerSku": "SKU-2",
        "flowInlet": "否",
        "picUrl": "https://m.media-amazon.com/images/I/product-secondary.jpg",
        "principalUserId": "42",
        "principalUserName": "Operator B",
    })
    payload["erp_asin_full_detail"] = [{
        "asin": binding.parent_asin,
        "images": {"main": "https://example.test/stale.jpg"},
    }]

    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))
    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    identity = normalized["identity"]
    assert identity["owner_user_ids"] == [35, 42]
    assert identity["owner_users"] == [
        {"user_id": 35, "user_name": "Operator A"},
        {"user_id": 42, "user_name": "Operator B"},
    ]
    assert identity["storefront"]["main_image_url"].endswith("product-main.jpg")
    assert identity["storefront"]["main_image_source"] == (
        "erp_listing_product_info.picUrl"
    )


def test_catalog_owner_fallback_is_used_when_product_info_has_no_owner(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"][0]["principalUserId"] = None
    payload["erp_listing_product_info"][0]["principalUserName"] = None
    catalog_binding = binding.with_owner_user_ids((35,))

    raw = asyncio.run(
        FactCollector(FakeMcpClient(responses=payload)).collect(catalog_binding, TODAY)
    )
    normalized, _ = FactNormalizer().normalize(catalog_binding, raw, as_of=TODAY)

    assert normalized["identity"]["owner_user_ids"] == [35]
    assert normalized["identity"]["owner_source"] == "operating_unit_catalog_fallback"


def test_product_info_supplements_identity_price_category_and_goals(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"] = [
        {
            "asin": "B0CHILD01",
            "sellerSku": "SKU-1",
            "status": "可售",
            "productName": "ERP Product Name",
            "parentAsin": binding.parent_asin,
            "productPrice": "$19.90",
            "firstCategory": '[{"classificationId":"10","title":"Home","rank":88}]',
            "lastCategory": '[{"classificationId":"20","title":"Hooks","rank":12}]',
            "targetStarRate": "4.6",
            "shortTermGoal": "Improve conversion",
            "achievesScale": "75%",
            "genericKeyword": "wall hooks",
            "principalUserId": "35",
            "principalUserName": "Operator A",
        }
    ]
    payload["erp_asin_full_detail"] = [
        {"asin": binding.parent_asin, "linkStatus": {}}
    ]
    raw = asyncio.run(
        FactCollector(
            FakeMcpClient(responses=payload), disabled_fact_keys=frozenset()
        ).collect(binding, TODAY)
    )

    norm, _ = FactNormalizer(disabled_fact_keys=frozenset()).normalize(
        binding, raw, as_of=TODAY
    )
    child = norm["identity"]["children"][0]

    assert norm["identity"]["product_name"] == "ERP Product Name"
    assert child["price"] == pytest.approx(19.9)
    assert child["first_category_id"] == "10"
    assert child["category_name"] == "Hooks"
    assert child["category_rank"] == 12
    assert child["target_star_rating"] == pytest.approx(4.6)
    assert child["short_term_goal"] == "Improve conversion"
    assert child["goal_achievement_rate"] == pytest.approx(0.75)
    assert child["generic_keyword"] == "wall hooks"
    assert norm["price"]["current_price"] == pytest.approx(19.9)
    assert norm["price"]["children_prices"][0]["child_asin"] == "B0CHILD01"
    assert norm["price"]["children_prices"][0]["price"] == pytest.approx(19.9)
    assert norm["quality"]["category_rank_major"] is None
    assert norm["quality"]["category_rank_minor"] is None


def test_different_child_prices_do_not_invent_parent_current_price(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"] = [
        {
            "asin": "B0CHILD01",
            "parentAsin": binding.parent_asin,
            "sellerSku": "SKU-1",
            "status": "可售",
            "productPrice": 19.9,
            "principalUserId": "35",
            "principalUserName": "Operator A",
        },
        {
            "asin": "B0CHILD02",
            "parentAsin": binding.parent_asin,
            "sellerSku": "SKU-2",
            "status": "可售",
            "productPrice": 24.9,
            "principalUserId": "35",
            "principalUserName": "Operator A",
        },
    ]
    payload["erp_asin_full_detail"] = [
        {"asin": binding.parent_asin, "linkStatus": {}}
    ]
    raw = asyncio.run(
        FactCollector(
            FakeMcpClient(responses=payload), disabled_fact_keys=frozenset()
        ).collect(binding, TODAY)
    )

    norm, _ = FactNormalizer(disabled_fact_keys=frozenset()).normalize(
        binding, raw, as_of=TODAY
    )

    assert norm["price"]["current_price"] is None
    assert [row["price"] for row in norm["price"]["children_prices"]] == [19.9, 24.9]


def test_normalizes_sales_series_newest_first(normalized):
    _, norm, _ = normalized
    rows = norm["sales"]["daily_rows"]
    assert rows[0]["stat_date"] > rows[-1]["stat_date"]
    assert norm["sales"]["avg_daily_units_7d"] == 12.0
    assert norm["sales"]["freshness"] == "FRESH"


def test_normalizes_documented_gross_profit_fields(binding):
    payload = healthy_mcp_payload()
    metric_date = TODAY - timedelta(days=1)
    payload["erp_listing_gross_profit_history"] = [
        {
            "recordDate": metric_date.isoformat(),
            "productSaleNum": 10,
            "orderNum": 8,
            "orderSaleAmount": 199,
            "adSaleNum": 3,
            "adSaleMoney": 100,
            "adCostAmount": 25,
            "adClick": 12,
            "adImpressions": 3456,
            "grossProfitAmountProportion": 0.2,
        }
    ]
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    row = norm["sales"]["daily_rows"][0]
    assert row == {
        "stat_date": metric_date.isoformat(),
        "orders": 8,
        "units": 10,
        "revenue": 199.0,
        "ad_cost": 25.0,
        "ad_sales": 100.0,
        "ad_orders": None,
        "ad_click": 12,
        "ad_impressions": 3456.0,
        "acos": 0.25,
        "gross_margin": 0.2,
        "ad_zero_sale": False,
        "gross_profit_amount": None,
        "ad_sale_num": 3,
    }


def test_normalizes_nested_gross_profit_detail_without_using_summary_as_daily(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_gross_profit_history"] = [
        {
            "summary": {"totalProductSaleNum": 21, "totalOrderSaleAmount": 418},
            "detail": [
                {
                    "recordDate": "2026-07-27",
                    "productSaleNum": 10,
                    "orderNum": 8,
                    "orderSaleAmount": 199,
                },
                {
                    "recordDate": "2026-07-28",
                    "productSaleNum": 11,
                    "orderNum": 9,
                    "orderSaleAmount": 219,
                },
            ],
        }
    ]
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    assert norm["sales"]["row_count"] == 2
    assert norm["sales"]["avg_daily_units_30d"] == pytest.approx(10.5)
    assert norm["sales"]["latest_stat_date"] == "2026-07-28"


def test_rejects_sales_rows_after_last_complete_day(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_gross_profit_history"] = [
        {"recordDate": "2026-07-28", "productSaleNum": 8, "orderNum": 7},
        {"recordDate": "2026-07-29", "productSaleNum": 99, "orderNum": 99},
        {"recordDate": "2026-07-30", "productSaleNum": 999, "orderNum": 999},
    ]
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))

    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert norm["sales"]["row_count"] == 1
    assert norm["sales"]["latest_stat_date"] == "2026-07-28"
    assert norm["sales"]["avg_daily_units_3d"] == 8
    assert "sales.rows_after_data_cutoff" in norm["_meta"]["unresolved_fields"]


def test_natural_flow_aggregate_remains_separate_from_derived_daily_series(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_natural_advert_flow"] = [
        {
            "isSummary": True,
            "totalOrderNum": 100,
            "naturalOrderNum": 70,
            "adOrderNum": 30,
        }
    ]
    raw = asyncio.run(
        FactCollector(
            FakeMcpClient(responses=payload), disabled_fact_keys=frozenset()
        ).collect(binding, TODAY)
    )
    norm, _ = FactNormalizer(disabled_fact_keys=frozenset()).normalize(
        binding, raw, as_of=TODAY
    )
    traffic = norm["traffic"]
    assert traffic["window_natural_ratio"] == pytest.approx(0.7)
    assert traffic["daily_series_source"].endswith("total_orders-ad_orders")


def test_derives_daily_natural_orders_from_compatible_daily_order_components(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_natural_advert_flow"] = [
        {
            "isSummary": True,
            "totalOrderNum": 100,
            "naturalOrderNum": 70,
            "adOrderNum": 30,
        }
    ]
    raw = asyncio.run(
        FactCollector(
            FakeMcpClient(responses=payload), disabled_fact_keys=frozenset()
        ).collect(binding, TODAY)
    )

    norm, _ = FactNormalizer(disabled_fact_keys=frozenset()).normalize(
        binding, raw, as_of=TODAY
    )
    traffic = norm["traffic"]

    assert len(traffic["daily_series"]) == 30
    assert traffic["daily_series"][0]["natural_orders"] == 8
    assert traffic["natural_orders_7d"] == 56
    assert traffic["natural_ratio_7d"] == pytest.approx(2 / 3)
    assert traffic["daily_series_source"].endswith("total_orders-ad_orders")
    assert traffic["daily_series_derivation_error"] is None


def test_rejects_daily_natural_series_when_ad_orders_exceed_total(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_gross_profit_history"][0]["广告单量"] = 13
    raw = asyncio.run(
        FactCollector(
            FakeMcpClient(responses=payload), disabled_fact_keys=frozenset()
        ).collect(binding, TODAY)
    )

    norm, _ = FactNormalizer(disabled_fact_keys=frozenset()).normalize(
        binding, raw, as_of=TODAY
    )
    traffic = norm["traffic"]

    assert traffic["daily_series"] == []
    assert traffic["natural_ratio_7d"] is None
    assert traffic["daily_series_derivation_error"] == "ORDER_COMPONENT_INCONSISTENT"


def test_ad_sales_quantity_is_not_treated_as_ad_order_count(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_gross_profit_history"] = [
        {
            "recordDate": (TODAY - timedelta(days=1)).isoformat(),
            "productSaleNum": 10,
            "orderNum": 8,
            "adSaleNum": 3,
        }
    ]
    raw = asyncio.run(
        FactCollector(
            FakeMcpClient(responses=payload), disabled_fact_keys=frozenset()
        ).collect(binding, TODAY)
    )

    norm, _ = FactNormalizer(disabled_fact_keys=frozenset()).normalize(
        binding, raw, as_of=TODAY
    )

    assert norm["sales"]["daily_rows"][0]["ad_orders"] is None


def test_quality_uses_business_report_conversion_when_storefront_omits_it(binding):
    payload = healthy_mcp_payload()
    payload["erp_asin_full_detail"] = [
        {"asin": binding.parent_asin, "linkStatus": {}}
    ]
    payload["erp_listing_business_report"] = [
        {"records": [{"reportStartTime": "2026-07-21", "conversionRate": 0.123}]}
    ]
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))

    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert norm["quality"]["conversion_rate"] == pytest.approx(0.123)
    assert norm["_meta"]["field_lineage"]["quality.conversion_rate"] == "conversionRate"


def test_normalizes_documented_business_report_nested_records(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_business_report"] = [
        {
            "summary": {},
            "records": [
                {
                    "reportStartTime": "2026-07-21",
                    "conversation": 900,
                    "productOrderQty": 95,
                    "conversionRate": 0.105,
                },
                {
                    "reportStartTime": "2026-07-14",
                    "conversation": 880,
                    "productOrderQty": 92,
                    "conversionRate": 0.104,
                },
            ],
        }
    ]
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    assert norm["traffic"]["sessions_this_week"] == 900
    assert norm["traffic"]["sessions_last_week"] == 880
    assert norm["traffic"]["conversion_this_week"] == pytest.approx(0.105)


def test_normalizes_documented_unit_profit_and_inventory_cost(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_unit_profit_analysis"] = [
        {
            "productPrice": 20,
            "referralFee": 3,
            "productCost": 5,
            "fbaPackFee": 4,
            "adCostRatio": "10%",
        }
    ]
    payload["erp_listing_inventory_cost_analysis"] = [
        {
            "parentAsin": binding.parent_asin,
            "reportMonth": "2026-07",
            "children": [
                {
                    "asin": "B0CHILD01",
                    "sellerSku": "SKU-1",
                    "longTermStorageFees": [
                        {"qtyCharged": 2, "amountCharged": 3.5},
                        {"qtyCharged": 1, "amountCharged": 1.5},
                    ],
                }
            ],
            "summary": {"totalLongTermStorageFee": 5},
        }
    ]
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    assert norm["profit"]["unit_contribution"] == pytest.approx(6.0)
    assert norm["profit"]["product_cost"] == pytest.approx(5.0)
    assert norm["profit"]["az_commission_fee"] == pytest.approx(3.0)
    assert norm["profit"]["fba_pack_fee"] == pytest.approx(4.0)
    assert norm["profit"]["ad_cost_ratio"] == "10%"
    assert norm["inventory"]["aged_quantity"] == 3
    assert norm["inventory"]["aged_storage_fee_monthly"] == 5
    assert norm["inventory"]["monthly_storage_fee"] == pytest.approx(5.0)


def test_does_not_invent_unit_profit_when_a_cost_component_is_missing(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_unit_profit_analysis"] = [
        {
            "productPrice": 20,
            "referralFee": 3,
            "productCost": 5,
            "adCostRatio": "10%",
        }
    ]
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))

    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert norm["profit"]["unit_contribution"] is None
    assert "profit.unit_contribution" in norm["_meta"]["unresolved_fields"]


def test_computes_inventory_days_of_supply(normalized):
    _, norm, _ = normalized
    # 900 可售 / 12 日均 = 75 天
    assert norm["inventory"]["inventory_days_of_supply"] == 75.0
    assert norm["inventory"]["days_of_supply_basis"] == "avg_daily_units_30d"


def test_days_of_supply_is_none_when_velocity_missing(binding):
    payload = healthy_mcp_payload(erp_listing_gross_profit_history=[])
    fake = FakeMcpClient(responses=payload)
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    # 不写成 999 天，也不写成 0 —— 算不出就是 None
    assert norm["inventory"]["inventory_days_of_supply"] is None


def test_records_field_lineage(normalized):
    _, norm, _ = normalized
    lineage = norm["_meta"]["field_lineage"]
    assert lineage["quality.star_rating"] == "star_rating"
    assert lineage["profit.target_acos"] == "targetAcosSuggest"


def test_records_mcp_contract_version_and_external_freeze_status(normalized):
    _, norm, _ = normalized

    assert norm["_meta"]["mcp_input_contract_version"] == AZLISTING_INTERNAL_CONTRACT_VERSION
    assert norm["_meta"]["mcp_external_contract_status"] == (
        AZLISTING_EXTERNAL_CONTRACT_STATUS.value
    )


def test_records_unresolved_fields(binding):
    payload = healthy_mcp_payload(erp_listing_unit_profit_analysis=[{"irrelevant": 1}])
    fake = FakeMcpClient(responses=payload)
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    assert "profit.unit_contribution" in norm["_meta"]["unresolved_fields"]
    assert norm["profit"]["unit_contribution"] is None


def test_source_refs_cover_every_tool(normalized):
    raw, _, refs = normalized
    assert len(refs) == len(raw)
    assert all(ref.content_hash for ref in refs)


def test_gross_profit_source_ref_uses_actual_data_cutoff(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_gross_profit_history"] = [
        {
            "recordDate": "2026-07-27",
            "productSaleNum": 10,
            "orderNum": 8,
            "orderSaleAmount": 199,
        },
        {
            "recordDate": "2026-07-28",
            "productSaleNum": 11,
            "orderNum": 9,
            "orderSaleAmount": 219,
        },
    ]
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))
    _, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    ref = next(ref for ref in refs if ref.tool_name == "erp_listing_gross_profit_history")
    assert ref.window_start == date(2026, 7, 27)
    assert ref.window_end == date(2026, 7, 28)


def test_failed_tool_source_ref_is_marked_failed(binding):
    fake = FakeMcpClient(
        responses=healthy_mcp_payload(),
        failures={"erp_listing_stock_alert": McpError("boom")},
    )
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    _, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    stock_ref = next(r for r in refs if r.tool_name == "erp_listing_stock_alert")
    assert stock_ref.quality_status == DataQualityStatus.FAILED


def test_natural_ad_flow_currency_is_flagged_when_enabled(binding):
    raw = asyncio.run(
        FactCollector(
            FakeMcpClient(responses=healthy_mcp_payload()),
            disabled_fact_keys=frozenset(),
        ).collect(binding, TODAY)
    )
    norm, _ = FactNormalizer(disabled_fact_keys=frozenset()).normalize(
        binding, raw, as_of=TODAY
    )
    notes = norm["_meta"]["currency_notes"]
    assert "CNY" in notes["natural_ad_flow"]
    assert "不参与利润判定" in notes["natural_ad_flow"]


# ---------------------------------------------------------------------------
# 质量检查
# ---------------------------------------------------------------------------
def test_core_tool_failure_is_blocking(binding):
    fake = FakeMcpClient(
        responses=healthy_mcp_payload(),
        failures={"erp_listing_stock_alert": McpError("boom")},
    )
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, norm)
    stock_gap = next(g for g in gaps if g.code == "MCP_STOCK_UNAVAILABLE")
    assert stock_gap.blocking is True


def test_optional_tool_failure_is_not_blocking(binding):
    fake = FakeMcpClient(
        responses=healthy_mcp_payload(),
        failures={"erp_listing_business_report": McpError("boom")},
    )
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, norm)
    gap = next(g for g in gaps if g.code == "MCP_BUSINESS_REPORT_UNAVAILABLE")
    assert gap.blocking is False


def test_dynamic_keyword_failure_is_reported_per_child_asin(binding):
    fake = FakeMcpClient(
        responses=healthy_mcp_payload(),
        failures={"erp_listing_asin_keyword_rank_history": McpError("boom")},
    )
    raw = collect_facts_with_children(FactCollector(fake), binding)
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    gaps = FactQualityService().check(binding, raw, norm)

    gap = next(g for g in gaps if g.code == "MCP_KEYWORD_RANK_UNAVAILABLE")
    assert gap.field == "traffic.keyword_rank_series[B0CHILD01]"
    assert gap.blocking is False
    assert gap.source_tool == "erp_listing_asin_keyword_rank_history"


def test_empty_reviews_and_account_health_are_not_treated_as_tool_failures(binding):
    payload = healthy_mcp_payload(
        erp_listing_review_analysis=[],
        erp_amazon_account_health_issue=[],
    )
    fake = FakeMcpClient(responses=payload)
    raw = collect_facts_with_children(FactCollector(fake), binding)
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    codes = {gap.code for gap in FactQualityService().check(binding, raw, norm)}

    assert "MCP_RECENT_REVIEWS_UNAVAILABLE" not in codes
    assert "MCP_ACCOUNT_HEALTH_UNAVAILABLE" not in codes


def test_daily_natural_series_removes_traffic_series_gap(binding):
    raw = asyncio.run(
        FactCollector(FakeMcpClient(responses=healthy_mcp_payload())).collect(binding, TODAY)
    )
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    gaps = FactQualityService().check(binding, raw, norm)

    assert "TRAFFIC_DAILY_SERIES_UNAVAILABLE" not in {gap.code for gap in gaps}


def test_invalid_daily_order_components_keep_traffic_series_gap(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_gross_profit_history"][0]["广告单量"] = 13
    raw = asyncio.run(
        FactCollector(
            FakeMcpClient(responses=payload), disabled_fact_keys=frozenset()
        ).collect(binding, TODAY)
    )
    norm, _ = FactNormalizer(disabled_fact_keys=frozenset()).normalize(
        binding, raw, as_of=TODAY
    )

    gaps = FactQualityService().check(binding, raw, norm)

    gap = next(g for g in gaps if g.code == "TRAFFIC_DAILY_SERIES_UNAVAILABLE")
    assert gap.blocking is False


def test_missing_children_is_identity_required(binding):
    payload = healthy_mcp_payload(erp_listing_product_info=[])
    fake = FakeMcpClient(responses=payload)
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    norm, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, norm)
    identity_gap = next(g for g in gaps if g.code == "IDENTITY_REQUIRED")
    assert identity_gap.blocking is True


def test_explicit_no_child_placeholder_becomes_identity_gap(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"] = [{
        "asin": None,
        "sellerSku": None,
        "parentAsin": binding.parent_asin,
        "parentSellerSku": binding.parent_seller_sku,
        "shopAccount": binding.shop_account,
        "plSiteCode": "Amazon_US",
        "childWarn": "未找到该父 ASIN 下的子体数据",
        "sellingPartnerId": "A1OWNSELLER",
    }]
    raw = asyncio.run(
        FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY)
    )

    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, normalized)

    identity_gap = next(gap for gap in gaps if gap.code == "IDENTITY_REQUIRED")
    assert identity_gap.blocking is True


def test_unexplained_missing_child_asin_remains_contract_invalid(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"][0]["asin"] = None
    payload["erp_listing_product_info"][0]["ASIN"] = None
    raw = asyncio.run(
        FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY)
    )

    with pytest.raises(McpContractInvalid, match="PRODUCT_CHILD_ASIN_MISSING"):
        FactNormalizer().normalize(binding, raw, as_of=TODAY)


def test_cross_parent_child_is_rejected_before_normalization(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_product_info"][0]["parentAsin"] = "B0OTHERPARENT"
    fake = FakeMcpClient(responses=payload)
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    with pytest.raises(McpContractInvalid, match="PRODUCT_PARENT_IDENTITY_MISMATCH"):
        FactNormalizer().normalize(binding, raw, as_of=TODAY)


@pytest.mark.parametrize("conflicting_sku", [False, True])
def test_duplicate_child_asin_is_rejected_before_normalization(binding, conflicting_sku):
    payload = healthy_mcp_payload()
    duplicate = dict(payload["erp_listing_product_info"][0])
    if conflicting_sku:
        duplicate["sellerSku"] = "SKU-CONFLICT"
        duplicate["SELLER_SKU"] = "SKU-CONFLICT"
    payload["erp_listing_product_info"].append(duplicate)
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))

    with pytest.raises(McpContractInvalid, match="PRODUCT_CHILD_ASIN_DUPLICATE"):
        FactNormalizer().normalize(binding, raw, as_of=TODAY)


def test_amazon_found_child_mirror_is_ignored_when_real_child_exists(binding):
    payload = healthy_mcp_payload()
    mirror = dict(payload["erp_listing_product_info"][0])
    mirror["asin"] = "Amazon.Found." + mirror["asin"]
    mirror["sellerSku"] = "Amazon.Found." + mirror["sellerSku"]
    payload["erp_listing_product_info"].append(mirror)
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))

    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["identity"]["child_count"] == 1


def test_amazon_found_sku_mirror_is_ignored_when_real_child_exists(binding):
    payload = healthy_mcp_payload()
    mirror = dict(payload["erp_listing_product_info"][0])
    mirror["sellerSku"] = "Amazon.Found." + mirror["asin"]
    payload["erp_listing_product_info"].append(mirror)
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))

    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["identity"]["child_count"] == 1


def test_unavailable_old_child_sku_is_ignored_when_sellable_replacement_exists(binding):
    payload = healthy_mcp_payload()
    replacement = dict(payload["erp_listing_product_info"][0])
    replacement["sellerSku"] = "SKU-REPLACEMENT"
    replacement["status"] = "可售"
    payload["erp_listing_product_info"][0]["status"] = "不可售"
    payload["erp_listing_product_info"].append(replacement)
    raw = asyncio.run(FactCollector(FakeMcpClient(responses=payload)).collect(binding, TODAY))

    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["identity"]["children"][0]["seller_sku"] == "SKU-REPLACEMENT"


def test_known_chinese_site_name_matches_canonical_site(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_advert_agent_config"][0]["siteCode"] = "美国"
    fake = FakeMcpClient(responses=payload)
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))

    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["profit"]["target_acos"] == pytest.approx(0.25)


def test_escaped_chinese_site_name_matches_canonical_site(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_advert_agent_config"][0]["siteCode"] = r"\u7f8e\u56fd"
    fake = FakeMcpClient(responses=payload)
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))

    normalized, _ = FactNormalizer().normalize(binding, raw, as_of=TODAY)

    assert normalized["profit"]["target_acos"] == pytest.approx(0.25)


def test_unknown_site_name_is_still_rejected(binding):
    payload = healthy_mcp_payload()
    payload["erp_listing_advert_agent_config"][0]["siteCode"] = "未知站点"
    fake = FakeMcpClient(responses=payload)
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))

    with pytest.raises(McpContractInvalid, match="FACT_SITE_CODE_MISMATCH"):
        FactNormalizer().normalize(binding, raw, as_of=TODAY)


def test_summarize_status_and_score():
    from core.contracts import DataGap

    blocking = DataGap(
        code="X_ONE",
        field="f",
        impact="i",
        repair_action="r",
        blocking=True,
    )
    minor = DataGap(
        code="X_TWO",
        field="f",
        impact="i",
        repair_action="r",
        blocking=False,
    )
    status, score = FactQualityService.summarize([blocking, minor])
    assert status == DataQualityStatus.INSUFFICIENT
    assert score == pytest.approx(0.80)

    status, score = FactQualityService.summarize([])
    assert status == DataQualityStatus.COMPLETE
    assert score == 1.0


# ---------------------------------------------------------------------------
# 快照哈希
# ---------------------------------------------------------------------------
def test_snapshot_hash_is_stable_across_builds(binding, unit):
    fake = FakeMcpClient(responses=healthy_mcp_payload())
    hashes = []
    for _ in range(2):
        raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
        norm, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
        gaps = FactQualityService().check(binding, raw, norm)
        snapshot = FactSnapshotBuilder().build(unit, norm, refs, gaps)
        hashes.append(snapshot.content_hash)
    # 采集时间不同、snapshot_id 不同，但事实内容一样 → 哈希必须一样
    assert hashes[0] == hashes[1]


def test_snapshot_hash_changes_with_facts(binding, unit):
    def build(payload):
        fake = FakeMcpClient(responses=payload)
        raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
        norm, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
        gaps = FactQualityService().check(binding, raw, norm)
        return FactSnapshotBuilder().build(unit, norm, refs, gaps)

    baseline = build(healthy_mcp_payload())
    changed_payload = healthy_mcp_payload()
    changed_payload["erp_listing_stock_alert"][0]["canSaleNum"] = 3
    changed = build(changed_payload)
    assert baseline.content_hash != changed.content_hash


def test_snapshot_verify_detects_tampering(binding, unit):
    fake = FakeMcpClient(responses=healthy_mcp_payload())
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    norm, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, norm)
    snapshot = FactSnapshotBuilder().build(unit, norm, refs, gaps)

    assert FactSnapshotBuilder.verify(snapshot) is True
    tampered = snapshot.model_copy(update={"inventory": {"fba_available": 99999}})
    assert FactSnapshotBuilder.verify(tampered) is False


def test_snapshot_ids_are_unique(binding, unit):
    fake = FakeMcpClient(responses=healthy_mcp_payload())
    raw = asyncio.run(FactCollector(fake).collect(binding, TODAY))
    norm, refs = FactNormalizer().normalize(binding, raw, as_of=TODAY)
    gaps = FactQualityService().check(binding, raw, norm)
    ids = {FactSnapshotBuilder().build(unit, norm, refs, gaps).snapshot_id for _ in range(5)}
    assert len(ids) == 5


def test_blocked_snapshot_is_still_a_valid_snapshot(unit):
    from core.contracts import DataGap
    from facts.snapshot import blocked_snapshot

    gap = DataGap(
        code="MCP_STOCK_UNAVAILABLE",
        field="inventory.fba_available",
        impact="i",
        repair_action="r",
        blocking=True,
    )
    snapshot = blocked_snapshot(unit, [gap], as_of=date(2026, 7, 29))
    assert snapshot.quality_status == DataQualityStatus.FAILED
    assert snapshot.completeness_score == 0.0
    assert snapshot.is_blocking is True
    assert snapshot.blocking_gap_codes == ["MCP_STOCK_UNAVAILABLE"]
