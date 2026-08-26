from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from core.operating_unit import OperatingUnitBinding

AZLISTING_INTERNAL_CONTRACT_VERSION = "amazon_ops.azlisting_mcp_input.internal.v3"


class AzListingExternalContractStatus(StrEnum):
    PENDING_OWNER_FREEZE = "PENDING_OWNER_FREEZE"
    FROZEN = "FROZEN"


AZLISTING_EXTERNAL_CONTRACT_STATUS = AzListingExternalContractStatus.PENDING_OWNER_FREEZE


@dataclass(frozen=True, slots=True)
class AzListingFactToolContract:
    tool_name: str
    request_fields: tuple[str, ...]
    critical: bool
    fact_field_groups: tuple[tuple[str, ...], ...]
    identity_policy: str = "VALIDATE_WHEN_PRESENT"
    #: 条件字段（如按站点配置才有的 zipCode）。合同据此完整声明请求形状。
    optional_request_fields: tuple[str, ...] = ()
    #: 日期窗口类型：None 无日期字段；"LOOKBACK" 用回看窗口；"YESTERDAY" 用昨天单日。
    date_window: str | None = None
    #: 参数构造风格：常规父体字段 / product_info 的 paramsJson / listing 门店详情。
    argument_style: str = "PARENT_FIELDS"


AZLISTING_LIST_UNITS_TOOL = "erp_amazon_listing_query_page"
AZLISTING_LIST_UNITS_REQUEST_FIELDS = ("pageNo", "pageSize")
AZLISTING_LIST_UNITS_REQUIRED_FIELDS = (
    "shopId",
    "shopAccount",
    "siteCode",
    "asin",
    "sellerSku",
    "userId",
    "status",
)
AZLISTING_LIST_UNITS_BY_PRINCIPAL_TOOL = "erp_listing_follow_up_by_principal"
AZLISTING_LIST_UNITS_BY_PRINCIPAL_REQUEST_FIELDS = (
    "principalName",
    "shopAccount",
    "pageNo",
    "pageSize",
)
AZLISTING_LIST_UNITS_BY_PRINCIPAL_REQUIRED_FIELDS = (
    "shopAccount",
    "parentAsin",
    "parentSellerSku",
    "status",
)
AZLISTING_DIRECTORY_TOOLS = frozenset({"sys_user_query", "sprout_shop_query"})

