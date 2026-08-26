"""验证中控 v1.0 多值标签协议：adPurposes / targetKeywordStrategies / adDirections。

走真实 build_request 构造载荷，打印 pipeline 产出的 tags（新格式），再强制把三个
标签设成多值数组实推 submit_patrol_batch，验证中控是否接受。
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

from scripts.submit_real_patrol_to_control_center import build_request

SOURCE_BATCH = "batch_a770fbd9268a480090adf798"
PROBE_BATCH_NO = "MULTI_VALUE_V2_VERIFY_20260817"


async def main() -> int:
    request = build_request(
        PROBE_BATCH_NO,
        source_batch_ids=[SOURCE_BATCH],
        expected_count=50,
        probe=True,
    )
    payload = request.model_dump(mode="json", by_alias=True)
    unit = payload["units"][0]
    parent_asin = unit["listing"]["parentAsin"]
    print(f"target unit parentAsin={parent_asin}")
    print("pipeline 产出 tags:", json.dumps(unit.get("tags"), ensure_ascii=False))

    # 强制多值，确定性验证中控是否接受三个复数数组
    # 先用大写 Java 枚举探明当前线上真实接收格式
    unit["tags"]["adPurposes"] = ["TRAFFIC", "CONVERSION", "RANKING"]
    unit["tags"]["targetKeywordStrategies"] = ["GENERIC", "LONG_TAIL", "COMPETITOR"]
    unit["tags"]["adDirections"] = ["PUSH_NATURAL_RANK", "OPTIMIZE_ACOS"]
    print("强制多值后 tags:", json.dumps(unit.get("tags"), ensure_ascii=False))

    url = os.environ["CONTROL_CENTER_MCP_URL"]
    token = os.environ["CONTROL_CENTER_MCP_TOKEN"]
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=90)) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                response = await session.call_tool("submit_patrol_batch", payload)

    print("\n=== 中控返回 ===")
    print("isError:", response.is_error)
    d = response.model_dump(mode="json", by_alias=True)
    if response.is_error:
        texts = [getattr(item, "text", "") for item in response.content]
        print("拒绝原因:", " | ".join(texts)[:3000])
        return 2
    print(json.dumps(d, ensure_ascii=False, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
