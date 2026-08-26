"""Create the weekly refreshed active operating-unit catalog.

Revision ID: 0011_operating_unit_catalog
Revises: 0010_product_image_cache
Create Date: 2026-08-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0011_operating_unit_catalog"
down_revision: str | None = "0010_product_image_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_patrol_operating_unit_catalog",
        sa.Column("operating_unit_id", sa.String(64), primary_key=True),
        sa.Column("shop_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("site_code", sa.String(32), nullable=False),
        sa.Column("parent_asin", sa.String(32), nullable=False),
        sa.Column("parent_seller_sku", sa.String(128), nullable=False),
        sa.Column("shop_account_ref", sa.String(128), nullable=False),
        sa.Column("owner_user_ids_json", mysql.JSON(), nullable=False),
        sa.Column("source_tool", sa.String(128), nullable=False),
        sa.Column("fetched_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint(
            "shop_id",
            "site_code",
            "parent_asin",
            "parent_seller_sku",
            name="ux_patrol_catalog_business_key",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("t_patrol_operating_unit_catalog")
