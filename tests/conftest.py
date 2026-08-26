"""当前巡检链路的共享测试夹具。"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clients.mcp_client import FakeMcpClient  # noqa: E402
from core.contracts import OperatingUnitRef  # noqa: E402
from core.operating_unit import OperatingUnitBinding  # noqa: E402
from facts.collector import FactCollector  # noqa: E402

TODAY = date(2026, 7, 29)


def collect_facts_with_children(
    collector: FactCollector,
    binding: OperatingUnitBinding,
    as_of: date = TODAY,
) -> dict[str, Any]:
    """测试装配完整 raw：父体 + 补充 + 子体扩展事实。

    生产由异步子体事实服务把子体结果合并进 raw；``collect()`` 本身只负责拉父体+补充。
    测试需要完整 raw（含子体）时用本辅助显式装配等价结果，替代已移除的同步子体路径。
    """
    raw = asyncio.run(collector.collect(binding, as_of))
    child_calls = collector.build_child_calls(binding, as_of, raw.get("product_info"))
    if child_calls:
        raw.update(asyncio.run(collector.collect(binding, as_of, calls=child_calls)))
    return raw


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------
@pytest.fixture
def binding() -> OperatingUnitBinding:
    return OperatingUnitBinding(
        shop_id=1622,
        site_code="US",
        parent_asin="B0TEST123",
        shop_account="test-shop-account",
        parent_seller_sku="PSKU-001",
    )


@pytest.fixture
def unit(binding: OperatingUnitBinding) -> OperatingUnitRef:
    return OperatingUnitRef.derive(
        shop_id=binding.shop_id,
        site_code=binding.site_code,
        parent_asin=binding.parent_asin,
        parent_seller_sku=binding.parent_seller_sku,
    )


# ---------------------------------------------------------------------------
# MCP 数据
# ---------------------------------------------------------------------------
def _daily_rows(days: int = 30, *, units: float = 12.0, acos: float = 0.2) -> list[dict]:
    rows = []
    for offset in range(days):
        stat_date = (TODAY - timedelta(days=offset + 1)).isoformat()
        rows.append({
            "statDate": stat_date,
            "全部单量": int(units),
            "全部销量": int(units),
            "全部销售额": round(units * 19.9, 2),
            "广告花费": 25.0,
            "广告销售额": round(25.0 / acos, 2) if acos else 0.0,
            "广告单量": 4,
            "ACOS": acos,
            "毛利率": 0.22,
            "广告零销售花费": 0,
        })
    return rows


def healthy_mcp_payload(**overrides: Any) -> dict[str, list[dict]]:
    """一份"什么都正常"的 MCP 数据。各用例在此基础上做最小改动。"""
    payload: dict[str, list[dict]] = {
        "erp_listing_product_info": [
            {
                "asin": "B0CHILD01",
                "sellerSku": "SKU-1",
                "ASIN": "B0CHILD01",
                "SELLER_SKU": "SKU-1",
                "SHOP_ID": 1622,
                "PL_SITE_CODE": "US",
                "SELLING_PARTNER_ID": "A1OWNSELLER",
                "status": "可售",
                "productName": "Test Product Red M",
                "principalUserId": "35",
                "principalUserName": "Operator A",
                "fiveBulletPoint1": "point one",
                "fiveBulletPoint2": "point two",
                "fiveBulletPoint3": "point three",
                "fiveBulletPoint4": "point four",
                "fiveBulletPoint5": "point five",
                "parentAsin": "B0TEST123",
                "variationThemeName": "COLOR_SIZE",
                "productColor": "Red",
                "productSize": "M",
                "finenessDesc": "PrimaryColor",
                "lastCategory": "Apparel",
                "genericKeyword": "test keyword",
            },
        ],
        "erp_asin_full_detail": [
            {
                "asin": "B0TEST123",
                "productName": "Test Product",
                "售价": 19.9,
                "星级": 4.5,
                "评论数": 320,
                "链接转化率": 0.11,
                "类目转化率": 0.10,
                "类目退换货率": 0.06,
                "16周退款率": 0.05,
                "32周退款率": 0.05,
                "大类排名": 12000,
                "小类排名": 320,
                "目标星级": 4.3,
            },
        ],
        "erp_listing_platform_activity": [],
        "erp_parent_listing_product_info": [],
        "erp_listing_price_promotion_analysis": [
            {
                "asin": "B0CHILD01",
                "price": "$19.90",
                "categoryName": "Apparel",
                "categoryId": "APPAREL",
                "breadCrumbs": "Clothing > Apparel",
                "inStock": "in_stock",
                "hasCart": True,
            }
        ],
        "erp_listing_review_analysis": [
            {
                "asin": "B0CHILD01",
                "reviews": [],
                "reviewsByStar": {},
            }
        ],
        "erp_amazon_account_health_issue": [
            {"asin": "B0CHILD01", "issues": []}
        ],
        "erp_listing_asin_keyword_rank_history": [
            {
                "asin": "B0CHILD01",
                "keyword": "test keyword",
                "siteCode": "US",
                "createTime": (TODAY - timedelta(days=offset)).isoformat(),
                "crawNatureRank": 20 + offset,
            }
            for offset in range(14)
        ],
        "erp_listing_gross_profit_history": _daily_rows(),
        "erp_listing_business_report": [
            {"sessions": 900, "orderedItems": 95, "链接转化率": 0.105},
            {"sessions": 880, "orderedItems": 92, "链接转化率": 0.104},
        ],
        "erp_listing_natural_advert_flow": [
            {
                "asin": "",
                "parentAsin": "B0TEST123",
                "isSummary": True,
                "totalOrderNum": 12,
                "naturalOrderNum": 8,
                "adOrderNum": 4,
                "adClick": 120,
                "adImpressions": 6000,
            }
        ],
        "erp_listing_stock_alert": [
            {
                "canSaleNum": 900,
                "inStockNum": 200,
                "reserveNum": 10,
                "noSaleNum": 0,
                "totalStock": 1110,
                "purchaseOnWay": 300,
            },
        ],
        "erp_listing_unit_profit_analysis": [
            {
                "targetAcos": 0.25,
                "dailyBudget": 40.0,
                "unitProfit": 4.2,
                "netCashRecovery": 5200.0,
            },
        ],
        "erp_listing_inventory_cost_analysis": [
            {
                "childAsin": "B0CHILD01",
                "sellerSku": "SKU-1",
                "汇总超龄库存数": 0,
                "汇总超龄仓租费": 0,
                "reportMonth": "2026-07",
            },
        ],
        "erp_listing_advert_agent_config": [
            {
                "parentAsin": "B0TEST123",
                "parentSellerSku": "PSKU-001",
                "siteCode": "US",
                "targetAcosSuggest": "25%",
                "dailyBudgetSuggest": "40",
                "productStage": "推进期",
                "productPosition": "重点产品",
                "seasonType": "旺季前期",
                "dayRange": "30",
                "operatingMode": "积极推进",
                "advertPurposes": "扩大销量、提升排名",
                "targetKeywordTypes": "核心词、长尾词",
                "advertDirectionTypes": "推进自然位",
            }
        ],
        "erp_listing_monthly_goal": [
            {
                "parentAsin": "B0TEST123",
                "parentSellerSku": "PSKU-001",
                "currentMonth": "2026年07月",
                "monthlyGoals": [
                    {
                        "parentSellerSku": "PSKU-001",
                        "everyMonthStr": "2026年07月",
                        "estimatedMonthOrderNum": 372,
                    },
                    {
                        "parentSellerSku": "PSKU-001",
                        "everyMonthStr": "2026年08月",
                        "estimatedMonthOrderNum": 700,
                    },
                    {
                        "parentSellerSku": "PSKU-001",
                        "everyMonthStr": "2026年09月",
                        "estimatedMonthOrderNum": 750,
                    },
                    {
                        "parentSellerSku": "PSKU-001",
                        "everyMonthStr": "2026年10月",
                        "estimatedMonthOrderNum": 800,
                    },
                ],
            }
        ],
        "erp_listing_refund_rate": [
            {
                "summary": {
                    "sixteenWeekRefundRate": 0.05,
                    "thirtyTwoWeekRefundRate": 0.05,
                },
                "children": [],
            }
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def mcp_payload() -> dict[str, list[dict]]:
    return healthy_mcp_payload()


@pytest.fixture
def fake_mcp(mcp_payload: dict[str, list[dict]]) -> FakeMcpClient:
    return FakeMcpClient(responses=mcp_payload)
