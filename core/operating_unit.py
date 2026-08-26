"""Amazon 经营单元身份边界。

来源：交接包 `源码参考/patrol/core/operating_unit.py`（原样保留算法），
在本项目中额外提供：
  - `derive_operating_unit_id()`：供 Pydantic 合同直接复用的纯函数；
  - `OperatingUnitBinding`：把"契约身份"与"取数账号"分离的绑定对象。

【为什么要拆出 shop_account】
  contracts/operating-unit-ref.v2.schema.json 的 additionalProperties = false，
  中控经营单元业务键固定为 shop_id × parent_asin × parent_seller_sku。ERP MCP 工具
  的入参用的是 `shopAccount` 字符串。若把 shop_account 塞进 OperatingUnitRef，导出的 JSON 将
  无法通过契约校验（交接包明令"不得用数据库临时字段绕过契约语义"）。
  因此 shop_account 作为取数绑定单独存在，不参与身份唯一性。

【身份不完整时的行为】（knowledge/06 §5.1）
  只输出 IDENTITY_REQUIRED 数据缺口，不得进入事件池、不得按 ASIN 兜底、
  不得跨店铺或跨站点合并证据。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

CONTRACT_VERSION = "amazon_ops.v2"

SITE_PATTERN = re.compile(r"^AMAZON_[A-Z0-9_-]{2,24}$")
OPERATING_UNIT_ID_PATTERN = re.compile(r"^ou_[0-9a-f]{24}$")
UNICODE_ESCAPE_PATTERN = re.compile(r"\\u([0-9a-fA-F]{4})")

#: MCP 事实源可能返回站点码或中文国家名。只映射已知 Amazon 站点，未知值仍失败关闭。
SITE_ALIASES: dict[str, str] = {
    "USA": "US",
    "GB": "UK",
    "美国": "US",
    "英国": "UK",
    "德国": "DE",
    "法国": "FR",
    "意大利": "IT",
    "西班牙": "ES",
    "加拿大": "CA",
    "日本": "JP",
    "澳大利亚": "AU",
    "墨西哥": "MX",
    "阿联酋": "AE",
    "土耳其": "TR",
}


class OperatingUnitIdentityError(ValueError):
    """身份不合法。调用方应转成 INVALID_OPERATING_UNIT 错误码。"""


def canonical_site_code(value: str | None) -> str:
    """归一化站点码：trim → 大写 → 别名映射 → 补 AMAZON_ 前缀。"""
    raw_site_code = str(value or "").strip()
    site_code = UNICODE_ESCAPE_PATTERN.sub(
        lambda match: chr(int(match.group(1), 16)),
        raw_site_code,
    ).upper()
    if site_code and not site_code.startswith("AMAZON_"):
        site_code = f"AMAZON_{SITE_ALIASES.get(site_code, site_code)}"
    return site_code


def canonical_parent_asin(value: str | None) -> str:
    return str(value or "").strip().upper()


def derive_operating_unit_id(
    shop_id: int,
    parent_asin: str,
    parent_seller_sku: str,
) -> str:
    """ou_ + sha256('amazon_ops.v2|{shop_id}|{ASIN}|{PARENT_SKU}')[:24]

    入参必须是**已归一化**的值。测试向量见
    contracts/operating-unit-ref.v2.vectors.json。
    """
    raw = f"{CONTRACT_VERSION}|{shop_id}|{parent_asin}|{parent_seller_sku}"
    return f"ou_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def normalize_identity(
    shop_id: Any,
    site_code: Any,
    parent_asin: Any,
    parent_seller_sku: Any = None,
) -> tuple[int, str, str, str]:
    """归一化并校验业务键及站点属性。"""
    try:
        normalized_shop_id = int(shop_id)
    except (TypeError, ValueError) as exc:
        raise OperatingUnitIdentityError("shop_id must be a positive integer") from exc
    if normalized_shop_id <= 0:
        raise OperatingUnitIdentityError("shop_id must be a positive integer")

    normalized_site = canonical_site_code(site_code)
    if not normalized_site:
        raise OperatingUnitIdentityError("site_code is required")
    if not SITE_PATTERN.fullmatch(normalized_site):
        raise OperatingUnitIdentityError("site_code has an invalid format")

    normalized_asin = canonical_parent_asin(parent_asin)
    if not normalized_asin:
        raise OperatingUnitIdentityError("parent_asin is required")
    if len(normalized_asin) > 32:
        raise OperatingUnitIdentityError("parent_asin exceeds 32 characters")

    normalized_sku = str(parent_seller_sku or "").strip()
    if not normalized_sku:
        raise OperatingUnitIdentityError("parent_seller_sku is required")
    if len(normalized_sku) > 128:
        raise OperatingUnitIdentityError("parent_seller_sku exceeds 128 characters")

    return normalized_shop_id, normalized_site, normalized_asin, normalized_sku


@dataclass(frozen=True, slots=True)
class OperatingUnitBinding:
    """身份 + 取数绑定。

    `shop_account` 是 ERP 侧的店铺账号字符串，只用于 MCP 取数与旧库查询，
    **不参与经营单元唯一性**，也不写进任何跨服务契约对象。
    `owner_user_ids` 是权威来源返回的负责人集合，同样不参与经营单元唯一性。
    """

    shop_id: int
    site_code: str
    parent_asin: str
    shop_account: str
    parent_seller_sku: str
    owner_user_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        shop_id, site_code, parent_asin, sku = normalize_identity(
            self.shop_id,
            self.site_code,
            self.parent_asin,
            self.parent_seller_sku,
        )
        shop_account = str(self.shop_account or "").strip()
        if not shop_account:
            raise OperatingUnitIdentityError("shop_account is required for fact collection")
        owner_user_ids: set[int] = set()
        for value in self.owner_user_ids:
            if isinstance(value, bool):
                raise OperatingUnitIdentityError("owner_user_ids must contain positive integers")
            try:
                owner_user_id = int(value)
            except (TypeError, ValueError) as exc:
                raise OperatingUnitIdentityError(
                    "owner_user_ids must contain positive integers"
                ) from exc
            if owner_user_id < 1:
                raise OperatingUnitIdentityError("owner_user_ids must contain positive integers")
            owner_user_ids.add(owner_user_id)
        object.__setattr__(self, "shop_id", shop_id)
        object.__setattr__(self, "site_code", site_code)
        object.__setattr__(self, "parent_asin", parent_asin)
        object.__setattr__(self, "parent_seller_sku", sku)
        object.__setattr__(self, "shop_account", shop_account)
        object.__setattr__(self, "owner_user_ids", tuple(sorted(owner_user_ids)))

    @property
    def operating_unit_id(self) -> str:
        return derive_operating_unit_id(
            self.shop_id,
            self.parent_asin,
            self.parent_seller_sku,
        )

    @property
    def storage_key(self) -> tuple[int, str, str]:
        return self.shop_id, self.parent_asin, self.parent_seller_sku

    def as_ref_dict(self) -> dict[str, Any]:
        """导出为 operating-unit-ref.v2 契约字典（不含 shop_account）。"""
        return {
            "contract_version": CONTRACT_VERSION,
            "operating_unit_id": self.operating_unit_id,
            "shop_id": self.shop_id,
            "site_code": self.site_code,
            "parent_asin": self.parent_asin,
            "parent_seller_sku": self.parent_seller_sku,
        }

    def with_owner_user_ids(self, owner_user_ids: tuple[int, ...]) -> OperatingUnitBinding:
        return OperatingUnitBinding(
            shop_id=self.shop_id,
            site_code=self.site_code,
            parent_asin=self.parent_asin,
            shop_account=self.shop_account,
            parent_seller_sku=self.parent_seller_sku,
            owner_user_ids=owner_user_ids,
        )

    def single_owner_user_id(self) -> str:
        if len(self.owner_user_ids) != 1:
            raise OperatingUnitIdentityError(
                "control-center ownerUserId requires exactly one authoritative owner"
            )
        return str(self.owner_user_ids[0])
