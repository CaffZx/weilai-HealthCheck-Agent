"""Create the private patrol runtime schema.

Revision ID: 0001_patrol_runtime
Revises:
Create Date: 2026-07-31
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0001_patrol_runtime"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_bin",
}


def utc_timestamp() -> sa.TextClause:
    return sa.text("UTC_TIMESTAMP(6)")


def datetime6() -> mysql.DATETIME:
    return mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "t_patrol_batch",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("scope_json", mysql.JSON(), nullable=False),
        sa.Column("scope_hash", sa.CHAR(64), nullable=False),
        sa.Column("rule_bundle_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("total_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("pending_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("running_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("succeeded_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("failed_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("started_at", datetime6(), nullable=True),
        sa.Column("finished_at", datetime6(), nullable=True),
        sa.Column("created_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.Column("updated_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.UniqueConstraint("idempotency_key", name="ux_patrol_batch_idempotency"),
        sa.CheckConstraint(
            "pending_count + running_count + succeeded_count + failed_count <= total_count",
            name="ck_patrol_batch_counts",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_patrol_batch_business_date", "t_patrol_batch", ["business_date", "status"])

    op.create_table(
        "t_patrol_job",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(191), nullable=False),
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("shop_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("site_code", sa.String(32), nullable=False),
        sa.Column("parent_asin", sa.String(32), nullable=False),
        sa.Column("parent_seller_sku", sa.String(128), nullable=True),
        sa.Column("shop_account_ref", sa.String(128), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("payload_json", mysql.JSON(), nullable=False),
        sa.Column("result_ref", sa.String(64), nullable=True),
        sa.Column("retry_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("max_retry", mysql.INTEGER(unsigned=True), nullable=False, server_default="3"),
        sa.Column("next_retry_at", datetime6(), nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("locked_at", datetime6(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.Column("updated_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.ForeignKeyConstraint(["batch_id"], ["t_patrol_batch.batch_id"], name="fk_patrol_job_batch"),
        sa.UniqueConstraint("batch_id", "operating_unit_id", name="ux_patrol_job_batch_unit"),
        sa.CheckConstraint("retry_count <= max_retry", name="ck_patrol_job_retry"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_patrol_job_claim", "t_patrol_job", ["status", "next_retry_at", "created_at"])
    op.create_index("ix_patrol_job_unit", "t_patrol_job", ["operating_unit_id", "created_at"])

    op.create_table(
        "t_patrol_run",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(191), nullable=False),
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("rule_versions_json", mysql.JSON(), nullable=False),
        sa.Column("fact_snapshot_id", sa.String(64), nullable=True),
        sa.Column("signal_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("data_gap_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("started_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.Column("finished_at", datetime6(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.Column("updated_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.ForeignKeyConstraint(["job_id"], ["t_patrol_job.job_id"], name="fk_patrol_run_job"),
        sa.ForeignKeyConstraint(["batch_id"], ["t_patrol_batch.batch_id"], name="fk_patrol_run_batch"),
        sa.UniqueConstraint("request_id", name="ux_patrol_run_request"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_patrol_run_unit", "t_patrol_run", ["operating_unit_id", "created_at"])
    op.create_index("ix_patrol_run_batch", "t_patrol_run", ["batch_id", "status"])

    op.create_table(
        "t_patrol_fact_snapshot",
        sa.Column("snapshot_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("window_start", datetime6(), nullable=True),
        sa.Column("window_end", datetime6(), nullable=True),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("quality_status", sa.String(24), nullable=False),
        sa.Column("completeness_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("source_refs_json", mysql.JSON(), nullable=False),
        sa.Column("data_gaps_json", mysql.JSON(), nullable=False),
        sa.Column("normalized_summary_json", mysql.JSON(), nullable=False),
        sa.Column("raw_reference_json", mysql.JSON(), nullable=True),
        sa.Column("created_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.ForeignKeyConstraint(["run_id"], ["t_patrol_run.run_id"], name="fk_patrol_snapshot_run"),
        sa.UniqueConstraint("run_id", "content_hash", name="ux_patrol_snapshot_run_hash"),
        sa.CheckConstraint(
            "completeness_score IS NULL OR (completeness_score >= 0 AND completeness_score <= 100)",
            name="ck_patrol_snapshot_completeness",
        ),
        sa.CheckConstraint(
            "window_start IS NULL OR window_end IS NULL OR window_end >= window_start",
            name="ck_patrol_snapshot_window",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_patrol_snapshot_unit", "t_patrol_fact_snapshot", ["operating_unit_id", "as_of"])
    op.create_index("ix_patrol_snapshot_hash", "t_patrol_fact_snapshot", ["content_hash"])

    op.create_table(
        "t_patrol_signal",
        sa.Column("signal_id", sa.String(64), primary_key=True),
        sa.Column("dedup_key", sa.CHAR(64), nullable=False),
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("issue_code", sa.String(64), nullable=False),
        sa.Column("child_scope_key", sa.String(191), nullable=False, server_default=""),
        sa.Column("signal_type", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(24), nullable=False),
        sa.Column("signal_state", sa.String(32), nullable=False),
        sa.Column("action_timing_status", sa.String(32), nullable=False),
        sa.Column("economic_currency", sa.String(16), nullable=True),
        sa.Column("economic_amount", sa.Numeric(18, 4), nullable=True),
        sa.Column("economic_window", sa.String(32), nullable=False),
        sa.Column("constraint_flags_json", mysql.JSON(), nullable=False),
        sa.Column("execution_readiness", sa.String(32), nullable=False),
        sa.Column("diagnosis_json", mysql.JSON(), nullable=False),
        sa.Column("handoff_json", mysql.JSON(), nullable=True),
        sa.Column("first_detected_at", datetime6(), nullable=False),
        sa.Column("last_detected_at", datetime6(), nullable=False),
        sa.Column("last_scan_run_id", sa.String(64), nullable=False),
        sa.Column("recurrence_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("consecutive_miss_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("next_inspection_at", datetime6(), nullable=True),
        sa.Column("valid_until", datetime6(), nullable=True),
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False, server_default="1"),
        sa.Column("created_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.Column("updated_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.ForeignKeyConstraint(["last_scan_run_id"], ["t_patrol_run.run_id"], name="fk_patrol_signal_last_run"),
        sa.UniqueConstraint("dedup_key", name="ux_patrol_signal_dedup"),
        sa.UniqueConstraint(
            "operating_unit_id", "issue_code", "child_scope_key", name="ux_patrol_signal_business_key"
        ),
        sa.CheckConstraint("version >= 1", name="ck_patrol_signal_version"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_patrol_signal_due", "t_patrol_signal", ["signal_state", "next_inspection_at"])
    op.create_index("ix_patrol_signal_unit", "t_patrol_signal", ["operating_unit_id", "updated_at"])

    op.create_table(
        "t_patrol_signal_occurrence",
        sa.Column("occurrence_id", sa.String(64), primary_key=True),
        sa.Column("signal_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=True),
        sa.Column("occurrence_type", sa.String(24), nullable=False),
        sa.Column("severity", sa.String(24), nullable=True),
        sa.Column("evidence_refs_json", mysql.JSON(), nullable=False),
        sa.Column("details_json", mysql.JSON(), nullable=False),
        sa.Column("occurred_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.Column("created_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.ForeignKeyConstraint(["signal_id"], ["t_patrol_signal.signal_id"], name="fk_patrol_occurrence_signal"),
        sa.ForeignKeyConstraint(["run_id"], ["t_patrol_run.run_id"], name="fk_patrol_occurrence_run"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["t_patrol_fact_snapshot.snapshot_id"], name="fk_patrol_occurrence_snapshot"
        ),
        sa.UniqueConstraint(
            "signal_id", "run_id", "occurrence_type", name="ux_patrol_occurrence_signal_run_type"
        ),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_patrol_occurrence_run", "t_patrol_signal_occurrence", ["run_id"])

    op.create_table(
        "t_patrol_feedback_inbox",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("signal_id", sa.String(64), nullable=False),
        sa.Column("signal_version", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("source_system", sa.String(128), nullable=False),
        sa.Column("occurred_at", datetime6(), nullable=False),
        sa.Column("payload_hash", sa.CHAR(64), nullable=False),
        sa.Column("payload_json", mysql.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result_json", mysql.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("received_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.Column("applied_at", datetime6(), nullable=True),
        sa.ForeignKeyConstraint(["signal_id"], ["t_patrol_signal.signal_id"], name="fk_patrol_feedback_signal"),
        sa.CheckConstraint("signal_version >= 1", name="ck_patrol_feedback_version"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_patrol_feedback_status", "t_patrol_feedback_inbox", ["status", "received_at"])

    op.create_table(
        "t_patrol_delivery_outbox",
        sa.Column("outbox_id", sa.String(64), primary_key=True),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("aggregate_version", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("payload_hash", sa.CHAR(64), nullable=False),
        sa.Column("payload_json", mysql.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("retry_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("max_retry", mysql.INTEGER(unsigned=True), nullable=False, server_default="10"),
        sa.Column("next_retry_at", datetime6(), nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("locked_at", datetime6(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivery_ack_json", mysql.JSON(), nullable=True),
        sa.Column("created_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.Column("published_at", datetime6(), nullable=True),
        sa.UniqueConstraint(
            "aggregate_type", "aggregate_id", "aggregate_version", name="ux_patrol_outbox_aggregate_version"
        ),
        sa.CheckConstraint("aggregate_version >= 1", name="ck_patrol_outbox_version"),
        sa.CheckConstraint("retry_count <= max_retry", name="ck_patrol_outbox_retry"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_patrol_outbox_claim", "t_patrol_delivery_outbox", ["status", "next_retry_at", "created_at"]
    )

    op.create_table(
        "t_patrol_idempotency",
        sa.Column("actor_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(191), primary_key=True),
        sa.Column("route", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("response_json", mysql.JSON(), nullable=True),
        sa.Column("status_code", sa.SmallInteger(), nullable=True),
        sa.Column("created_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.Column("expires_at", datetime6(), nullable=False),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_patrol_idempotency_expiry", "t_patrol_idempotency", ["expires_at"])

    op.create_table(
        "t_patrol_scheduler_lock",
        sa.Column("lock_name", sa.String(128), primary_key=True),
        sa.Column("holder_id", sa.String(128), nullable=False),
        sa.Column("acquired_at", datetime6(), nullable=False),
        sa.Column("renewed_at", datetime6(), nullable=False),
        sa.Column("expires_at", datetime6(), nullable=False),
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False, server_default="1"),
        sa.CheckConstraint("expires_at > acquired_at", name="ck_patrol_scheduler_lock_expiry"),
        sa.CheckConstraint("version >= 1", name="ck_patrol_scheduler_lock_version"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_patrol_scheduler_lock_expiry", "t_patrol_scheduler_lock", ["expires_at"])


def downgrade() -> None:
    op.drop_table("t_patrol_scheduler_lock")
    op.drop_table("t_patrol_idempotency")
    op.drop_table("t_patrol_delivery_outbox")
    op.drop_table("t_patrol_feedback_inbox")
    op.drop_table("t_patrol_signal_occurrence")
    op.drop_table("t_patrol_signal")
    op.drop_table("t_patrol_fact_snapshot")
    op.drop_table("t_patrol_run")
    op.drop_table("t_patrol_job")
    op.drop_table("t_patrol_batch")
