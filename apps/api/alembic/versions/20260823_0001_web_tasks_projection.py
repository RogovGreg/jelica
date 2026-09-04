"""Create initial web_tasks projection table.

Revision ID: 20260823_0001
Revises:
Create Date: 2026-08-23 23:05:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260823_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("core_task_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_web_tasks_core_task_id", "web_tasks", ["core_task_id"], unique=True)
    op.create_index("ix_web_tasks_status", "web_tasks", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_web_tasks_status", table_name="web_tasks")
    op.drop_index("ix_web_tasks_core_task_id", table_name="web_tasks")
    op.drop_table("web_tasks")
