"""Allow rejected feedback for unknown signals to be audited.

Revision ID: 0003_feedback_inbox_decoupling
Revises: 0002_signal_payload
Create Date: 2026-07-31
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_feedback_inbox_decoupling"
down_revision: str | None = "0002_signal_payload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_patrol_feedback_signal",
        "t_patrol_feedback_inbox",
        type_="foreignkey",
    )


def downgrade() -> None:
    op.create_foreign_key(
        "fk_patrol_feedback_signal",
        "t_patrol_feedback_inbox",
        "t_patrol_signal",
        ["signal_id"],
        ["signal_id"],
    )
