"""Create the MySQL owner directory populated by forward principal lookup.

Revision ID: 0006_owner_directory
Revises: 0005_patrol_review
Create Date: 2026-08-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0006_owner_directory"
down_revision: str | None = "0005_patrol_review"
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
        "t_patrol_sys_user",
        sa.Column(
            "user_id",
            mysql.BIGINT(unsigned=True),
            nullable=False,
            autoincrement=False,
        ),
        sa.Column("user_name", sa.String(128), nullable=False),
        sa.Column("user_account", sa.String(128), nullable=True),
        sa.Column("user_state", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", datetime6(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_patrol_sys_user"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_patrol_sys_user_name_state",
        "t_patrol_sys_user",
        ["user_name", "is_active"],
    )

    op.create_table(
        "t_patrol_user_role",
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("role_code", sa.String(128), nullable=False),
        sa.Column("role_name", sa.String(128), nullable=True),
        sa.Column("fetched_at", datetime6(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "role_code", name="pk_patrol_user_role"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["t_patrol_sys_user.user_id"],
            name="fk_patrol_user_role_user",
            ondelete="CASCADE",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_patrol_user_role_code",
        "t_patrol_user_role",
        ["role_code", "user_id"],
    )

    op.create_table(
        "t_patrol_asin_owner",
        sa.Column("operating_unit_id", sa.String(64), nullable=False),
        sa.Column("assignment_key", sa.String(96), nullable=False),
        sa.Column("shop_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("shop_account_ref", sa.String(128), nullable=False),
        sa.Column("site_code", sa.String(32), nullable=False),
        sa.Column("parent_asin", sa.String(32), nullable=False),
        sa.Column("parent_seller_sku", sa.String(128), nullable=True),
        sa.Column("principal_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("editor_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("creator_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("source_tool", sa.String(128), nullable=False),
        sa.Column("fetched_at", datetime6(), nullable=False),
        sa.PrimaryKeyConstraint(
            "operating_unit_id",
            "assignment_key",
            name="pk_patrol_asin_owner",
        ),
        sa.CheckConstraint(
            "principal_user_id IS NOT NULL OR editor_user_id IS NOT NULL "
            "OR creator_user_id IS NOT NULL",
            name="ck_patrol_asin_owner_access",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_patrol_asin_owner_business",
        "t_patrol_asin_owner",
        ["shop_id", "site_code", "parent_asin"],
    )
    op.create_index(
        "ix_patrol_asin_owner_principal",
        "t_patrol_asin_owner",
        ["principal_user_id", "shop_id"],
    )
    op.execute(
        """
        CREATE VIEW v_patrol_product_access AS
        SELECT operating_unit_id, shop_id, site_code, parent_asin,
               parent_seller_sku, principal_user_id, editor_user_id, creator_user_id,
               COALESCE(principal_user_id, editor_user_id, creator_user_id) AS access_user_id,
               fetched_at
        FROM t_patrol_asin_owner
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_patrol_product_access")
    op.drop_table("t_patrol_asin_owner")
    op.drop_table("t_patrol_user_role")
    op.drop_table("t_patrol_sys_user")
