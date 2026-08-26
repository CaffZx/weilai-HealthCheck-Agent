from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from clients.mcp_client import McpToolResult
from core.errors import McpContractInvalid
from core.operating_unit import (
    OperatingUnitBinding,
    OperatingUnitIdentityError,
    canonical_parent_asin,
    canonical_site_code,
)
from integrations.mcp_operating_units import ACTIVE_LISTING_STATUSES, McpPort
from integrations.repositories.tables import (
    patrol_asin_owner,
    patrol_operating_unit_catalog,
    patrol_sys_user,
    patrol_user_role,
)

USER_TOOL = "sys_user_query"
SHOP_TOOL = "sprout_shop_query"
FOLLOW_UP_TOOL = "erp_listing_follow_up_by_principal"
DEFAULT_OPERATOR_ROLE = "GROUP_FBASALER"
ACTIVE_USER_STATES = frozenset({"1", "ACTIVE", "ENABLED", "NORMAL", "启用", "正常"})
_RATE_LIMIT_WAIT_PATTERN = re.compile(
    r"请\s*(\d+(?:\.\d+)?)\s*(MS|毫秒|秒|S)\s*后重试",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class UserRole:
    code: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class SystemUser:
    user_id: int
    user_name: str
    user_account: str | None
    user_state: str
    roles: tuple[UserRole, ...]

    @property
    def active(self) -> bool:
        return self.user_state.upper() in ACTIVE_USER_STATES


@dataclass(frozen=True, slots=True)
class ShopIdentity:
    shop_id: int
    shop_account: str
    site_code: str


@dataclass(frozen=True, slots=True)
class ManagedUnit:
    shop_id: int
    site_code: str
    parent_asin: str
    parent_seller_sku: str
    editor_user_id: int | None
    creator_user_id: int | None

    @property
    def key(self) -> tuple[int, str, str]:
        return self.shop_id, self.parent_asin, self.parent_seller_sku


@dataclass(frozen=True, slots=True)
class OwnerTarget:
    shop_id: int
    parent_asin: str
    parent_seller_sku: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "shop_id", _positive_int(self.shop_id, "target.shop_id"))
        parent_asin = canonical_parent_asin(self.parent_asin)
        parent_seller_sku = _optional_text(self.parent_seller_sku)
        if not parent_asin or not parent_seller_sku:
            raise McpContractInvalid("target parent identity is required")
        object.__setattr__(self, "parent_asin", parent_asin)
        object.__setattr__(self, "parent_seller_sku", parent_seller_sku)


def parse_owner_targets_json(value: str) -> tuple[OwnerTarget, ...]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("owner targets must be valid JSON") from exc
    rows = payload.get("operating_units") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("owner targets must be a non-empty array")
    targets: dict[tuple[int, str, str], OwnerTarget] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each owner target must be an object")
        target = OwnerTarget(
            shop_id=row.get("shop_id"),
            parent_asin=row.get("parent_asin"),
            parent_seller_sku=row.get("parent_seller_sku"),
        )
        key = target.shop_id, target.parent_asin, target.parent_seller_sku
        targets.setdefault(key, target)
    return tuple(targets[key] for key in sorted(targets))


@dataclass(frozen=True, slots=True)
class OwnerAssignment:
    binding: OperatingUnitBinding
    principal_user_id: int | None
    editor_user_id: int | None
    creator_user_id: int | None
    source_tool: str = FOLLOW_UP_TOOL


