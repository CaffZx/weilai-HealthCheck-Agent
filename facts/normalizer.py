"""原始 MCP 数据 → 统一事实结构。

改造方案 v1.0 §4（facts/normalizer.py）。

【归一化目标形状】
  八个业务域：identity / sales / traffic / profit / inventory / price / quality /
  execution_history。域名和字段名是**业务语言**，不是表名或接口名。

【三条硬规则】
  1. 取不到的字段一律留 None，并登记 unresolved —— 绝不用 0、空串或旧标签兜底。
  2. 每个字段记录它实际命中的原始 key（field_lineage），出问题能一眼看到血缘。
  3. 金额单位显式标注。natural_ad_flow 的金额是 CNY，与站点结算币种不同，
     **不参与利润判定**，只用于流量结构比例。
"""
from __future__ import annotations

import json
import logging
import re
from calendar import monthrange
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from clients.azlisting_contract import (
    AZLISTING_EXTERNAL_CONTRACT_STATUS,
    AZLISTING_INTERNAL_CONTRACT_VERSION,
)
from clients.mcp_client import McpToolResult
from core.contracts import SourceRef
from core.enums import DataQualityStatus
from core.operating_unit import OperatingUnitBinding, canonical_site_code
from facts.collector import (
    DEFAULT_DISABLED_FACT_KEYS,
    SUPPLEMENTAL_TOOL_NAMES,
    TOOL_NAMES,
    TARGET_DATA_SITE_CODES,
    site_scoped_disabled_fact_keys,
    FactCollector,
)
from facts.contract_validator import (
    is_amazon_found_row,
    is_obsolete_product_duplicate,
    require_fact_contract,
)
from integrations.pangolinfo_product import is_amazon_product_image_url

logger = logging.getLogger(__name__)

#: 子体重要性别名（沿用既有 R2/R3 规则口径，不得擅改）
IMPORTANCE_ALIASES: dict[str, str] = {
    "PrimaryColor": "主要色",
    "SecondaryColor": "次要色",
    "LongTailColor": "长尾色",
    "主要色": "主要色",
    "次要色": "次要色",
    "长尾色": "长尾色",
}

SELLABLE_STATUSES: frozenset[str] = frozenset({"可售", "在售", "正常", "在售中"})


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    text = "".join(character for character in text if character in "+-.0123456789")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


def _ratio(value: Any, *, numerator: Any, denominator: Any) -> float | None:
    explicit = _to_float(value)
    if explicit is not None:
        return explicit / 100 if isinstance(value, str) and "%" in value else explicit
    top = _to_float(numerator)
    bottom = _to_float(denominator)
    return top / bottom if top is not None and bottom and bottom > 0 else None


def _target_acos_ratio(value: Any) -> float | None:
    ratio = _ratio(value, numerator=None, denominator=None)
    if ratio is None:
        return None
    return ratio / 100 if abs(ratio) > 1 else ratio


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_status(value: Any) -> str | None:
    """status 归一：把 'None' 字符串脏值当空处理，其余走 _clean_str。"""
    text = _clean_str(value)
    return None if text in ("None", "none", "NONE") else text


def _clean_color(value: Any) -> str | None:
    """颜色归一：去掉 '#颜色码' 前缀污染，如 '#Royal Blue' → 'Royal Blue'。"""
    text = _clean_str(value)
    return text.lstrip("#").strip() or None if text is not None else None


class FieldResolver:
    """按候选 key 依次尝试取值，并记录血缘与未命中项。

    为什么要这样做：ERP MCP 各工具的字段命名不完全统一（中文/camelCase 混用），
    且交接包没有冻结每个工具的响应字段表。硬编码单一 key 一旦对不上就会静默
    变成 None，然后被当成"数据缺口"上报，掩盖真正的问题。显式候选 + 血缘
    让"没配对上"和"确实没有值"可以区分。
    """

    def __init__(self) -> None:
        self.lineage: dict[str, str] = {}
        self.unresolved: list[str] = []

    def pick(
        self,
        source: dict[str, Any] | None,
        field: str,
        candidates: Iterable[str],
        *,
        cast: str = "raw",
    ) -> Any:
        source = source or {}
        for key in candidates:
            if key in source and source[key] is not None:
                self.lineage[field] = key
                raw = source[key]
                if cast == "float":
                    return _to_float(raw)
                if cast == "int":
                    return _to_int(raw)
                if cast == "str":
                    return _clean_str(raw)
                return raw
        self.unresolved.append(field)
        return None


def _rows(result: McpToolResult | Exception | None) -> list[dict[str, Any]]:
    if isinstance(result, McpToolResult):
        return [row for row in result.data if isinstance(row, dict)]
    return []


def _first(result: McpToolResult | Exception | None) -> dict[str, Any]:
    rows = _rows(result)
    return rows[0] if rows else {}


