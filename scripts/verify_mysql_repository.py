from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.contracts import (
    Diagnosis,
    EconomicExposure,
    HandoffDirective,
    InspectionSignal,
    OperatingFactSnapshot,
    OperatingUnitRef,
    sha256_json,
)
from core.enums import (
    ActionTimingStatus,
    CapabilityCode,
    DataQualityStatus,
    EconomicExposureWindow,
    ExecutionReadiness,
    HumanAttention,
    InspectionRunStatus,
    PermittedNextStep,
    ReasonCode,
    RoutingMode,
    Severity,
    SignalState,
    SignalType,
    TriggerType,
)
from core.inspection_run import InspectionRun
from core.result_envelope import InspectionResultEnvelope
from integrations.database import create_database_engine
from integrations.repositories import PatrolUnitOfWork

TABLES = (
    "t_patrol_run",
    "t_patrol_fact_snapshot",
    "t_patrol_signal",
    "t_patrol_signal_occurrence",
    "t_patrol_delivery_outbox",
)


def identifiers() -> dict[str, str]:
    suffix = uuid4().hex[:20]
    return {
        "batch_id": f"verify-batch-{suffix}",
        "job_id": f"verify-job-{suffix}",
        "request_id": f"verify-request-{suffix}",
        "run_id": f"pr_{uuid4().hex[:24]}",
    }


def insert_prerequisites(connection, ids: dict[str, str], unit: OperatingUnitRef) -> None:
    connection.execute(
        text(
            "INSERT INTO t_patrol_batch "
            "(batch_id,idempotency_key,trigger_type,business_date,scope_json,scope_hash,"
            "rule_bundle_version,status,total_count,pending_count,running_count,succeeded_count,failed_count) "
            "VALUES (:batch_id,:idempotency_key,'MANUAL',:business_date,CAST('{}' AS JSON),"
            ":scope_hash,'verify','RUNNING',1,0,1,0,0)"
        ),
        {
            "batch_id": ids["batch_id"],
            "idempotency_key": ids["batch_id"],
            "business_date": date(2026, 7, 31),
            "scope_hash": "0" * 64,
        },
    )
    connection.execute(
        text(
            "INSERT INTO t_patrol_job "
            "(job_id,batch_id,request_id,operating_unit_id,shop_id,site_code,parent_asin,"
            "parent_seller_sku,shop_account_ref,job_type,status,payload_json,retry_count,max_retry) "
            "VALUES (:job_id,:batch_id,:request_id,:unit_id,:shop_id,:site_code,:parent_asin,"
            ":sku,'verify','MANUAL','RUNNING',CAST('{}' AS JSON),0,3)"
        ),
        {
            "job_id": ids["job_id"],
            "batch_id": ids["batch_id"],
            "request_id": ids["request_id"],
            "unit_id": unit.operating_unit_id,
            "shop_id": unit.shop_id,
            "site_code": unit.site_code,
            "parent_asin": unit.parent_asin,
            "sku": unit.parent_seller_sku,
        },
    )


