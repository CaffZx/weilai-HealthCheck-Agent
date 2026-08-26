from __future__ import annotations

import os

import pytest

from integrations.database import create_database_engine
from integrations.repositories import PatrolUnitOfWork
from integrations.runtime_queries import RuntimeQueryService
from scripts.verify_mysql_repository import (
    TABLES,
    counts,
    identifiers,
    insert_prerequisites,
    make_objects,
    verify_failure_rollback,
    verify_success_rollback,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def mysql_engine():
    if not os.environ.get("PATROL_DATABASE_URL"):
        pytest.skip("PATROL_DATABASE_URL is not configured")
    engine = create_database_engine()
    try:
        yield engine
    finally:
        engine.dispose()


def test_repository_atomicity_and_decimal_precision(mysql_engine):
    with mysql_engine.connect() as connection:
        before = counts(connection)
    verify_success_rollback(mysql_engine)
    with mysql_engine.connect() as connection:
        assert counts(connection) == before


def test_repository_rolls_back_partial_result(mysql_engine):
    with mysql_engine.connect() as connection:
        before = counts(connection)
    verify_failure_rollback(mysql_engine)
    with mysql_engine.connect() as connection:
        assert counts(connection) == before
        assert set(counts(connection)) == set(TABLES)


def test_runtime_queries_return_persisted_run_signal_and_occurrences(mysql_engine):
    ids = identifiers()
    unit, snapshot, started, envelope = make_objects(ids)
    signal_id = envelope.signals[0].signal_id
    try:
        with PatrolUnitOfWork(mysql_engine) as work:
            assert work.connection is not None and work.repository is not None
            insert_prerequisites(work.connection, ids, unit)
            work.repository.create_run(started)
            work.repository.persist_result(envelope, snapshot)
            work.commit()

        queries = RuntimeQueryService(mysql_engine)
        run = queries.get_run(envelope.run.run_id)
        signal = queries.get_signal(signal_id)

        assert run is not None
        assert run["status"] == "COMPLETED"
        assert run["delivery"]["status"] == "PENDING"
        assert signal is not None
        assert signal["signal"]["signal_id"] == signal_id
        assert signal["signal"]["version"] == 1
        assert [item["occurrence_type"] for item in signal["occurrences"]] == ["DETECTED"]
    finally:
        with mysql_engine.begin() as connection:
            connection.exec_driver_sql(
                "DELETE FROM t_patrol_delivery_outbox WHERE aggregate_id=%s",
                (envelope.run.run_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM t_patrol_signal_occurrence WHERE signal_id=%s",
                (signal_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM t_patrol_signal WHERE signal_id=%s",
                (signal_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM t_patrol_raw_fact WHERE run_id=%s",
                (envelope.run.run_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM t_patrol_fact_snapshot WHERE run_id=%s",
                (envelope.run.run_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM t_patrol_run WHERE run_id=%s",
                (envelope.run.run_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM t_patrol_job WHERE job_id=%s",
                (ids["job_id"],),
            )
            connection.exec_driver_sql(
                "DELETE FROM t_patrol_batch WHERE batch_id=%s",
                (ids["batch_id"],),
            )
