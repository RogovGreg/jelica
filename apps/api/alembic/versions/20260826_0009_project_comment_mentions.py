"""Add Project comment mentions.

Revision ID: 20260826_0009
Revises: 20260825_0008
Create Date: 2026-08-26 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0009"
down_revision: str | Sequence[str] | None = "20260825_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_comment_mentions",
        sa.Column("comment_id", sa.String(length=36), nullable=False),
        sa.Column("mentioned_user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["project_comments.id"],
            name="fk_project_comment_mentions_comment_id_project_comments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mentioned_user_id"],
            ["users.id"],
            name="fk_project_comment_mentions_mentioned_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("comment_id", "mentioned_user_id"),
    )
    op.create_index(
        "ix_project_comment_mentions_mentioned_user_id",
        "project_comment_mentions",
        ["mentioned_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_comment_mentions_mentioned_user_id",
        table_name="project_comment_mentions",
    )
    op.drop_table("project_comment_mentions")
