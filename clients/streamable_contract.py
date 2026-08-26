from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SupplementalContractStatus(StrEnum):
    VERIFIED_DRAFT = "VERIFIED_DRAFT"
    FROZEN = "FROZEN"


@dataclass(frozen=True, slots=True)
class StreamableToolContract:
    tool_name: str
    domain: str
    status: SupplementalContractStatus
    allowed_row_fields: frozenset[str]


def _contract(
    tool_name: str,
    domain: str,
    *fields: str,
    status: SupplementalContractStatus = SupplementalContractStatus.VERIFIED_DRAFT,
) -> StreamableToolContract:
    return StreamableToolContract(
        tool_name=tool_name,
        domain=domain,
        status=status,
        allowed_row_fields=frozenset(fields),
    )


def _draft(tool_name: str, domain: str, *fields: str) -> StreamableToolContract:
    return _contract(tool_name, domain, *fields)


def _frozen(tool_name: str, domain: str, *fields: str) -> StreamableToolContract:
    return _contract(
        tool_name,
        domain,
        *fields,
        status=SupplementalContractStatus.FROZEN,
    )


STREAMABLE_TOOL_CONTRACTS = {
    contract.tool_name: contract
    for contract in (
        _draft("sys_user_query", "operator_identity", "id", "userName", "userAccount", "userState", "roles"),
        _draft("sprout_shop_query", "shop_identity", "id", "account", "name", "plSiteCode", "platformSite", "siteCode", "waste"),
        _draft("parent_listing_detail", "identity", "父ASIN", "父卖家SKU", "产品中文名", "产品名称", "店铺账号", "店铺ID", "站点"),
        _frozen("listing_basic_info", "quality", "标题", "售价", "评论数", "星级", "变体数量", "评论内容", "16周退款率", "32周退款率", "类目退换货率", "大类排名", "小类排名", "类目转化率", "链接转化率"),
        _draft("parent_listing_stock_summary", "inventory", "FBA可售库存", "FBA入库库存", "FBA预留库存", "FBA不可售库存"),
        _draft("direct_competitors", "competition", "竞品", "父ASIN", "竞品标题", "价格", "星级", "评论数", "ratings数", "一级类目排名", "末级类目排名", "末级类目名称", "品牌", "颜色", "尺码", "面料", "变体数量", "是否Aplus", "buyBox国家"),
        _frozen("az_extend_detail", "listing_extension", "ASIN", "SELLER_SKU", "SHOP_ID", "ACCOUNT", "PL_SITE_CODE", "SELLING_PARTNER_ID", "ASIN_PRINCIPAL_USER_ID", "EDITOR_ID", "CREATOR_ID", "UPDATE_TIME", "TARGET_STAR_RATE", "REFUND_RATE", "PRODUCT_GRADE", "SEASONALITY", "STOCK_CYCLE", "PURCHASE_CYCLE"),
        _draft("product_sales", "sales", "全部销售额", "全部销量", "全部单量", "广告花费", "广告销售额", "广告单量", "ACOS", "毛利率"),
        _draft("ad_product_report", "traffic", "曝光量", "点击量", "CTR", "CPC", "CVR", "销售额", "销售数量", "广告订单量", "花费", "ACOS", "币种"),
        _draft("own_keyword_flow", "keyword", "ASIN", "关键词", "周搜索量", "自然位排位", "自然排位排名", "词的周排名"),
        _draft("ad_search_term_report", "ad_search_term", "搜索词", "曝光量", "点击量", "CTR", "CPC", "CVR", "销售额", "销售数量", "广告订单量", "花费", "ACOS"),
        _draft("ad_campaign_list", "ad_campaign", "广告活动id", "广告活动名称", "广告组合id", "广告组合名称"),
        _draft("flow_keywords", "keyword", "关键词", "搜索排名", "搜索量"),
    )
}
STREAMABLE_ALLOWED_TOOLS = frozenset(STREAMABLE_TOOL_CONTRACTS)
