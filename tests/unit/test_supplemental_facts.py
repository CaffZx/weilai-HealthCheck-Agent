from __future__ import annotations

import asyncio

import pytest

from clients.mcp_client import AzListingMcpClient, FakeMcpClient, McpToolResult
from clients.streamable_contract import (
    STREAMABLE_ALLOWED_TOOLS,
    STREAMABLE_TOOL_CONTRACTS,
    SupplementalContractStatus,
)
from core.contracts import sha256_json
from facts.supplemental import (
    StreamableSupplementalAdapter,
    SupplementalContractNotFrozen,
    SupplementalFactsDisabled,
    map_streamable_result,
)


def test_only_required_supplemental_contracts_are_frozen():
    assert len(STREAMABLE_ALLOWED_TOOLS) == 13
    frozen = {
        name
        for name, contract in STREAMABLE_TOOL_CONTRACTS.items()
        if contract.status is SupplementalContractStatus.FROZEN
    }
    assert frozen == {"az_extend_detail", "listing_basic_info"}
    assert all(
        contract.status is SupplementalContractStatus.VERIFIED_DRAFT
        for name, contract in STREAMABLE_TOOL_CONTRACTS.items()
        if name not in frozen
    )


def test_az_extend_detail_mapping_keeps_seller_id():
    result = McpToolResult(
        tool_name="az_extend_detail",
        data=[{"rows": [{"SELLING_PARTNER_ID": "A1SELLER", "PASSWORD": "hidden"}]}],
        raw={},
    )

    batch = map_streamable_result("az_extend_detail", result)

    assert batch.rows == [{"SELLING_PARTNER_ID": "A1SELLER"}]


def test_listing_basic_info_mapping_keeps_rating_and_review_count():
    result = McpToolResult(
        tool_name="listing_basic_info",
        data=[{"rows": [{"星级": 4.6, "评论数": 321, "PASSWORD": "hidden"}]}],
        raw={},
    )

    batch = map_streamable_result("listing_basic_info", result)

    assert batch.rows == [{"星级": 4.6, "评论数": 321}]


def test_streamable_mapping_keeps_only_explicit_business_fields():
    result = McpToolResult(
        tool_name="sprout_shop_query",
        data=[{
            "rows": [{
                "id": 72516,
                "account": "shop_us",
                "siteCode": "US",
                "password": "must-not-survive",
                "thirdAzSpApiAccessToken": "must-not-survive",
                "unexpectedBusinessField": "must-not-survive",
            }],
        }],
        raw={},
    )

    batch = map_streamable_result("sprout_shop_query", result)

    assert batch.domain == "shop_identity"
    assert batch.rows == [{"id": 72516, "account": "shop_us", "siteCode": "US"}]
    assert batch.source_content_hash == result.content_hash
    assert batch.mapped_content_hash == sha256_json(batch.rows)


def test_disabled_supplemental_adapter_does_not_call_mcp():
    client = FakeMcpClient(responses={"product_sales": [{"全部销量": 1}]})
    adapter = StreamableSupplementalAdapter(client, enabled=False)

    with pytest.raises(SupplementalFactsDisabled):
        asyncio.run(adapter.collect("product_sales", {}))

    assert client.calls == []


def test_enabled_adapter_rejects_draft_contract_without_calling_mcp():
    client = FakeMcpClient(responses={"product_sales": [{"全部销量": 1}]})
    adapter = StreamableSupplementalAdapter(client, enabled=True)

    with pytest.raises(SupplementalContractNotFrozen):
        asyncio.run(adapter.collect("product_sales", {}))

    assert client.calls == []


def test_unregistered_streamable_result_is_rejected():
    with pytest.raises(SupplementalContractNotFrozen):
        map_streamable_result("unknown_tool", McpToolResult("unknown_tool", [], {}))


def test_streamable_client_uses_raw_x_api_key_header():
    client = AzListingMcpClient(
        "https://streamable.test/mcp",
        "test-token",
        allowed_tools=STREAMABLE_ALLOWED_TOOLS,
        auth_header="X-Api-Key",
        auth_scheme="",
    )

    headers = client._client().headers
    assert headers["X-Api-Key"] == "test-token"
    assert "Authorization" not in headers
    asyncio.run(client.aclose())
