from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from integrations.repositories.tables import (
    metadata,
    patrol_batch,
    patrol_delivery_outbox,
    patrol_job,
    patrol_raw_fact,
    patrol_run,
    patrol_signal,
    patrol_signal_occurrence,
)
from integrations.runtime_queries import RuntimeQueryService


def make_service() -> tuple[sa.Engine, RuntimeQueryService]:
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    return engine, RuntimeQueryService(engine)


def test_query_service_returns_run_and_delivery_status():
    engine, service = make_service()
    started_at = datetime(2026, 8, 3, 1, 0)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(patrol_run).values(
                run_id="pr_0123456789abcdef01234567",
                job_id="job-query",
                batch_id="batch-query",
                request_id="request-query",
                operating_unit_id="ou_0123456789abcdef01234567",
                trigger_type="MANUAL",
                status="COMPLETED",
                rule_versions_json={"R2": "current", "R3": "current"},
                fact_snapshot_id="fs_0123456789abcdef01234567",
                signal_count=1,
                data_gap_count=0,
                started_at=started_at,
                finished_at=started_at,
            )
        )
        connection.execute(
            sa.insert(patrol_delivery_outbox).values(
                outbox_id="ob-query",
                aggregate_type="INSPECTION_RUN",
                aggregate_id="pr_0123456789abcdef01234567",
                aggregate_version=1,
                payload_hash="0" * 64,
                payload_json={},
                status="SUPPRESSED",
                retry_count=0,
                max_retry=10,
            )
        )

    result = service.get_run("pr_0123456789abcdef01234567")

    assert result is not None
    assert result["contract_version"] == "amazon_ops.inspection_run.v1"
    assert result["status"] == "COMPLETED"
    assert result["started_at"].tzinfo is UTC
    assert result["delivery"]["status"] == "SUPPRESSED"
    assert service.get_run("pr_missing") is None


def test_query_service_returns_signal_current_state_and_ordered_history():
    engine, service = make_service()
    signal_id = "is_0123456789abcdef01234567"
    first_at = datetime(2026, 8, 3, 1, 0)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(patrol_signal).values(
                signal_id=signal_id,
                dedup_key="1" * 64,
                operating_unit_id="ou_0123456789abcdef01234567",
                issue_code="VERIFY_ISSUE",
                child_scope_key="PARENT",
                signal_type="ANOMALY",
                severity="S1",
                signal_state="AWAITING_RESCAN",
                action_timing_status="DUE_TODAY",
                economic_currency="USD",
                economic_amount="12.3456",
                economic_window="PER_DAY",
                constraint_flags_json=[],
                execution_readiness="READY_HUMAN",
                diagnosis_json={},
                handoff_json=None,
                signal_payload_json={"signal_id": signal_id, "signal_state": "NEW"},
                first_detected_at=first_at,
                last_detected_at=first_at,
                last_scan_run_id="pr_0123456789abcdef01234567",
                recurrence_count=1,
                consecutive_miss_count=0,
                next_inspection_at=first_at,
                valid_until=first_at,
                version=3,
            )
        )
        for index, occurrence_type in ((2, "HANDLED"), (1, "DETECTED")):
            connection.execute(
                sa.insert(patrol_signal_occurrence).values(
                    occurrence_id=f"oc-{index}",
                    signal_id=signal_id,
                    run_id=f"pr_{index:024d}",
                    snapshot_id=None,
                    occurrence_type=occurrence_type,
                    severity="S1",
                    evidence_refs_json=[],
                    details_json={"index": index},
                    occurred_at=first_at.replace(hour=index),
                )
            )

    result = service.get_signal(signal_id)

    assert result is not None
    assert result["signal"]["signal_state"] == "AWAITING_RESCAN"
    assert result["signal"]["version"] == 3
    assert [item["occurrence_type"] for item in result["occurrences"]] == [
        "DETECTED",
        "HANDLED",
    ]
    assert all(item["occurred_at"].tzinfo is UTC for item in result["occurrences"])
    assert service.get_signal("is_missing") is None


