"""MCP 事实采集器。

改造方案 v1.0 §8。

【行为约定】
  - 所有工具并行采集，单个失败不中断其它工具（gather 后逐个判定）。
  - 失败结果以异常对象形式返回，**不静默丢弃**。是阻断还是降级由
    FactQualityService 决定，采集器不做业务判断。
  - 核心工具与非核心工具在这里声明，是"数据阻断"与"降级运行"的分界线。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Protocol

from clients.azlisting_contract import (
    AZLISTING_EXTENSION_TOOL_NAMES,
    AZLISTING_FACT_CONTRACTS,
    AZLISTING_FACT_TOOL_NAMES,
    build_fact_request_arguments,
)
from clients.mcp_client import McpToolResult
from core.operating_unit import OperatingUnitBinding
from facts.supplemental import StreamableSupplementalAdapter

logger = logging.getLogger(__name__)


class McpPort(Protocol):
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
    ) -> McpToolResult: ...


#: 采集键 → MCP 工具名。键名是本项目内部语义名，工具名是 ERP 侧契约。
TOOL_NAMES = AZLISTING_FACT_TOOL_NAMES
EXTENSION_TOOL_NAMES = AZLISTING_EXTENSION_TOOL_NAMES
SUPPLEMENTAL_TOOL_NAMES = {
    "listing_basic_info": "listing_basic_info",
}
KEYWORD_RANK_LOOKBACK_DAYS = 14
# 关键词卡位和目标类数据只对美国、英国、德国开放。站点码使用内部规范化
# 形式，避免 US / AMAZON_US 混用导致策略漏判。
TARGET_DATA_SITE_CODES: frozenset[str] = frozenset({"AMAZON_US", "AMAZON_UK", "AMAZON_DE"})
SITE_SCOPED_FACT_KEYS: frozenset[str] = frozenset({
    "keyword_rank", "advert_config", "monthly_goal",
})


def site_scoped_disabled_fact_keys(
    site_code: str,
    disabled_fact_keys: frozenset[str],
) -> frozenset[str]:
    """Add site policy disables without mutating the caller's configured set."""
    if str(site_code or "").strip().upper() in TARGET_DATA_SITE_CODES:
        return disabled_fact_keys
    return disabled_fact_keys | SITE_SCOPED_FACT_KEYS
#: 默认禁用的父体事实键。monthly_goal 已恢复采集（见 TOOL_CALL_OVERRIDES 降超时不重试）。
DEFAULT_DISABLED_FACT_KEYS: frozenset[str] = frozenset()

#: 每工具调用超时/重试覆盖。monthly_goal 95% 单元返回"无月度目标"（快速 isError），
#: 成功也慢（平均 35s+）。降低等待、显式不重试，把父体采集长尾压住，同时保留 5% 有目标单元。
TOOL_CALL_OVERRIDES: dict[str, dict[str, Any]] = {
    "monthly_goal": {"timeout_seconds": 15.0, "max_attempts": 1},
    # 卡位接口在真实运行中存在长尾响应；允许有限重试，避免一次网络超时
    # 直接把子体卡位标成不可用。MCP 返回的确定性业务错误仍由客户端停止重试。
    "keyword_rank": {"timeout_seconds": 90.0, "max_attempts": 3},
}

#: 核心事实。任一失败 → 整轮产出数据阻断包，不生成可审批建议。
#:
#: ⚠ 待冻结事项 F-03：这份清单是工程默认值，需 Owner 确认。
#: 判断依据：身份 product_info 决定能不能落到正确的经营单元；库存 stock 决定 G2 现金
#: 可行性和断货风险；毛利 gross_profit 决定净现金回收结论。
#: 注意：listing_identity（erp_asin_full_detail）已知上游经常返空且不稳（MCP 侧作
#: best-effort 处理），移出 CORE，缺失只算 data_gap，不再阻断整个 run。前台快照相关的
#: 异常检测（Buy Box / A+ / 主图）在没有 listing_identity 时同样不会命中，符合上游语义。
CORE_KEYS: frozenset[str] = frozenset({
    "product_info",
    "stock",
    "gross_profit",
})

#: 非核心事实。失败只降低完整度并记录缺口，主流程继续。
OPTIONAL_KEYS: frozenset[str] = (
    frozenset(TOOL_NAMES) - CORE_KEYS - DEFAULT_DISABLED_FACT_KEYS
)

#: 可禁用的子体扩展工具。recent_reviews（评论爬虫）单次 180s，是子体采集的长尾瓶颈；
#: 砍掉后子体采集从 180s 降到 ~30s，牺牲"近期集中差评"这一异常点，换取其余
#: 13 个子体异常点快速判出。想恢复差评异常时从这里移除即可。
DISABLED_EXTENSION_TOOLS: frozenset[str] = frozenset({"recent_reviews"})