def make_objects(ids: dict[str, str], *, duplicate_business_key: bool = False):
    now = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
    unit = OperatingUnitRef.derive(
        shop_id=1622,
        site_code="US",
        parent_asin="B0VERIFY01",
        parent_seller_sku="VERIFY-SKU",
    )
    snapshot = OperatingFactSnapshot(
        snapshot_id=f"fs_{uuid4().hex[:24]}",
        operating_unit=unit,
        as_of_time=now,
        content_hash=sha256_json({"verification": ids["run_id"]}),
        quality_status=DataQualityStatus.COMPLETE,
        completeness_score=1,
        profit={"currency": "USD", "verification_amount": "12.3456"},
    )
    signal_count = 2 if duplicate_business_key else 1
    started = InspectionRun(
        run_id=ids["run_id"],
        job_id=ids["job_id"],
        batch_id=ids["batch_id"],
        request_id=ids["request_id"],
        operating_unit_id=unit.operating_unit_id,
        trigger_type=TriggerType.MANUAL,
        status=InspectionRunStatus.RECEIVED,
        rule_versions={"R2": "verify", "R3": "verify"},
        started_at=now,
    )

    def make_signal() -> InspectionSignal:
        signal_id = f"is_{uuid4().hex[:24]}"
        handoff = HandoffDirective(
            handoff_id=f"hd_{uuid4().hex[:24]}",
            source_result_ref=f"{ids['run_id']}:VERIFY_ISSUE",
            target_ref=unit,
            next_capability=CapabilityCode.HUMAN_REVIEW,
            reason_code=ReasonCode.HUMAN_BOUNDARY_DECISION_REQUIRED,
            requested_outcome="verify repository transaction",
            required_input_refs=[snapshot.snapshot_id],
            missing_input_codes=[],
            action_timing_status=ActionTimingStatus.DUE_TODAY,
            human_attention=HumanAttention.DECISION_REQUIRED,
            permitted_next_step=PermittedNextStep.HUMAN_DECISION,
            routing_mode=RoutingMode.HUMAN_ONLY,
            producer_versions={"business-inspection": "verify"},
        )
        return InspectionSignal(
            signal_id=signal_id,
            operating_unit_ref=unit,
            signal_type=SignalType.ANOMALY,
            issue_code="VERIFY_ISSUE",
            severity=Severity.S1,
            signal_state=SignalState.NEW,
            action_timing_status=ActionTimingStatus.DUE_TODAY,
            economic_exposure=EconomicExposure(
                currency="USD",
                amount=Decimal("12.34565"),
                window=EconomicExposureWindow.PER_DAY,
                calculation_basis="repository precision verification",
                as_of=now,
            ),
            constraint_flags=[],
            execution_readiness=ExecutionReadiness.READY_HUMAN,
            first_detected_at=now,
            last_detected_at=now,
            last_scan_run_id=ids["run_id"],
            recurrence_count=0,
            evidence_refs=[snapshot.snapshot_id],
            diagnosis=Diagnosis(
                summary="repository verification",
                known_causes=["test"],
                unknowns=[],
                confidence=1,
            ),
            handoff_directive=handoff,
            valid_until=now + timedelta(days=1),
        )

    signals = [make_signal() for _ in range(signal_count)]
    completed = started.model_copy(
        update={
            "status": InspectionRunStatus.COMPLETED,
            "fact_snapshot_id": snapshot.snapshot_id,
            "signal_count": len(signals),
            "finished_at": now + timedelta(seconds=1),
        }
    )
    envelope = InspectionResultEnvelope(
        run=completed,
        signals=signals,
        handoff_directives=[item.handoff_directive for item in signals],
        producer_versions={"business-inspection": "verify"},
        generated_at=now + timedelta(seconds=1),
    )
    return unit, snapshot, started, envelope


def counts(connection) -> dict[str, int]:
    return {
        table_name: connection.execute(
            text(f"SELECT COUNT(*) FROM `{table_name}`")
        ).scalar_one()
        for table_name in TABLES
    }


def verify_success_rollback(engine) -> None:
    with engine.connect() as connection:
        before = counts(connection)
    ids = identifiers()
    unit, snapshot, started, envelope = make_objects(ids)
    with PatrolUnitOfWork(engine) as work:
        assert work.connection is not None and work.repository is not None
        insert_prerequisites(work.connection, ids, unit)
        work.repository.create_run(started)
        assert work.repository.get_run(started.run_id) == started
        work.repository.persist_result(envelope, snapshot)
        assert counts(work.connection) == {
            table_name: before[table_name] + 1 for table_name in TABLES
        }
        amount = work.connection.execute(
            text("SELECT economic_amount FROM t_patrol_signal WHERE signal_id=:signal_id"),
            {"signal_id": envelope.signals[0].signal_id},
        ).scalar_one()
        assert amount == Decimal("12.3457")
        assert work.repository.get_run(started.run_id) == envelope.run


def verify_failure_rollback(engine) -> None:
    ids = identifiers()
    unit, snapshot, started, envelope = make_objects(ids, duplicate_business_key=True)
    try:
        with PatrolUnitOfWork(engine) as work:
            assert work.connection is not None and work.repository is not None
            insert_prerequisites(work.connection, ids, unit)
            work.repository.create_run(started)
            work.repository.persist_result(envelope, snapshot)
    except IntegrityError:
        return
    raise AssertionError("duplicate signal business key was accepted")


def main() -> None:
    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            before = counts(connection)
        verify_success_rollback(engine)
        with engine.connect() as connection:
            assert counts(connection) == before
        verify_failure_rollback(engine)
        with engine.connect() as connection:
            assert counts(connection) == before
        print("repository atomicity, rollback, and decimal precision verification passed")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
