from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.contracts import OperatingFactSnapshot, OperatingUnitRef
from core.control_center_contracts import (
    PatrolListing,
    PatrolUnitPayload,
    ReviewCriterion,
    ReviewRequest,
    SubmitPatrolBatchResult,
)
from core.enums import DataQualityStatus
from core.operating_unit import OperatingUnitBinding
from facts.collector import FactCollector
from facts.normalizer import FactNormalizer
from facts.quality import FactQualityService
from facts.snapshot import FactSnapshotBuilder
from integrations.review import ReviewService

ROOT = Path(__file__).resolve().parents[2]


class MemoryReviewStore:
    def __init__(self, baseline: OperatingFactSnapshot) -> None:
        self.baseline = baseline
        self.result = None
        self.hash = None
        self.started = False

    def load_baseline(self, snapshot_id, operating_unit_id):
        assert snapshot_id == self.baseline.snapshot_id
        assert operating_unit_id == self.baseline.operating_unit.operating_unit_id
        return self.baseline

    def get_result(self, request_id, review_round, request_hash):
        del request_id, review_round
        if self.hash and self.hash != request_hash:
            raise RuntimeError("hash mismatch")
        return self.result

    def start(self, request):
        if self.started:
            return False
        self.started = True
        self.hash = request.request_hash()
        return True

    def complete(self, request, result):
        del request
        self.result = result

    def fail(self, request, error):
        del request, error

    def resolve_owner_names(self, owner_user_ids):
        del owner_user_ids
        return {}

    def load_owner_user_ids(self, operating_unit_id):
        del operating_unit_id
        return ()