class FactCollector:
    """按经营单元并行拉取 MCP 事实。

    ``as_of`` 表示巡检运行日；所有日期型查询只采集运行日前已经结束的完整自然日。
    返回 `{采集键: McpToolResult | Exception}`，调用方必须逐项检查类型。
    """

    def __init__(
        self,
        client: McpPort,
        *,
        lookback_days: int = 30,
        storefront_zip_codes: dict[str, str] | None = None,
        supplemental_adapter: StreamableSupplementalAdapter | None = None,
        disabled_fact_keys: frozenset[str] = DEFAULT_DISABLED_FACT_KEYS,
        child_ext_enabled: bool = True,
        keyword_rank_enabled: bool = True,
        compliance_enabled: bool = True,
    ) -> None:
        self.client = client
        self.lookback_days = lookback_days
        self.core_keys = CORE_KEYS
        self.supplemental_adapter = supplemental_adapter
        unknown_disabled = disabled_fact_keys - frozenset(TOOL_NAMES)
        if unknown_disabled:
            raise ValueError(f"unknown facts to disable: {sorted(unknown_disabled)}")
        self.disabled_fact_keys = disabled_fact_keys
        # 临时开关（config/settings.yaml feature_flags.child_fact_enabled）：
        # 关闭时不再构造子 ASIN 扩展采集请求（child_detail / price_promotion /
        # account_health / keyword_rank / recent_reviews），巡检只保留父体事实。
        self.child_ext_enabled = child_ext_enabled
        # 临时开关（feature_flags.keyword_rank_enabled）：关闭时不再采集卡位数据。
        self.keyword_rank_enabled = keyword_rank_enabled
        # 临时开关（feature_flags.compliance_enabled）：关闭时不再采集账户健康数据
        #（account_health，合规异常点位停用）。
        self.compliance_enabled = compliance_enabled
        self.storefront_zip_codes = {
            str(site_code).strip().upper(): str(zip_code).strip()
            for site_code, zip_code in (storefront_zip_codes or {}).items()
            if str(site_code).strip() and str(zip_code).strip()
        }

    def build_calls(
        self,
        unit: OperatingUnitBinding,
        as_of: date,
    ) -> dict[str, tuple[str, dict[str, Any]]]:
        """构造全部工具调用参数。

        每个工具的请求字段与取值由 ``AZLISTING_FACT_CONTRACTS`` 派生（单一真相源），
        本方法只负责计算日期窗口、取站点 zip，并把补充 MCP 拼进来。
        """
        window_start, window_end = self.window_for(as_of, self.lookback_days)
        yesterday = as_of - timedelta(days=1)
        zip_code = self.storefront_zip_codes.get(unit.site_code)
        effective_disabled_fact_keys = site_scoped_disabled_fact_keys(
            unit.site_code, self.disabled_fact_keys
        )

        calls: dict[str, tuple[str, dict[str, Any]]] = {
            key: (
                contract.tool_name,
                build_fact_request_arguments(
                    contract,
                    unit,
                    window_start=window_start,
                    window_end=window_end,
                    yesterday=yesterday,
                    zip_code=zip_code,
                ),
            )
            for key, contract in AZLISTING_FACT_CONTRACTS.items()
        }
        if self.supplemental_adapter is not None and self.supplemental_adapter.enabled:
            calls["listing_basic_info"] = (
                SUPPLEMENTAL_TOOL_NAMES["listing_basic_info"],
                {
                    "shop_account": unit.shop_account,
                    "parent_asin": unit.parent_asin,
                    "parent_seller_sku": unit.parent_seller_sku,
                },
            )
        return {
            key: value
            for key, value in calls.items()
            if key not in effective_disabled_fact_keys
        }

    def build_child_calls(
        self,
        unit: OperatingUnitBinding,
        as_of: date,
        product_info: McpToolResult | Exception | None,
    ) -> dict[str, tuple[str, dict[str, Any]]]:
        # 临时开关：关闭时整个子体扩展采集停摆，返回空请求集合。
        if not self.child_ext_enabled:
            return {}
        children = [] if not isinstance(product_info, McpToolResult) else product_info.data
        calls: dict[str, tuple[str, dict[str, Any]]] = {}
        site_code = unit.site_code.removeprefix("AMAZON_")
        window_start, window_end = self.window_for(as_of, self.lookback_days)
        keyword_start, keyword_end = self.window_for(
            as_of,
            KEYWORD_RANK_LOOKBACK_DAYS,
        )
        rank_enabled = unit.site_code in TARGET_DATA_SITE_CODES
        # 2026-08-21 方案：爬虫类工具（前台抓取）只对英德美（US/UK/DE）构造请求。
        # 其他站点（CA/MX/TR/FR 等）上游爬虫覆盖不到（实测 100% 空壳），
        # 不再发无效请求；这些站点的爬虫点位走"前台不可得不判"。
        crawler_enabled = unit.site_code in TARGET_DATA_SITE_CODES
        for row in children:
            child_asin = str(row.get("asin") or "").strip().upper()
            seller_sku = str(row.get("sellerSku") or "").strip()
            if not child_asin or not seller_sku:
                continue
            prefix = child_asin
            if crawler_enabled:
                calls[f"child_detail:{prefix}"] = (
                    EXTENSION_TOOL_NAMES["child_detail"],
                    {"asin": child_asin, "siteCode": site_code, "includeReviews": "false"},
                )
                calls[f"price_promotion:{prefix}"] = (
                    EXTENSION_TOOL_NAMES["price_promotion"],
                    {"asin": child_asin, "siteCode": site_code},
                )
                if "recent_reviews" not in DISABLED_EXTENSION_TOOLS:
                    calls[f"recent_reviews:{prefix}"] = (
                        EXTENSION_TOOL_NAMES["recent_reviews"],
                        {"asin": child_asin, "siteCode": site_code, "days": 14},
                    )
            if self.compliance_enabled:
                calls[f"account_health:{prefix}"] = (
                    EXTENSION_TOOL_NAMES["account_health"],
                    {
                        "shopAccount": unit.shop_account,
                        "asin": child_asin,
                        "startDate": window_start.isoformat(),
                        "endDate": window_end.isoformat(),
                    },
                )
            keyword = str(row.get("genericKeyword") or "").strip().split(",")[0].strip()
            # 临时开关：keyword_rank_enabled=false 时不采集卡位数据
            if self.keyword_rank_enabled and keyword and rank_enabled:
                calls[f"keyword_rank:{prefix}"] = (
                    EXTENSION_TOOL_NAMES["keyword_rank"],
                    {
                        "asin": child_asin,
                        "keyword": keyword,
                        "siteCode": site_code,
                        "startDate": keyword_start.isoformat(),
                        "endDate": keyword_end.isoformat(),
                    },
                )
        return calls

    async def collect(
        self,
        unit: OperatingUnitBinding,
        as_of: date,
        *,
        calls: dict[str, tuple[str, dict[str, Any]]] | None = None,
    ) -> dict[str, McpToolResult | Exception]:
        calls = calls or self.build_calls(unit, as_of)

        async def run(
            key: str,
            tool_name: str,
            arguments: dict[str, Any],
        ) -> tuple[str, McpToolResult | Exception]:
            try:
                if key in SUPPLEMENTAL_TOOL_NAMES:
                    if self.supplemental_adapter is None:
                        raise RuntimeError("supplemental MCP adapter is not configured")
                    batch = await self.supplemental_adapter.collect(tool_name, arguments)
                    return key, McpToolResult(
                        tool_name=tool_name,
                        data=batch.rows,
                        raw={
                            "structuredContent": {"data": batch.rows},
                            "sourceContentHash": batch.source_content_hash,
                            "mappedContentHash": batch.mapped_content_hash,
                        },
                        request_hash=batch.request_hash,
                        latency_ms=batch.latency_ms,
                        warning=batch.warning,
                        fetched_at=batch.fetched_at,
                    )
                override = (
                    TOOL_CALL_OVERRIDES.get(key)
                    or TOOL_CALL_OVERRIDES.get(key.split(":", 1)[0], {})
                )
                return key, await self.client.call_tool(
                    tool_name, arguments, **override
                )
            except Exception as exc:  # 采集器不做业务判断，原样上交
                logger.warning(
                    "fact_collect failed unit=%s key=%s tool=%s error=%s",
                    unit.operating_unit_id, key, tool_name, exc,
                )
                return key, exc

        results = await asyncio.gather(
            *[
                run(key, tool_name, arguments)
                for key, (tool_name, arguments) in calls.items()
            ]
        )
        collected = dict(results)

        # product_info 返回了行但子体 asin 全空 → 可能是 ERP 瞬时数据未同步，
        # 等待 1s 后重试一次，避免因 ERP 同步延迟产生"五点数据缺失"误报。
        product_info_result = collected.get("product_info")
        if isinstance(product_info_result, McpToolResult) and product_info_result.data:
            valid = [r for r in product_info_result.data if r.get("asin")]
            if not valid and "product_info" in calls:
                tool_name, arguments = calls["product_info"]
                logger.warning(
                    "product_info all %d children have null asin, retrying once",
                    len(product_info_result.data),
                )
                await asyncio.sleep(1)
                retry_key, retry_result = await run("product_info", tool_name, arguments)
                if isinstance(retry_result, McpToolResult):
                    retry_valid = [r for r in retry_result.data if r.get("asin")]
                    logger.info(
                        "product_info retry: %d children, %d valid asin",
                        len(retry_result.data), len(retry_valid),
                    )
                    collected["product_info"] = retry_result

        failed_core = sorted(
            key for key in CORE_KEYS if isinstance(collected.get(key), Exception)
        )
        if failed_core:
            logger.error(
                "core facts failed unit=%s keys=%s", unit.operating_unit_id, failed_core,
            )
        return collected

    @staticmethod
    def window_for(as_of: date, lookback_days: int) -> tuple[date, date]:
        """返回截至巡检运行日前一天（含）的完整自然日窗口。"""
        if lookback_days < 1:
            raise ValueError("lookback_days must be positive")
        window_end = as_of - timedelta(days=1)
        return window_end - timedelta(days=lookback_days - 1), window_end
