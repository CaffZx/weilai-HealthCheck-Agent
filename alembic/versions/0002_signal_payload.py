"""Persist the canonical InspectionSignal payload.

Revision ID: 0002_signal_payload
Revises: 0001_patrol_runtime
Create Date: 2026-07-31
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0002_signal_payload"
down_revision: str | None = "0001_patrol_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_patrol_signal",
        sa.Column("signal_payload_json", mysql.JSON(), nullable=True),
    )
    op.execute(
        "UPDATE t_patrol_signal "
        "SET signal_payload_json = JSON_OBJECT('migration_status', 'REQUIRES_BACKFILL') "
        "WHERE signal_payload_json IS NULL"
    )
    op.alter_column(
        "t_patrol_signal",
        "signal_payload_json",
        existing_type=mysql.JSON(),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("t_patrol_signal", "signal_payload_json")
