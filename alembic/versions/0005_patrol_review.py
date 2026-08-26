"""Persist control-center initiated patrol reviews.

Revision ID: 0005_patrol_review
Revises: 0004_raw_fact_archive
Create Date: 2026-08-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0005_patrol_review"
down_revision: str | None = "0004_raw_fact_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_patrol_review",
        sa.Column("review_request_id", sa.String(128), nullable=False),
        sa.Column("review_round", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("patrol_batch_no", sa.String(128), nullable=False),
        sa.Column("proposal_id", sa.String(128), nullable=False),
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("baseline_snapshot_id", sa.String(64), nullable=False),
        sa.Column("current_snapshot_id", sa.String(64), nullable=True),
        sa.Column("request_json", mysql.JSON(), nullable=False),
        sa.Column("current_snapshot_json", mysql.JSON(), nullable=True),
        sa.Column("result_json", mysql.JSON(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("outcome_status", sa.String(32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("UTC_TIMESTAMP(6)"),
        ),
        sa.PrimaryKeyConstraint(
            "review_request_id", "review_round", name="pk_patrol_review"
        ),
        sa.ForeignKeyConstraint(
            ["baseline_snapshot_id"],
            ["t_patrol_fact_snapshot.snapshot_id"],
            name="fk_patrol_review_baseline",
        ),
        sa.CheckConstraint(
            "status IN ('PROCESSING', 'COMPLETED', 'FAILED')",
            name="ck_patrol_review_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index(
        "ix_patrol_review_unit",
        "t_patrol_review",
        ["operating_unit_id", "requested_at"],
    )
    op.create_index(
        "ix_patrol_review_proposal",
        "t_patrol_review",
        ["proposal_id", "review_round"],
    )


def downgrade() -> None:
    op.drop_table("t_patrol_review")
