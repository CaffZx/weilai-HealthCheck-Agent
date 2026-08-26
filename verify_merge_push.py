"""验证：子 ASIN 异常合并后，实推中控确认 targetType=OPERATING_UNIT + summary 带子体列表。

流程：重建快照 → inspect（合并）→ 构建信号 → _build_unit → 打印 proposal → 推中控。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime

from dotenv import load_dotenv

load_dotenv("/opt/weilai-HealthCheck-Agent-v2.0/.env")
sys.path.insert(0, "/opt/weilai-HealthCheck-Agent-v2.0")

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from core.contracts import (
    DataGap,
    OperatingFactSnapshot,
    OperatingUnitRef,
    SourceRef,
)
from core.enums import DataQualityStatus
from inspector.legacy_rule_adapter import LegacyRuleAdapter
from scripts.submit_real_patrol_to_control_center import (
    _build_unit,
    _latest_sources,
    _load_snapshot,
)
from core.control_center_contracts import SubmitPatrolBatchRequest
from web.backend.deps import get_database_engine

TARGET_OU = "ou_717ccd497bae7e38a94b49a8"
TARGET_ASIN = "B0EXAMPLE0"
SOURCE_BATCH = "batch_3f276bc01a8c4256b9c1834c"
PROBE_BATCH_NO = "MERGE_PUSH_VERIFY_20260817"


def _reconstruct(snapshot_dict: dict) -> OperatingFactSnapshot:
    norm = snapshot_dict.get("normalized_summary_json") or {}
    identity = norm.get("identity") or {}
    unit = OperatingUnitRef.derive(
        shop_id=identity.get("shop_id"),
        site_code=identity.get("site_code"),
        parent_asin=identity.get("parent_asin"),
        parent_seller_sku=identity.get("parent_seller_sku"),
    )
    return OperatingFactSnapshot(
        snapshot_id=snapshot_dict["snapshot_id"],
        operating_unit=unit,
        as_of_time=snapshot_dict["as_of"],
        content_hash=snapshot_dict["content_hash"],
        quality_status=DataQualityStatus(snapshot_dict["quality_status"]),
        completeness_score=float(snapshot_dict["completeness_score"] or 0),
        identity=identity,
        sales=norm.get("sales") or {},
        traffic=norm.get("traffic") or {},
        profit=norm.get("profit") or {},
        inventory=norm.get("inventory") or {},
        price=norm.get("price") or {},
        quality=norm.get("quality") or {},
        execution_history=norm.get("execution_history") or {},
        source_refs=[SourceRef.model_validate(r) for r in (snapshot_dict.get("source_refs_json") or [])],
        data_gaps=[DataGap.model_validate(g) for g in (snapshot_dict.get("data_gaps_json") or [])],
    )


def _signal_dicts(anomalies, snapshot_id: str) -> list[dict]:
    now = datetime.now(UTC)
    result = []
    for a in anomalies:
        result.append({
            "signal_id": a.signal_id,
            "signal_type": a.signal_type.value,
            "issue_code": a.issue_code,
            "severity": a.severity.value,
            "last_detected_at": now,
            "signal_payload_json": {
                "diagnosis": {
                    "summary": a.description,
                    "known_causes": [a.reason] if a.reason else [],
                    "unknowns": [],
                    "confidence": 0.9,
                },
                "child_scope": (
                    {"child_asins": list(a.child_asins), "exception_type": "OTHER"}
                    if a.child_asins
                    else None
                ),
                "evidence_refs": [snapshot_id],
            },
        })
    return result


async def main() -> int:
    with get_database_engine().connect() as conn:
        sources = _latest_sources(
            conn, source_batch_ids=[SOURCE_BATCH], expected_count=49, probe=False,
        )
        target = next(s for s in sources if str(s["parent_asin"]).strip().upper() == TARGET_ASIN)
        snapshot_dict = _load_snapshot(conn, target["result_ref"])

    snapshot = _reconstruct(snapshot_dict)
    anomalies = LegacyRuleAdapter().inspect(snapshot)
    signal_dicts = _signal_dicts(anomalies, snapshot_dict["snapshot_id"])

    unit = _build_unit(target, snapshot_dict, signal_dicts, None)
    proposal = unit["proposal"]
    print(f"父 ASIN: {unit['listing']['parentAsin']}")
    print(f"异常条数: {len(proposal['anomalies'])}")
    print("=== proposal.anomalies ===")
    for item in proposal["anomalies"]:
        print(f"  [{item['anomalyCode']}] targetType={item['targetType']} "
              f"targetId={item['targetId']}")
        print(f"      summary={item['summary'][:120]}")

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
        texts = [getattr(i, "text", "") for i in response.content]
        print("拒绝:", " | ".join(texts)[:2000])
        return 2
    print(json.dumps(d, ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
