"""Add Project comment reactions.

Revision ID: 20260825_0008
Revises: 20260825_0007
Create Date: 2026-08-25 22:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260825_0008"
down_revision: str | Sequence[str] | None = "20260825_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_comment_reactions",
        sa.Column("comment_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("reaction", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "reaction IN ('support', 'oppose')",
            name="ck_project_comment_reactions_reaction",
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["project_comments.id"],
            name="fk_project_comment_reactions_comment_id_project_comments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_project_comment_reactions_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("comment_id", "user_id"),
    )
    op.create_index(
        "ix_project_comment_reactions_user_id",
        "project_comment_reactions",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_comment_reactions_user_id",
        table_name="project_comment_reactions",
    )
    op.drop_table("project_comment_reactions")