@dataclass(frozen=True, slots=True)
class OwnerSyncSnapshot:
    users: tuple[SystemUser, ...]
    operator_user_count: int
    active_listing_count: int
    out_of_scope_count: int
    unresolved_shop_count: int
    assignments: tuple[OwnerAssignment, ...]
    fetched_at: datetime
    failed_operators: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Page:
    records: list[dict[str, Any]]
    total_count: int | None
    total_pages: int | None
    page_no: int | None


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise McpContractInvalid(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise McpContractInvalid(f"{field} must be a positive integer") from exc
    if number < 1:
        raise McpContractInvalid(f"{field} must be a positive integer")
    return number


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _rate_limit_wait_seconds(message: str) -> float | None:
    match = _RATE_LIMIT_WAIT_PATTERN.search(message)
    if match is None:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    return value / 1000.0 if unit in {"MS", "毫秒"} else value


def _raise_missing_parent_sku() -> str:
    raise McpContractInvalid("managed.parent_seller_sku is required")


def _parsed_values(value: Any) -> Iterable[Any]:
    queue: deque[Any] = deque([value])
    seen_strings: set[str] = set()
    while queue:
        current = queue.popleft()
        yield current
        if isinstance(current, dict):
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
        elif isinstance(current, str):
            text = current.strip()
            if text in seen_strings or not text.startswith(("{", "[")):
                continue
            seen_strings.add(text)
            try:
                queue.append(json.loads(text))
            except json.JSONDecodeError:
                continue


def _page(result: McpToolResult, *, tool_name: str) -> _Page:
    candidates = [result.data, result.raw]
    for candidate in candidates:
        for value in _parsed_values(candidate):
            if not isinstance(value, dict):
                continue
            records = next(
                (
                    value[key]
                    for key in ("records", "rows", "data")
                    if isinstance(value.get(key), list)
                ),
                None,
            )
            if records is None or any(not isinstance(record, dict) for record in records):
                continue
            has_page_shape = any(
                key in value
                for key in ("total", "totalCount", "totalPages", "pageNo", "pageNum")
            )
            if not has_page_shape and value is not result.raw:
                continue
            total = value.get("totalCount", value.get("total"))
            pages = value.get("totalPages")
            page_no = value.get("pageNo", value.get("pageNum"))
            return _Page(
                records=list(records),
                total_count=int(total) if total is not None else None,
                total_pages=int(pages) if pages is not None else None,
                page_no=int(page_no) if page_no is not None else None,
            )
    if all(isinstance(record, dict) for record in result.data):
        return _Page(list(result.data), None, None, None)
    raise McpContractInvalid(f"{tool_name} response does not contain a valid page")


def _role_values(value: Any) -> tuple[UserRole, ...]:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("[", "{")):
            try:
                return _role_values(json.loads(text))
            except json.JSONDecodeError:
                pass
        parts = [part.strip() for part in text.replace(";", ",").split(",")]
        return tuple(UserRole(part.upper()) for part in parts if part)
    if isinstance(value, dict):
        code = _optional_text(
            value.get("roleCode")
            or value.get("code")
            or value.get("roleKey")
            or value.get("groupCode")
        )
        name = _optional_text(value.get("roleName") or value.get("name"))
        direct = (UserRole(code.upper(), name),) if code else ()
        nested = tuple(
            role
            for key, child in value.items()
            if key not in {"roleCode", "code", "roleKey", "groupCode", "roleName", "name"}
            for role in _role_values(child)
        )
        return direct + nested
    if isinstance(value, list):
        return tuple(role for item in value for role in _role_values(item))
    return ()


def normalize_user(record: dict[str, Any]) -> SystemUser:
    user_id = _positive_int(record.get("id"), "sys_user.id")
    user_name = _optional_text(record.get("userName"))
    if not user_name:
        raise McpContractInvalid("sys_user.userName is required")
    roles = {
        role.code: role
        for role in _role_values(record.get("roles"))
        if role.code
    }
    return SystemUser(
        user_id=user_id,
        user_name=user_name,
        user_account=_optional_text(record.get("userAccount")),
        user_state=str(record.get("userState") or "").strip(),
        roles=tuple(roles[code] for code in sorted(roles)),
    )


def normalize_shop(record: dict[str, Any]) -> ShopIdentity:
    shop_id = _positive_int(record.get("id"), "shop.id")
    shop_account = _optional_text(record.get("account"))
    if not shop_account:
        raise McpContractInvalid("shop.account is required")
    site_code = canonical_site_code(
        record.get("plSiteCode") or record.get("platformSite") or record.get("siteCode")
    )
    if not site_code:
        raise McpContractInvalid("shop.siteCode is required")
    return ShopIdentity(shop_id, shop_account, site_code)


