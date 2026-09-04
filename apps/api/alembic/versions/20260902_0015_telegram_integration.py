"""Add Telegram account links, one-time tokens, and durable message contexts.

Revision ID: 20260902_0015
Revises: 20260830_0014
Create Date: 2026-09-02 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0015"
down_revision: str | Sequence[str] | None = "20260830_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_account_links",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("display_name", sa.String(length=160), nullable=True),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("telegram_chat_id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_table(
        "telegram_link_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_telegram_link_tokens_user_id", "telegram_link_tokens", ["user_id"])
    op.create_index(
        "ix_telegram_link_tokens_user_created",
        "telegram_link_tokens",
        ["user_id", "created_at"],
    )
    op.create_table(
        "telegram_message_contexts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("notification_id", sa.String(length=36), nullable=True),
        sa.Column("delivery_id", sa.String(length=36), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("callback_token", sa.String(length=32), nullable=False),
        sa.Column("context_type", sa.String(length=48), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("comment_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"], ["notification_deliveries.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("callback_token"),
        sa.UniqueConstraint("delivery_id"),
        sa.UniqueConstraint(
            "telegram_chat_id",
            "telegram_message_id",
            name="uq_telegram_message_context_chat_message",
        ),
    )
    op.create_index(
        "ix_telegram_message_contexts_user_id", "telegram_message_contexts", ["user_id"]
    )
    op.create_index(
        "ix_telegram_message_contexts_notification_id",
        "telegram_message_contexts",
        ["notification_id"],
    )
    op.create_index(
        "ix_telegram_message_contexts_user_created",
        "telegram_message_contexts",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_message_contexts_user_created", table_name="telegram_message_contexts"
    )
    op.drop_index(
        "ix_telegram_message_contexts_notification_id",
        table_name="telegram_message_contexts",
    )
    op.drop_index("ix_telegram_message_contexts_user_id", table_name="telegram_message_contexts")
    op.drop_table("telegram_message_contexts")
    op.drop_index("ix_telegram_link_tokens_user_created", table_name="telegram_link_tokens")
    op.drop_index("ix_telegram_link_tokens_user_id", table_name="telegram_link_tokens")
    op.drop_table("telegram_link_tokens")
    op.drop_table("telegram_account_links")
