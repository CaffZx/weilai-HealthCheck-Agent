from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from core.enums import SignalState
from inspector.signal_reconciler import SignalReconciler
from integrations.database import create_database_engine
from integrations.repositories import PatrolUnitOfWork
from scripts.verify_mysql_repository import (
    counts,
    identifiers,
    insert_prerequisites,
    make_objects,
)


def main() -> None:
    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            before = counts(connection)

        initial_ids = identifiers()
        unit, initial_snapshot, started, initial_envelope = make_objects(initial_ids)
        initial_signal = initial_envelope.signals[0]
        reconciler = SignalReconciler()

        with PatrolUnitOfWork(engine) as work:
            assert work.connection is not None and work.repository is not None
            insert_prerequisites(work.connection, initial_ids, unit)
            work.repository.create_run(started)
            work.repository._insert_snapshot(started.run_id, initial_snapshot)
            first_result = reconciler.reconcile(
                existing=[],
                candidates=[initial_signal],
                facts_complete=True,
                occurred_at=initial_signal.last_detected_at,
                scan_run_id=started.run_id,
            )
            work.repository.apply_reconciliation(
                result=first_result,
                run_id=started.run_id,
                snapshot_id=initial_snapshot.snapshot_id,
            )

            existing = work.repository.list_signals_for_update(unit.operating_unit_id)
            assert len(existing) == 1
            assert existing[0].version == 1

            first_miss_ids = identifiers()
            _, first_miss_snapshot, first_miss_run, _ = make_objects(first_miss_ids)
            first_miss_run = first_miss_run.model_copy(
                update={
                    "job_id": initial_ids["job_id"],
                    "batch_id": initial_ids["batch_id"],
                    "request_id": first_miss_ids["request_id"],
                }
            )
            work.repository.create_run(first_miss_run)
            work.repository._insert_snapshot(first_miss_run.run_id, first_miss_snapshot)
            first_miss = reconciler.reconcile(
                existing=existing,
                candidates=[],
                facts_complete=True,
                occurred_at=datetime.now(UTC) + timedelta(minutes=1),
                scan_run_id=first_miss_run.run_id,
            )
            work.repository.apply_reconciliation(
                result=first_miss,
                run_id=first_miss_run.run_id,
                snapshot_id=first_miss_snapshot.snapshot_id,
            )
            existing = work.repository.list_signals_for_update(unit.operating_unit_id)
            assert existing[0].signal_state is SignalState.NEW
            assert existing[0].consecutive_miss_count == 1
            assert existing[0].version == 2

            second_miss_ids = identifiers()
            _, second_miss_snapshot, second_miss_run, _ = make_objects(second_miss_ids)
            second_miss_run = second_miss_run.model_copy(
                update={
                    "job_id": initial_ids["job_id"],
                    "batch_id": initial_ids["batch_id"],
                    "request_id": second_miss_ids["request_id"],
                }
            )
            work.repository.create_run(second_miss_run)
            work.repository._insert_snapshot(second_miss_run.run_id, second_miss_snapshot)
            second_miss = reconciler.reconcile(
                existing=existing,
                candidates=[],
                facts_complete=True,
                occurred_at=datetime.now(UTC) + timedelta(minutes=2),
                scan_run_id=second_miss_run.run_id,
            )
            work.repository.apply_reconciliation(
                result=second_miss,
                run_id=second_miss_run.run_id,
                snapshot_id=second_miss_snapshot.snapshot_id,
            )
            existing = work.repository.list_signals_for_update(unit.operating_unit_id)
            assert existing[0].signal_state is SignalState.RESOLVED
            assert existing[0].consecutive_miss_count == 2
            assert existing[0].version == 3

            recurrence_ids = identifiers()
            _, recurrence_snapshot, recurrence_run, recurrence_envelope = make_objects(
                recurrence_ids
            )
            recurrence_run = recurrence_run.model_copy(
                update={
                    "job_id": initial_ids["job_id"],
                    "batch_id": initial_ids["batch_id"],
                    "request_id": recurrence_ids["request_id"],
                }
            )
            recurrence_signal = recurrence_envelope.signals[0].model_copy(
                update={"last_scan_run_id": recurrence_run.run_id}
            )
            work.repository.create_run(recurrence_run)
            work.repository._insert_snapshot(recurrence_run.run_id, recurrence_snapshot)
            recurrence = reconciler.reconcile(
                existing=existing,
                candidates=[recurrence_signal],
                facts_complete=True,
                occurred_at=datetime.now(UTC) + timedelta(minutes=3),
                scan_run_id=recurrence_run.run_id,
            )
            work.repository.apply_reconciliation(
                result=recurrence,
                run_id=recurrence_run.run_id,
                snapshot_id=recurrence_snapshot.snapshot_id,
            )
            existing = work.repository.list_signals_for_update(unit.operating_unit_id)
            assert existing[0].signal_state is SignalState.NEW
            assert existing[0].recurrence_count == 1
            assert existing[0].consecutive_miss_count == 0
            assert existing[0].version == 4

            occurrences = work.connection.execute(
                text(
                    "SELECT occurrence_type FROM t_patrol_signal_occurrence "
                    "WHERE signal_id=:signal_id ORDER BY occurred_at"
                ),
                {"signal_id": existing[0].signal_id},
            ).scalars().all()
            assert occurrences == ["DETECTED", "MISSED", "RECOVERED", "RECURRED"]

        with engine.connect() as connection:
            assert counts(connection) == before
        print("S2 signal first-hit, miss, recovery, recurrence, version, and rollback verification passed")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
