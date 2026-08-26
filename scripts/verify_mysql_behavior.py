from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

INSERT_BATCH = text(
    """
    INSERT INTO t_patrol_batch
    (batch_id, idempotency_key, trigger_type, business_date, scope_json, scope_hash,
     rule_bundle_version, status, total_count, pending_count, running_count,
     succeeded_count, failed_count)
    VALUES
    (:batch_id, :idempotency_key, :trigger_type, :business_date, CAST(:scope_json AS JSON),
     :scope_hash, :rule_bundle_version, :status, :total_count, :pending_count, 0, 0, 0)
    """
)


def batch_values(**overrides):
    values = {
        "batch_id": "verify-batch-1",
        "idempotency_key": "verify-key-1",
        "trigger_type": "MANUAL",
        "business_date": "2026-07-31",
        "scope_json": "{}",
        "scope_hash": "0" * 64,
        "rule_bundle_version": "verify",
        "status": "CREATED",
        "total_count": 1,
        "pending_count": 1,
    }
    values.update(overrides)
    return values


def expect_database_rejection(connection, values, message: str) -> None:
    try:
        connection.execute(INSERT_BATCH, values)
    except DBAPIError:
        return
    raise AssertionError(message)


def main() -> None:
    database_url = os.environ.get("PATROL_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("PATROL_DATABASE_URL is required")
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM t_patrol_batch")).scalar_one() == 0
            connection.rollback()

            connection.execute(INSERT_BATCH, batch_values())
            assert connection.execute(
                text(
                    "SELECT job_id FROM t_patrol_job WHERE status='PENDING' "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
            ).all() == []
            connection.rollback()
            assert connection.execute(text("SELECT COUNT(*) FROM t_patrol_batch")).scalar_one() == 0
            connection.rollback()

        with engine.connect() as connection:
            connection.execute(INSERT_BATCH, batch_values())
            expect_database_rejection(
                connection,
                batch_values(batch_id="verify-batch-2"),
                "duplicate idempotency key was accepted",
            )
            connection.rollback()

        with engine.connect() as connection:
            expect_database_rejection(
                connection,
                batch_values(
                    batch_id="verify-batch-3",
                    idempotency_key="verify-key-3",
                    total_count=0,
                    pending_count=1,
                ),
                "invalid aggregate counts were accepted",
            )
            connection.rollback()

        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM t_patrol_batch")).scalar_one() == 0
        print("transaction, unique, check, and skip-locked verification passed")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
