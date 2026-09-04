"""Create support requests table.

Revision ID: 20260824_0002
Revises: 20260823_0001
Create Date: 2026-08-24 15:45:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260824_0002"
down_revision: str | Sequence[str] | None = "20260823_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_support_requests_created_at", "support_requests", ["created_at"], unique=False)
    op.create_index("ix_support_requests_status", "support_requests", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_support_requests_status", table_name="support_requests")
    op.drop_index("ix_support_requests_created_at", table_name="support_requests")
    op.drop_table("support_requests")
