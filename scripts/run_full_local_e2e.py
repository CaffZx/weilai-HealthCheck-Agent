from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import sqlalchemy as sa
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from clients.control_center_mcp import ControlCenterPatrolMcpClient
from clients.operating_mode_mcp import OperatingModeMcpClient
from core.contracts import canonical_json, sha256_json
from core.control_center_contracts import (
    PatrolUnitPayload,
    ReviewRequest,
    ReviewResult,
    SubmitPatrolBatchRequest,
    _to_platform_site_code,
    apply_current_operating_mode,
    build_patrol_listing,
    derive_advert_mode_candidate,
)
from core.enums import DeliveryStatus, JobStatus, TriggerType
from core.operating_unit import OperatingUnitBinding
from integrations.control_center_delivery import (
    ControlCenterBatchPublisher,
    MySqlControlCenterDeliveryWorker,
)
from integrations.review import MySqlReviewStore
from integrations.rollout import RolloutPolicy
from integrations.runtime_queue import MySqlRuntimeQueue
from integrations.runtime_worker import MySqlRuntimeWorker
from web.backend.deps import (
    get_database_engine,
    get_runtime_orchestrator,
)

ROOT = Path(__file__).resolve().parent.parent


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _required_number(value: Any, field: str) -> float:
    number = _number(value)
    if number is None:
        raise ValueError(f"local E2E cannot fabricate missing field: {field}")
    return number


