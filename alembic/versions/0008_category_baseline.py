"""Create the operator-confirmed category baseline registry.

Revision ID: 0008_category_baseline
Revises: 0007_control_center_business_key
Create Date: 2026-08-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0008_category_baseline"
down_revision: str | None = "0007_control_center_business_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_patrol_category_baseline",
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("child_asin", sa.String(32), nullable=False),
        sa.Column("shop_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("parent_asin", sa.String(32), nullable=False),
        sa.Column("parent_seller_sku", sa.String(128), nullable=False),
        sa.Column("category_id", sa.String(128), nullable=True),
        sa.Column("category_path", sa.String(1024), nullable=False),
        sa.Column("source_tool", sa.String(128), nullable=False),
        sa.Column("source_snapshot_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("first_observed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("last_observed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("confirmed_by", sa.String(128), nullable=True),
        sa.Column("confirmed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.PrimaryKeyConstraint(
            "operating_unit_id",
            "child_asin",
            name="pk_patrol_category_baseline",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_CONFIRMATION','CONFIRMED','REJECTED')",
            name="ck_patrol_category_baseline_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index(
        "ix_patrol_category_baseline_business",
        "t_patrol_category_baseline",
        ["shop_id", "parent_asin", "parent_seller_sku"],
    )


def downgrade() -> None:
    op.drop_table("t_patrol_category_baseline")
