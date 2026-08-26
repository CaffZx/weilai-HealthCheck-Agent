from __future__ import annotations

import pytest
import sqlalchemy as sa

from clients.mcp_client import McpToolResult
from core.errors import McpContractInvalid
from integrations.owner_directory import (
    FOLLOW_UP_TOOL,
    SHOP_TOOL,
    USER_TOOL,
    MySqlOwnerDirectoryProvider,
    MySqlOwnerDirectoryStore,
    OwnerDirectoryCollector,
)


class FakeMcp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        key = (tool_name, arguments.get("principalName"), arguments["pageNo"])
        return McpToolResult(tool_name, [self.responses[key]], {})


def user_page(*users):
    return {"success": True, "data": list(users), "total": len(users), "pageNo": 1}


def shop_page():
    return {
        "success": True,
        "data": [
            {"id": 101, "account": "shop-us", "plSiteCode": "US"},
        ],
        "total": 1,
        "pageNo": 1,
    }


def principal_page(records):
    return {
        "totalCount": len(records),
        "totalPages": 1 if records else 0,
        "pageNo": 1,
        "pageSize": 200,
        "records": records,
    }


def listing(parent_asin="B0PARENT", **changes):
    value = {
        "shopAccount": "shop-us",
        "asin": "B0CHILD",
        "sellerSku": "CHILD-SKU",
        "parentAsin": parent_asin,
        "parentSellerSku": "PARENT-SKU",
        "status": "Active",
    }
    value.update(changes)
    return value


@pytest.fixture
def managed_engine():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE t_ops_operating_unit_config (
                shop_id TEXT, site_code TEXT, parent_asin TEXT,
                parent_seller_sku TEXT, editor_id INTEGER, creator_id INTEGER
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO t_ops_operating_unit_config VALUES "
            "('101','US','B0PARENT','PARENT-SKU',88,99)"
        )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_collector_filters_role_and_maps_user_id_to_managed_parent(managed_engine):
    directory = FakeMcp(
        {
            (USER_TOOL, None, 1): user_page(
                {
                    "id": 35,
                    "userName": "Operator A",
                    "userAccount": "operator-a",
                    "userState": "1",
                    "roles": [{"roleCode": "GROUP_FBASaler", "roleName": "FBA"}],
                    "password": "must-not-be-used",
                },
                {
                    "id": 77,
                    "userName": "Warehouse A",
                    "userState": "1",
                    "roles": [{"roleCode": "GROUP_WAREHOUSE"}],
                },
            ),
            (SHOP_TOOL, None, 1): shop_page(),
        }
    )
    listing_client = FakeMcp(
        {(FOLLOW_UP_TOOL, "Operator A", 1): principal_page([listing()])}
    )

    snapshot = await OwnerDirectoryCollector(
        directory, listing_client, managed_engine
    ).collect()

    assert len(snapshot.users) == 2
    assert snapshot.operator_user_count == 1
    assert snapshot.active_listing_count == 1
    assert len(snapshot.assignments) == 1
    assignment = snapshot.assignments[0]
    assert assignment.principal_user_id == 35
    assert assignment.editor_user_id == 88
    assert assignment.creator_user_id == 99
    assert assignment.binding.owner_user_ids == (35,)
    assert [call[1]["principalName"] for call in listing_client.calls] == ["Operator A"]
    assert not hasattr(snapshot.users[0], "password")


@pytest.mark.asyncio
async def test_collector_rejects_duplicate_active_operator_names(managed_engine):
    directory = FakeMcp(
        {
            (USER_TOOL, None, 1): user_page(
                {
                    "id": 35,
                    "userName": "Same Name",
                    "userState": "ACTIVE",
                    "roles": ["GROUP_FBASaler"],
                },
                {
                    "id": 42,
                    "userName": "Same Name",
                    "userState": "ACTIVE",
                    "roles": ["GROUP_FBASaler"],
                },
            ),
            (SHOP_TOOL, None, 1): shop_page(),
        }
    )

    with pytest.raises(McpContractInvalid, match="names are not unique"):
        await OwnerDirectoryCollector(directory, FakeMcp({}), managed_engine).collect()