class OwnerDirectoryCollector:
    def __init__(
        self,
        directory_client: McpPort,
        listing_client: McpPort,
        engine: Engine,
        *,
        operator_role: str = DEFAULT_OPERATOR_ROLE,
        directory_page_size: int = 10,
        listing_page_size: int = 200,
        max_pages: int = 10_000,
        principal_retry: int = 3,
        principal_retry_delay: float = 3.0,
        min_listing_interval: float = 2.2,
        inter_operator_delay: float = 1.0,
        targets: tuple[OwnerTarget, ...] = (),
    ) -> None:
        self.directory_client = directory_client
        self.listing_client = listing_client
        self.engine = engine
        self.operator_role = operator_role.strip().upper()
        self.directory_page_size = directory_page_size
        self.listing_page_size = listing_page_size
        self.max_pages = max_pages
        self.principal_retry = max(1, principal_retry)
        self.principal_retry_delay = max(0.0, principal_retry_delay)
        self.min_listing_interval = max(0.0, min_listing_interval)
        self.inter_operator_delay = max(0.0, inter_operator_delay)
        self.targets = targets
        self._listing_lock = asyncio.Lock()
        self._last_listing_call_at = 0.0

    async def collect(self) -> OwnerSyncSnapshot:
        users = tuple(await self._users())
        operators = [
            user
            for user in users
            if user.active and any(role.code == self.operator_role for role in user.roles)
        ]
        if not operators:
            raise McpContractInvalid("no active FBA operators were returned")
        names: dict[str, int] = {}
        for operator in operators:
            existing = names.setdefault(operator.user_name, operator.user_id)
            if existing != operator.user_id:
                raise McpContractInvalid("active operator names are not unique")

        shops = await self._shops()
        shop_by_account: dict[str, ShopIdentity] = {}
        for shop in shops:
            existing = shop_by_account.setdefault(shop.shop_account, shop)
            if existing != shop:
                raise McpContractInvalid("shopAccount maps to inconsistent shop identities")
        managed_units = self._managed_units()
        if not managed_units:
            raise McpContractInvalid("managed operating-unit directory is empty")

        assignments: dict[tuple[int, str, str, int], OwnerAssignment] = {}
        active_listing_count = 0
        out_of_scope_count = 0
        unresolved_shop_count = 0
        failed_operators: list[str] = []
        for index, operator in enumerate(operators):
            if index and self.inter_operator_delay:
                await asyncio.sleep(self.inter_operator_delay)
            try:
                records = await self._principal_records_with_retry(operator.user_name)
            except McpContractInvalid:
                raise
            except Exception as exc:
                failed_operators.append(
                    f"{operator.user_name}:{type(exc).__name__}:{str(exc)[:200]}"
                )
                continue
            for record in records:
                if str(record.get("status") or "").strip().upper() not in ACTIVE_LISTING_STATUSES:
                    continue
                active_listing_count += 1
                shop_account = str(record.get("shopAccount") or "").strip()
                shop = shop_by_account.get(shop_account)
                if shop is None:
                    unresolved_shop_count += 1
                    continue
                parent_asin = canonical_parent_asin(record.get("parentAsin"))
                response_sku = _optional_text(record.get("parentSellerSku"))
                if response_sku is None:
                    raise McpContractInvalid("active listing lacks parent SKU")
                managed = managed_units.get((shop.shop_id, parent_asin, response_sku))
                if managed is None:
                    out_of_scope_count += 1
                    continue
                try:
                    binding = OperatingUnitBinding(
                        shop_id=shop.shop_id,
                        shop_account=shop.shop_account,
                        site_code=shop.site_code,
                        parent_asin=managed.parent_asin,
                        parent_seller_sku=managed.parent_seller_sku,
                        owner_user_ids=(operator.user_id,),
                    )
                except OperatingUnitIdentityError as exc:
                    raise McpContractInvalid("owner assignment has invalid identity") from exc
                key = (*binding.storage_key, operator.user_id)
                assignments[key] = OwnerAssignment(
                    binding,
                    operator.user_id,
                    managed.editor_user_id,
                    managed.creator_user_id,
                )
        principal_unit_ids = {
            assignment.binding.operating_unit_id for assignment in assignments.values()
        }
        for managed in managed_units.values():
            shop = next(
                (
                    item
                    for item in shops
                    if item.shop_id == managed.shop_id and item.site_code == managed.site_code
                ),
                None,
            )
            if shop is None:
                continue
            binding = OperatingUnitBinding(
                shop_id=shop.shop_id,
                shop_account=shop.shop_account,
                site_code=shop.site_code,
                parent_asin=managed.parent_asin,
                parent_seller_sku=managed.parent_seller_sku,
            )
            if binding.operating_unit_id in principal_unit_ids:
                continue
            if managed.editor_user_id is None and managed.creator_user_id is None:
                continue
            assignments[(*binding.storage_key, 0)] = OwnerAssignment(
                binding,
                None,
                managed.editor_user_id,
                managed.creator_user_id,
            )
        if not principal_unit_ids:
            raise McpContractInvalid("active listings were returned but none matched managed units")
        return OwnerSyncSnapshot(
            users=users,
            operator_user_count=len(operators),
            active_listing_count=active_listing_count,
            out_of_scope_count=out_of_scope_count,
            unresolved_shop_count=unresolved_shop_count,
            assignments=tuple(assignments[key] for key in sorted(assignments)),
            fetched_at=datetime.now(UTC),
            failed_operators=tuple(sorted(failed_operators)),
        )

    async def _principal_records_with_retry(
        self,
        principal_name: str,
    ) -> list[dict[str, Any]]:
        """单个负责人采集失败自动重试（指数退避），契约错误不重试。"""
        for attempt in range(1, self.principal_retry + 1):
            try:
                return await self._principal_records(principal_name)
            except McpContractInvalid:
                raise
            except Exception as exc:
                if attempt >= self.principal_retry:
                    raise
                wait = self.principal_retry_delay * attempt
                suggested = _rate_limit_wait_seconds(str(exc))
                if suggested is not None:
                    wait = max(wait, suggested)
                await asyncio.sleep(wait)
        return []  # pragma: no cover - 循环必然返回或抛出

    async def _users(self) -> list[SystemUser]:
        records = await self._directory_records(USER_TOOL, {})
        users: dict[int, SystemUser] = {}
        for record in records:
            user = normalize_user(record)
            existing = users.setdefault(user.user_id, user)
            if existing != user:
                raise McpContractInvalid("sys_user.id maps to inconsistent user records")
        return [users[user_id] for user_id in sorted(users)]

    async def _shops(self) -> list[ShopIdentity]:
        records = await self._directory_records(SHOP_TOOL, {"platformCode": "Amazon"})
        shops: dict[int, ShopIdentity] = {}
        for record in records:
            shop = normalize_shop(record)
            existing = shops.setdefault(shop.shop_id, shop)
            if existing != shop:
                raise McpContractInvalid("shop.id maps to inconsistent shop records")
        return [shops[shop_id] for shop_id in sorted(shops)]

    async def _directory_records(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        expected_total: int | None = None
        for page_no in range(1, self.max_pages + 1):
            result = await self.directory_client.call_tool(
                tool_name,
                {**arguments, "pageNo": page_no, "pageSize": self.directory_page_size},
            )
            page = _page(result, tool_name=tool_name)
            if page.page_no is not None and page.page_no != page_no:
                raise McpContractInvalid(f"{tool_name} returned an unexpected page")
            if page.total_count is not None:
                if expected_total is None:
                    expected_total = page.total_count
                elif expected_total != page.total_count:
                    raise McpContractInvalid(f"{tool_name} pagination total changed")
            if len(page.records) > self.directory_page_size:
                raise McpContractInvalid(f"{tool_name} returned more than pageSize records")
            records.extend(page.records)
            if page.total_pages is not None:
                if page_no >= page.total_pages:
                    break
            elif page.total_count is not None:
                if len(records) >= page.total_count:
                    break
            elif len(page.records) < self.directory_page_size:
                break
        else:
            raise McpContractInvalid(f"{tool_name} pagination did not terminate")
        if expected_total is not None and len(records) != expected_total:
            raise McpContractInvalid(f"{tool_name} record count does not match total")
        return records

    async def _call_listing(self, arguments: dict[str, Any]) -> McpToolResult:
        """串行节流调用 follow-up 工具，遵守 AZ MCP 10 秒内最多 5 次的限流。"""
        async with self._listing_lock:
            elapsed = time.monotonic() - self._last_listing_call_at
            if elapsed < self.min_listing_interval:
                await asyncio.sleep(self.min_listing_interval - elapsed)
            try:
                return await self.listing_client.call_tool(FOLLOW_UP_TOOL, arguments)
            finally:
                self._last_listing_call_at = time.monotonic()

    async def _principal_records(self, principal_name: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        expected_total: int | None = None
        expected_pages: int | None = None
        for page_no in range(1, self.max_pages + 1):
            result = await self._call_listing(
                {
                    "principalName": principal_name,
                    "pageNo": page_no,
                    "pageSize": self.listing_page_size,
                },
            )
            page = _page(result, tool_name=FOLLOW_UP_TOOL)
            if page.page_no != page_no:
                raise McpContractInvalid(f"{FOLLOW_UP_TOOL} returned an unexpected page")
            if page.total_count == 0 and page.total_pages == 0 and not page.records:
                return []
            if page.total_count is None or page.total_pages is None:
                raise McpContractInvalid(f"{FOLLOW_UP_TOOL} pagination metadata is required")
            if expected_total is None:
                expected_total = page.total_count
                expected_pages = page.total_pages
            elif expected_total != page.total_count or expected_pages != page.total_pages:
                raise McpContractInvalid(f"{FOLLOW_UP_TOOL} pagination totals changed")
            records.extend(page.records)
            if page_no >= page.total_pages:
                break
        else:
            raise McpContractInvalid(f"{FOLLOW_UP_TOOL} pagination did not terminate")
        if expected_total is not None and len(records) != expected_total:
            raise McpContractInvalid(f"{FOLLOW_UP_TOOL} record count does not match total")
        return records

    def _managed_units(self) -> dict[tuple[int, str, str], ManagedUnit]:
        requested = {
            (target.shop_id, target.parent_asin, target.parent_seller_sku): target
            for target in self.targets
        }
        rows: list[dict[str, Any]] = []
        with self.engine.connect() as connection:
            config_rows = connection.execute(
                sa.text(
                    """
                    SELECT shop_id,site_code,parent_asin,parent_seller_sku,editor_id,creator_id
                    FROM t_ops_operating_unit_config
                    WHERE shop_id IS NOT NULL AND site_code IS NOT NULL
                      AND parent_asin IS NOT NULL
                    """
                )
            ).mappings()
            rows = [dict(row) for row in config_rows]
        if not rows:
            # 运营配置表为空时，回退到已同步的真实经营单元目录，确保负责人同步可跑通。
            with self.engine.connect() as connection:
                catalog_rows = connection.execute(
                    sa.select(
                        patrol_operating_unit_catalog.c.shop_id,
                        patrol_operating_unit_catalog.c.site_code,
                        patrol_operating_unit_catalog.c.parent_asin,
                        patrol_operating_unit_catalog.c.parent_seller_sku,
                    )
                ).mappings()
                rows = [
                    {
                        "shop_id": row["shop_id"],
                        "site_code": row["site_code"],
                        "parent_asin": row["parent_asin"],
                        "parent_seller_sku": row["parent_seller_sku"],
                        "editor_id": None,
                        "creator_id": None,
                    }
                    for row in catalog_rows
                ]
        units: dict[tuple[int, str, str], ManagedUnit] = {}
        for row in rows:
            unit = ManagedUnit(
                shop_id=_positive_int(row["shop_id"], "managed.shop_id"),
                site_code=canonical_site_code(row["site_code"]),
                parent_asin=canonical_parent_asin(row["parent_asin"]),
                parent_seller_sku=(
                    _optional_text(row["parent_seller_sku"])
                    or _raise_missing_parent_sku()
                ),
                editor_user_id=(
                    _positive_int(row["editor_id"], "managed.editor_id")
                    if row["editor_id"] is not None
                    else None
                ),
                creator_user_id=(
                    _positive_int(row["creator_id"], "managed.creator_id")
                    if row["creator_id"] is not None
                    else None
                ),
            )
            target = requested.get(unit.key)
            if requested and target is None:
                continue
            existing = units.setdefault(unit.key, unit)
            if existing != unit:
                raise McpContractInvalid("managed operating-unit identity is inconsistent")
        matched = {unit.key for unit in units.values()}
        if set(requested) - matched:
            raise McpContractInvalid("owner targets are missing from managed unit directory")
        return units


class MySqlOwnerDirectoryStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def replace(self, snapshot: OwnerSyncSnapshot) -> None:
        with self.engine.begin() as connection:
            connection.execute(sa.delete(patrol_asin_owner))
            connection.execute(sa.delete(patrol_user_role))
            connection.execute(sa.delete(patrol_sys_user))
            if snapshot.users:
                connection.execute(
                    sa.insert(patrol_sys_user),
                    [
                        {
                            "user_id": user.user_id,
                            "user_name": user.user_name,
                            "user_account": user.user_account,
                            "user_state": user.user_state,
                            "is_active": user.active,
                            "fetched_at": snapshot.fetched_at,
                        }
                        for user in snapshot.users
                    ],
                )
            role_rows = [
                {
                    "user_id": user.user_id,
                    "role_code": role.code,
                    "role_name": role.name,
                    "fetched_at": snapshot.fetched_at,
                }
                for user in snapshot.users
                for role in user.roles
            ]
            if role_rows:
                connection.execute(sa.insert(patrol_user_role), role_rows)
            if snapshot.assignments:
                connection.execute(
                    sa.insert(patrol_asin_owner),
                    [self._assignment_values(assignment, snapshot.fetched_at) for assignment in snapshot.assignments],
                )
            self._backfill_catalog_owners(connection, snapshot)

    def replace_targets(self, snapshot: OwnerSyncSnapshot, targets: tuple[OwnerTarget, ...]) -> None:
        if not targets:
            raise ValueError("targeted owner replacement requires targets")
        target_keys = {
            (target.shop_id, target.parent_asin, target.parent_seller_sku)
            for target in targets
        }
        snapshot_keys = {
            (
                assignment.binding.shop_id,
                assignment.binding.parent_asin,
                assignment.binding.parent_seller_sku,
            )
            for assignment in snapshot.assignments
        }
        if not snapshot_keys.issubset(target_keys):
            raise McpContractInvalid("targeted owner snapshot contains an unrequested unit")
        with self.engine.begin() as connection:
            for target in targets:
                connection.execute(
                    sa.delete(patrol_asin_owner).where(
                        patrol_asin_owner.c.shop_id == target.shop_id,
                        patrol_asin_owner.c.parent_asin == target.parent_asin,
                        patrol_asin_owner.c.parent_seller_sku == target.parent_seller_sku,
                    )
                )
            existing_user_ids = set(connection.execute(sa.select(patrol_sys_user.c.user_id)).scalars())
            for user in snapshot.users:
                values = {
                    "user_id": user.user_id,
                    "user_name": user.user_name,
                    "user_account": user.user_account,
                    "user_state": user.user_state,
                    "is_active": user.active,
                    "fetched_at": snapshot.fetched_at,
                }
                statement = (
                    sa.update(patrol_sys_user)
                    .where(patrol_sys_user.c.user_id == user.user_id)
                    .values(**values)
                    if user.user_id in existing_user_ids
                    else sa.insert(patrol_sys_user).values(**values)
                )
                connection.execute(statement)
            user_ids = [user.user_id for user in snapshot.users]
            if user_ids:
                connection.execute(
                    sa.delete(patrol_user_role).where(patrol_user_role.c.user_id.in_(user_ids))
                )
            role_rows = [
                {
                    "user_id": user.user_id,
                    "role_code": role.code,
                    "role_name": role.name,
                    "fetched_at": snapshot.fetched_at,
                }
                for user in snapshot.users
                for role in user.roles
            ]
            if role_rows:
                connection.execute(sa.insert(patrol_user_role), role_rows)
            if snapshot.assignments:
                connection.execute(
                    sa.insert(patrol_asin_owner),
                    [
                        self._assignment_values(assignment, snapshot.fetched_at)
                        for assignment in snapshot.assignments
                    ],
                )
            self._backfill_catalog_owners(connection, snapshot)

    def _backfill_catalog_owners(
        self,
        connection: sa.Connection,
        snapshot: OwnerSyncSnapshot,
    ) -> None:
        """把负责人分配合并进经营单元目录，供巡检快照/中控/复查消费。"""
        owners_by_unit: dict[str, set[int]] = {}
        for assignment in snapshot.assignments:
            if assignment.principal_user_id is None:
                continue
            owners_by_unit.setdefault(
                assignment.binding.operating_unit_id, set()
            ).add(assignment.principal_user_id)
        if not owners_by_unit:
            return
        try:
            rows = connection.execute(
                sa.select(
                    patrol_operating_unit_catalog.c.operating_unit_id,
                    patrol_operating_unit_catalog.c.owner_user_ids_json,
                ).where(
                    patrol_operating_unit_catalog.c.operating_unit_id.in_(
                        list(owners_by_unit)
                    )
                )
            ).mappings()
        except sa.exc.OperationalError:
            return
        for row in rows:
            existing: set[int] = {
                int(value)
                for value in (row["owner_user_ids_json"] or [])
                if str(value).isdigit() and int(value) > 0
            }
            merged = sorted(
                existing | owners_by_unit.get(row["operating_unit_id"], set())
            )
            if merged == sorted(existing):
                continue
            connection.execute(
                sa.update(patrol_operating_unit_catalog)
                .where(
                    patrol_operating_unit_catalog.c.operating_unit_id
                    == row["operating_unit_id"]
                )
                .values(
                    owner_user_ids_json=merged,
                    updated_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )

    @staticmethod
    def _assignment_values(
        assignment: OwnerAssignment,
        fetched_at: datetime,
    ) -> dict[str, Any]:
        return {
            "operating_unit_id": assignment.binding.operating_unit_id,
            "shop_id": assignment.binding.shop_id,
            "shop_account_ref": assignment.binding.shop_account,
            "site_code": assignment.binding.site_code,
            "parent_asin": assignment.binding.parent_asin,
            "parent_seller_sku": assignment.binding.parent_seller_sku,
            "assignment_key": (
                f"PRINCIPAL:{assignment.principal_user_id}"
                if assignment.principal_user_id is not None
                else "FALLBACK"
            ),
            "principal_user_id": assignment.principal_user_id,
            "editor_user_id": assignment.editor_user_id,
            "creator_user_id": assignment.creator_user_id,
            "source_tool": assignment.source_tool,
            "fetched_at": fetched_at,
        }


class MySqlOwnerDirectoryProvider:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def list_all_active_units(self) -> list[OperatingUnitBinding]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(patrol_asin_owner).order_by(
                    patrol_asin_owner.c.operating_unit_id,
                    patrol_asin_owner.c.principal_user_id,
                )
            ).mappings()
            units: dict[str, OperatingUnitBinding] = {}
            for row in rows:
                principal_user_id = row["principal_user_id"]
                binding = OperatingUnitBinding(
                    shop_id=row["shop_id"],
                    shop_account=row["shop_account_ref"],
                    site_code=row["site_code"],
                    parent_asin=row["parent_asin"],
                    parent_seller_sku=row["parent_seller_sku"],
                    owner_user_ids=(principal_user_id,) if principal_user_id else (),
                )
                existing = units.get(binding.operating_unit_id)
                units[binding.operating_unit_id] = (
                    binding
                    if existing is None
                    else existing.with_owner_user_ids(
                        tuple({*existing.owner_user_ids, *binding.owner_user_ids})
                    )
                )
        return sorted(units.values(), key=lambda unit: unit.operating_unit_id)