def test_query_service_returns_complete_raw_fact_payload():
    engine, service = make_service()
    fetched_at = datetime(2026, 8, 3, 1, 0)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(patrol_raw_fact).values(
                raw_fact_id="rf_0123456789abcdef01234567",
                run_id="pr_0123456789abcdef01234567",
                snapshot_id="fs_0123456789abcdef01234567",
                operating_unit_id="ou_0123456789abcdef01234567",
                as_of_date=fetched_at.date(),
                fact_key="stock",
                tool_name="erp_listing_stock_alert",
                request_hash="1" * 64,
                request_json={"parentAsin": "B0TEST123"},
                response_content_hash="2" * 64,
                response_json={"structuredContent": {"data": [{"quantity": 10}]}},
                extracted_data_json=[{"quantity": 10}],
                status="SUCCESS",
                is_core=True,
                row_count=1,
                latency_ms=12,
                fetched_at=fetched_at,
            )
        )

    facts = service.get_raw_facts("pr_0123456789abcdef01234567")

    assert facts[0]["request_hash"] == "sha256:" + "1" * 64
    assert facts[0]["response_content_hash"] == "sha256:" + "2" * 64
    assert facts[0]["response_json"]["structuredContent"]["data"][0]["quantity"] == 10
    assert facts[0]["fetched_at"].tzinfo is UTC


def test_query_service_lists_workspace_records_with_filters_and_pagination():
    engine, service = make_service()
    started_at = datetime(2026, 8, 3, 1, 0)
    with engine.begin() as connection:
        connection.execute(sa.insert(patrol_batch), [
            {
                "batch_id": "batch-old",
                "idempotency_key": "old",
                "trigger_type": "MANUAL",
                "business_date": started_at.date(),
                "scope_json": {},
                "scope_hash": "1" * 64,
                "rule_bundle_version": "current",
                "status": "FAILED",
                "total_count": 1,
                "pending_count": 0,
                "running_count": 0,
                "succeeded_count": 0,
                "failed_count": 1,
                "started_at": started_at,
            },
            {
                "batch_id": "batch-new",
                "idempotency_key": "new",
                "trigger_type": "DAILY_SCHEDULE",
                "business_date": started_at.date(),
                "scope_json": {},
                "scope_hash": "2" * 64,
                "rule_bundle_version": "current",
                "status": "SUCCEEDED",
                "total_count": 1,
                "pending_count": 0,
                "running_count": 0,
                "succeeded_count": 1,
                "failed_count": 0,
                "started_at": started_at.replace(hour=2),
            },
        ])
        connection.execute(sa.insert(patrol_job).values(
            job_id="job-workspace",
            batch_id="batch-new",
            request_id="request-workspace",
            operating_unit_id="ou_0123456789abcdef01234567",
            shop_id=1622,
            site_code="US",
            parent_asin="B0TEST123",
            parent_seller_sku="PSKU-1",
            shop_account_ref="private-account-ref",
            job_type="patrol.run",
            status="SUCCEEDED",
            payload_json={},
            result_ref="pr_0123456789abcdef01234567",
            retry_count=0,
            max_retry=3,
            created_at=started_at,
        ))

    batches = service.list_batches(page=1, page_size=1)
    succeeded = service.list_batches(page=1, page_size=20, status="SUCCEEDED")
    jobs = service.list_jobs(page=1, page_size=20, batch_id="batch-new")
    summary = service.workspace_summary()

    assert batches["total"] == 2
    assert batches["pages"] == 2
    assert batches["items"][0]["batch_id"] == "batch-new"
    assert succeeded["total"] == 1
    assert succeeded["items"][0]["status"] == "SUCCEEDED"
    assert jobs["items"][0]["parent_asin"] == "B0TEST123"
    assert "shop_account_ref" not in jobs["items"][0]
    assert summary["batches"] == 2
    assert summary["jobs"] == 1
    assert summary["latest_batch"]["batch_id"] == "batch-new"


