from __future__ import annotations

import pytest

from clients.mcp_client import McpToolResult
from core.errors import McpContractInvalid
from integrations.mcp_operating_units import (
    LIST_UNITS_BY_PRINCIPAL_TOOL,
    LIST_UNITS_TOOL,
    McpOperatingUnitProvider,
    PrincipalFollowUpOperatingUnitProvider,
    PrincipalDirectoryOperatingUnitProvider,
    parse_principal_scopes_json,
)


class PagedMcp:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        page = self.pages[arguments["pageNo"]]
        return McpToolResult(tool_name=tool_name, data=[page], raw={})


def record(index: int, **changes):
    value = {
        "shopId": 1600 + index,
        "shopAccount": f"shop-{index}",
        "siteCode": "Amazon_US",
        "asin": f"B0PARENT{index:02d}",
        "sellerSku": f"PARENT-{index}",
        "parentAsin": f"B0PARENT{index:02d}",
        "parentSellerSku": f"PARENT-{index}",
        "userId": 3500 + index,
        "status": "Active",
    }
    value.update(changes)
    return value


@pytest.mark.asyncio
async def test_provider_reads_every_page_and_filters_inactive():
    client = PagedMcp(
        {
            1: {
                "totalCount": 3,
                "totalPages": 2,
                "pageNo": 1,
                "pageSize": 2,
                "records": [record(1), record(2, status="Inactive")],
            },
            2: {
                "totalCount": 3,
                "totalPages": 2,
                "pageNo": 2,
                "pageSize": 2,
                "records": [record(3)],
            },
        }
    )
    units = await McpOperatingUnitProvider(client, page_size=2).list_all_active_units()
    assert {unit.parent_asin for unit in units} == {"B0PARENT01", "B0PARENT03"}
    assert {unit.owner_user_ids for unit in units} == {()}
    assert [arguments["pageNo"] for _, arguments in client.calls] == [1, 2]
    assert all(tool == LIST_UNITS_TOOL for tool, _ in client.calls)


@pytest.mark.asyncio
async def test_provider_uses_v3_asin_and_seller_sku_as_parent_identity():
    client = PagedMcp(
        {
            1: {
                "totalCount": 1,
                "totalPages": 1,
                "pageNo": 1,
                "pageSize": 20,
                "records": [
                    {
                        "shopId": 1622,
                        "shopAccount": "shop-account",
                        "siteCode": "Amazon_US",
                        "asin": "B0PARENT001",
                        "sellerSku": "PARENT-1",
                        "status": "Active",
                    }
                ],
            }
        }
    )
    units = await McpOperatingUnitProvider(client).list_all_active_units()
    assert len(units) == 1
    assert units[0].parent_asin == "B0PARENT001"
    assert units[0].parent_seller_sku == "PARENT-1"


@pytest.mark.asyncio
async def test_listing_directory_maps_v3_asin_and_seller_sku_to_parent_fields():
    client = PagedMcp(
        {
            1: {
                "totalCount": 1,
                "totalPages": 1,
                "pageNo": 1,
                "pageSize": 20,
                "records": [{
                    "shopId": 1622,
                    "shopAccount": "shop-account",
                    "siteCode": "Amazon_US",
                    "asin": "US-2P防冻袋17*27",
                    "sellerSku": "US-FDD725",
                    "userId": 35,
                    "status": "Active",
                }],
            }
        }
    )
    records = await McpOperatingUnitProvider(client).list_all_active_listing_records()
    assert records[0]["parent_asin"] == "US-2P防冻袋17*27"
    assert records[0]["parent_seller_sku"] == "US-FDD725"


@pytest.mark.asyncio
async def test_provider_rejects_pagination_drift():
    client = PagedMcp(
        {
            1: {
                "totalCount": 2,
                "totalPages": 2,
                "pageNo": 1,
                "pageSize": 1,
                "records": [record(1)],
            },
            2: {
                "totalCount": 3,
                "totalPages": 2,
                "pageNo": 2,
                "pageSize": 1,
                "records": [record(2)],
            },
        }
    )
    with pytest.raises(McpContractInvalid, match="pagination totals changed"):
        await McpOperatingUnitProvider(client, page_size=1).list_all_active_units()


@pytest.mark.asyncio
async def test_provider_ignores_listing_user_ids_for_one_operating_unit():
    shared = {
        "shopId": 1622,
        "shopAccount": "shop-shared",
        "siteCode": "Amazon_US",
        "parentAsin": "B0PARENTSHARED",
        "parentSellerSku": "PARENT-SHARED",
        "status": "Active",
    }
    client = PagedMcp(
        {
            1: {
                "totalCount": 3,
                "totalPages": 1,
                "pageNo": 1,
                "pageSize": 20,
                "records": [
                    {**shared, "asin": "B0PARENTSHARED", "sellerSku": "PARENT-SHARED", "userId": 35},
                    {**shared, "asin": "B0PARENTSHARED", "sellerSku": "PARENT-SHARED", "userId": 42},
                    {**shared, "asin": "B0PARENTSHARED", "sellerSku": "PARENT-SHARED", "userId": 35},
                ],
            }
        }
    )

    units = await McpOperatingUnitProvider(client).list_all_active_units()

    assert len(units) == 1
    assert units[0].owner_user_ids == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, "", 0, -1, True])
async def test_provider_ignores_non_authoritative_owner_user_id(user_id):
    client = PagedMcp(
        {
            1: {
                "totalCount": 1,
                "totalPages": 1,
                "pageNo": 1,
                "pageSize": 20,
                "records": [record(1, userId=user_id)],
            }
        }
    )

    units = await McpOperatingUnitProvider(client).list_all_active_units()
    assert units[0].owner_user_ids == ()


