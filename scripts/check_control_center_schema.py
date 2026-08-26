from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _resolve_schema(schema: dict, node: dict) -> dict:
    reference = node.get("$ref")
    if not reference:
        return node
    current = schema
    for part in reference.removeprefix("#/").split("/"):
        current = current[part]
    return current


def _find_property(schema: dict, node: dict, *path: str) -> dict:
    current = node
    for part in path:
        current = _resolve_schema(schema, current)
        if current.get("type") == "array":
            current = current["items"]
            current = _resolve_schema(schema, current)
        current = current.get("properties", {}).get(part, {})
    return _resolve_schema(schema, current)


async def check(url: str, token: str) -> int:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20, read=40)) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                response = await session.list_tools()

    tool = next((item for item in response.tools if item.name == "submit_patrol_batch"), None)
    if tool is None:
        print("tool=submit_patrol_batch present=false")
        return 2
    schema = tool.input_schema
    serialized_schema = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    units = _find_property(schema, schema, "units")
    units_item = _resolve_schema(schema, units.get("items", {}))
    proposal = _find_property(schema, units, "proposal")
    anomalies = _find_property(schema, proposal, "anomalies")
    details = _find_property(schema, proposal, "details")
    detail_uid = _find_property(schema, details, "anomalyUid")
    required = set(proposal.get("required", []))
    details_item = _resolve_schema(schema, details.get("items", {}))
    detail_required = set(details_item.get("required", []))
    checks = {
        "anomalies_present": bool(anomalies),
        "anomalies_required": "anomalies" in required,
        "detail_anomaly_uid_present": bool(detail_uid),
        "detail_anomaly_uid_required": "anomalyUid" in detail_required,
    }
    print("tool=submit_patrol_batch present=true")
    print(
        "schema_sha256="
        + hashlib.sha256(serialized_schema.encode("utf-8")).hexdigest()
    )
    print("schema_top_properties=" + ",".join(sorted(schema.get("properties", {}).keys())))
    print("units_schema_keys=" + ",".join(sorted(units.keys())))
    print("units_item_schema_keys=" + ",".join(sorted(units_item.keys())))
    print(
        "units_item_properties="
        + ",".join(sorted(units_item.get("properties", {}).keys()))
    )
    print("proposal_schema_keys=" + ",".join(sorted(proposal.keys())))
    print("proposal_type=" + str(proposal.get("type")))
    print("proposal_required=" + ",".join(sorted(proposal.get("required", []))))
    has_anomalies = '"anomalies"' in serialized_schema
    print(f"schema_contains_anomalies={str(has_anomalies).lower()}")
    print(
        "schema_contains_anomaly_uid="
        + ("true" if 'anomalyUid' in serialized_schema else "false")
    )
    print(
        "proposal_properties="
        + ",".join(sorted(proposal.get("properties", {}).keys()))
    )
    print(
        "detail_properties="
        + ",".join(sorted(details_item.get("properties", {}).keys()))
    )
    for name, passed in checks.items():
        print(f"{name}={str(passed).lower()}")
    return 0 if all(checks.values()) else 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    token = os.environ.get("CONTROL_CENTER_MCP_TOKEN", "")
    if not token:
        raise SystemExit("CONTROL_CENTER_MCP_TOKEN is required")
    return asyncio.run(check(args.url, token))


if __name__ == "__main__":
    raise SystemExit(main())
