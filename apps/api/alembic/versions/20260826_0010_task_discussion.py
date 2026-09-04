"""Add Task Discussion domain tables."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0010"
down_revision: str | Sequence[str] | None = "20260826_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_discussions",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["web_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_table(
        "task_discussion_comments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("author_user_id", sa.String(length=36), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["task_discussions.task_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_discussion_comments_task_id", "task_discussion_comments", ["task_id"])
    op.create_index(
        "ix_task_discussion_comments_author_user_id", "task_discussion_comments", ["author_user_id"]
    )
    op.create_index(
        "ix_task_discussion_comments_task_id_created_at",
        "task_discussion_comments",
        ["task_id", "created_at"],
    )
    op.create_table(
        "task_discussion_comment_reactions",
        sa.Column("comment_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("reaction", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "reaction IN ('support', 'oppose')", name="ck_task_discussion_reactions_reaction"
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"], ["task_discussion_comments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("comment_id", "user_id"),
    )
    op.create_index(
        "ix_task_discussion_comment_reactions_user_id",
        "task_discussion_comment_reactions",
        ["user_id"],
    )
    op.create_table(
        "task_discussion_comment_mentions",
        sa.Column("comment_id", sa.String(length=36), nullable=False),
        sa.Column("mentioned_user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"], ["task_discussion_comments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["mentioned_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("comment_id", "mentioned_user_id"),
    )
    op.create_index(
        "ix_task_discussion_comment_mentions_mentioned_user_id",
        "task_discussion_comment_mentions",
        ["mentioned_user_id"],
    )
    # Discussion is enabled only for authenticated tasks already linked at migration time.
    op.execute(
        sa.text(
            "INSERT INTO task_discussions (task_id) "
            "SELECT id FROM web_tasks WHERE owner_user_id IS NOT NULL AND project_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_discussion_comment_mentions_mentioned_user_id",
        table_name="task_discussion_comment_mentions",
    )
    op.drop_table("task_discussion_comment_mentions")
    op.drop_index(
        "ix_task_discussion_comment_reactions_user_id",
        table_name="task_discussion_comment_reactions",
    )
    op.drop_table("task_discussion_comment_reactions")
    op.drop_index(
        "ix_task_discussion_comments_task_id_created_at", table_name="task_discussion_comments"
    )
    op.drop_index(
        "ix_task_discussion_comments_author_user_id", table_name="task_discussion_comments"
    )
    op.drop_index("ix_task_discussion_comments_task_id", table_name="task_discussion_comments")
    op.drop_table("task_discussion_comments")
    op.drop_table("task_discussions")