def test_signal_workspace_list_uses_safe_summary_fields():
    engine, service = make_service()
    detected_at = datetime(2026, 8, 3, 1, 0)
    signal_id = "is_0123456789abcdef01234567"
    with engine.begin() as connection:
        connection.execute(sa.insert(patrol_signal).values(
            signal_id=signal_id,
            dedup_key="1" * 64,
            operating_unit_id="ou_0123456789abcdef01234567",
            issue_code="VERIFY_ISSUE",
            child_scope_key="PARENT",
            signal_type="ANOMALY",
            severity="S1",
            signal_state="NEW",
            action_timing_status="DUE_TODAY",
            economic_window="UNASSESSED",
            constraint_flags_json=[],
            execution_readiness="READY_HUMAN",
            diagnosis_json={"secret": "not exposed directly"},
            signal_payload_json={
                "operating_unit_ref": {
                    "shop_id": 1622,
                    "site_code": "US",
                    "parent_asin": "B0TEST123",
                },
                "diagnosis": {"summary": "需要复核库存事实"},
                "private_extension": {"token": "not-for-list"},
            },
            first_detected_at=detected_at,
            last_detected_at=detected_at,
            last_scan_run_id="pr_0123456789abcdef01234567",
            recurrence_count=0,
            consecutive_miss_count=0,
            version=1,
        ))

    result = service.list_signals(page=1, page_size=20, severity="S1")

    assert result["total"] == 1
    assert result["items"][0]["parent_asin"] == "B0TEST123"
    assert result["items"][0]["child_asins"] == []
    assert result["items"][0]["child_skus"] == []
    assert result["items"][0]["diagnosis_summary"] == "需要复核库存事实"
    assert "signal_payload_json" not in result["items"][0]
    assert "private_extension" not in result["items"][0]


def test_signal_workspace_filters_by_batch_parent_and_type():
    engine, service = make_service()
    detected_at = datetime(2026, 8, 3, 1, 0)
    signal_id = "is_filter0123456789abcdef012345"
    run_id = "pr_filter0123456789abcdef012345"
    operating_unit_id = "ou_filter0123456789abcdef012345"
    with engine.begin() as connection:
        connection.execute(sa.insert(patrol_job).values(
            job_id="job-filter",
            batch_id="batch-filter",
            request_id="request-filter",
            operating_unit_id=operating_unit_id,
            shop_id=1622,
            site_code="AMAZON_US",
            parent_asin="B0FILTER01",
            parent_seller_sku="FILTER-SKU",
            shop_account_ref="filter-account",
            job_type="patrol.run",
            status="SUCCEEDED",
            payload_json={},
            result_ref=run_id,
            retry_count=0,
            max_retry=3,
            created_at=detected_at,
        ))
        connection.execute(sa.insert(patrol_run).values(
            run_id=run_id,
            job_id="job-filter",
            batch_id="batch-filter",
            request_id="run-request-filter",
            operating_unit_id=operating_unit_id,
            trigger_type="MANUAL",
            status="COMPLETED_WITH_GAPS",
            rule_versions_json={},
            signal_count=1,
            data_gap_count=0,
            started_at=detected_at,
            finished_at=detected_at,
        ))
        connection.execute(sa.insert(patrol_signal).values(
            signal_id=signal_id,
            dedup_key="2" * 64,
            operating_unit_id=operating_unit_id,
            issue_code="FILTER_ISSUE",
            child_scope_key="PARENT",
            signal_type="ANOMALY",
            severity="S1",
            signal_state="NEW",
            action_timing_status="DUE_SOON",
            economic_window="UNASSESSED",
            constraint_flags_json=[],
            execution_readiness="READY_HUMAN",
            diagnosis_json={},
            signal_payload_json={
                "operating_unit_ref": {
                    "shop_id": 1622,
                    "site_code": "AMAZON_US",
                    "parent_asin": "B0FILTER01",
                },
                "diagnosis": {"summary": "筛选测试异常"},
            },
            first_detected_at=detected_at,
            last_detected_at=detected_at,
            last_scan_run_id=run_id,
            recurrence_count=0,
            consecutive_miss_count=0,
            version=1,
        ))
        connection.execute(sa.insert(patrol_signal_occurrence).values(
            occurrence_id="oc-filter",
            signal_id=signal_id,
            run_id=run_id,
            occurrence_type="DETECTED",
            evidence_refs_json=[],
            details_json={},
            occurred_at=detected_at,
        ))

    matched = service.list_signals(
        batch_id="batch-filter",
        parent_asin="b0filter01",
        signal_type="ANOMALY",
    )
    wrong_batch = service.list_signals(batch_id="batch-other")

    assert matched["total"] == 1
    assert matched["items"][0]["issue_code"] == "FILTER_ISSUE"
    assert wrong_batch["total"] == 0


