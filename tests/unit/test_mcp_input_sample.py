from __future__ import annotations

import asyncio
from copy import deepcopy

from scripts.validate_mcp_input_sample import validate_payload
from tests.conftest import healthy_mcp_payload


def sample_payload() -> dict:
    fact_samples = healthy_mcp_payload()
    fact_samples["erp_asin_full_detail"][0]["asin"] = "B0TEST123"
    return {
        "contract_version": "amazon_ops.mcp_input_acceptance.v1",
        "as_of": "2026-07-29",
        "operating_unit_pages": [
            {
                "pageNo": 1,
                "pageSize": 20,
                "totalCount": 2,
                "totalPages": 1,
                "records": [
                    {
                        "shopId": 1622,
                        "shopAccount": "test-shop-account",
                        "siteCode": "US",
                        "asin": "B0TEST123",
                        "sellerSku": "PSKU-001",
                        "parentAsin": "B0TEST123",
                        "parentSellerSku": "PSKU-001",
                        "userId": 35,
                        "status": "Active",
                    },
                    {
                        "shopId": 1623,
                        "shopAccount": "inactive-shop",
                        "siteCode": "US",
                        "asin": "B0INACTIVE",
                        "sellerSku": "PSKU-002",
                        "parentAsin": "B0INACTIVE",
                        "parentSellerSku": "PSKU-002",
                        "userId": 42,
                        "status": "Inactive",
                    },
                ],
            }
        ],
        "fact_sample_identity": {
            "shopId": 1622,
            "siteCode": "US",
            "parentAsin": "B0TEST123",
            "parentSellerSku": "PSKU-001",
        },
        "fact_samples": fact_samples,
    }


def test_accepts_complete_redacted_sample():
    result = asyncio.run(validate_payload(sample_payload()))

    assert result["status"] == "ACCEPTED_WITH_GAPS"
    assert result["active_operating_unit_count"] == 1
    assert len(result["validated_fact_tools"]) == 13
    assert result["source_ref_count"] == 13
    assert result["blocking_gap_count"] == 0
    assert result["inspection_date"] == "2026-07-29"
    assert result["as_of"] == "2026-07-28"
    assert result["window_start"] == "2026-06-29"
    assert result["window_end"] == "2026-07-28"
    assert all(result["critical_field_coverage"].values())
    assert result["errors"] == []


def test_rejects_list_rows_without_v3_parent_identity():
    payload = sample_payload()
    del payload["operating_unit_pages"][0]["records"][0]["asin"]
    del payload["operating_unit_pages"][0]["records"][0]["parentAsin"]

    result = asyncio.run(validate_payload(payload))

    assert result["status"] == "REJECTED"
    assert "lacks parent identity" in result["errors"][0]


def test_rejects_missing_fact_tool_sample():
    payload = sample_payload()
    del payload["fact_samples"]["erp_listing_refund_rate"]

    result = asyncio.run(validate_payload(payload))

    assert result["status"] == "REJECTED"
    assert result["errors"][0].startswith("SCHEMA:")


def test_rejects_empty_core_fact():
    payload = sample_payload()
    payload["fact_samples"]["erp_listing_stock_alert"] = []

    result = asyncio.run(validate_payload(payload))

    assert result["status"] == "REJECTED"
    assert "CORE_FACT_EMPTY:erp_listing_stock_alert" in result["errors"]


def test_rejects_cross_parent_product_evidence():
    payload = deepcopy(sample_payload())
    payload["fact_samples"]["erp_listing_product_info"][0]["parentAsin"] = "B0OTHER"

    result = asyncio.run(validate_payload(payload))

    assert result["status"] == "REJECTED"
    assert "PRODUCT_PARENT_IDENTITY_MISMATCH:0" in result["errors"]


def test_rejects_listing_sample_without_explicit_parent_asin():
    payload = sample_payload()
    del payload["fact_samples"]["erp_asin_full_detail"][0]["asin"]

    result = asyncio.run(validate_payload(payload))

    assert result["status"] == "REJECTED"
    assert "LISTING_PARENT_IDENTITY_MISSING" in result["errors"]


def test_rejects_listing_sample_for_another_parent():
    payload = sample_payload()
    payload["fact_samples"]["erp_asin_full_detail"][0]["asin"] = "B0OTHER"

    result = asyncio.run(validate_payload(payload))

    assert result["status"] == "REJECTED"
    assert "LISTING_PARENT_IDENTITY_MISMATCH" in result["errors"]


def test_rejects_cross_unit_identity_in_optional_fact_tool():
    payload = sample_payload()
    payload["fact_samples"]["erp_listing_advert_agent_config"][0]["shopId"] = 9999

    result = asyncio.run(validate_payload(payload))

    assert result["status"] == "REJECTED"
    assert "FACT_SHOP_ID_MISMATCH:erp_listing_advert_agent_config" in result["errors"]
