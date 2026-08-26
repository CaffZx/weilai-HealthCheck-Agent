from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa

from clients.mcp_client import McpToolResult, request_hash
from integrations.repositories import PatrolUnitOfWork
from integrations.repositories.patrol import RepositoryInvariantError
from integrations.repositories.tables import metadata, patrol_raw_fact, patrol_run


def make_engine() -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    return engine


def create_run(engine: sa.Engine, *, run_id: str, operating_unit_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.insert(patrol_run).values(
                run_id=run_id,
                job_id="job-raw-fact",
                batch_id="batch-raw-fact",
                request_id=run_id,
                operating_unit_id=operating_unit_id,
                trigger_type="MANUAL",
                status="RECEIVED",
                rule_versions_json={},
                signal_count=0,
                data_gap_count=0,
                started_at=datetime(2026, 8, 3, 1, 0),
            )
        )


def test_repository_archives_success_and_failure_results():
    engine = make_engine()
    run_id = "pr_0123456789abcdef01234567"
    unit_id = "ou_0123456789abcdef01234567"
    create_run(engine, run_id=run_id, operating_unit_id=unit_id)
    fetched_at = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
    calls = {
        "stock": ("stock_tool", {"parentAsin": "B0TEST123"}),
        "refund_rate": ("refund_tool", {"parentAsin": "B0TEST123"}),
    }
    results = {
        "stock": McpToolResult(
            tool_name="stock_tool",
            data=[{"quantity": 10}],
            raw={"structuredContent": {"data": [{"quantity": 10}]}},
            request_hash=request_hash(*calls["stock"]),
            latency_ms=8,
            fetched_at=fetched_at,
        ),
        "refund_rate": RuntimeError("source unavailable"),
    }

    with PatrolUnitOfWork(engine) as work:
        assert work.repository is not None
        work.repository.persist_raw_facts(
            run_id=run_id,
            operating_unit_id=unit_id,
            as_of_date=date(2026, 8, 3),
            calls=calls,
            results=results,
            core_keys=frozenset({"stock"}),
            fetched_at=fetched_at,
        )
        work.commit()

    with engine.connect() as connection:
        rows = (
            connection.execute(sa.select(patrol_raw_fact).order_by(patrol_raw_fact.c.fact_key))
            .mappings()
            .all()
        )
    assert [row["status"] for row in rows] == ["FAILED", "SUCCESS"]
    success = rows[1]
    assert success["response_json"]["structuredContent"]["data"] == [{"quantity": 10}]
    assert success["extracted_data_json"] == [{"quantity": 10}]
    assert success["is_core"] is True
    assert rows[0]["error_code"] == "RUNTIMEERROR"
    assert rows[0]["response_json"] is None
    assert rows[0]["extracted_data_json"] is None


def test_failed_raw_fact_json_none_binds_as_sql_null_for_mysql():
    dialect = sa.create_engine("mysql+pymysql://").dialect

    for column_name in ("response_json", "extracted_data_json"):
        column_type = patrol_raw_fact.c[column_name].type
        processor = column_type.bind_processor(dialect)
        assert column_type.none_as_null is True
        assert processor is not None
        assert processor(None) is None


def test_repository_loads_verified_facts_for_explicit_reuse():
    engine = make_engine()
    run_id = "pr_0123456789abcdef01234567"
    unit_id = "ou_0123456789abcdef01234567"
    create_run(engine, run_id=run_id, operating_unit_id=unit_id)
    fetched_at = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
    calls = {"stock": ("stock_tool", {"parentAsin": "B0TEST123"})}
    result = McpToolResult(
        tool_name="stock_tool",
        data=[{"quantity": 10}],
        raw={"structuredContent": {"data": [{"quantity": 10}]}},
        request_hash=request_hash(*calls["stock"]),
        fetched_at=fetched_at,
    )
    with PatrolUnitOfWork(engine) as work:
        assert work.repository is not None
        work.repository.persist_raw_facts(
            run_id=run_id,
            operating_unit_id=unit_id,
            as_of_date=date(2026, 8, 3),
            calls=calls,
            results={"stock": result},
            core_keys=frozenset({"stock"}),
            fetched_at=fetched_at,
        )
        work.commit()

    with PatrolUnitOfWork(engine) as work:
        assert work.repository is not None
        as_of, loaded_at, loaded_calls, loaded_results, source_ids = (
            work.repository.load_reusable_raw_facts(
                source_run_id=run_id,
                operating_unit_id=unit_id,
                expected_fact_keys=frozenset({"stock"}),
            )
        )

    assert as_of == date(2026, 8, 3)
    assert loaded_at == fetched_at
    assert loaded_calls == calls
    assert loaded_results["stock"].data == [{"quantity": 10}]
    assert source_ids["stock"].startswith("rf_")


