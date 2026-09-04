"""Add Project comments.

Revision ID: 20260825_0007
Revises: 20260825_0006
Create Date: 2026-08-25 21:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260825_0007"
down_revision: str | Sequence[str] | None = "20260825_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_comments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("author_user_id", sa.String(length=36), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name="fk_project_comments_author_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_comments_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_comments_project_id",
        "project_comments",
        ["project_id"],
    )
    op.create_index(
        "ix_project_comments_author_user_id",
        "project_comments",
        ["author_user_id"],
    )
    op.create_index(
        "ix_project_comments_project_id_created_at",
        "project_comments",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_comments_project_id_created_at",
        table_name="project_comments",
    )
    op.drop_index(
        "ix_project_comments_author_user_id",
        table_name="project_comments",
    )
    op.drop_index("ix_project_comments_project_id", table_name="project_comments")
    op.drop_table("project_comments")
