"""Create independent asynchronous child-fact tasks and snapshots.

Revision ID: 0009_child_fact_async
Revises: 0008_category_baseline
Create Date: 2026-08-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0009_child_fact_async"
down_revision: str | None = "0008_category_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def datetime6() -> mysql.DATETIME:
    return mysql.DATETIME(fsp=6)


TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_bin",
}


def upgrade() -> None:
    op.create_table(
        "t_patrol_child_fact_task",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("task_key", sa.CHAR(64), nullable=False),
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("child_asin", sa.String(32), nullable=False),
        sa.Column("seller_sku", sa.String(128), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("arguments_json", mysql.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("retry_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("max_retry", mysql.INTEGER(unsigned=True), nullable=False, server_default="3"),
        sa.Column("next_attempt_at", datetime6(), nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("locked_at", datetime6(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", datetime6(), nullable=False),
        sa.Column("updated_at", datetime6(), nullable=False),
        sa.UniqueConstraint("task_key", name="ux_patrol_child_fact_task_key"),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','DEAD')",
            name="ck_patrol_child_fact_task_status",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_patrol_child_fact_task_claim",
        "t_patrol_child_fact_task",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_table(
        "t_patrol_child_fact_snapshot",
        sa.Column("snapshot_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("child_asin", sa.String(32), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("response_content_hash", sa.CHAR(64), nullable=False),
        sa.Column("response_json", mysql.JSON(), nullable=False),
        sa.Column("extracted_data_json", mysql.JSON(), nullable=False),
        sa.Column("fetched_at", datetime6(), nullable=False),
        sa.Column("expires_at", datetime6(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["t_patrol_child_fact_task.task_id"],
            name="fk_patrol_child_fact_snapshot_task",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_patrol_child_fact_snapshot_latest",
        "t_patrol_child_fact_snapshot",
        ["operating_unit_id", "child_asin", "domain", "fetched_at"],
    )


def downgrade() -> None:
    op.drop_table("t_patrol_child_fact_snapshot")
    op.drop_table("t_patrol_child_fact_task")