def test_signal_workspace_batch_excludes_lifecycle_only_occurrence():
    engine, service = make_service()
    detected_at = datetime(2026, 8, 3, 1, 0)
    later_at = datetime(2026, 8, 4, 1, 0)
    operating_unit_id = "ou_lifecycle0123456789abcdef01"
    signal_id = "is_lifecycle0123456789abcdef01"
    with engine.begin() as connection:
        for batch_id, run_id, occurred_at in (
            ("batch-detected", "pr_detected0123456789abcdef01", detected_at),
            ("batch-rescan", "pr_rescan0123456789abcdef012", later_at),
        ):
            connection.execute(sa.insert(patrol_run).values(
                run_id=run_id,
                job_id=f"job-{batch_id}",
                batch_id=batch_id,
                request_id=f"request-{batch_id}",
                operating_unit_id=operating_unit_id,
                trigger_type="MANUAL",
                status="COMPLETED_WITH_GAPS",
                rule_versions_json={},
                signal_count=1,
                data_gap_count=0,
                started_at=occurred_at,
                finished_at=occurred_at,
            ))
        connection.execute(sa.insert(patrol_signal).values(
            signal_id=signal_id,
            dedup_key="3" * 64,
            operating_unit_id=operating_unit_id,
            issue_code="OLD_ISSUE",
            child_scope_key="PARENT",
            signal_type="ANOMALY",
            severity="S1",
            signal_state="NEW",
            action_timing_status="DUE_SOON",
            economic_window="UNASSESSED",
            constraint_flags_json=[],
            execution_readiness="READY_HUMAN",
            diagnosis_json={},
            signal_payload_json={
                "operating_unit_ref": {"parent_asin": "B0OLDISSUE"},
                "diagnosis": {"summary": "旧异常"},
            },
            first_detected_at=detected_at,
            last_detected_at=detected_at,
            last_scan_run_id="pr_rescan0123456789abcdef012",
            recurrence_count=0,
            consecutive_miss_count=1,
            version=2,
        ))
        connection.execute(sa.insert(patrol_signal_occurrence), [
            {
                "occurrence_id": "oc-detected",
                "signal_id": signal_id,
                "run_id": "pr_detected0123456789abcdef01",
                "occurrence_type": "DETECTED",
                "evidence_refs_json": [],
                "details_json": {},
                "occurred_at": detected_at,
            },
            {
                "occurrence_id": "oc-missed",
                "signal_id": signal_id,
                "run_id": "pr_rescan0123456789abcdef012",
                "occurrence_type": "MISSED",
                "evidence_refs_json": [],
                "details_json": {},
                "occurred_at": later_at,
            },
        ])

    assert service.list_signals(batch_id="batch-detected")["total"] == 0
    assert service.list_signals(batch_id="batch-rescan")["total"] == 0
    assert service.list_signals(latest_detection_only=True)["total"] == 0


def test_run_workspace_filters_by_batch():
    engine, service = make_service()
    started_at = datetime(2026, 8, 3, 1, 0)
    with engine.begin() as connection:
        for batch_id, run_id in (
            ("batch-one", "pr_batchone0123456789abcdef01"),
            ("batch-two", "pr_batchtwo0123456789abcdef01"),
        ):
            connection.execute(sa.insert(patrol_run).values(
                run_id=run_id,
                job_id=f"job-{batch_id}",
                batch_id=batch_id,
                request_id=f"request-{batch_id}",
                operating_unit_id="ou_batchfilter0123456789abcdef",
                trigger_type="MANUAL",
                status="COMPLETED",
                rule_versions_json={},
                signal_count=0,
                data_gap_count=0,
                started_at=started_at,
                finished_at=started_at,
            ))

    result = service.list_runs(batch_id="batch-one")

    assert result["total"] == 1
    assert result["items"][0]["batch_id"] == "batch-one"