AZLISTING_FACT_CONTRACTS: dict[str, AzListingFactToolContract] = {
    "product_info": AzListingFactToolContract(
        "erp_listing_product_info",
        ("paramsJson",),
        True,
        (
            ("asin",),
            ("parentAsin",),
            ("sellerSku",),
            ("status",),
            ("principalUserId",),
            ("principalUserName",),
        ),
        "REQUIRE_PARENT_ASIN_PER_ROW",
        argument_style="PRODUCT_INFO",
    ),
    "listing_identity": AzListingFactToolContract(
        "erp_asin_full_detail",
        ("asin", "siteCode", "includeReviews"),
        True,
        (
            ("asin",),
            (
                "linkStatus",
                "bonus",
                "images",
                "aplus",
                "reviews",
                "productName",
                "title",
                "售价",
                "星级",
                "评论数",
            ),
        ),
        "REQUIRE_REQUESTED_PARENT_ASIN",
        optional_request_fields=("zipCode",),
        argument_style="STOREFRONT",
    ),
    "gross_profit": AzListingFactToolContract(
        "erp_listing_gross_profit_history",
        ("shopAccount", "parentAsin", "parentSeller", "startDate", "endDate"),
        True,
        (("recordDate", "statDate", "detail"), ("productSaleNum", "全部销量", "detail")),
        date_window="LOOKBACK",
    ),
    "business_report": AzListingFactToolContract(
        "erp_listing_business_report",
        ("shopAccount", "parentAsin", "parentSellerSku"),
        False,
        (("records", "reportStartTime", "statDate", "sessions", "conversation"),),
    ),
    "natural_ad_flow": AzListingFactToolContract(
        "erp_listing_natural_advert_flow",
        ("shopAccount", "parentAsin", "parentSeller", "startDate", "endDate"),
        False,
        (("totalOrderNum",), ("naturalOrderNum",), ("adOrderNum",)),
        # 真实 MCP 在单日窗口通常返回空；回看窗口才会提供流量入口子体的
        # 自然/广告结构。响应没有日期维度，因此下游只能作窗口汇总。
        date_window="LOOKBACK",
    ),
    "stock": AzListingFactToolContract(
        "erp_listing_stock_alert",
        ("shopAccount", "parentAsin", "parentSeller"),
        True,
        (("canSaleNum", "FBA可售库存", "availableQuantity"),),
    ),
    "unit_profit": AzListingFactToolContract(
        "erp_listing_unit_profit_analysis",
        ("shopAccount", "parentAsin", "parentSeller"),
        False,
        (("unitProfit", "productPrice"),),
    ),
    "inventory_cost": AzListingFactToolContract(
        "erp_listing_inventory_cost_analysis",
        ("shopAccount", "parentAsin", "parentSeller"),
        False,
        (("children", "childAsin", "asin", "reportMonth"),),
    ),
    "advert_config": AzListingFactToolContract(
        "erp_listing_advert_agent_config",
        ("shopAccount", "parentAsin", "parentSellerSku"),
        False,
        (("targetAcosSuggest", "targetAcos"), ("dailyBudgetSuggest", "dailyBudget")),
    ),
    "monthly_goal": AzListingFactToolContract(
        "erp_listing_monthly_goal",
        ("shopAccount", "parentAsin", "parentSellerSku"),
        False,
        (("monthlyGoals", "estimatedMonthOrderNum", "warn"),),
    ),
    "refund_rate": AzListingFactToolContract(
        "erp_listing_refund_rate",
        ("shopAccount", "parentAsin", "parentSellerSku"),
        False,
        (("summary", "sixteenWeekRefundRate", "16周退款率"),),
    ),
    "platform_activity": AzListingFactToolContract(
        "erp_listing_platform_activity",
        ("shopAccount", "parentAsin", "parentSellerSku"),
        False,
        (("sellerSku", "activityName", "activityType", "recordDate"),),
    ),
    "parent_extension": AzListingFactToolContract(
        "erp_parent_listing_product_info",
        ("shopAccount", "parentAsin", "parentSellerSku"),
        False,
        ((
            "progressPreparationCode",
            "progressPreparation",
            "seasonalityCode",
            "seasonality",
        ),),
    ),
}

AZLISTING_FACT_TOOL_NAMES: dict[str, str] = {
    key: contract.tool_name for key, contract in AZLISTING_FACT_CONTRACTS.items()
}


def build_fact_request_arguments(
    contract: AzListingFactToolContract,
    binding: OperatingUnitBinding,
    *,
    window_start: date,
    window_end: date,
    yesterday: date,
    zip_code: str | None = None,
) -> dict[str, Any]:
    """按合同为单个事实工具构造 MCP 请求参数。

    这是每个工具**请求形状的唯一真相源**：collector 只负责算窗口、取 zip，
    具体字段与取值都由合同在这里派生，避免"合同声明"与"实际构造"两处手工对齐漂移。
    """
    style = contract.argument_style
    if style == "PRODUCT_INFO":
        return {
            "paramsJson": json.dumps(
                {
                    "shopAccount": binding.shop_account,
                    "parentAsin": binding.parent_asin,
                    "parentSellerSku": binding.parent_seller_sku,
                    "qryFiveBulletPoint": True,
                    "qryFineness": True,
                },
                ensure_ascii=False,
            )
        }
    if style == "STOREFRONT":
        arguments: dict[str, Any] = {
            "asin": binding.parent_asin,
            "siteCode": binding.site_code.removeprefix("AMAZON_"),
            "includeReviews": "false",
        }
        if zip_code:
            arguments["zipCode"] = zip_code
        return arguments
    if style != "PARENT_FIELDS":
        raise ValueError(f"unknown argument_style: {style}")
    resolved: dict[str, Any] = {}
    for request_field in contract.request_fields:
        if request_field == "shopAccount":
            resolved[request_field] = binding.shop_account
        elif request_field == "parentAsin":
            resolved[request_field] = binding.parent_asin
        elif request_field in ("parentSeller", "parentSellerSku"):
            resolved[request_field] = binding.parent_seller_sku
        elif request_field == "startDate":
            start = yesterday if contract.date_window == "YESTERDAY" else window_start
            resolved[request_field] = start.isoformat()
        elif request_field == "endDate":
            end = yesterday if contract.date_window == "YESTERDAY" else window_end
            resolved[request_field] = end.isoformat()
        else:
            raise ValueError(
                f"{contract.tool_name} has an unmapped request field: {request_field}"
            )
    return resolved