def _prefixed_rows(
    raw: dict[str, McpToolResult | Exception], prefix: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, result in raw.items():
        if key.startswith(f"{prefix}:"):
            rows.extend(_rows(result))
    return rows


def _prefixed_first(
    raw: dict[str, McpToolResult | Exception], prefix: str, child_asin: str
) -> dict[str, Any]:
    row = _first(raw.get(f"{prefix}:{child_asin.upper()}"))
    response_asin = (_clean_str(row.get("asin")) or "").upper()
    return row if not response_asin or response_asin == child_asin.upper() else {}


def _flow_inlet_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the configured traffic-entry child, if the ERP supplies one."""
    return next(
        (
            row for row in rows
            if _clean_str(row.get("flowInlet")) == "是"
            and _clean_str(row.get("asin"))
        ),
        {},
    )


def _extension_evaluated(
    raw: dict[str, McpToolResult | Exception], prefix: str, child_asin: str
) -> bool:
    result = raw.get(f"{prefix}:{child_asin.upper()}")
    if not isinstance(result, McpToolResult):
        return False
    row = _first(result)
    response_asin = (_clean_str(row.get("asin")) or "").upper()
    return not response_asin or response_asin == child_asin.upper()


NEGATIVE_REVIEW_CATEGORIES: dict[str, tuple[str, ...]] = {
    "尺码": ("size", "sizing", "fit", "small", "large", "tight", "loose", "尺码", "偏小", "偏大"),
    "质量": ("quality", "broken", "cheap", "tear", "defect", "poor", "质量", "破损", "瑕疵", "掉色"),
    "物流": ("delivery", "shipping", "package", "late", "物流", "配送", "包装", "延迟"),
    "描述不符": ("description", "different", "not as", "picture", "color", "描述不符", "色差", "不一致"),
}


def _review_category(content: str) -> str:
    normalized = content.lower()
    scores = Counter({
        category: sum(keyword in normalized for keyword in keywords)
        for category, keywords in NEGATIVE_REVIEW_CATEGORIES.items()
    })
    category, score = scores.most_common(1)[0]
    return category if score else "其他"


def _recent_reviews(rows: list[dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    cutoff = as_of - timedelta(days=13)
    reviews: list[dict[str, Any]] = []
    for row in rows:
        grouped = row.get("reviewsByStar")
        candidates = (
            [item for values in grouped.values() for item in values]
            if isinstance(grouped, dict)
            else row.get("reviews") if isinstance(row.get("reviews"), list)
            else []
        )
        for item in candidates:
            if not isinstance(item, dict):
                continue
            content = _clean_str(
                item.get("content") or item.get("reviewContent") or item.get("review")
            )
            rating = _to_float(item.get("rating") or item.get("star") or item.get("score"))
            reviewed_on = _parse_date(
                item.get("date") or item.get("reviewDate") or item.get("createTime")
            )
            if not content or rating is None or reviewed_on is None:
                continue
            if not cutoff <= reviewed_on <= as_of:
                continue
            reviews.append({
                "date": reviewed_on.isoformat(),
                "rating": rating,
                "category": _review_category(content) if rating <= 3 else None,
                "content": content[:300],
            })
    deduplicated = {
        (item["date"], item["rating"], item["content"]): item for item in reviews
    }
    return sorted(deduplicated.values(), key=lambda item: item["date"], reverse=True)


def _normalized_text(value: Any) -> str | None:
    text = _clean_str(value)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text.lower()) if text else None


def _listing_records(result: McpToolResult | Exception | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for page in _rows(result):
        page_records = page.get("records")
        if isinstance(page_records, list):
            records.extend(record for record in page_records if isinstance(record, dict))
        elif "totalPages" not in page and "totalCount" not in page:
            records.append(page)
    return records


def _business_report_records(
    result: McpToolResult | Exception | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in _rows(result):
        nested = row.get("records")
        if isinstance(nested, list):
            records.extend(item for item in nested if isinstance(item, dict))
        else:
            records.append(row)
    records.sort(
        key=lambda row: str(row.get("reportStartTime") or row.get("statDate") or ""),
        reverse=True,
    )
    return records


def _gross_profit_records(
    result: McpToolResult | Exception | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in _rows(result):
        detail = row.get("detail")
        if isinstance(detail, list):
            records.extend(item for item in detail if isinstance(item, dict))
        elif _parse_date(row.get("recordDate") or row.get("statDate")) is not None:
            records.append(row)
    return records


def _unit_contribution(row: dict[str, Any]) -> float | None:
    legacy = _to_float(
        row.get("unitProfit", row.get("单位毛利", row.get("unit_contribution")))
    )
    if legacy is not None:
        return legacy
    price = _to_float(row.get("productPrice"))
    if price is None:
        return None
    referral = _to_float(row.get("referralFee"))
    product_cost = _to_float(row.get("productCost"))
    fulfillment = _to_float(row.get("fbaPackFee"))
    ad_ratio = _ratio(row.get("adCostRatio"), numerator=None, denominator=None)
    if any(
        component is None
        for component in (referral, product_cost, fulfillment, ad_ratio)
    ):
        return None
    return price - referral - product_cost - fulfillment - price * ad_ratio


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_date(value: Any) -> date | None:
    text = _clean_str(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _current_platform_activities(
    rows: list[dict[str, Any]],
    product_rows: list[dict[str, Any]],
    as_of: date,
) -> list[dict[str, Any]]:
    """Keep activities belonging to this listing and active on ``as_of``."""
    known_skus = {
        sku
        for row in product_rows
        if (sku := (_clean_str(row.get("sellerSku")) or ""))
    }
    active: list[dict[str, Any]] = []
    for row in rows:
        seller_sku = _clean_str(row.get("sellerSku"))
        if known_skus and seller_sku and seller_sku not in known_skus:
            continue

        start_date = _parse_date(row.get("startActivityDate")) or _parse_date(
            row.get("recordDate")
        )
        end_date = _parse_date(row.get("endActivityDate"))
        completion_date = _parse_date(row.get("completionDate"))
        if start_date and start_date > as_of:
            continue
        if end_date and end_date < as_of:
            continue
        if completion_date and completion_date <= as_of:
            continue
        active.append(row)
    return active


def _month_key(value: Any) -> str | None:
    text = _clean_str(value)
    if not text:
        return None
    match = re.search(r"(\d{4})\D*(\d{1,2})", text)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if month < 1 or month > 12:
        return None
    return f"{year:04d}-{month:02d}"


def _month_offset(month_key: str, offset: int) -> str:
    year, month = (int(part) for part in month_key.split("-", 1))
    absolute_month = year * 12 + month - 1 + offset
    return f"{absolute_month // 12:04d}-{absolute_month % 12 + 1:02d}"


def _monthly_goal_rows(result: McpToolResult | Exception | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in _rows(result):
        nested = row.get("monthlyGoals")
        if isinstance(nested, list):
            rows.extend(item for item in nested if isinstance(item, dict))
        else:
            rows.append(row)
    return rows


def _join_warning(current: str | None, addition: str) -> str:
    return f"{current}; {addition}" if current else addition


def _parse_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_category(value: Any) -> dict[str, Any]:
    parsed = value
    if isinstance(value, str) and value.strip().startswith(("[", "{")):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = value
    if isinstance(parsed, list):
        parsed = parsed[-1] if parsed else None
    if isinstance(parsed, dict):
        return {
            "id": _clean_str(
                parsed.get("classificationId")
                or parsed.get("categoryId")
                or parsed.get("id")
            ),
            "name": _clean_str(
                parsed.get("title")
                or parsed.get("categoryName")
                or parsed.get("name")
            ),
            "rank": _to_int(
                parsed.get("rank")
                or parsed.get("categoryRank")
                or parsed.get("ranking")
            ),
        }
    return {"id": None, "name": _clean_str(parsed), "rank": None}


def _natural_order_series(
    sales_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    dated_rows = [row for row in sales_rows if _parse_date(row.get("stat_date"))]
    if not dated_rows:
        return [], "NO_DATED_SALES_ROWS"
    dates = [row["stat_date"] for row in dated_rows]
    if len(dates) != len(set(dates)):
        return [], "DUPLICATE_STAT_DATE"

    series: list[dict[str, Any]] = []
    for row in dated_rows:
        total_orders = _to_int(row.get("orders"))
        ad_orders = _to_int(row.get("ad_orders"))
        if total_orders is None or ad_orders is None:
            return [], "ORDER_COMPONENT_MISSING"
        if total_orders < 0 or ad_orders < 0 or ad_orders > total_orders:
            return [], "ORDER_COMPONENT_INCONSISTENT"
        natural_orders = total_orders - ad_orders
        series.append({
            "stat_date": row["stat_date"],
            "total_orders": total_orders,
            "ad_orders": ad_orders,
            "natural_orders": natural_orders,
            "natural_ratio": natural_orders / total_orders if total_orders > 0 else None,
        })
    return series, None


def _period_natural_ratio(rows: list[dict[str, Any]]) -> float | None:
    total_orders = sum(row["total_orders"] for row in rows)
    natural_orders = sum(row["natural_orders"] for row in rows)
    return natural_orders / total_orders if total_orders > 0 else None


def _mean(values: list[float]) -> float | None:
    usable = [v for v in values if v is not None]
    return sum(usable) / len(usable) if usable else None


def _consecutive_below(series: list[float | None], baseline: float | None) -> int:
    """从最近一天往回数，连续低于基线的天数。baseline 为 None 时返回 0。"""
    if baseline is None:
        return 0
    count = 0
    for value in series:
        if value is None or value >= baseline:
            break
        count += 1
    return count


def _consecutive_above(series: list[float | None], baseline: float | None) -> int:
    if baseline is None:
        return 0
    count = 0
    for value in series:
        if value is None or value <= baseline:
            break
        count += 1
    return count


class FactNormalizer:
    """把 collector 的原始结果转成统一事实结构 + 溯源记录。"""

    def __init__(
        self,
        *,
        lookback_days: int = 30,
        sales_stale_days: int = 4,
        disabled_fact_keys: frozenset[str] = DEFAULT_DISABLED_FACT_KEYS,
    ) -> None:
        self.lookback_days = lookback_days
        self.sales_stale_days = sales_stale_days
        self.disabled_fact_keys = disabled_fact_keys

    # -- 入口 --------------------------------------------------------------
    def normalize(
        self,
        unit: OperatingUnitBinding,
        raw: dict[str, McpToolResult | Exception],
        *,
        as_of: date | None = None,
    ) -> tuple[dict[str, Any], list[SourceRef]]:
        inspection_date = as_of or date.today()
        window_start, data_as_of = FactCollector.window_for(
            inspection_date,
            self.lookback_days,
        )
        require_fact_contract(unit, raw)
        resolver = FieldResolver()

        effective_disabled_fact_keys = site_scoped_disabled_fact_keys(
            unit.site_code, self.disabled_fact_keys
        )
        target_data_enabled = unit.site_code in TARGET_DATA_SITE_CODES
        identity = self._identity(unit, raw, resolver, target_data_enabled)
        sales = self._sales(
            raw, resolver, data_as_of,
            monthly_goal_enabled="monthly_goal" not in effective_disabled_fact_keys,
        )
        traffic = self._traffic(
            raw, resolver, window_start, data_as_of, sales, identity,
            keyword_rank_enabled="keyword_rank" not in effective_disabled_fact_keys,
        )
        profit = self._profit(
            raw, resolver, sales,
            advert_target_enabled="advert_config" not in effective_disabled_fact_keys,
        )
        inventory = self._inventory(raw, resolver, sales, identity)
        price = self._price(raw, resolver, inspection_date)
        quality = self._quality(
            raw, resolver, data_as_of, target_data_enabled=target_data_enabled
        )

        normalized: dict[str, Any] = {
            "identity": identity,
            "sales": sales,
            "traffic": traffic,
            "profit": profit,
            "inventory": inventory,
            "price": price,
            "quality": quality,
            "execution_history": {
                "last_patrol_run_id": None,
                "open_signal_codes": [],
                "recent_actions": [],
            },
            "_meta": {
                "mcp_input_contract_version": AZLISTING_INTERNAL_CONTRACT_VERSION,
                "mcp_external_contract_status": (
                    AZLISTING_EXTERNAL_CONTRACT_STATUS.value
                ),
                "as_of": data_as_of.isoformat(),
                "inspection_date": inspection_date.isoformat(),
                "window_start": window_start.isoformat(),
                "window_end": data_as_of.isoformat(),
                "lookback_days": self.lookback_days,
                "disabled_fact_keys": sorted(effective_disabled_fact_keys),
                "field_lineage": resolver.lineage,
                "unresolved_fields": sorted(set(resolver.unresolved)),
                "currency_notes": {
                    **(
                        {}
                        if "natural_ad_flow" in self.disabled_fact_keys
                        else {
                            "natural_ad_flow": (
                                "CNY，仅用于流量结构比例，不参与利润判定"
                            )
                        }
                    ),
                    "gross_profit": "站点结算币种，参与利润判定",
                },
            },
        }
        return normalized, self._source_refs(raw, window_start, data_as_of)

    # -- 溯源 --------------------------------------------------------------
    def _source_refs(
        self,
        raw: dict[str, McpToolResult | Exception],
        window_start: date,
        window_end: date,
    ) -> list[SourceRef]:
        refs: list[SourceRef] = []
        for key, result in raw.items():
            tool_name = (
                result.tool_name
                if isinstance(result, McpToolResult)
                else TOOL_NAMES.get(
                    key,
                    SUPPLEMENTAL_TOOL_NAMES.get(key, key.split(":", 1)[0]),
                )
            )
            if isinstance(result, McpToolResult):
                actual_window_start = window_start
                actual_window_end = window_end
                warning = result.warning or ("empty result" if result.is_empty else None)
                if key == "gross_profit" and not result.is_empty:
                    dates = sorted(
                        parsed
                        for row in _gross_profit_records(result)
                        if (parsed := _parse_date(row.get("recordDate") or row.get("statDate")))
                    )
                    if dates:
                        actual_window_start = dates[0]
                        actual_window_end = dates[-1]
                    else:
                        warning = _join_warning(warning, "source data cutoff unavailable")
                refs.append(SourceRef(
                    tool_name=tool_name,
                    request_hash=result.request_hash,
                    fetched_at=result.fetched_at or datetime.now(UTC),
                    window_start=(
                        actual_window_start
                        if key in {"gross_profit", "natural_ad_flow"}
                        else None
                    ),
                    window_end=(
                        actual_window_end
                        if key in {"gross_profit", "natural_ad_flow"}
                        else None
                    ),
                    content_hash=result.content_hash,
                    quality_status=(
                        DataQualityStatus.PARTIAL if result.is_empty else DataQualityStatus.COMPLETE
                    ),
                    latency_ms=result.latency_ms,
                    warning=warning,
                ))
            elif isinstance(result, Exception):
                refs.append(SourceRef(
                    tool_name=tool_name,
                    request_hash="",
                    fetched_at=datetime.now(UTC),
                    content_hash="",
                    quality_status=DataQualityStatus.FAILED,
                    warning=f"{type(result).__name__}: {result}",
                ))
        return refs

    # -- 身份 --------------------------------------------------------------
    def _identity(
        self,
        unit: OperatingUnitBinding,
        raw: dict[str, McpToolResult | Exception],
        resolver: FieldResolver,
        target_data_enabled: bool = True,
    ) -> dict[str, Any]:
        children_rows = _rows(raw.get("product_info"))
        real_child_asins = {
            (_clean_str(row.get("asin") or row.get("ASIN")) or "").upper()
            for row in children_rows
            if (_clean_str(row.get("asin") or row.get("ASIN")) or "")
            and not (_clean_str(row.get("asin") or row.get("ASIN")) or "").upper().startswith(
                "AMAZON.FOUND."
            )
        }
        children_rows = [
            row
            for row in children_rows
            if not is_amazon_found_row(row, real_child_asins)
            and not is_obsolete_product_duplicate(row, children_rows)
        ]
        listing_row = _first(raw.get("listing_identity"))
        flow_inlet_row = _flow_inlet_row(children_rows)
        flow_inlet_asin = (_clean_str(flow_inlet_row.get("asin")) or "").upper() or None
        flow_inlet_seller_sku = _clean_str(flow_inlet_row.get("sellerSku"))
        flow_inlet_detail = (
            _prefixed_first(raw, "child_detail", flow_inlet_asin)
            if flow_inlet_asin else {}
        )
        # 父 ASIN 没有详情页是亚马逊变体的正常语义。前台字段应以流量入口
        # 子体为准；仅在未配置入口或入口详情为空时回退父体响应。
        storefront_row = flow_inlet_detail or listing_row
        parent_extension = _first(raw.get("parent_extension"))
        bonus = _mapping(storefront_row.get("bonus"))
        link_status = _mapping(storefront_row.get("linkStatus"))
        images = _mapping(storefront_row.get("images"))
        aplus = _mapping(storefront_row.get("aplus"))
        reviews = _mapping(storefront_row.get("reviews"))
        storefront_main_image_url = _clean_str(images.get("main"))
        product_image_rows = [
            row
            for row in children_rows
            if is_amazon_product_image_url(row.get("picUrl"))
        ]
        preferred_product_image = next(
            (
                _clean_str(row.get("picUrl"))
                for row in product_image_rows
                if _clean_str(row.get("flowInlet")) == "是"
            ),
            None,
        )
        storefront_main_image_url = (
            preferred_product_image
            or (_clean_str(images.get("main")) if flow_inlet_detail else None)
        )
        owner_users: dict[int, str | None] = {}
        for row in children_rows:
            try:
                principal_user_id = int(row.get("principalUserId"))
            except (TypeError, ValueError):
                continue
            if principal_user_id > 0:
                owner_users[principal_user_id] = _clean_str(row.get("principalUserName"))
        owner_source = "product_info" if owner_users else None
        if not owner_users and unit.owner_user_ids:
            owner_users = {user_id: None for user_id in unit.owner_user_ids}
            owner_source = "operating_unit_catalog_fallback"
        seller_ids: set[str] = set()
        for row in children_rows:
            row_shop_id = row.get("shopId", row.get("SHOP_ID"))
            row_site_code = row.get(
                "siteCode",
                row.get("PL_SITE_CODE", row.get("plSiteCode")),
            )
            if row_shop_id is not None:
                try:
                    if int(row_shop_id) != unit.shop_id:
                        continue
                except (TypeError, ValueError):
                    continue
            if row_site_code is not None:
                if canonical_site_code(row_site_code) != unit.site_code:
                    continue
            legacy_asin = _clean_str(row.get("asin"))
            extension_asin = _clean_str(row.get("ASIN"))
            if legacy_asin and extension_asin and legacy_asin.upper() != extension_asin.upper():
                continue
            legacy_sku = _clean_str(row.get("sellerSku"))
            extension_sku = _clean_str(row.get("SELLER_SKU"))
            if legacy_sku and extension_sku and legacy_sku != extension_sku:
                continue
            seller_id = _clean_str(row.get("SELLING_PARTNER_ID") or row.get("sellingPartnerId") or row.get("selling_partner_id"))
            if seller_id:
                seller_ids.add(seller_id)
        if len(seller_ids) == 1:
            own_seller_id = next(iter(seller_ids))
            resolver.lineage["identity.own_seller_id"] = (
                "product_info.SELLING_PARTNER_ID"
            )
        else:
            own_seller_id = None
            if len(seller_ids) > 1:
                resolver.unresolved.append("identity.own_seller_id.conflict")

        children: list[dict[str, Any]] = []
        for row in children_rows:
            child_asin = _clean_str(row.get("asin"))
            if not child_asin:
                continue
            bullets = [
                _clean_str(row.get(f"fiveBulletPoint{i}")) for i in range(1, 6)
            ]
            theme = (_clean_str(row.get("variationThemeName")) or "").upper()
            first_category = _parse_category(row.get("firstCategory"))
            last_category = _parse_category(row.get("lastCategory"))
            child_asin = child_asin.upper()
            child_detail = _prefixed_first(raw, "child_detail", child_asin)
            child_link = _mapping(child_detail.get("linkStatus"))
            child_images = _mapping(child_detail.get("images"))
            child_aplus = _mapping(child_detail.get("aplus"))
            child_bonus = _mapping(child_detail.get("bonus"))
            # P1-1 修复：child_detail 返回前台五点（features 字段）。
            # 仅在响应为 list 时提取；空响应/缺字段 → None（未采集，判定侧不判）。
            # 实测 features 位置不固定：有的在顶层，有的在 aplus.features（如 B0EXAMPLE0）——
            # 顶层取不到时补取 aplus.features，避免真实五点被漏判为"前台未采集"。
            child_features = child_detail.get("features")
            if not isinstance(child_features, list):
                child_features = (
                    child_aplus.get("features")
                    if isinstance(child_aplus.get("features"), list)
                    else None
                )
            price_promotion = _prefixed_first(raw, "price_promotion", child_asin)
            frontend_category = _parse_category({
                "categoryId": price_promotion.get("categoryId"),
                "categoryName": price_promotion.get("categoryName"),
            })
            health_row = _prefixed_first(raw, "account_health", child_asin)
            health_evaluated = _extension_evaluated(
                raw, "account_health", child_asin
            )
            health_issues = [
                issue for issue in health_row.get("issues", []) if isinstance(issue, dict)
            ]
            detail_parent = (_clean_str(child_bonus.get("parentAsin")) or "").upper() or None
            children.append({
                "child_asin": child_asin,
                "seller_sku": _clean_str(row.get("sellerSku")),
                "status": _clean_status(row.get("status")),
                "sellable": _clean_status(row.get("status")) in SELLABLE_STATUSES,
                "title": _clean_str(row.get("productName")),
                "bullet_points": bullets,
                "bullet_count": sum(1 for b in bullets if b),
                "importance": (
                    IMPORTANCE_ALIASES.get(row.get("finenessDesc"))
                    or IMPORTANCE_ALIASES.get(row.get("fineness"))
                ),
                "parent_asin": (_clean_str(row.get("parentAsin")) or "").upper() or None,
                "variation_theme": theme or None,
                "color": _clean_color(row.get("productColor")),
                "size": _clean_str(row.get("productSize")),
                "price": _to_float(row.get("productPrice")),
                "first_category_id": first_category["id"],
                "first_category_name": first_category["name"],
                "first_category_rank": first_category["rank"],
                "category_id": last_category["id"],
                "category_name": last_category["name"],
                "category_rank": last_category["rank"],
                "target_star_rating": (
                    _to_float(row.get("targetStarRate")) if target_data_enabled else None
                ),
                "short_term_goal": _clean_str(row.get("shortTermGoal")),
                "goal_achievement_rate": _ratio(
                    row.get("achievesScale"), numerator=None, denominator=None
                ),
                "generic_keyword": _clean_str(row.get("genericKeyword")),
                "principal_user_id": (
                    int(row["principalUserId"])
                    if str(row.get("principalUserId") or "").isdigit()
                    and int(row["principalUserId"]) > 0
                    else None
                ),
                "principal_user_name": _clean_str(row.get("principalUserName")),
                "frontend": {
                    "evaluated": _extension_evaluated(
                        raw, "child_detail", child_asin
                    ),
                    "in_stock": child_link.get("inStock"),
                    "has_cart": child_link.get("hasCart"),
                    "buy_box_owner": _clean_str(child_link.get("buyBoxOwner")),
                    "buy_box_seller_id": _clean_str(child_link.get("buyBoxSellerId")),
                    "buy_box_type": _clean_str(child_link.get("buyBoxType")),
                    "buy_box_owned": (
                        _clean_str(child_link.get("buyBoxSellerId")) == own_seller_id
                        if _clean_str(child_link.get("buyBoxSellerId")) and own_seller_id
                        else None
                    ),
                    # 前台不可得就不判：去掉 ERP picUrl 兜底，主图严格用爬虫前台图
                    # （child_detail images.main），避免爬虫空响应时用 ERP 图冒充前台主图，
                    # 与五点/标题/属性的"前台不可得不判"口径一致。
                    "main_image_url": _clean_str(child_images.get("main")),
                    "gallery_count": (
                        len(child_images.get("all"))
                        if isinstance(child_images.get("all"), list) else None
                    ),
                    "aplus_present": child_aplus.get("hasAplus"),
                    "parent_asin": detail_parent,
                    "category_id": frontend_category["id"],
                    "category_name": frontend_category["name"],
                    "breadcrumbs": _clean_str(
                        price_promotion.get("breadCrumbs")
                        or child_bonus.get("breadCrumbs")
                    ),
                    "attributes": {
                        "color": _clean_str(child_bonus.get("color")),
                        "size": _clean_str(child_bonus.get("size")),
                    },
                    # 前台标题（爬虫 bonus.title）——标题异常判定用，前台不可得时不判
                    "title": _clean_str(child_bonus.get("title")),
                    # P1-1 修复：前台五点（child_detail.features）。
                    # None = 前台未采集（判定侧回退 ERP 兜底）；[] = 前台采集到 0 条。
                    "bullet_points": (
                        [
                            _clean_str(item) for item in child_features
                            if isinstance(item, str)
                        ]
                        if isinstance(child_features, list)
                        else None
                    ),
                },
                "price_promotion": {
                    "evaluated": _extension_evaluated(
                        raw, "price_promotion", child_asin
                    ),
                    "price": _to_float(price_promotion.get("price")),
                    "strikethrough_price": _to_float(
                        price_promotion.get("strikethroughPrice")
                    ),
                    "coupon": _clean_str(price_promotion.get("coupon")),
                    "savings_percentage": _ratio(
                        price_promotion.get("savingsPercentage"),
                        numerator=None,
                        denominator=None,
                    ),
                    "promotions": _clean_str(price_promotion.get("promotions")),
                    "promotion_summary": _clean_str(
                        price_promotion.get("promotionSummary")
                    ),
                    "discount_types": _clean_str(price_promotion.get("discountTypes")),
                },
                "compliance_issues": health_issues,
                "compliance_evaluated": health_evaluated,
            })

        product_row = (
            flow_inlet_row if _clean_str(flow_inlet_row.get("productName")) else next(
                (row for row in children_rows if _clean_str(row.get("productName"))), {}
            )
        )

        return {
            "operating_unit_id": unit.operating_unit_id,
            "parent_asin": unit.parent_asin,
            "parent_seller_sku": unit.parent_seller_sku,
            "flow_inlet_child_asin": flow_inlet_asin,
            "flow_inlet_seller_sku": flow_inlet_seller_sku,
            "shop_id": unit.shop_id,
            "shop_account": unit.shop_account,
            "site_code": unit.site_code,
            "own_seller_id": own_seller_id,
            "owner_user_ids": sorted(owner_users),
            "owner_users": [
                {"user_id": user_id, "user_name": owner_users[user_id]}
                for user_id in sorted(owner_users)
            ],
            "owner_source": owner_source,
            "product_name": resolver.pick(
                {**product_row, **listing_row, **bonus}, "identity.product_name",
                ("title", "productName", "商品名称", "标题"), cast="str",
            ),
            "variation_theme": next(
                (c["variation_theme"] for c in children if c["variation_theme"]), None,
            ),
            "children": children,
            "child_count": len(children),
            "sellable_child_count": sum(1 for c in children if c["sellable"]),
            "storefront": {
                "source_asin": (
                    flow_inlet_asin
                    or (_clean_str(listing_row.get("asin")) or unit.parent_asin).upper()
                ),
                "in_stock": link_status.get("inStock"),
                "has_cart": link_status.get("hasCart"),
                "price": _clean_str(link_status.get("price")),
                "strikethrough_price": _clean_str(link_status.get("strikethroughPrice")),
                "coupon": _clean_str(link_status.get("coupon")),
                "buy_box_owner": _clean_str(link_status.get("buyBoxOwner")),
                "buy_box_seller_id": _clean_str(link_status.get("buyBoxSellerId")),
                "main_image_url": storefront_main_image_url,
                "main_image_source": (
                    "erp_listing_product_info.picUrl" if preferred_product_image
                    else ("erp_asin_full_detail:flow_inlet" if flow_inlet_detail else None)
                ),
                "main_image_fetched_at": None,
                "main_image_cached": False,
                "gallery_count": len(images.get("all")) if isinstance(images.get("all"), list) else None,
                "aplus_present": aplus.get("hasAplus"),
                "aplus_block_count": (
                    len(aplus.get("descriptionBlocks"))
                    if isinstance(aplus.get("descriptionBlocks"), list)
                    else None
                ),
                "star_rating": _to_float(bonus.get("star")),
                "review_count": _to_int(bonus.get("ratingsNum")),
                "recent_low_star_count": _to_int(reviews.get("lowStarRecentCount")),
            },
            "parent_extension": {
                "progress_preparation_code": resolver.pick(
                    parent_extension,
                    "identity.parent_extension.progress_preparation_code",
                    ("progressPreparationCode", "progress_preparation_code"),
                    cast="str",
                ),
                "progress_preparation": resolver.pick(
                    parent_extension,
                    "identity.parent_extension.progress_preparation",
                    ("progressPreparation", "progress_preparation"),
                    cast="str",
                ),
                "seasonality_code": resolver.pick(
                    parent_extension,
                    "identity.parent_extension.seasonality_code",
                    ("seasonalityCode", "seasonality_code"),
                    cast="str",
                ),
                "seasonality": resolver.pick(
                    parent_extension,
                    "identity.parent_extension.seasonality",
                    ("seasonality", "seasonalityName"),
                    cast="str",
                ),
            },
        }

    # -- 销量 --------------------------------------------------------------
    def _sales(
        self,
        raw: dict[str, McpToolResult | Exception],
        resolver: FieldResolver,
        as_of: date,
        *,
        monthly_goal_enabled: bool = True,
    ) -> dict[str, Any]:
        rows = _gross_profit_records(raw.get("gross_profit"))
        daily: list[dict[str, Any]] = []
        data_cutoff = as_of
        rejected_future_rows = 0
        for row in rows:
            stat_date = (
                _clean_str(row.get("statDate"))
                or _clean_str(row.get("recordDate"))
                or _clean_str(row.get("stat_date"))
                or _clean_str(row.get("日期"))
                or _clean_str(row.get("date"))
            )
            parsed_stat_date = _parse_date(stat_date)
            if parsed_stat_date is not None and parsed_stat_date > data_cutoff:
                rejected_future_rows += 1
                continue
            daily.append({
                "stat_date": parsed_stat_date.isoformat() if parsed_stat_date else None,
                "orders": _to_int(row.get("全部单量") if "全部单量" in row else row.get("orderNum")),
                "units": _to_int(
                    row.get("全部销量")
                    if "全部销量" in row
                    else row.get("productSaleNum", row.get("saleNum"))
                ),
                "revenue": _to_float(
                    row.get("全部销售额")
                    if "全部销售额" in row
                    else row.get("orderSaleAmount", row.get("saleAmount"))
                ),
                "ad_cost": _to_float(
                    row.get("广告花费")
                    if "广告花费" in row
                    else row.get("adCostAmount", row.get("adCost"))
                ),
                "ad_sales": _to_float(
                    row.get("广告销售额")
                    if "广告销售额" in row
                    else row.get("adSaleMoney", row.get("adSaleAmount"))
                ),
                "ad_orders": _to_int(
                    row.get("广告订单数")
                    if "广告订单数" in row
                    else row.get("广告单量", row.get("adOrderNum"))
                ),
                "ad_click": _to_int(row.get("adClick", row.get("广告点击量"))),
                "ad_impressions": _to_float(
                    row.get("adImpressions", row.get("广告曝光量"))
                ),
                "acos": _ratio(
                    row.get("ACOS", row.get("acos")),
                    numerator=row.get("adCostAmount", row.get("adCost")),
                    denominator=row.get("adSaleMoney", row.get("adSaleAmount")),
                ),
                "gross_margin": _to_float(
                    row.get("毛利率")
                    if "毛利率" in row
                    else row.get("grossProfitAmountProportion", row.get("grossMargin"))
                ),
                "ad_zero_sale": bool(
                    (_to_float(row.get("adCostAmount", row.get("adCost"))) or 0) > 0
                    and (_to_float(row.get("adSaleMoney", row.get("adSaleAmount"))) or 0) == 0
                ),
                "gross_profit_amount": _to_float(row.get("grossProfitAmount")),
                "ad_sale_num": _to_int(row.get("adSaleNum")),
            })
        if rejected_future_rows:
            resolver.unresolved.append("sales.rows_after_data_cutoff")

        # 按日期倒序（最新在前），与旧 query_parent_daily_sales 的口径一致。
        dated = [r for r in daily if r["stat_date"]]
        dated.sort(key=lambda r: r["stat_date"], reverse=True)
        undated = [r for r in daily if not r["stat_date"]]
        ordered = dated or undated

        units = [_to_float(r["units"]) for r in ordered]
        orders = [_to_float(r["orders"]) for r in ordered]

        avg_3d = _mean([v for v in units[:3] if v is not None])
        avg_7d = _mean([v for v in units[:7] if v is not None])
        avg_30d = _mean([v for v in units[:30] if v is not None])

        latest_date = ordered[0]["stat_date"] if ordered else None
        data_age_days: int | None = None
        if latest_date:
            try:
                data_age_days = (as_of - date.fromisoformat(latest_date)).days
            except ValueError:
                data_age_days = None

        if data_age_days is None:
            freshness = "NO_DATA"
        elif data_age_days <= 2:
            freshness = "FRESH"
        elif data_age_days <= 3:
            freshness = "DELAYED"
        else:
            freshness = "STALE"

        if not ordered:
            resolver.unresolved.append("sales.daily_rows")

        current_month = f"{as_of.year:04d}-{as_of.month:02d}"
        goals = []
        for row in (
            _monthly_goal_rows(raw.get("monthly_goal")) if monthly_goal_enabled else []
        ):
            month = _month_key(row.get("everyMonthStr") or row.get("month"))
            target = _to_float(
                row.get("estimatedMonthOrderNum", row.get("当月目标销量"))
            )
            if month and target is not None:
                goals.append({
                    "month": month,
                    "target_orders": target,
                    "source": "erp_listing_monthly_goal",
                })
        goal_by_month = {row["month"]: row for row in goals}
        current_target = (goal_by_month.get(current_month) or {}).get("target_orders")
        month_completed_orders = sum(
            float(row["orders"])
            for row in ordered
            if row.get("orders") is not None
            and str(row.get("stat_date") or "").startswith(current_month)
        )
        days_in_month = monthrange(as_of.year, as_of.month)[1]
        daily_target = (
            current_target / days_in_month
            if current_target is not None and current_target > 0
            else None
        )
        monthly_completion = (
            month_completed_orders / current_target
            if current_target is not None and current_target > 0
            else None
        )

        return {
            "daily_rows": ordered,
            "row_count": len(ordered),
            "avg_daily_units_3d": avg_3d,
            "avg_daily_units_7d": avg_7d,
            "avg_daily_units_30d": avg_30d,
            "daily_units_7d": [v for v in units[:7] if v is not None],
            "daily_orders_7d": [v for v in orders[:7] if v is not None],
            "avg_daily_orders_3d": _mean([v for v in orders[:3] if v is not None]),
            "consecutive_decline_days": _consecutive_below(units[:7], avg_7d),
            "latest_stat_date": latest_date,
            "data_age_days": data_age_days,
            "freshness": freshness,
            "effective_days": sum(1 for v in units if v is not None),
            "monthly_goals": goals,
            "current_month": current_month,
            "monthly_target": current_target,
            "monthly_target_source": (
                (goal_by_month.get(current_month) or {}).get("source")
            ),
            "daily_target_orders": daily_target,
            "month_completed_orders": month_completed_orders,
            "monthly_completion_rate": monthly_completion,
            "month_time_progress": as_of.day / days_in_month,
        }

    # -- 流量 --------------------------------------------------------------
    def _traffic(
        self,
        raw: dict[str, McpToolResult | Exception],
        resolver: FieldResolver,
        window_start: date,
        as_of: date,
        sales: dict[str, Any],
        identity: dict[str, Any],
        *,
        keyword_rank_enabled: bool = True,
    ) -> dict[str, Any]:
        sequence_enabled = "natural_ad_flow" not in self.disabled_fact_keys
        flow_rows = _rows(raw.get("natural_ad_flow")) if sequence_enabled else []

        # 该工具返回窗口聚合行，不含日期。不能把行序号伪装成逐日时间序列。
        expected_days = (as_of - window_start).days + 1
        flow_inlet_asin = _clean_str(identity.get("flow_inlet_child_asin"))
        flow_inlet_seller_sku = _clean_str(identity.get("flow_inlet_seller_sku"))
        if flow_inlet_asin:
            parent_rows = [
                row for row in flow_rows
                if (_clean_str(row.get("asin")) or "").upper() == flow_inlet_asin.upper()
                and (
                    not flow_inlet_seller_sku
                    or not _clean_str(row.get("sellerSku"))
                    or _clean_str(row.get("sellerSku")) == flow_inlet_seller_sku
                )
            ]
        else:
            parent_rows = [
                row for row in flow_rows
                if not _clean_str(row.get("asin"))
                or (_clean_str(row.get("asin")) or "").upper() == (
                    _clean_str(row.get("parentAsin")) or ""
                ).upper()
            ] or flow_rows

        summary = next(
            (row for row in parent_rows if row.get("isSummary") is True),
            parent_rows[0] if len(parent_rows) == 1 else {},
        )
        if not summary and parent_rows:
            # 真实接口常返回多行子体明细，但没有 isSummary 或日期字段。
            # 这些行属于同一回看窗口，按订单分量求和后再计算结构比例；
            # 不把行号解释成天数，也不生成伪逐日趋势。
            summary = {
                key: sum(_to_float(row.get(key)) or 0.0 for row in parent_rows)
                for key in ("totalOrderNum", "naturalOrderNum", "adOrderNum")
            }
        total_orders = _to_float(summary.get("totalOrderNum"))
        natural_orders = _to_float(summary.get("naturalOrderNum"))
        natural_ratio = (
            natural_orders / total_orders
            if total_orders and natural_orders is not None and total_orders > 0
            else None
        )
        yesterday_ad_orders = _to_int(summary.get("adOrderNum"))

        business_rows = _business_report_records(raw.get("business_report"))
        this_week = business_rows[0] if business_rows else {}
        last_week = business_rows[1] if len(business_rows) > 1 else {}
        if sequence_enabled:
            daily_series, derivation_error = _natural_order_series(
                list(sales.get("daily_rows") or [])
            )
        else:
            daily_series, derivation_error = [], "DISABLED_BY_POLICY"
        recent_3d = daily_series[:3]
        recent_7d = daily_series[:7]
        previous_7d = daily_series[7:14]
        rank_series: list[dict[str, Any]] = []
        for row in _prefixed_rows(raw, "keyword_rank"):
            stat_date = _clean_str(row.get("createTime"))
            natural_rank = _to_float(row.get("crawNatureRank"))
            sponsored_rank = _to_float(row.get("crawSpRank"))
            if stat_date and (natural_rank is not None or sponsored_rank is not None):
                # 保留旧的 rank 字段作为自然位优先、广告位兜底的兼容值，
                # 同时保留两个原始排名，避免广告位被静默丢弃。
                rank_series.append({
                    "stat_date": stat_date[:10],
                    "keyword": _clean_str(row.get("keyword")),
                    "rank": natural_rank if natural_rank is not None else sponsored_rank,
                    "natural_rank": natural_rank,
                    "sponsored_rank": sponsored_rank,
                })
        rank_series.sort(key=lambda row: row["stat_date"], reverse=True)
        rank_values = [row["rank"] for row in rank_series]
        natural_rank_values = [
            row["natural_rank"] for row in rank_series
            if row["natural_rank"] is not None
        ]
        sponsored_rank_values = [
            row["sponsored_rank"] for row in rank_series
            if row["sponsored_rank"] is not None
        ]
        keyword_children = [
            child for child in (identity.get("children") or [])
            if child.get("generic_keyword")
        ]
        if not keyword_rank_enabled:
            keyword_rank_status = "SITE_NOT_SUPPORTED"
            keyword_rank_message = "仅支持美国、英国、德国"
        elif not keyword_children:
            keyword_rank_status = "NO_GENERIC_KEYWORD"
            keyword_rank_message = "暂无这个数据"
        elif rank_series:
            keyword_rank_status = "AVAILABLE"
            keyword_rank_message = None
        else:
            keyword_rank_status = "UNAVAILABLE"
            keyword_rank_message = "暂无这个数据"

        return {
            "natural_ratio_3d": _period_natural_ratio(recent_3d),
            "natural_ratio_7d": _period_natural_ratio(recent_7d),
            "natural_ratio_prev_7d": _period_natural_ratio(previous_7d),
            "natural_orders_3d": (
                sum(row["natural_orders"] for row in recent_3d) if recent_3d else None
            ),
            "natural_orders_7d": (
                sum(row["natural_orders"] for row in recent_7d) if recent_7d else None
            ),
            "daily_natural_ratio_7d": [
                row["natural_ratio"] for row in recent_7d
                if row["natural_ratio"] is not None
            ],
            "daily_series": daily_series,
            "daily_series_source": (
                "erp_listing_gross_profit_history:total_orders-ad_orders"
                if daily_series else None
            ),
            "daily_series_derivation_error": derivation_error,
            "daily_series_status": (
                "ENABLED" if sequence_enabled else "DISABLED_BY_POLICY"
            ),
            "aggregation_only": bool(parent_rows),
            "flow_data_scope": "WINDOW_AGGREGATE_ONLY",
            "flow_inlet_child_asin": flow_inlet_asin,
            "flow_inlet_seller_sku": flow_inlet_seller_sku,
            "window_natural_ratio": natural_ratio,
            "window_natural_orders": natural_orders,
            "window_start": window_start.isoformat(),
            "window_end": as_of.isoformat(),
            "expected_days": expected_days,
            "observed_days": len(daily_series),
            "window_observed_rows": len(parent_rows),
            "yesterday_date": as_of.isoformat(),
            "yesterday_ad_orders": yesterday_ad_orders,
            "sessions_this_week": resolver.pick(
                this_week, "traffic.sessions_this_week",
                ("conversation", "sessions", "会话数", "sessionNum", "本周会话"), cast="int",
            ),
            "sessions_last_week": resolver.pick(
                last_week, "traffic.sessions_last_week",
                ("conversation", "sessions", "会话数", "sessionNum", "上周会话"), cast="int",
            ),
            "ordered_items_this_week": resolver.pick(
                this_week, "traffic.ordered_items_this_week",
                ("productOrderQty", "orderedItems", "订单商品总数", "orderItemNum"), cast="int",
            ),
            "ordered_items_last_week": resolver.pick(
                last_week, "traffic.ordered_items_last_week",
                ("productOrderQty", "orderedItems", "订单商品总数", "orderItemNum"), cast="int",
            ),
            "conversion_this_week": resolver.pick(
                this_week, "traffic.conversion_this_week",
                ("链接转化率", "conversionRate", "cvr"), cast="float",
            ),
            "conversion_last_week": resolver.pick(
                last_week, "traffic.conversion_last_week",
                ("链接转化率", "conversionRate", "cvr"), cast="float",
            ),
            "keyword_rank_3d": _mean(rank_values[:3]),
            "keyword_rank_7d": _mean(rank_values[:7]),
            "keyword_rank_prev_7d": _mean(rank_values[7:14]),
            "natural_keyword_rank_7d": _mean(natural_rank_values[:7]),
            "sponsored_keyword_rank_7d": _mean(sponsored_rank_values[:7]),
            "daily_keyword_rank_7d": rank_values[:7],
            "keyword_rank_series": rank_series,
            "keyword_rank_status": keyword_rank_status,
            "keyword_rank_message": keyword_rank_message,
        }

    # -- 利润与广告 --------------------------------------------------------
    def _profit(
        self,
        raw: dict[str, McpToolResult | Exception],
        resolver: FieldResolver,
        sales: dict[str, Any],
        *,
        advert_target_enabled: bool = True,
    ) -> dict[str, Any]:
        rows = sales["daily_rows"]
        acos_series = [_to_float(r["acos"]) for r in rows]
        spend_series = [_to_float(r["ad_cost"]) for r in rows]
        margin_series = [_to_float(r["gross_margin"]) for r in rows]

        # 从 product_info 找流量入口子体 ASIN
        product_info_rows = _rows(raw.get("product_info"))
        flow_inlet_asin: str | None = None
        for row in product_info_rows:
            if _clean_str(row.get("flowInlet")) == "是":
                asin = (_clean_str(row.get("asin")) or "").strip().upper()
                if asin:
                    flow_inlet_asin = asin
                    break

        unit_rows = _rows(raw.get("unit_profit"))
        unit_row: dict[str, Any] = {}
        if flow_inlet_asin and unit_rows:
            unit_row = next(
                (
                    r for r in unit_rows
                    if (_clean_str(r.get("asin")) or "").strip().upper() == flow_inlet_asin
                ),
                {},
            )
        if not unit_row:
            unit_row = unit_rows[0] if unit_rows else {}
        advert_row = _first(raw.get("advert_config")) if advert_target_enabled else {}
        # 2026-08-24：标签兜底源——广告配置缺失时用父级产品信息兜底
        # （seasonality→season_type、progressPreparation→product_stage）。
        parent_extension_row = _first(raw.get("parent_extension")) or {}

        acos_3d = _mean([v for v in acos_series[:3] if v is not None])
        acos_7d = _mean([v for v in acos_series[:7] if v is not None])
        spend_3d = _mean([v for v in spend_series[:3] if v is not None])
        spend_7d = _mean([v for v in spend_series[:7] if v is not None])

        target_acos_raw = resolver.pick(
            {**unit_row, **advert_row},
            "profit.target_acos",
            ("targetAcosSuggest", "targetAcos", "目标ACOS", "target_acos"),
        )
        target_acos = _target_acos_ratio(target_acos_raw)
        target_budget = resolver.pick(
            {**unit_row, **advert_row},
            "profit.target_daily_budget",
            ("dailyBudgetSuggest", "dailyBudget", "目标每日预算", "target_daily_budget"),
            cast="float",
        )
        unit_contributions = [
            value
            for row in unit_rows
            if (
                value := _unit_contribution(row)
            ) is not None
        ]

        return {
            "acos_3d": acos_3d,
            "acos_7d": acos_7d,
            "ad_spend_3d_total": sum(v for v in spend_series[:3] if v is not None) or None,
            "ad_spend_3d_avg": spend_3d,
            "ad_spend_7d_avg": spend_7d,
            "ad_clicks_3d_total": None,   # 点击数在 natural_ad_flow，不在毛利历史
            "gross_margin_30d": _mean([v for v in margin_series if v is not None]),
            "gross_margin_7d": _mean([v for v in margin_series[:7] if v is not None]),
            "unit_contribution": resolver.pick(
                {"value": _mean(unit_contributions)}, "profit.unit_contribution",
                ("value",), cast="float",
            ),
            "best_feasible_net_cash_recovery": resolver.pick(
                {}, "profit.best_feasible_net_cash_recovery",
                ("netCashRecovery", "净现金回收", "best_feasible_net_cash_recovery"), cast="float",
            ),
            "target_acos": target_acos,
            "target_daily_budget": target_budget,
            "acos_over_target_days": _consecutive_above(acos_series[:7], target_acos),
            "spend_deviation_days": _consecutive_above(spend_series[:7], target_budget),
            "ad_zero_sale_days": sum(1 for r in rows[:7] if r.get("ad_zero_sale")),
            "currency": "SITE_SETTLEMENT",
            "product_cost": _to_float(unit_row.get("productCost")),
            "az_commission_fee": _to_float(unit_row.get("referralFee")),
            "fba_pack_fee": _to_float(unit_row.get("fbaPackFee")),
            "ad_cost_ratio": _clean_str(unit_row.get("adCostRatio")),
            "operating_tags": {
                "product_position": _clean_str(advert_row.get("productPosition")),
                "product_stage": (
                    _clean_str(advert_row.get("productStage"))
                    or _clean_str(parent_extension_row.get("progressPreparation"))
                ),
                "season_type": (
                    _clean_str(advert_row.get("seasonType"))
                    or _clean_str(parent_extension_row.get("seasonality"))
                ),
                "operating_mode": _clean_str(advert_row.get("operatingMode")),
                "advert_purposes": _clean_str(advert_row.get("advertPurposes")),
                "target_keyword_types": _clean_str(advert_row.get("targetKeywordTypes")),
                "advert_direction_types": _clean_str(advert_row.get("advertDirectionTypes")),
                "day_range": _clean_str(advert_row.get("dayRange")),
            },
        }

    # -- 库存 --------------------------------------------------------------
    def _inventory(
        self,
        raw: dict[str, McpToolResult | Exception],
        resolver: FieldResolver,
        sales: dict[str, Any],
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        stock_rows = _rows(raw.get("stock"))
        flow_inlet_asin = _clean_str(identity.get("flow_inlet_child_asin"))
        flow_inlet_seller_sku = _clean_str(identity.get("flow_inlet_seller_sku"))
        stock_row = next(
            (
                row for row in stock_rows
                if flow_inlet_asin
                and (_clean_str(row.get("asin")) or "").upper() == flow_inlet_asin.upper()
                and (
                    not flow_inlet_seller_sku
                    or not _clean_str(row.get("sellerSku"))
                    or _clean_str(row.get("sellerSku")) == flow_inlet_seller_sku
                )
            ),
            _first(raw.get("stock")),
        )
        stock_asin = (_clean_str(stock_row.get("asin")) or "").upper() or None
        stock_seller_sku = _clean_str(stock_row.get("sellerSku"))
        stock_scope = "CHILD" if stock_asin else "PARENT_SUMMARY"
        cost_rows = _rows(raw.get("inventory_cost"))

        available = resolver.pick(
            stock_row, "inventory.fba_available",
            ("canSaleNum", "FBA可售库存", "availableQuantity"), cast="float",
        )
        avg_30d = sales.get("avg_daily_units_30d")
        avg_7d = sales.get("avg_daily_units_7d")
        velocity = avg_30d if avg_30d else avg_7d

        # 库存可覆盖天数：分母为 0 或缺失时留 None，不写成"无限大"。
        days_of_supply: float | None = None
        if available is not None and velocity and velocity > 0:
            days_of_supply = round(available / velocity, 1)

        aged_qty = 0.0
        aged_fee = 0.0
        slow_children: list[dict[str, Any]] = []
        for row in cost_rows:
            children = row.get("children") if isinstance(row.get("children"), list) else [row]
            for child in children:
                if not isinstance(child, dict):
                    continue
                fees = child.get("longTermStorageFees")
                if isinstance(fees, list):
                    qty = sum(
                        _to_float(item.get("qtyCharged")) or 0.0
                        for item in fees
                        if isinstance(item, dict)
                    )
                    fee = sum(
                        _to_float(item.get("amountCharged")) or 0.0
                        for item in fees
                        if isinstance(item, dict)
                    )
                else:
                    qty = _to_float(child.get("汇总超龄库存数")) or 0.0
                    fee = _to_float(child.get("汇总超龄仓租费")) or 0.0
                aged_qty += qty
                aged_fee += fee
                if qty <= 0:
                    continue
                slow_children.append({
                    "child_asin": (_clean_str(child.get("childAsin"))
                                   or _clean_str(child.get("asin")) or "").upper() or None,
                    "seller_sku": _clean_str(child.get("sellerSku")),
                    "aged_quantity": qty,
                    "aged_storage_fee": fee,
                    "report_month": _clean_str(row.get("reportMonth")),
                })

        return {
            "stock_scope": stock_scope,
            "stock_child_asin": stock_asin if stock_scope == "CHILD" else None,
            "stock_seller_sku": stock_seller_sku if stock_scope == "CHILD" else None,
            "fba_available": available,
            "fba_inbound": resolver.pick(
                stock_row, "inventory.fba_inbound",
                ("inStockNum", "FBA入库库存"), cast="float",
            ),
            "fba_reserved": resolver.pick(
                stock_row, "inventory.fba_reserved",
                ("reserveNum", "FBA预留库存"), cast="float",
            ),
            "fba_unsellable": resolver.pick(
                stock_row, "inventory.fba_unsellable",
                ("noSaleNum", "FBA不可售库存"), cast="float",
            ),
            "purchase_on_way": resolver.pick(
                stock_row, "inventory.purchase_on_way",
                ("purchaseOnWay", "采购在途"), cast="float",
            ),
            "total_stock": resolver.pick(
                stock_row, "inventory.total_stock", ("totalStock", "总库存"), cast="float",
            ),
            "inventory_days_of_supply": days_of_supply,
            "days_of_supply_basis": (
                "avg_daily_units_30d" if avg_30d else ("avg_daily_units_7d" if avg_7d else None)
            ),
            "has_aged_inventory": aged_qty > 0,
            "aged_quantity": aged_qty or None,
            "aged_storage_fee_monthly": aged_fee or None,
            "monthly_storage_fee": _to_float(
                (cost_rows[0].get("summary") or {}).get("totalLongTermStorageFee")
            ) if cost_rows else None,
            "slow_moving_children": slow_children,
            "target_coverage_months": self._target_coverage_months(
                available,
                sales.get("monthly_goals") or [],
                as_of_month=sales.get("current_month"),
            ),
        }

    @staticmethod
    def _target_coverage_months(
        available: float | None,
        goals: list[dict[str, Any]],
        *,
        as_of_month: str | None,
    ) -> float | None:
        if available is None or available <= 0:
            return None
        future = [
            float(row["target_orders"])
            for row in goals
            if row.get("target_orders") is not None
            and float(row["target_orders"]) > 0
            and (as_of_month is None or str(row.get("month")) > as_of_month)
        ][:3]
        if not future:
            return None
        return available / (sum(future) / len(future))

    # -- 价格 --------------------------------------------------------------
    def _price(
        self,
        raw: dict[str, McpToolResult | Exception],
        resolver: FieldResolver,
        as_of: date,
    ) -> dict[str, Any]:
        storefront = _mapping(
            self._identity_storefront(raw)
        )
        product_rows = _rows(raw.get("product_info"))
        children_prices = []
        for row in product_rows:
            child_asin = (_clean_str(row.get("asin")) or "").upper() or None
            promotion = _prefixed_first(raw, "price_promotion", child_asin or "")
            # P0-2 修复：标记"实际价格来源"而非"调用过的工具"。
            # price 来自前台促销响应 → frontend；前台无价回退 ERP 原价 → erp。
            # 后续变体价差比较只允许同源比较，避免前台价与 ERP 原价混算导致价差假性放大。
            price = _to_float(promotion.get("price"))
            price_source = "frontend"
            if price is None:
                price = _to_float(row.get("productPrice"))
                price_source = "erp"
            if price is not None:
                children_prices.append({
                    "child_asin": child_asin,
                    "seller_sku": _clean_str(row.get("sellerSku")),
                    "price": price,
                    "strikethrough_price": _to_float(
                        promotion.get("strikethroughPrice")
                    ),
                    "coupon": _clean_str(promotion.get("coupon")),
                    "savings_percentage": _ratio(
                        promotion.get("savingsPercentage"),
                        numerator=None,
                        denominator=None,
                    ),
                    "promotions": _clean_str(promotion.get("promotions")),
                    "discount_types": _clean_str(promotion.get("discountTypes")),
                    "source": price_source,
                })
        activity_history = _rows(raw.get("platform_activity"))
        activities = _current_platform_activities(activity_history, product_rows, as_of)
        activities_evaluated = isinstance(raw.get("platform_activity"), McpToolResult)
        unique_child_prices = {row["price"] for row in children_prices}
        common_child_price = (
            next(iter(unique_child_prices)) if len(unique_child_prices) == 1 else None
        )
        return {
            "current_price": resolver.pick(
                {**storefront, "common_child_price": common_child_price},
                "price.current_price", ("price", "common_child_price"), cast="float",
            ),
            "strikethrough_price": resolver.pick(
                storefront, "price.strikethrough_price", ("strikethrough_price",), cast="float",
            ),
            "coupon": resolver.pick(
                storefront, "price.coupon", ("coupon",), cast="str",
            ),
            "effective_price": None,   # 到手价需前台快照，MCP 本轮未覆盖
            "children_prices": children_prices,
            "platform_activities": activities,
            "platform_activity_history": activity_history,
            "platform_activity_evaluated": activities_evaluated,
            "promotion_active": any(
                row.get("coupon") or row.get("promotions") or row.get("discount_types")
                for row in children_prices
            ),
        }

    # -- 质量 --------------------------------------------------------------
    def _quality(
        self,
        raw: dict[str, McpToolResult | Exception],
        resolver: FieldResolver,
        as_of: date,
        *,
        target_data_enabled: bool = True,
    ) -> dict[str, Any]:
        storefront = self._identity_storefront(raw)
        listing_basic_info = _first(raw.get("listing_basic_info"))
        legacy_row = _first(raw.get("listing_identity"))
        product_row = _first(raw.get("product_info"))
        business_rows = _business_report_records(raw.get("business_report"))
        refund_row = _first(raw.get("refund_rate"))
        refund_summary = _mapping(refund_row.get("summary"))
        this_week = business_rows[0] if business_rows else {}
        last_week = business_rows[1] if len(business_rows) > 1 else {}
        recent_reviews = _recent_reviews(_prefixed_rows(raw, "recent_reviews"), as_of)
        recent_reviews_evaluated = any(
            key.startswith("recent_reviews:") and isinstance(result, McpToolResult)
            for key, result in raw.items()
        )
        compliance_issues = [
            issue
            for row in _prefixed_rows(raw, "account_health")
            for issue in (row.get("issues") or [])
            if isinstance(issue, dict)
        ]

        return {
            "star_rating": resolver.pick(
                {**listing_basic_info, **storefront},
                "quality.star_rating",
                ("star_rating", "星级"),
                cast="float",
            ),
            "target_star_rating": (
                resolver.pick(
                    product_row, "quality.target_star_rating",
                    ("目标星级", "TARGET_STAR_RATE", "targetStarRate"), cast="float",
                )
                if target_data_enabled else None
            ),
            "review_count": resolver.pick(
                {**listing_basic_info, **storefront},
                "quality.review_count",
                ("review_count", "评论数"),
                cast="int",
            ),
            "conversion_rate": resolver.pick(
                {**legacy_row, **this_week}, "quality.conversion_rate",
                ("链接转化率", "conversionRate", "cvr"), cast="float",
            ),
            "category_conversion_rate": resolver.pick(
                {**listing_basic_info, **legacy_row},
                "quality.category_conversion_rate", ("类目转化率",), cast="float",
            ),
            "category_refund_rate": resolver.pick(
                {**listing_basic_info, **legacy_row},
                "quality.category_refund_rate", ("类目退换货率",), cast="float",
            ),
            "refund_rate_16w": resolver.pick(
                {**legacy_row, **refund_summary}, "quality.refund_rate_16w",
                ("sixteenWeekRefundRate", "16周退款率"), cast="float",
            ),
            "refund_rate_32w": resolver.pick(
                {**legacy_row, **refund_summary}, "quality.refund_rate_32w",
                ("thirtyTwoWeekRefundRate", "32周退款率"), cast="float",
            ),
            "refund_rate_this_week": resolver.pick(
                this_week, "quality.refund_rate_this_week",
                ("退款率", "refundRate"), cast="float",
            ),
            "refund_rate_last_week": resolver.pick(
                last_week, "quality.refund_rate_last_week",
                ("退款率", "refundRate"), cast="float",
            ),
            "category_rank_major": resolver.pick(
                {**listing_basic_info, **legacy_row},
                "quality.category_rank_major",
                ("大类排名",), cast="int",
            ),
            "category_rank_minor": resolver.pick(
                {**listing_basic_info, **legacy_row},
                "quality.category_rank_minor",
                ("小类排名",), cast="int",
            ),
            "buy_box_owned": None,
            "compliance_issues": compliance_issues,
            "recent_negative_reviews": [
                review for review in recent_reviews if review["rating"] <= 3
            ],
            "recent_reviews": recent_reviews,
            "recent_reviews_evaluated": recent_reviews_evaluated,
        }

    @staticmethod
    def _identity_storefront(
        raw: dict[str, McpToolResult | Exception],
    ) -> dict[str, Any]:
        inlet = _flow_inlet_row(_rows(raw.get("product_info")))
        inlet_asin = (_clean_str(inlet.get("asin")) or "").upper()
        row = _prefixed_first(raw, "child_detail", inlet_asin) if inlet_asin else {}
        row = row or _first(raw.get("listing_identity"))
        bonus = _mapping(row.get("bonus"))
        link_status = _mapping(row.get("linkStatus"))
        reviews = _mapping(row.get("reviews"))
        return {
            "price": link_status.get("price", row.get("售价")),
            "strikethrough_price": link_status.get("strikethroughPrice", row.get("划线价")),
            "coupon": link_status.get("coupon", row.get("coupon")),
            "star_rating": bonus.get("star", row.get("星级")),
            "review_count": bonus.get("ratingsNum", row.get("评论数")),
            "recent_low_star_count": reviews.get("lowStarRecentCount"),
        }