@pytest.mark.asyncio
async def test_collector_preserves_multiple_principals_for_one_parent(managed_engine):
    directory = FakeMcp(
        {
            (USER_TOOL, None, 1): user_page(
                {
                    "id": 35,
                    "userName": "Operator A",
                    "userState": "ACTIVE",
                    "roles": ["GROUP_FBASaler"],
                },
                {
                    "id": 42,
                    "userName": "Operator B",
                    "userState": "ACTIVE",
                    "roles": ["GROUP_FBASaler"],
                },
            ),
            (SHOP_TOOL, None, 1): shop_page(),
        }
    )
    listing_client = FakeMcp(
        {
            (FOLLOW_UP_TOOL, "Operator A", 1): principal_page([listing()]),
            (FOLLOW_UP_TOOL, "Operator B", 1): principal_page([listing()]),
        }
    )

    snapshot = await OwnerDirectoryCollector(
        directory, listing_client, managed_engine
    ).collect()

    assert {item.principal_user_id for item in snapshot.assignments} == {35, 42}
    assert len({item.binding.operating_unit_id for item in snapshot.assignments}) == 1


def test_store_replaces_snapshot_and_provider_aggregates_multiple_owners(managed_engine):
    from datetime import UTC, datetime

    from core.operating_unit import OperatingUnitBinding
    from integrations.owner_directory import (
        OwnerAssignment,
        OwnerSyncSnapshot,
        SystemUser,
        UserRole,
    )
    from integrations.repositories.tables import (
        patrol_asin_owner,
        patrol_operating_unit_catalog,
        patrol_sys_user,
        patrol_user_role,
    )

    patrol_sys_user.create(managed_engine)
    patrol_user_role.create(managed_engine)
    patrol_asin_owner.create(managed_engine)
    patrol_operating_unit_catalog.create(managed_engine)
    fetched_at = datetime.now(UTC)
    binding = OperatingUnitBinding(101, "US", "B0PARENT", "shop-us", "PARENT-SKU")
    with managed_engine.begin() as connection:
        connection.execute(
            sa.insert(patrol_operating_unit_catalog).values(
                operating_unit_id=binding.operating_unit_id,
                shop_id=101,
                site_code="AMAZON_US",
                parent_asin="B0PARENT",
                parent_seller_sku="PARENT-SKU",
                shop_account_ref="shop-us",
                owner_user_ids_json=[],
                source_tool="test",
                fetched_at=fetched_at.replace(tzinfo=None),
                created_at=fetched_at.replace(tzinfo=None),
                updated_at=fetched_at.replace(tzinfo=None),
            )
        )
    users = (
        SystemUser(35, "Operator A", "a", "ACTIVE", (UserRole("GROUP_FBASALER"),)),
        SystemUser(42, "Operator B", "b", "ACTIVE", (UserRole("GROUP_FBASALER"),)),
    )
    snapshot = OwnerSyncSnapshot(
        users=users,
        operator_user_count=2,
        active_listing_count=2,
        out_of_scope_count=0,
        unresolved_shop_count=0,
        assignments=(
            OwnerAssignment(binding.with_owner_user_ids((35,)), 35, 88, 99),
            OwnerAssignment(binding.with_owner_user_ids((42,)), 42, 88, 99),
        ),
        fetched_at=fetched_at,
    )

    MySqlOwnerDirectoryStore(managed_engine).replace(snapshot)
    units = __import__("asyncio").run(
        MySqlOwnerDirectoryProvider(managed_engine).list_all_active_units()
    )

    assert len(units) == 1
    assert units[0].owner_user_ids == (35, 42)
    with managed_engine.connect() as connection:
        catalog_owners = connection.execute(
            sa.select(patrol_operating_unit_catalog.c.owner_user_ids_json)
        ).scalar_one()
    assert sorted(catalog_owners) == [35, 42]
