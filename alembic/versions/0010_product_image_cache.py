"""Create the retained product main-image cache.

Revision ID: 0010_product_image_cache
Revises: 0009_child_fact_async
Create Date: 2026-08-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0010_product_image_cache"
down_revision: str | None = "0009_child_fact_async"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_patrol_product_image",
        sa.Column("operating_unit_id", sa.String(64), primary_key=True),
        sa.Column("shop_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("parent_asin", sa.String(32), nullable=False),
        sa.Column("parent_seller_sku", sa.String(128), nullable=False),
        sa.Column("image_url", sa.String(2048), nullable=False),
        sa.Column("source_tool", sa.String(128), nullable=False),
        sa.Column("source_fetched_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("first_observed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("last_observed_at", mysql.DATETIME(fsp=6), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index(
        "ix_patrol_product_image_business_key",
        "t_patrol_product_image",
        ["shop_id", "parent_asin", "parent_seller_sku"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("t_patrol_product_image")
