"""Archive raw MCP facts for audit and controlled reuse.

Revision ID: 0004_raw_fact_archive
Revises: 0003_feedback_inbox_decoupling
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0004_raw_fact_archive"
down_revision: str | None = "0003_feedback_inbox_decoupling"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def utc_timestamp() -> sa.TextClause:
    return sa.text("UTC_TIMESTAMP(6)")


def datetime6() -> mysql.DATETIME:
    return mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "t_patrol_raw_fact",
        sa.Column("raw_fact_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=True),
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("fact_key", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("request_json", mysql.JSON(), nullable=False),
        sa.Column("response_content_hash", sa.CHAR(64), nullable=True),
        sa.Column("response_json", mysql.JSON(none_as_null=True), nullable=True),
        sa.Column("extracted_data_json", mysql.JSON(none_as_null=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("is_core", sa.Boolean(), nullable=False),
        sa.Column("row_count", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("latency_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("warning", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("fetched_at", datetime6(), nullable=False),
        sa.Column("reused_from_raw_fact_id", sa.String(64), nullable=True),
        sa.Column("created_at", datetime6(), nullable=False, server_default=utc_timestamp()),
        sa.ForeignKeyConstraint(["run_id"], ["t_patrol_run.run_id"], name="fk_patrol_raw_fact_run"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["t_patrol_fact_snapshot.snapshot_id"],
            name="fk_patrol_raw_fact_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["reused_from_raw_fact_id"],
            ["t_patrol_raw_fact.raw_fact_id"],
            name="fk_patrol_raw_fact_reused_from",
        ),
        sa.UniqueConstraint("run_id", "fact_key", name="ux_patrol_raw_fact_run_key"),
        sa.CheckConstraint("status IN ('SUCCESS', 'FAILED')", name="ck_patrol_raw_fact_status"),
        sa.CheckConstraint(
            "(status = 'SUCCESS' AND response_content_hash IS NOT NULL "
            "AND response_json IS NOT NULL AND extracted_data_json IS NOT NULL "
            "AND row_count IS NOT NULL AND error_code IS NULL) "
            "OR (status = 'FAILED' AND response_content_hash IS NULL "
            "AND response_json IS NULL AND extracted_data_json IS NULL "
            "AND row_count IS NULL AND error_code IS NOT NULL)",
            name="ck_patrol_raw_fact_payload",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_patrol_raw_fact_run", "t_patrol_raw_fact", ["run_id", "fact_key"])
    op.create_index(
        "ix_patrol_raw_fact_lookup",
        "t_patrol_raw_fact",
        ["operating_unit_id", "tool_name", "fetched_at"],
    )
    op.create_index(
        "ix_patrol_raw_fact_content_hash",
        "t_patrol_raw_fact",
        ["response_content_hash"],
    )


def downgrade() -> None:
    op.drop_table("t_patrol_raw_fact")
