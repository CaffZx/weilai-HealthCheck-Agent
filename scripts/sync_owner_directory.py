from __future__ import annotations

import argparse
import asyncio
import json
import os

from clients.azlisting_contract import AZLISTING_LIST_UNITS_BY_PRINCIPAL_TOOL
from clients.mcp_client import ReusableMcpSessionClient
from integrations.database import create_database_engine
from integrations.owner_directory import (
    DEFAULT_OPERATOR_ROLE,
    SHOP_TOOL,
    USER_TOOL,
    MySqlOwnerDirectoryStore,
    OwnerDirectoryCollector,
    parse_owner_targets_json,
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def run(*, apply: bool, targets_json: str = "") -> dict[str, int | bool]:
    token = _required_environment("MCP_API_KEY")
    engine = create_database_engine()
    targets = parse_owner_targets_json(targets_json) if targets_json else ()
    directory_client = ReusableMcpSessionClient(
        os.environ.get(
            "MCP_SYNC_GATEWAY",
            "http://mcp-gateway.example.com/mcp",
        ),
        token,
        timeout_seconds=60,
        allowed_tools=frozenset({USER_TOOL, SHOP_TOOL}),
        auth_header="X-Api-Key",
        auth_scheme="",
    )
    listing_client = ReusableMcpSessionClient(
        os.environ.get(
            "AZLISTING_GATEWAY",
            "http://mcp-gateway.example.com/mcp",
        ),
        token,
        timeout_seconds=60,
        allowed_tools=frozenset({AZLISTING_LIST_UNITS_BY_PRINCIPAL_TOOL}),
    )
    try:
        async with directory_client, listing_client:
            snapshot = await OwnerDirectoryCollector(
                directory_client,
                listing_client,
                engine,
                operator_role=os.environ.get(
                    "OWNER_OPERATOR_ROLE",
                    DEFAULT_OPERATOR_ROLE,
                ),
                targets=targets,
            ).collect()
            if apply:
                store = MySqlOwnerDirectoryStore(engine)
                if targets:
                    store.replace_targets(snapshot, targets)
                else:
                    store.replace(snapshot)
            principal_units = {
                assignment.binding.operating_unit_id
                for assignment in snapshot.assignments
                if assignment.principal_user_id is not None
            }
            return {
                "applied": apply,
                "user_count": len(snapshot.users),
                "operator_user_count": snapshot.operator_user_count,
                "active_listing_count": snapshot.active_listing_count,
                "principal_unit_count": len(principal_units),
                "assignment_count": len(snapshot.assignments),
                "target_count": len(targets),
                "out_of_scope_count": snapshot.out_of_scope_count,
                "unresolved_shop_count": snapshot.unresolved_shop_count,
                "failed_operator_count": len(snapshot.failed_operators),
                "failed_operators": list(snapshot.failed_operators[:20]),
            }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="同步巡检负责人目录")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="原子替换 MySQL 负责人目录；默认只预演不写库",
    )
    parser.add_argument(
        "--targets-env",
        default="OWNER_TARGET_UNITS_JSON",
        help="定向经营单元 JSON 所在环境变量",
    )
    args = parser.parse_args()
    targets_json = os.environ.get(args.targets_env, "").strip()
    print(
        json.dumps(
            asyncio.run(run(apply=args.apply, targets_json=targets_json)),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
