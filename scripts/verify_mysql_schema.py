from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, inspect, text

EXPECTED_TABLES = {
    "t_patrol_batch",
    "t_patrol_job",
    "t_patrol_run",
    "t_patrol_fact_snapshot",
    "t_patrol_raw_fact",
    "t_patrol_review",
    "t_patrol_signal",
    "t_patrol_signal_occurrence",
    "t_patrol_feedback_inbox",
    "t_patrol_delivery_outbox",
    "t_patrol_idempotency",
    "t_patrol_scheduler_lock",
    "t_patrol_schema_version",
    "t_patrol_sys_user",
    "t_patrol_user_role",
    "t_patrol_asin_owner",
    "t_patrol_category_baseline",
    "t_patrol_child_fact_task",
    "t_patrol_child_fact_snapshot",
    "t_patrol_product_image",
    "t_patrol_operating_unit_catalog",
    "t_patrol_listing_catalog",
}

EXPECTED_UNIQUES = {
    "t_patrol_batch": {"ux_patrol_batch_idempotency"},
    "t_patrol_job": {"ux_patrol_job_batch_unit"},
    "t_patrol_run": {"ux_patrol_run_request"},
    "t_patrol_signal": {"ux_patrol_signal_dedup", "ux_patrol_signal_business_key"},
    "t_patrol_signal_occurrence": {"ux_patrol_occurrence_signal_run_type"},
    "t_patrol_delivery_outbox": {"ux_patrol_outbox_aggregate_version"},
    "t_patrol_raw_fact": {"ux_patrol_raw_fact_run_key"},
    "t_patrol_child_fact_task": {"ux_patrol_child_fact_task_key"},
    "t_patrol_product_image": {"ix_patrol_product_image_business_key"},
    "t_patrol_listing_catalog": {"ux_patrol_listing_catalog_child_key"},
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-empty", action="store_true")
    args = parser.parse_args()

    database_url = os.environ.get("PATROL_DATABASE_URL", "").strip()
    if not database_url:
        print("PATROL_DATABASE_URL is required", file=sys.stderr)
        return 2

    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SELECT VERSION()")) .scalar_one()
            sql_mode = connection.execute(text("SELECT @@sql_mode")) .scalar_one()
            collation = connection.execute(text("SELECT @@collation_database")) .scalar_one()
            inspector = inspect(connection)
            actual_tables = {name for name in inspector.get_table_names() if name.startswith("t_patrol_")}
            if actual_tables != EXPECTED_TABLES:
                print(f"table mismatch: missing={sorted(EXPECTED_TABLES - actual_tables)} "
                      f"extra={sorted(actual_tables - EXPECTED_TABLES)}", file=sys.stderr)
                return 1
            if not str(version).startswith("8."):
                print(f"unsupported MySQL version: {version}", file=sys.stderr)
                return 1
            if "STRICT_TRANS_TABLES" not in str(sql_mode):
                print(f"strict SQL mode is missing: {sql_mode}", file=sys.stderr)
                return 1
            if not str(collation).startswith("utf8mb4"):
                print(f"unexpected database collation: {collation}", file=sys.stderr)
                return 1

            for table_name, expected_names in EXPECTED_UNIQUES.items():
                actual_names = {
                    item["name"] for item in inspector.get_unique_constraints(table_name) if item.get("name")
                }
                missing = expected_names - actual_names
                if missing:
                    print(f"{table_name} missing unique constraints: {sorted(missing)}", file=sys.stderr)
                    return 1

            signal_columns = {item["name"]: item for item in inspector.get_columns("t_patrol_signal")}
            payload_column = signal_columns.get("signal_payload_json")
            if payload_column is None or payload_column.get("nullable", True):
                print("t_patrol_signal.signal_payload_json must exist and be NOT NULL", file=sys.stderr)
                return 1

            if args.expect_empty:
                for table_name in sorted(EXPECTED_TABLES - {"t_patrol_schema_version"}):
                    count = connection.execute(text(f"SELECT COUNT(*) FROM `{table_name}`")).scalar_one()
                    if count != 0:
                        print(f"{table_name} is not empty: {count}", file=sys.stderr)
                        return 1

            revision = connection.execute(text("SELECT version_num FROM t_patrol_schema_version")).scalar_one()
            if revision != "0012_listing_catalog_source":
                print(f"unexpected schema revision: {revision}", file=sys.stderr)
                return 1

            for table_name in ("t_patrol_job", "t_patrol_asin_owner"):
                columns = {item["name"]: item for item in inspector.get_columns(table_name)}
                parent_sku = columns.get("parent_seller_sku")
                if parent_sku is None or parent_sku.get("nullable", True):
                    print(
                        f"{table_name}.parent_seller_sku must be NOT NULL",
                        file=sys.stderr,
                    )
                    return 1
            print(f"schema ok: mysql={version} revision={revision} tables={len(actual_tables)}")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