@pytest.mark.asyncio
async def test_provider_rejects_missing_records_despite_stable_totals():
    client = PagedMcp(
        {
            1: {
                "totalCount": 2,
                "totalPages": 2,
                "pageNo": 1,
                "pageSize": 1,
                "records": [record(1)],
            },
            2: {
                "totalCount": 2,
                "totalPages": 2,
                "pageNo": 2,
                "pageSize": 1,
                "records": [],
            },
        }
    )
    with pytest.raises(McpContractInvalid, match="empty final page"):
        await McpOperatingUnitProvider(client, page_size=1).list_all_active_units()


@pytest.mark.asyncio
async def test_provider_accepts_canonical_empty_page():
    client = PagedMcp(
        {
            1: {
                "totalCount": 0,
                "totalPages": 0,
                "pageNo": 1,
                "pageSize": 20,
                "records": [],
            }
        }
    )
    assert await McpOperatingUnitProvider(client).list_all_active_units() == []


class PrincipalPagedMcp:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        key = (arguments["principalName"], arguments["shopAccount"], arguments["pageNo"])
        return McpToolResult(tool_name=tool_name, data=[self.pages[key]], raw={})


@pytest.mark.asyncio
async def test_principal_directory_provider_composes_users_shops_and_parent_follow_up():
    class Directory:
        async def call_tool(self, tool_name, arguments):
            if tool_name == "sys_user_query":
                return McpToolResult(tool_name, [{"records": [{
                    "id": 35, "userName": "Operator A", "userState": "ACTIVE",
                    "roles": [{"roleCode": "GROUP_FBASALER"}],
                }], "totalCount": 1, "totalPages": 1, "pageNo": 1}], {})
            return McpToolResult(tool_name, [{"records": [{
                "id": 101, "account": "shop-us", "siteCode": "Amazon_US",
            }], "totalCount": 1, "totalPages": 1, "pageNo": 1}], {})

    class Listing:
        async def call_tool(self, tool_name, arguments):
            assert arguments["principalName"] == "Operator A"
            assert arguments["shopAccount"] == "shop-us"
            return McpToolResult(tool_name, [{"records": [{
                "shopAccount": "shop-us", "parentAsin": "B0PARENT01",
                "parentSellerSku": "PARENT-01", "status": "Active",
            }], "totalCount": 1, "totalPages": 1, "pageNo": 1}], {})

    units = await PrincipalDirectoryOperatingUnitProvider(Directory(), Listing()).list_all_active_units()
    assert len(units) == 1
    assert units[0].shop_id == 101
    assert units[0].parent_asin == "B0PARENT01"
    assert units[0].owner_user_ids == (35,)


def principal_record(parent_asin: str, **changes):
    value = {
        "shopAccount": "shop-us",
        "asin": f"{parent_asin}-CHILD",
        "sellerSku": f"{parent_asin}-CHILD-SKU",
        "parentAsin": parent_asin,
        "parentSellerSku": f"{parent_asin}-PARENT-SKU",
        "status": "Active",
    }
    value.update(changes)
    return value


@pytest.mark.asyncio
async def test_principal_provider_aggregates_authoritative_scopes_and_deduplicates():
    scopes = parse_principal_scopes_json(
        '[{"principalName":"A","principalUserId":35,"shopId":101,"shopAccount":"shop-us",'
        '"siteCode":"US"},{"principalName":"B","principalUserId":42,"shopId":101,'
        '"shopAccount":"shop-us","siteCode":"Amazon_US"}]'
    )
    client = PrincipalPagedMcp(
        {
            ("A", "shop-us", 1): {
                "totalCount": 2,
                "totalPages": 1,
                "pageNo": 1,
                "pageSize": 200,
                "records": [
                    principal_record("B0PARENT01"),
                    principal_record("B0INACTIVE", status="Inactive"),
                ],
            },
            ("B", "shop-us", 1): {
                "totalCount": 1,
                "totalPages": 1,
                "pageNo": 1,
                "pageSize": 200,
                "records": [principal_record("B0PARENT01")],
            },
        }
    )

    units = await PrincipalFollowUpOperatingUnitProvider(
        client,
        scopes,
    ).list_all_active_units()

    assert len(units) == 1
    assert units[0].shop_id == 101
    assert units[0].site_code == "AMAZON_US"
    assert units[0].parent_asin == "B0PARENT01"
    assert units[0].owner_user_ids == (35, 42)
    assert all(tool == LIST_UNITS_BY_PRINCIPAL_TOOL for tool, _ in client.calls)


@pytest.mark.asyncio
async def test_principal_provider_rejects_cross_shop_result():
    scopes = parse_principal_scopes_json(
        '[{"principalName":"A","principalUserId":35,"shopId":101,'
        '"shopAccount":"shop-us","siteCode":"US"}]'
    )
    client = PrincipalPagedMcp(
        {
            ("A", "shop-us", 1): {
                "totalCount": 1,
                "totalPages": 1,
                "pageNo": 1,
                "pageSize": 200,
                "records": [principal_record("B0PARENT01", shopAccount="other-shop")],
            },
        }
    )

    with pytest.raises(McpContractInvalid, match="inconsistent shopAccount"):
        await PrincipalFollowUpOperatingUnitProvider(
            client,
            scopes,
        ).list_all_active_units()


def test_principal_scope_parser_rejects_conflicting_shop_identity():
    with pytest.raises(ValueError, match="inconsistent shop identity"):
        parse_principal_scopes_json(
            '[{"principalName":"A","principalUserId":35,"shopId":101,'
            '"shopAccount":"shop-us","siteCode":"US"},{"principalName":"B",'
            '"principalUserId":42,"shopId":102,'
            '"shopAccount":"shop-us","siteCode":"US"}]'
        )