def make_request(binding, baseline):
    return ReviewRequest(
        review_request_id="review-request-1",
        review_round=1,
        patrol_batch_no="PT-20260804-001",
        proposal_id="proposal-1",
        baseline_snapshot_id=baseline.snapshot_id,
        shop_id=binding.shop_id,
        shop_account=binding.shop_account,
        site_code=binding.site_code,
        parent_asin=binding.parent_asin,
        parent_seller_sku=binding.parent_seller_sku,
        approved_plan={"items": [{"planItemId": "item-1"}]},
        execution_receipts=[{"planItemId": "item-1", "platformStatus": "EFFECTIVE"}],
        success_criteria=[
            ReviewCriterion(
                criterion_id="sales-present",
                metric_path="sales.avg_daily_units_7d",
                operator="GTE",
                target=1,
            )
        ],
        failure_criteria=[],
        requested_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_review_is_computed_inside_patrol_and_idempotent(binding, fake_mcp):
    unit = OperatingUnitRef.derive(
        shop_id=binding.shop_id,
        site_code=binding.site_code,
        parent_asin=binding.parent_asin,
        parent_seller_sku=binding.parent_seller_sku,
    )
    baseline = OperatingFactSnapshot(
        snapshot_id="fs_0123456789abcdef01234567",
        operating_unit=unit,
        as_of_time=datetime.now(UTC),
        content_hash="sha256:" + "0" * 64,
        quality_status=DataQualityStatus.COMPLETE,
        completeness_score=1,
    )
    store = MemoryReviewStore(baseline)
    service = ReviewService(
        store=store,
        collector=FactCollector(fake_mcp),
        normalizer=FactNormalizer(),
        quality_service=FactQualityService(),
        snapshot_builder=FactSnapshotBuilder(),
    )
    request = make_request(binding, baseline)

    first = await service.review(request, as_of=datetime(2026, 7, 29).date())
    second = await service.review(request, as_of=datetime(2026, 7, 29).date())

    assert first == second
    assert first.outcome_status == "SUCCESS"
    assert {gap.code for gap in first.data_gaps} >= {"FIELD_MAPPING_UNRESOLVED"}
    assert {gap.code for gap in first.data_gaps} >= {
        "INSPECTION_POINT_COVERAGE_LIMITED"
    }
    assert first.success_results[0].evaluation_status == "MET"
    assert first.success_results[0].missing_reason is None
    assert first.success_results[0].met is True
    # 未配置异步子体服务时，复盘只发起父体采集；子体事实由 child_fact_service 异步提供。
    assert len(fake_mcp.calls) == len(
        service.collector.build_calls(binding, datetime(2026, 7, 29).date())
    )


@pytest.mark.asyncio
async def test_review_reports_missing_criterion_as_not_evaluated(binding, fake_mcp):
    unit = OperatingUnitRef.derive(
        shop_id=binding.shop_id,
        site_code=binding.site_code,
        parent_asin=binding.parent_asin,
        parent_seller_sku=binding.parent_seller_sku,
    )
    baseline = OperatingFactSnapshot(
        snapshot_id="fs_0123456789abcdef01234567",
        operating_unit=unit,
        as_of_time=datetime.now(UTC),
        content_hash="sha256:" + "0" * 64,
        quality_status=DataQualityStatus.COMPLETE,
        completeness_score=1,
    )
    store = MemoryReviewStore(baseline)
    service = ReviewService(
        store=store,
        collector=FactCollector(fake_mcp),
        normalizer=FactNormalizer(),
        quality_service=FactQualityService(),
        snapshot_builder=FactSnapshotBuilder(),
    )
    request = make_request(binding, baseline).model_copy(
        update={
            "success_criteria": [
                ReviewCriterion(
                    criterion_id="missing-metric",
                    metric_path="sales.not_available",
                    operator="GTE",
                    target=1,
                )
            ]
        }
    )

    result = await service.review(request, as_of=datetime(2026, 7, 29).date())

    criterion = result.success_results[0]
    assert result.outcome_status == "INCONCLUSIVE"
    assert result.next_recommendation == "REPAIR_DATA"
    assert criterion.evaluation_status == "NOT_EVALUATED"
    assert criterion.actual is None
    assert criterion.met is None
    assert criterion.missing_reason == "复盘事实缺少字段 sales.not_available"


def test_control_center_batch_receipt_counts_are_strict():
    with pytest.raises(ValueError):
        SubmitPatrolBatchResult.model_validate(
            {
                "patrolBatchNo": "PT-1",
                "receivedCount": 2,
                "successCount": 1,
                "partialSuccessCount": 0,
                "failedCount": 0,
                "results": [
                    {
                        "shopId": "1",
                        "parentAsin": "B0TEST",
                        "parentSellerSku": "SKU",
                        "status": "SUCCESS",
                        "code": "OK",
                        "message": "ok",
                    }
                ],
            }
        )


def test_control_center_batch_receipt_status_counts_are_strict():
    with pytest.raises(ValueError, match="status counts"):
        SubmitPatrolBatchResult.model_validate(
            {
                "patrolBatchNo": "PT-1",
                "receivedCount": 1,
                "successCount": 1,
                "partialSuccessCount": 0,
                "failedCount": 0,
                "results": [
                    {
                        "shopId": "1",
                        "parentAsin": "B0TEST",
                        "parentSellerSku": "SKU",
                        "status": "FAILED",
                        "code": "INVALID_OPERATING_METRIC",
                        "message": "bad metric",
                    }
                ],
            }
        )


def test_control_center_batch_receipt_business_keys_are_unique():
    item = {
        "shopId": "1",
        "parentAsin": "B0TEST",
        "parentSellerSku": "SKU",
        "status": "SUCCESS",
        "code": "OK",
        "message": "ok",
    }
    with pytest.raises(ValueError, match="duplicate business keys"):
        SubmitPatrolBatchResult.model_validate(
            {
                "patrolBatchNo": "PT-1",
                "receivedCount": 2,
                "successCount": 2,
                "partialSuccessCount": 0,
                "failedCount": 0,
                "results": [item, item],
            }
        )


def make_patrol_unit_payload():
    example = ROOT / "contracts/examples/control-center-submit-patrol-batch.v2.example.json"
    return json.loads(example.read_text(encoding="utf-8"))["units"][0]


def test_control_center_v2_daily_metric_and_tag_contract():
    unit = PatrolUnitPayload.model_validate(make_patrol_unit_payload())

    assert unit.operating_metric.metric_date.isoformat() == "2026-08-04"
    assert unit.operating_metric.ad_cost == 1520.3
    assert unit.tags.product_level == "P1_PRODUCT"


def test_control_center_v2_accepts_empty_tags_object():
    payload = make_patrol_unit_payload()
    payload["tags"] = {}

    unit = PatrolUnitPayload.model_validate(payload)

    assert unit.tags.model_dump(exclude_none=True) == {}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("tags", "productLevel"), "P9_PRODUCT"),
        (("operatingMetric", "metricDate"), "2026-08-04T00:00:00+08:00"),
        (("operatingMetric", "refundRate"), 1.01),
        (("proposal", "recommendedBusinessModel"), "UNKNOWN_MODEL"),
    ],
)
def test_control_center_v2_rejects_invalid_values(path, value):
    payload = make_patrol_unit_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValueError):
        PatrolUnitPayload.model_validate(payload)


