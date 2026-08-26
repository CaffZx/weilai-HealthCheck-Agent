"""端到端验证：B0EXAMPLE0 的多值标签从 pipeline 自然产出 → 推中控。

不做任何手动塞值，完整走 _latest_sources → _build_unit → _control_center_tags，
观察 pipeline 自然产出的 tags（复数数组 + 值格式），再实推 submit_patrol_batch。
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

from core.control_center_contracts import SubmitPatrolBatchRequest
from scripts.submit_real_patrol_to_control_center import (
    _build_unit,
    _latest_sources,
    _load_signals,
    _load_snapshot,
)
from web.backend.deps import get_database_engine

SOURCE_BATCH = "batch_a770fbd9268a480090adf798"
TARGET_ASIN = "B0EXAMPLE0"
PROBE_BATCH_NO = "E2E_MULTI_VALUE_20260817"


async def main() -> int:
    with get_database_engine().connect() as connection:
        sources = _latest_sources(
            connection,
            source_batch_ids=[SOURCE_BATCH],
            expected_count=50,
            probe=False,
        )
        target = next(
            (s for s in sources if str(s["parent_asin"]).strip().upper() == TARGET_ASIN),
            None,
        )
        if target is None:
            print(f"未找到目标 ASIN {TARGET_ASIN}")
            return 2
        snapshot = _load_snapshot(connection, target["result_ref"])
        signals = _load_signals(connection, target["result_ref"])
        unit = _build_unit(target, snapshot, signals, None)

    print(f"target parentAsin={unit['listing']['parentAsin']} "
          f"parentSellerSku={unit['listing']['parentSellerSku']}")
    print("pipeline 自然产出 tags:")
    print(json.dumps(unit.get("tags"), ensure_ascii=False, indent=2))

    request = SubmitPatrolBatchRequest.model_validate(
        {"patrolBatchNo": PROBE_BATCH_NO, "units": [unit]}
    )
    payload = request.model_dump(mode="json", by_alias=True)

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
        print("拒绝:", " | ".join(texts)[:3000])
        return 2
    print(json.dumps(d, ensure_ascii=False, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