def _fact_snapshot(
    *,
    snapshot: Any,
    snapshot_type: str,
    section: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    metrics = {
        key: value
        for key, value in section.items()
        if isinstance(value, (str, int, float, bool)) and value is not None
    }
    if not metrics:
        metrics = {"sourceSnapshotId": snapshot.snapshot_id}
    return {
        "snapshotType": snapshot_type,
        "sourceSystem": "weilai-healthcheck-agent-v2",
        "windowStart": window_start.isoformat(),
        "windowEnd": window_end.isoformat(),
        "contentHash": sha256_json(
            {
                "sourceSnapshotId": snapshot.snapshot_id,
                "snapshotType": snapshot_type,
                "section": section,
            }
        ),
        "qualityStatus": "PARTIAL" if snapshot.is_blocking else "COMPLETE",
        "completenessScore": snapshot.completeness_score * 100,
        "keyMetricsJson": metrics,
        "rawReferenceJson": {
            "isSimulated": False,
            "sourceSnapshotId": snapshot.snapshot_id,
            "sourceSnapshotHash": snapshot.content_hash,
            "sourceRefCount": len(snapshot.source_refs),
        },
    }


def build_control_center_request(
    *,
    patrol_batch_no: str,
    run_id: str,
    binding: OperatingUnitBinding,
    snapshot: Any,
    mode: Any,
    product_pic_url_cache: str | None = None,
) -> SubmitPatrolBatchRequest:
    inspection_time = snapshot.as_of_time.astimezone(UTC)
    metric_date = inspection_time.date() - timedelta(days=1)
    sales_rows = list(snapshot.sales.get("daily_rows") or [])
    latest = next(
        (row for row in sales_rows if row.get("stat_date") == metric_date.isoformat()),
        None,
    )
    if latest is None:
        raise ValueError(
            f"local E2E cannot fabricate operatingMetric for {metric_date.isoformat()}"
        )
    sales_amount = _required_number(latest.get("revenue"), "salesAmount")
    sales_quantity = int(_required_number(latest.get("units"), "salesQuantity"))
    order_quantity = _number(latest.get("orders"))
    ad_cost = _required_number(latest.get("ad_cost"), "adCost")
    ad_sales = _number(latest.get("ad_sales"))
    ad_orders = _number(latest.get("ad_sale_num"))
    # contributionProfit: 优先 unit_contribution，兜底 gross_profit_amount
    unit_contribution = _number(snapshot.profit.get("unit_contribution"))
    if unit_contribution is not None:
        contribution_profit = unit_contribution * sales_quantity
    else:
        contribution_profit = _required_number(
            latest.get("gross_profit_amount"), "contributionProfit"
        )
    available_inventory = _required_number(
        snapshot.inventory.get("fba_available"), "canSaleNum"
    )
    window_end = datetime.combine(metric_date, datetime.max.time(), tzinfo=UTC)
    window_start = datetime.combine(
        metric_date - timedelta(days=29),
        datetime.min.time(),
        tzinfo=UTC,
    )
    fact_snapshots = [
        _fact_snapshot(
            snapshot=snapshot,
            snapshot_type="SALES",
            section=snapshot.sales,
            window_start=window_start,
            window_end=window_end,
        ),
        _fact_snapshot(
            snapshot=snapshot,
            snapshot_type="ADVERTISING",
            section={
                "acos7d": snapshot.profit.get("acos_7d"),
                "adSpend7d": ad_cost,
                "adSales7d": ad_sales,
                "adOrders7d": ad_orders,
            },
            window_start=window_start,
            window_end=window_end,
        ),
        _fact_snapshot(
            snapshot=snapshot,
            snapshot_type="PROFIT",
            section=snapshot.profit,
            window_start=window_start,
            window_end=window_end,
        ),
        _fact_snapshot(
            snapshot=snapshot,
            snapshot_type="INVENTORY",
            section=snapshot.inventory,
            window_start=datetime.combine(metric_date, datetime.min.time(), tzinfo=UTC),
            window_end=window_end,
        ),
    ]
    evidence_ref = fact_snapshots[0]["contentHash"]
    listing = build_patrol_listing(
        binding, snapshot, product_pic_url_cache=product_pic_url_cache
    )
    mode_candidate = derive_advert_mode_candidate(snapshot)
    unit = PatrolUnitPayload.model_validate(
        {
            "listing": listing.model_dump(mode="json", by_alias=True),
            "tags": {
                "productLevel": "P1_PRODUCT",
            },
            "operatingMetric": {
                "metricDate": metric_date.isoformat(),
                "salesAmount": sales_amount,
                "salesQuantity": sales_quantity,
                "orderQuantity": int(order_quantity) if order_quantity is not None else None,
                "contributionProfit": contribution_profit,
                "adCost": ad_cost,
                "adSalesAmount": ad_sales,
                "adOrder": int(ad_orders) if ad_orders is not None else None,
                "canSaleNum": int(available_inventory),
                "inboundInventory": (
                    int(value)
                    if (value := _number(snapshot.inventory.get("fba_inbound")))
                    is not None
                    else None
                ),
                "reservedInventory": (
                    int(value)
                    if (value := _number(snapshot.inventory.get("fba_reserved")))
                    is not None
                    else None
                ),
            },
            "factSnapshots": fact_snapshots,
            "proposal": {
                "status": "PENDING_APPROVAL",
                "recommendedBusinessModel": None,
                "modeConfidence": None,
                "modeReasonSummary": None,
                "problemSummary": "真实巡检事实完整度不足，本次仅验证完整技术闭环",
                "rootCauses": [
                    {
                        "code": "DATA_INSUFFICIENT",
                        "summary": "巡检快照未达到生成可执行专业建议的证据要求",
                        "evidenceRef": evidence_ref,
                    }
                ],
                "recommendationSummary": "不执行经营动作，仅完成中控接收与复盘合同验证",
                "goal": "验证真实巡检事实、中控回执和复盘结果可端到端追溯",
                "expectedEffect": {"technicalE2EValidated": True},
                "riskSummary": {
                    "businessExecution": "禁止执行该模拟建议",
                    "mitigation": "待负责人、经营配置和专业建议来源冻结后重新巡检",
                },
                "confidence": 1,
                "approvalLevel": "H3",
                "observationWindowDays": 1,
                "reviewDueAt": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "rawAnalysis": {
                    "isSimulated": True,
                    "samplePurpose": "完整技术闭环联调，不代表真实经营建议",
                    "realPatrolRunId": run_id,
                    "realFactSnapshotId": snapshot.snapshot_id,
                    "realFactQualityStatus": snapshot.quality_status.value,
                    "realFactCompleteness": snapshot.completeness_score,
                    "simulatedFields": [
                        "tags",
                        "proposal",
                    ],
                    "operatingModeLookupStatus": (
                        mode.decision_status if mode is not None else "NO_RECORD"
                    ),
                    **(
                        {"operatingModeCandidate": mode_candidate}
                        if mode_candidate is not None
                        else {}
                    ),
                },
                "anomalies": [
                    {
                        "anomalyUid": "ANOMALY-DATA-GAP-001",
                        "anomalyCategory": "TRANSACTION_PERFORMANCE",
                        "anomalyCode": "TARGET_DEVIATION",
                        "causeMode": "INVESTIGATE",
                        "targetType": "OPERATING_UNIT",
                        "targetId": binding.operating_unit_id,
                        "severity": "LOW",
                        "summary": "真实巡检事实完整度不足，暂不生成可执行经营建议",
                        "metric": {
                            "completenessScore": snapshot.completeness_score,
                        },
                        "evidenceRefs": [evidence_ref],
                        "rootCauseStatus": "PENDING",
                        "rootCauses": [
                            {
                                "code": "DATA_INSUFFICIENT",
                                "description": "事实未达到专业建议证据要求",
                            }
                        ],
                        "detectedAt": datetime.now(UTC).isoformat(),
                        "status": "PROPOSED",
                    }
                ],
                "details": [
                    {
                        "anomalyUid": "ANOMALY-DATA-GAP-001",
                        "domain": "OTHER",
                        "actionType": "OTHER",
                        "targetType": "OPERATING_UNIT",
                        "targetId": binding.operating_unit_id,
                        "proposedValue": {"action": "NO_BUSINESS_EXECUTION"},
                        "reason": "仅验证完整技术链路，真实事实不足且负责人缺失",
                        "expectedEffect": {"technicalE2EValidated": True},
                        "risk": {"businessRisk": "不得投产执行"},
                        "evidenceRefs": [evidence_ref],
                        "approvalLevel": "H3",
                        "reversible": True,
                        "priority": 1,
                        "observationWindowDays": 1,
                        "status": "PENDING_APPROVAL",
                    }
                ],
            },
        }
    )
    enriched = apply_current_operating_mode(unit, mode)
    return SubmitPatrolBatchRequest(patrol_batch_no=patrol_batch_no, units=[enriched])


def _structured_content(response: Any) -> dict[str, Any]:
    payload = response.model_dump(mode="json")
    if payload.get("isError") is True or payload.get("is_error") is True:
        raise RuntimeError("review MCP returned isError=true")
    structured = payload.get("structuredContent") or payload.get("structured_content")
    if not isinstance(structured, dict):
        raise RuntimeError("review MCP returned no structured result")
    return structured


async def call_review_mcp(url: str, request: ReviewRequest) -> ReviewResult:
    async with httpx.AsyncClient(
        headers={"Accept": "application/json, text/event-stream"},
        timeout=httpx.Timeout(30, read=90),
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                response = await session.call_tool(
                    "review_patrol_result",
                    {"payload": request.model_dump(mode="json", by_alias=True)},
                )
    return ReviewResult.model_validate(_structured_content(response))


async def run(args: argparse.Namespace) -> dict[str, Any]:
    token = uuid4().hex[:12]
    binding = OperatingUnitBinding(
        shop_id=1562,
        site_code="AMAZON_US",
        parent_asin="B0EXAMPLE0",
        parent_seller_sku="FS04026-zhu",
        shop_account="am_example_us",
    )
    engine = get_database_engine()
    queue = MySqlRuntimeQueue(engine)
    batch_id, created = queue.create_batch(
        trigger_type=TriggerType.MANUAL,
        business_date=date.today(),
        units=[binding],
        rule_bundle_version="full-local-e2e-v1",
        idempotency_key=f"FULL_LOCAL_E2E:{token}",
        scope={
            "kind": "MANUAL",
            "request_id": f"full-e2e-{token}",
            "trace_id": f"full-e2e-trace-{token}",
            "initialization_ready": False,
            "operating_unit_ids": [binding.operating_unit_id],
        },
    )
    if not created:
        raise RuntimeError("full E2E batch unexpectedly already existed")
    envelopes = []

    async def patrol_handler(job: Any) -> str:
        envelope = await get_runtime_orchestrator().run_job(job, as_of=date.today())
        envelopes.append(envelope)
        return envelope.run.run_id

    worker = MySqlRuntimeWorker(
        queue=queue,
        worker_id=f"full-local-e2e:{token}",
        handlers={"patrol.run": patrol_handler},
        rollout_policy=RolloutPolicy(stage="CANARY", allowlisted_shop_ids=[binding.shop_id]),
    )
    worker_result = await worker.process_once()
    if worker_result.status is not JobStatus.SUCCEEDED or len(envelopes) != 1:
        raise RuntimeError(f"real patrol worker failed: {worker_result.status}")
    envelope = envelopes[0]
    snapshot = MySqlReviewStore(engine).load_baseline(
        envelope.run.fact_snapshot_id,
        binding.operating_unit_id,
    )

    mode_client = OperatingModeMcpClient(args.mode_url, "local-mock-token")
    try:
        mode = await mode_client.get_evaluate_by_key(
            shop_id=binding.shop_id,
            shop_account=binding.shop_account,
            site_code=binding.site_code,
            parent_asin=binding.parent_asin,
            parent_seller_sku=binding.parent_seller_sku or "",
        )
    finally:
        await mode_client.aclose()

    patrol_batch_no = f"FULL-E2E-{token}"
    request = build_control_center_request(
        patrol_batch_no=patrol_batch_no,
        run_id=envelope.run.run_id,
        binding=binding,
        snapshot=snapshot,
        mode=mode,
    )
    outbox_id = await ControlCenterBatchPublisher(
        engine=engine,
        rollout_policy=RolloutPolicy(stage="FULL"),
    ).enqueue(request)
    control_client = ControlCenterPatrolMcpClient(
        args.control_center_url,
        "local-mock-token",
    )
    delivery_worker = MySqlControlCenterDeliveryWorker(
        engine=engine,
        client=control_client,
        worker_id=f"full-local-e2e-control:{token}",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    try:
        delivery = await delivery_worker.process_once()
    finally:
        await control_client.aclose()
    if delivery.status is not DeliveryStatus.DELIVERED or delivery.receipt is None:
        raise RuntimeError(f"control center mock delivery failed: {delivery.error}")
    proposal_id = delivery.receipt.results[0].proposal_id
    if not proposal_id:
        raise RuntimeError("control center receipt did not include proposalId")

    review_request = ReviewRequest.model_validate(
        {
            "reviewRequestId": f"FULL-E2E-REVIEW-{token}",
            "reviewRound": 1,
            "patrolBatchNo": patrol_batch_no,
            "proposalId": proposal_id,
            "baselineSnapshotId": snapshot.snapshot_id,
            "shopId": binding.shop_id,
            "shopAccount": binding.shop_account,
            "siteCode": _to_platform_site_code(binding.site_code),
            "parentAsin": binding.parent_asin,
            "parentSellerSku": binding.parent_seller_sku,
            "approvedPlan": {
                "items": [{"planItemId": "SIM-NO-EXECUTION-001"}]
            },
            "executionReceipts": [
                {
                    "planItemId": "SIM-NO-EXECUTION-001",
                    "platformStatus": "EFFECTIVE",
                }
            ],
            "successCriteria": [
                {
                    "criterionId": "facts-collected",
                    "metricPath": "sales.row_count",
                    "operator": "GTE",
                    "target": 1,
                }
            ],
            "failureCriteria": [],
            "requestedAt": datetime.now(UTC).isoformat(),
        }
    )
    review = await call_review_mcp(args.review_url, review_request)

    with engine.connect() as connection:
        run_row = connection.execute(
            sa.text(
                "SELECT status, fact_snapshot_id, signal_count, data_gap_count "
                "FROM t_patrol_run WHERE run_id=:run_id"
            ),
            {"run_id": envelope.run.run_id},
        ).mappings().one()
        raw_fact_count = connection.execute(
            sa.text("SELECT COUNT(*) FROM t_patrol_raw_fact WHERE run_id=:run_id"),
            {"run_id": envelope.run.run_id},
        ).scalar_one()
        outbox_row = connection.execute(
            sa.text(
                "SELECT status, delivery_ack_json FROM t_patrol_delivery_outbox "
                "WHERE outbox_id=:outbox_id"
            ),
            {"outbox_id": outbox_id},
        ).mappings().one()
        review_status = connection.execute(
            sa.text(
                "SELECT status FROM t_patrol_review "
                "WHERE review_request_id=:request_id AND review_round=1"
            ),
            {"request_id": review.review_request_id},
        ).scalar_one()

    return {
        "testDataBoundary": {
            "real": ["AZListing facts", "MySQL snapshot", "patrol analysis", "review facts"],
            "mock": ["operating mode response", "owner/tags/proposal", "control center"],
        },
        "batchId": batch_id,
        "runId": envelope.run.run_id,
        "runStatus": run_row["status"],
        "snapshotId": run_row["fact_snapshot_id"],
        "snapshotQuality": snapshot.quality_status.value,
        "snapshotCompleteness": snapshot.completeness_score,
        "rawFactCount": int(raw_fact_count),
        "signalCount": int(run_row["signal_count"]),
        "dataGapCount": int(run_row["data_gap_count"]),
        "operatingModeStatus": mode.decision_status if mode else "NO_RECORD",
        "operatingModeCode": mode.recommended_mode_code if mode else None,
        "controlCenterBatchNo": patrol_batch_no,
        "controlCenterOutboxId": outbox_id,
        "controlCenterDeliveryStatus": outbox_row["status"],
        "controlCenterReceiptCode": delivery.receipt.results[0].code,
        "proposalId": proposal_id,
        "reviewRequestId": review.review_request_id,
        "reviewPersistenceStatus": review_status,
        "reviewOutcome": review.outcome_status,
        "reviewCurrentSnapshotId": review.current_fact_snapshot.snapshot_id,
        "reviewNextRecommendation": review.next_recommendation,
        "evidenceDigest": sha256_json(
            {
                "batchId": batch_id,
                "runId": envelope.run.run_id,
                "snapshotId": snapshot.snapshot_id,
                "outboxId": outbox_id,
                "reviewRequestId": review.review_request_id,
            }
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the complete local patrol E2E flow")
    parser.add_argument(
        "--mode-url",
        default="http://127.0.0.1:18801/mcp",
    )
    parser.add_argument(
        "--control-center-url",
        default="http://127.0.0.1:18757/mcp",
    )
    parser.add_argument(
        "--review-url",
        default="http://127.0.0.1:18791/mcp",
    )
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(canonical_json(result))


if __name__ == "__main__":
    main()