def test_repository_reuses_failed_non_core_fact_as_gap():
    engine = make_engine()
    run_id = "pr_3123456789abcdef01234567"
    unit_id = "ou_3123456789abcdef01234567"
    create_run(engine, run_id=run_id, operating_unit_id=unit_id)
    fetched_at = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
    calls = {"keyword_rank:B0CHILD01": ("keyword_tool", {"asin": "B0CHILD01"})}
    with PatrolUnitOfWork(engine) as work:
        assert work.repository is not None
        work.repository.persist_raw_facts(
            run_id=run_id,
            operating_unit_id=unit_id,
            as_of_date=date(2026, 8, 3),
            calls=calls,
            results={"keyword_rank:B0CHILD01": RuntimeError("explicitly skipped")},
            core_keys=frozenset(),
            fetched_at=fetched_at,
        )
        work.commit()

    with PatrolUnitOfWork(engine) as work:
        assert work.repository is not None
        _, _, loaded_calls, loaded_results, _ = work.repository.load_reusable_raw_facts(
            source_run_id=run_id,
            operating_unit_id=unit_id,
            expected_fact_keys=frozenset(),
        )

    assert loaded_calls == calls
    assert isinstance(loaded_results["keyword_rank:B0CHILD01"], RuntimeError)


def test_repository_redacts_nested_credentials_before_archiving():
    engine = make_engine()
    run_id = "pr_1123456789abcdef01234567"
    unit_id = "ou_1123456789abcdef01234567"
    create_run(engine, run_id=run_id, operating_unit_id=unit_id)
    fetched_at = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
    calls = {"shop": ("shop_tool", {"shopAccount": "shop_us"})}
    result = McpToolResult(
        tool_name="shop_tool",
        data=[{"shop": "shop_us", "refreshToken": "secret"}],
        raw={"content": [{"type": "text", "text": "Bearer secret-value"}]},
        fetched_at=fetched_at,
    )

    with PatrolUnitOfWork(engine) as work:
        assert work.repository is not None
        work.repository.persist_raw_facts(
            run_id=run_id,
            operating_unit_id=unit_id,
            as_of_date=date(2026, 8, 3),
            calls=calls,
            results={"shop": result},
            core_keys=frozenset(),
            fetched_at=fetched_at,
        )
        work.commit()

    with engine.connect() as connection:
        row = connection.execute(sa.select(patrol_raw_fact)).mappings().one()
    assert row["extracted_data_json"] == [{"shop": "shop_us"}]
    assert row["response_json"]["content"][0]["text"] == "Bearer [REDACTED]"


def test_repository_rejects_credentials_in_request_arguments():
    engine = make_engine()
    run_id = "pr_2123456789abcdef01234567"
    unit_id = "ou_2123456789abcdef01234567"
    create_run(engine, run_id=run_id, operating_unit_id=unit_id)
    fetched_at = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)

    with PatrolUnitOfWork(engine) as work:
        assert work.repository is not None
        with pytest.raises(RepositoryInvariantError, match="forbidden credential"):
            work.repository.persist_raw_facts(
                run_id=run_id,
                operating_unit_id=unit_id,
                as_of_date=date(2026, 8, 3),
                calls={"shop": ("shop_tool", {"apiKey": "must-not-persist"})},
                results={"shop": McpToolResult("shop_tool", [], {})},
                core_keys=frozenset(),
                fetched_at=fetched_at,
            )
