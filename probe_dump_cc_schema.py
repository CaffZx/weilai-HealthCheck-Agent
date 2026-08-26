"""探针：dump 中控 submit_patrol_batch 的实时输入 schema 到本地文件。

只执行 initialize + tools/list，不调用 tools/call，不写任何中控业务数据。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv("/opt/weilai-HealthCheck-Agent-v2.0/.env")
sys.path.insert(0, "/opt/weilai-HealthCheck-Agent-v2.0")

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> int:
    url = os.environ["CONTROL_CENTER_MCP_URL"]
    token = os.environ["CONTROL_CENTER_MCP_TOKEN"]
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20, read=40)) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                response = await session.list_tools()

    tool = next((t for t in response.tools if t.name == "submit_patrol_batch"), None)
    if tool is None:
        print("tool=submit_patrol_batch present=false")
        return 2
    schema = tool.input_schema
    out = "/opt/weilai-HealthCheck-Agent-v2.0/artifacts/cc_live_schema.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False, indent=2, sort_keys=True)
    print("dumped:", out)
    print("bytes:", os.path.getsize(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
