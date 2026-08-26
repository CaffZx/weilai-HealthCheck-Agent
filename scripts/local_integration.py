from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import sys
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import inspect, text

from integrations.database import create_database_engine, database_url

EXPECTED_REVISION = "0012_listing_catalog_source"
REVIEW_TOOL = "review_patrol_result"


def is_loopback_host(host: str) -> bool:
    value = host.strip().lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def require_environment(*names: str) -> None:
    missing = [name for name in names if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError(f"missing environment variables: {', '.join(missing)}")


def database_status() -> dict[str, Any]:
    url = database_url()
    if url.get_backend_name() != "mysql":
        raise RuntimeError("local integration requires MySQL")
    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM t_patrol_schema_version")
            ).scalar_one()
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            baseline_count = connection.execute(
                text("SELECT COUNT(*) FROM t_patrol_fact_snapshot")
            ).scalar_one()
        if revision != EXPECTED_REVISION:
            raise RuntimeError(
                f"schema revision must be {EXPECTED_REVISION}, got {revision}"
            )
        if "t_patrol_review" not in tables:
            raise RuntimeError("t_patrol_review is missing")
        return {
            "host": url.host or "",
            "database": url.database or "",
            "revision": revision,
            "baseline_count": int(baseline_count),
        }
    finally:
        engine.dispose()


def preflight(*, require_control_center: bool) -> int:
    required = ["PATROL_DATABASE_URL", "MCP_API_KEY"]
    if require_control_center:
        required.extend(("CONTROL_CENTER_MCP_URL", "CONTROL_CENTER_MCP_TOKEN"))
    require_environment(*required)
    status = database_status()
    print(
        "local integration preflight ok: "
        f"mysql_host={status['host']} database={status['database']} "
        f"revision={status['revision']} baselines={status['baseline_count']} "
        f"control_center={'required' if require_control_center else 'optional'}"
    )
    return 0


def serve_review() -> int:
    require_environment("PATROL_DATABASE_URL", "MCP_API_KEY")
    host = os.getenv("REVIEW_MCP_HOST", "127.0.0.1")
    if not is_loopback_host(host):
        raise RuntimeError("local integration review MCP must bind to a loopback host")
    if os.getenv("REVIEW_MCP_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        raise RuntimeError("REVIEW_MCP_ENABLED must be explicitly enabled")
    database_status()

    from integrations.review_mcp_server import create_review_mcp_server
    from web.backend.deps import get_review_service

    port = int(os.getenv("REVIEW_MCP_PORT", "8791"))
    server = create_review_mcp_server(get_review_service(), enabled=lambda: True)
    print(f"local review MCP ready on http://{host}:{port}/mcp")
    server.run(
        "streamable-http",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )
    return 0


async def _smoke_review(url: str) -> None:
    async with httpx.AsyncClient(
        headers={"Accept": "application/json, text/event-stream"},
        timeout=httpx.Timeout(10, read=20),
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
    names = sorted(tool.name for tool in tools.tools)
    if REVIEW_TOOL not in names:
        raise RuntimeError(f"{REVIEW_TOOL} is not discoverable")
    print(f"review MCP smoke ok: tools={','.join(names)}")


async def _smoke_azlisting(url: str, token: str) -> None:
    async with httpx.AsyncClient(
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        timeout=httpx.Timeout(10, read=20),
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
    names = sorted(tool.name for tool in tools.tools)
    print(f"AZListing MCP smoke ok: discovered_tools={len(names)}")


def smoke_review(url: str) -> int:
    if not url.startswith(("http://127.0.0.1:", "http://[::1]:", "http://localhost:")):
        raise RuntimeError("local smoke URL must use a loopback host")
    asyncio.run(_smoke_review(url))
    return 0


def smoke_azlisting() -> int:
    require_environment("MCP_API_KEY")
    url = os.getenv(
        "AZLISTING_GATEWAY",
        "http://mcp-gateway.example.com/mcp",
    ).strip()
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("AZLISTING_GATEWAY must use HTTP or HTTPS")
    asyncio.run(_smoke_azlisting(url, os.environ["MCP_API_KEY"]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe local integration utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--require-control-center", action="store_true")
    subparsers.add_parser("serve-review")
    smoke_parser = subparsers.add_parser("smoke-review")
    smoke_parser.add_argument("--url", default="http://127.0.0.1:8791/mcp")
    subparsers.add_parser("smoke-azlisting")
    args = parser.parse_args()

    try:
        if args.command == "preflight":
            return preflight(require_control_center=args.require_control_center)
        if args.command == "serve-review":
            return serve_review()
        if args.command == "smoke-review":
            return smoke_review(args.url)
        return smoke_azlisting()
    except KeyboardInterrupt:
        print("local integration stopped")
        return 130
    except Exception as exc:
        print(f"local integration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