def test_control_center_v2_rejects_old_operating_metric_fields():
    payload = make_patrol_unit_payload()
    payload["operatingMetric"]["statStartTime"] = "2026-08-04T00:00:00+08:00"

    with pytest.raises(ValueError):
        PatrolUnitPayload.model_validate(payload)


def test_control_center_v2_rejects_duplicate_fact_snapshot_source():
    payload = make_patrol_unit_payload()
    payload["factSnapshots"].append(dict(payload["factSnapshots"][0]))

    with pytest.raises(ValueError, match="duplicate snapshotType/sourceSystem"):
        PatrolUnitPayload.model_validate(payload)


def test_control_center_v2_accepts_one_fact_snapshot_with_optional_fields_omitted():
    payload = make_patrol_unit_payload()
    payload["factSnapshots"] = [{"snapshotType": "SALES", "sourceSystem": "test-source"}]

    unit = PatrolUnitPayload.model_validate(payload)

    assert len(unit.fact_snapshots) == 1
    assert unit.fact_snapshots[0].content_hash is None


def test_control_center_v2_accepts_external_evidence_reference():
    payload = make_patrol_unit_payload()
    payload["proposal"]["details"][0]["evidenceRefs"] = ["external:evidence-1"]

    unit = PatrolUnitPayload.model_validate(payload)

    assert unit.proposal.details[0].evidence_refs == ["external:evidence-1"]


@pytest.mark.parametrize("field", ["reason", "expectedEffect", "risk", "evidenceRefs"])
def test_control_center_v2_requires_auditable_proposal_detail(field):
    payload = make_patrol_unit_payload()
    del payload["proposal"]["details"][0][field]

    unit = PatrolUnitPayload.model_validate(payload)

    assert getattr(unit.proposal.details[0], {
        "expectedEffect": "expected_effect",
        "evidenceRefs": "evidence_refs",
    }.get(field, field)) is None


def test_control_center_v2_rejects_duplicate_proposal_anomaly_uid():
    payload = make_patrol_unit_payload()
    payload["proposal"]["details"].append(dict(payload["proposal"]["details"][0]))

    with pytest.raises(ValueError, match="duplicate anomalyUid"):
        PatrolUnitPayload.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "problemSummary",
        "rootCauses",
        "recommendationSummary",
        "goal",
        "expectedEffect",
        "riskSummary",
        "observationWindowDays",
        "reviewDueAt",
        "rawAnalysis",
    ],
)
def test_control_center_v2_accepts_optional_proposal_summary_fields_omitted(field):
    payload = make_patrol_unit_payload()
    del payload["proposal"][field]

    PatrolUnitPayload.model_validate(payload)


def test_control_center_v2_accepts_mode_without_reason_summary():
    payload = make_patrol_unit_payload()
    del payload["proposal"]["modeReasonSummary"]

    unit = PatrolUnitPayload.model_validate(payload)

    assert unit.proposal.recommended_business_model == "CONTROLLED_GROWTH"
    assert unit.proposal.mode_reason_summary is None


def test_control_center_v2_does_not_require_mode_or_mode_reason():
    payload = make_patrol_unit_payload()
    del payload["proposal"]["recommendedBusinessModel"]
    del payload["proposal"]["modeReasonSummary"]

    unit = PatrolUnitPayload.model_validate(payload)

    assert unit.proposal.recommended_business_model is None


def test_control_center_v2_accepts_missing_owner_user_id():
    payload = make_patrol_unit_payload()
    del payload["listing"]["ownerUserId"]

    unit = PatrolUnitPayload.model_validate(payload)

    assert unit.listing.owner_user_id is None


