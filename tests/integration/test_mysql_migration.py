from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_mock_engine

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0001_patrol_runtime.py"
SIGNAL_PAYLOAD_MIGRATION_PATH = ROOT / "alembic" / "versions" / "0002_signal_payload.py"
FEEDBACK_MIGRATION_PATH = (
    ROOT / "alembic" / "versions" / "0003_feedback_inbox_decoupling.py"
)
RAW_FACT_MIGRATION_PATH = ROOT / "alembic" / "versions" / "0004_raw_fact_archive.py"
REVIEW_MIGRATION_PATH = ROOT / "alembic" / "versions" / "0005_patrol_review.py"
OWNER_MIGRATION_PATH = ROOT / "alembic" / "versions" / "0006_owner_directory.py"
BUSINESS_KEY_MIGRATION_PATH = (
    ROOT / "alembic" / "versions" / "0007_control_center_business_key.py"
)
CATEGORY_BASELINE_MIGRATION_PATH = (
    ROOT / "alembic" / "versions" / "0008_category_baseline.py"
)
CHILD_FACT_MIGRATION_PATH = (
    ROOT / "alembic" / "versions" / "0009_child_fact_async.py"
)
PRODUCT_IMAGE_MIGRATION_PATH = (
    ROOT / "alembic" / "versions" / "0010_product_image_cache.py"
)
CATALOG_MIGRATION_PATH = ROOT / "alembic" / "versions" / "0011_operating_unit_catalog.py"


def load_migration():
    spec = importlib.util.spec_from_file_location("patrol_migration_0001", MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render_upgrade_sql() -> str:
    statements: list[str] = []

    def executor(sql, *multiparams, **params):
        statements.append(str(sql.compile(dialect=engine.dialect)))

    engine = create_mock_engine("mysql+pymysql://", executor)
    connection = engine.connect()
    context = MigrationContext.configure(connection)
    operations = Operations(context)
    migration = load_migration()
    original_op = migration.op
    try:
        migration.op = operations
        migration.upgrade()
    finally:
        migration.op = original_op
    return "\n".join(statements)


def test_mysql_migration_creates_only_patrol_tables():
    sql = render_upgrade_sql()
    expected_tables = {
        "t_patrol_batch",
        "t_patrol_job",
        "t_patrol_run",
        "t_patrol_fact_snapshot",
        "t_patrol_signal",
        "t_patrol_signal_occurrence",
        "t_patrol_feedback_inbox",
        "t_patrol_delivery_outbox",
        "t_patrol_idempotency",
        "t_patrol_scheduler_lock",
    }
    for table_name in expected_tables:
        assert f"CREATE TABLE {table_name}" in sql
    assert "t_ops_" not in sql
    assert "CREATE TABLE patrol_" not in sql


def test_mysql_migration_preserves_precision_and_identity_constraints():
    sql = render_upgrade_sql()
    assert "NUMERIC(18, 4)" in sql
    assert "NUMERIC(5, 2)" in sql
    assert "UNIQUE (operating_unit_id, issue_code, child_scope_key)" in sql
    assert "FOREIGN KEY(batch_id) REFERENCES t_patrol_batch" in sql


def test_signal_payload_migration_is_patrol_only():
    source = SIGNAL_PAYLOAD_MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"t_patrol_signal"' in source
    assert '"signal_payload_json"' in source
    assert "t_ops_" not in source


def test_feedback_migration_remains_patrol_only():
    source = FEEDBACK_MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"t_patrol_feedback_inbox"' in source
    assert '"fk_patrol_feedback_signal"' in source
    assert "t_ops_" not in source


def test_raw_fact_migration_archives_complete_mcp_payloads():
    source = RAW_FACT_MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"t_patrol_raw_fact"' in source
    assert '"request_json"' in source
    assert '"response_json"' in source
    assert '"extracted_data_json"' in source
    assert '"reused_from_raw_fact_id"' in source
    assert "t_ops_" not in source


def test_review_migration_is_mysql_patrol_only():
    source = REVIEW_MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"t_patrol_review"' in source
    assert '"baseline_snapshot_id"' in source
    assert '"result_json"' in source
    assert "t_ops_" not in source


def test_owner_migration_creates_mysql_directory_and_access_view():
    source = OWNER_MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"t_patrol_sys_user"' in source
    assert '"t_patrol_user_role"' in source
    assert '"t_patrol_asin_owner"' in source
    assert "v_patrol_product_access" in source
    assert "COALESCE(principal_user_id, editor_user_id, creator_user_id)" in source
    assert "sqlite" not in source.lower()


def test_business_key_migration_rewrites_all_operating_unit_relations():
    source = BUSINESS_KEY_MIGRATION_PATH.read_text(encoding="utf-8")
    assert "shop_id,parent_asin,parent_seller_sku" in source
    assert '"t_patrol_job"' in source
    assert '"t_patrol_run"' in source
    assert '"t_patrol_fact_snapshot"' in source
    assert '"t_patrol_raw_fact"' in source
    assert '"t_patrol_signal"' in source
    assert '"t_patrol_review"' in source
    assert '"t_patrol_asin_owner"' in source
    assert "parent_seller_sku is missing" in source
    assert "irreversible" in source


def test_category_baseline_migration_requires_operator_confirmation():
    source = CATEGORY_BASELINE_MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"t_patrol_category_baseline"' in source
    assert "PENDING_CONFIRMATION" in source
    assert '"confirmed_by"' in source
    assert '"confirmed_at"' in source
    assert "sqlite" not in source.lower()


def test_child_fact_migration_uses_independent_task_and_snapshot_tables():
    source = CHILD_FACT_MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"t_patrol_child_fact_task"' in source
    assert '"t_patrol_child_fact_snapshot"' in source
    assert "next_attempt_at" in source
    assert "expires_at" in source
    assert "patrol.run" not in source
    assert "sqlite" not in source.lower()


def test_product_image_migration_retains_images_by_business_key():
    source = PRODUCT_IMAGE_MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"t_patrol_product_image"' in source
    assert '"image_url"' in source
    assert '"source_tool"' in source
    assert "shop_id" in source
    assert "parent_asin" in source
    assert "parent_seller_sku" in source
    assert "sqlite" not in source.lower()


def test_operating_unit_catalog_migration_is_weekly_refreshable_and_authoritative():
    source = CATALOG_MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"t_patrol_operating_unit_catalog"' in source
    assert '"parent_seller_sku"' in source
    assert '"shop_account_ref"' in source
    assert '"ux_patrol_catalog_business_key"' in source
    assert '"owner_user_ids_json"' in source
    assert "sqlite" not in source.lower()
