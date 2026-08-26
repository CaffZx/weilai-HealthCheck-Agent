"""探针：探测中控 OperatingMetric 真实 DTO 里有哪些字段。

手法：给每个候选字段塞一个"错误类型"的值（JSON object），
   - 中控报 "Cannot deserialize ... <field> from Object"  → 字段存在（类型不匹配）
   - 中控报 "Unrecognized field <field>"                  → 字段不存在且开启未知字段拒绝
   - 中控返回成功（静默忽略）                             → 字段不存在且未知字段被忽略

每个字段单独发一次 submit_patrol_batch，避免 Jackson 只报第一个错。
"""
from __future__ import annotations

import asyncio
import copy
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

# 候选字段：quality / traffic / inventory 里我们查到但可能漏传的
# 探测 inboundInventory / reservedInventory 在中控 DTO 的真实字段名
CANDIDATES = [
    "inboundNum", "inboundQuantity", "inboundStock", "inbound", "inTransitQuantity",
    "onWayQuantity", "incomingStock", "inboundQty",
    "reservedNum", "reservedQuantity", "reservedStock", "reserved",
    "fcTransferQuantity", "reservedQty",
]


def _classify(is_error: bool, text: str, field: str) -> str:
    if not is_error:
        return "ACCEPTED(字段不存在且被静默忽略)"
    low = text.lower()
    if "cannot deserialize" in low and field.lower() in low:
        return "EXISTS(字段存在，类型不符被拒)"
    if "unrecognized field" in low and field.lower() in low:
        return "NOT_EXIST(未知字段被拒=中控开启FAIL_ON_UNKNOWN)"
    if "cannot deserialize" in low or "unrecognized field" in low:
        return "UNKNOWN(报错但未命中字段名: " + text[:120] + ")"
    return "OTHER: " + text[:160]


async def main() -> int:
    request = build_request(
        "FIELD_PROBE_20260817",
        source_batch_ids=[SOURCE_BATCH],
        expected_count=50,
        probe=True,
    )
    base_payload = request.model_dump(mode="json", by_alias=True)
    parent_asin = base_payload["units"][0]["listing"]["parentAsin"]
    print(f"target unit parentAsin={parent_asin}")

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
                for i, field in enumerate(CANDIDATES):
                    payload = copy.deepcopy(base_payload)
                    payload["patrolBatchNo"] = f"FIELD_PROBE_20260817_{i:02d}"
                    payload["units"][0]["operatingMetric"][field] = {"__probe__": "wrong_type"}
                    try:
                        response = await session.call_tool("submit_patrol_batch", payload)
                        if response.is_error:
                            texts = [getattr(item, "text", "") for item in response.content]
                            text = " | ".join(texts)
                            verdict = _classify(True, text, field)
                            detail = text[:200]
                        else:
                            verdict = _classify(False, "", field)
                            detail = ""
                        print(f"[{field:28s}] {verdict}  {detail}")
                    except Exception as exc:  # noqa: BLE001
                        text = str(exc)
                        print(f"[{field:28s}] EXC {_classify(True, text, field)}  {text[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