def test_control_center_v2_accepts_minimum_documented_payload():
    unit = PatrolUnitPayload.model_validate(
        {
            "listing": {
                "shopId": "1",
                "siteCode": "Amazon_US",
                "parentAsin": "B0TEST",
                "parentSellerSku": "SKU-P",
            },
            "tags": {},
            "operatingMetric": {
                "metricDate": "2026-08-05",
                "salesAmount": 0,
                "contributionProfit": 0,
                "adCost": 0,
                "availableInventory": 0,
            },
            "factSnapshots": [
                {"snapshotType": "SALES", "sourceSystem": "azlisting"}
            ],
            "proposal": {
                "anomalies": [
                    {
                        "anomalyUid": "ANOMALY-1",
                        "anomalyCategory": "TRANSACTION_PERFORMANCE",
                        "anomalyCode": "SALES_ABNORMAL",
                        "causeMode": "INVESTIGATE",
                        "targetType": "OPERATING_UNIT",
                        "targetId": "1:B0TEST:SKU-P",
                        "severity": "LOW",
                        "summary": "销售异常待调查",
                        "rootCauseStatus": "PENDING",
                        "detectedAt": "2026-08-05T12:00:00+08:00",
                        "status": "CAUSE_PENDING",
                    }
                ],
                "details": [
                    {
                        "anomalyUid": "ANOMALY-1",
                        "domain": "OTHER",
                        "actionType": "OTHER",
                        "targetType": "OPERATING_UNIT",
                        "targetId": "1:B0TEST:SKU-P",
                    }
                ],
            },
        }
    )

    assert unit.proposal.status == "PENDING_APPROVAL"
    assert unit.proposal.details[0].status == "PENDING_APPROVAL"


def test_control_center_v2_requires_anomalies():
    payload = make_patrol_unit_payload()
    payload["proposal"]["anomalies"] = []

    with pytest.raises(ValueError):
        PatrolUnitPayload.model_validate(payload)


def test_control_center_v2_rejects_duplicate_anomaly_uid():
    payload = make_patrol_unit_payload()
    payload["proposal"]["anomalies"].append(
        dict(payload["proposal"]["anomalies"][0])
    )

    with pytest.raises(ValueError, match="duplicate anomalyUid"):
        PatrolUnitPayload.model_validate(payload)


def test_control_center_v2_rejects_unknown_detail_anomaly_uid():
    payload = make_patrol_unit_payload()
    payload["proposal"]["details"][0]["anomalyUid"] = "ANOMALY-UNKNOWN"

    with pytest.raises(ValueError, match="unknown anomalyUid"):
        PatrolUnitPayload.model_validate(payload)


def test_control_center_v2_defaults_initial_proposal_statuses():
    payload = make_patrol_unit_payload()
    del payload["proposal"]["status"]
    del payload["proposal"]["details"][0]["status"]

    unit = PatrolUnitPayload.model_validate(payload)

    assert unit.proposal.status == "PENDING_APPROVAL"
    assert unit.proposal.details[0].status == "PENDING_APPROVAL"


def test_control_center_listing_maps_single_authoritative_owner():
    binding = OperatingUnitBinding(
        shop_id=1622,
        shop_account="shop-account",
        site_code="Amazon_US",
        parent_asin="B0PARENT",
        parent_seller_sku="PARENT-SKU",
        owner_user_ids=(35,),
    )

    listing = PatrolListing.from_binding(binding, status="ONLINE")

    assert listing.shop_id == "1622"
    assert listing.owner_user_id == "35"


@pytest.mark.parametrize("owner_user_ids", [(), (35, 42)])
def test_control_center_listing_omits_non_single_owner_set(owner_user_ids):
    binding = OperatingUnitBinding(
        shop_id=1622,
        shop_account="shop-account",
        site_code="Amazon_US",
        parent_asin="B0PARENT",
        parent_seller_sku="PARENT-SKU",
        owner_user_ids=owner_user_ids,
    )

    listing = PatrolListing.from_binding(binding)

    assert listing.owner_user_id is None


def test_control_center_v2_accepts_expired_proposal_statuses():
    payload = make_patrol_unit_payload()
    payload["proposal"]["status"] = "EXPIRED"
    payload["proposal"]["details"][0]["status"] = "EXPIRED"

    unit = PatrolUnitPayload.model_validate(payload)

    assert unit.proposal.status == "EXPIRED"
    assert unit.proposal.details[0].status == "EXPIRED"
