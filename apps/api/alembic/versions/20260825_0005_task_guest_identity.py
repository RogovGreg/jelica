"""Add Web task guest-session identity.

Revision ID: 20260825_0005
Revises: 20260825_0004
Create Date: 2026-08-25 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260825_0005"
down_revision: str | Sequence[str] | None = "20260825_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "web_tasks",
        sa.Column("guest_session_hash", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_web_tasks_single_owner_identity",
        "web_tasks",
        "owner_user_id IS NULL OR guest_session_hash IS NULL",
    )
    op.create_index(
        "ix_web_tasks_guest_session_hash",
        "web_tasks",
        ["guest_session_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_web_tasks_guest_session_hash", table_name="web_tasks")
    op.drop_constraint(
        "ck_web_tasks_single_owner_identity",
        "web_tasks",
        type_="check",
    )
    op.drop_column("web_tasks", "guest_session_hash")
