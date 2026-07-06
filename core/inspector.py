"""巡检主编排。骨架，具体规则接入待知识库结构化后补。"""
from __future__ import annotations
from datetime import date, timedelta
from data import erp_repo, shop_map
from data.mcp_client import MCPClient


def inspect_one(cfg_row: dict, lookback_days: int = 30) -> dict:
    """针对单条 decision_config 拉数据，返回原始 payload（暂不做判定）。"""
    shop_id = cfg_row["shop_id"]
    account = shop_map.account(shop_id)
    if not account:
        return {"error": f"shop_id {shop_id} 无 account 映射"}
    mcp = MCPClient()
    end = date.today()
    start = end - timedelta(days=lookback_days)
    args_basic = {"shop_account": account, "parent_asin": cfg_row["parent_asin"],
                  "parent_seller_sku": cfg_row["parent_seller_sku"]}
    args_sales = {**args_basic, "start_date": start.isoformat(), "end_date": end.isoformat()}
    return {
        "shop_account": account,
        "listing": mcp.call("listing_basic_info_v2", args_basic),
        "sales": mcp.call("product_sales", args_sales),
        "campaigns": mcp.call("ad_campaign_list", args_basic),
    }


def inspect_batch(limit: int = 5) -> list[dict]:
    return [inspect_one(r) for r in erp_repo.fetch_decision_configs(limit=limit)]