AZLISTING_EXTENSION_TOOL_NAMES: dict[str, str] = {
    "child_detail": "erp_asin_full_detail",
    "price_promotion": "erp_listing_price_promotion_analysis",
    "recent_reviews": "erp_listing_review_analysis",
    "account_health": "erp_amazon_account_health_issue",
    "keyword_rank": "erp_listing_asin_keyword_rank_history",
}
AZLISTING_ALLOWED_TOOLS = frozenset(
    {
        AZLISTING_LIST_UNITS_TOOL,
        AZLISTING_LIST_UNITS_BY_PRINCIPAL_TOOL,
        *AZLISTING_FACT_TOOL_NAMES.values(),
        *AZLISTING_EXTENSION_TOOL_NAMES.values(),
    }
)


def azlisting_contract_manifest() -> dict[str, object]:
    return {
        "contract_version": AZLISTING_INTERNAL_CONTRACT_VERSION,
        "external_contract_status": (AZLISTING_EXTERNAL_CONTRACT_STATUS.value),
        "operating_unit_strategies": {
            "PRINCIPAL_DIRECTORY": {
                "directory_tools": ["sys_user_query", "sprout_shop_query"],
                "listing_tool": AZLISTING_LIST_UNITS_BY_PRINCIPAL_TOOL,
                "operator_role": "GROUP_FBASALER",
                "listing_request_fields": list(
                    AZLISTING_LIST_UNITS_BY_PRINCIPAL_REQUEST_FIELDS
                ),
                "required_record_fields": list(
                    AZLISTING_LIST_UNITS_BY_PRINCIPAL_REQUIRED_FIELDS
                ),
            },
            "QUERY_PAGE": {
                "tool_name": AZLISTING_LIST_UNITS_TOOL,
                "request_fields": list(AZLISTING_LIST_UNITS_REQUEST_FIELDS),
                "required_record_fields": list(AZLISTING_LIST_UNITS_REQUIRED_FIELDS),
            },
            "PRINCIPAL_FOLLOW_UP": {
                "tool_name": AZLISTING_LIST_UNITS_BY_PRINCIPAL_TOOL,
                "request_fields": list(AZLISTING_LIST_UNITS_BY_PRINCIPAL_REQUEST_FIELDS),
                "required_record_fields": list(AZLISTING_LIST_UNITS_BY_PRINCIPAL_REQUIRED_FIELDS),
                "required_scope_fields": [
                    "principalName",
                    "principalUserId",
                    "shopId",
                    "shopAccount",
                    "siteCode",
                ],
            },
        },
        "fact_tools": {
            key: {
                "tool_name": contract.tool_name,
                "request_fields": list(contract.request_fields),
                "critical": contract.critical,
                "identity_policy": contract.identity_policy,
                "fact_field_groups": [list(group) for group in contract.fact_field_groups],
            }
            for key, contract in AZLISTING_FACT_CONTRACTS.items()
        },
        "extension_tools": dict(AZLISTING_EXTENSION_TOOL_NAMES),
    }
