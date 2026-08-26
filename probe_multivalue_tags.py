"""探针：测试中控 submit_patrol_batch 是否接受多值标签（array）。

复用 build_request 生成一个真实可投递的 unit 载荷，然后把 tags.targetKeywordStrategy
和 tags.adDirection 从单值 string 改成 array，绕过本侧 Pydantic/schema 校验，
直接调中控 submit_patrol_batch，打印原始返回，验证中控是否接受多值。
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

# 该经营单元最近一次巡检所属 batch（B0EXAMPLE0 / ou_24d0bdb407032eb75718a11f）
SOURCE_BATCH = "batch_a770fbd9268a480090adf798"
PROBE_BATCH_NO = "MULTI_VALUE_PROBE_20260817_01"


async def main() -> int:
    # 1. 构建一个真实可投递的 unit 载荷（probe=True 只取第一个 submittable unit）
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
    print("原始 tags:", json.dumps(unit.get("tags"), ensure_ascii=False))

    # 2. 把两个标签改成多值 array
    unit["tags"]["targetKeywordStrategy"] = ["GENERIC", "LONG_TAIL"]
    unit["tags"]["adDirection"] = ["PUSH_NATURAL_RANK", "OPTIMIZE_ACOS"]
    print("改成多值后 tags:", json.dumps(unit.get("tags"), ensure_ascii=False))

    # 3. 直接调中控（绕过本侧 Pydantic/schema 校验）
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
                response = await session.call_tool(
                    "submit_patrol_batch",
                    payload,
                )

    print("\n=== 中控原始返回 ===")
    print("isError:", response.is_error)
    d = response.model_dump(mode="json", by_alias=True)
    if response.is_error:
        texts = [getattr(item, "text", "") for item in response.content]
        print("拒绝原因:", " | ".join(texts)[:3000])
    else:
        print(json.dumps(d, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
