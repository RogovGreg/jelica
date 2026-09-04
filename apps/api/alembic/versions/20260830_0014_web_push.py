"""Add Web Push subscriptions and per-subscription delivery targets.

Revision ID: 20260830_0014
Revises: 20260830_0013
Create Date: 2026-08-30 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0014"
down_revision: str | Sequence[str] | None = "20260830_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_notification_settings",
        sa.Column(
            "sound_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )

    op.create_table(
        "web_push_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("auth_session_id", sa.String(length=36), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("endpoint_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("p256dh_key", sa.String(length=256), nullable=False),
        sa.Column("auth_key", sa.String(length=128), nullable=False),
        sa.Column("expiration_time", sa.BigInteger(), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["auth_session_id"],
            ["auth_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "auth_session_id",
            name="uq_web_push_subscription_auth_session",
        ),
        sa.UniqueConstraint(
            "endpoint_fingerprint",
            name="uq_web_push_subscription_endpoint",
        ),
    )
    op.create_index(
        "ix_web_push_subscriptions_user_id",
        "web_push_subscriptions",
        ["user_id"],
    )
    op.create_index(
        "ix_web_push_subscriptions_auth_session_id",
        "web_push_subscriptions",
        ["auth_session_id"],
    )
    op.create_index(
        "ix_web_push_subscriptions_user_active",
        "web_push_subscriptions",
        ["user_id", "active"],
    )

    op.create_table(
        "notification_delivery_targets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=True),
        sa.Column("target_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_notification_delivery_target_attempts",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'retry', 'sent', 'failed')",
            name="ck_notification_delivery_target_status",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["notification_deliveries.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["web_push_subscriptions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_id",
            "target_fingerprint",
            name="uq_notification_delivery_target",
        ),
    )
    op.create_index(
        "ix_notification_delivery_targets_delivery_id",
        "notification_delivery_targets",
        ["delivery_id"],
    )
    op.create_index(
        "ix_notification_delivery_targets_subscription_id",
        "notification_delivery_targets",
        ["subscription_id"],
    )
    op.create_index(
        "ix_notification_delivery_targets_pending",
        "notification_delivery_targets",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_delivery_targets_pending",
        table_name="notification_delivery_targets",
    )
    op.drop_index(
        "ix_notification_delivery_targets_subscription_id",
        table_name="notification_delivery_targets",
    )
    op.drop_index(
        "ix_notification_delivery_targets_delivery_id",
        table_name="notification_delivery_targets",
    )
    op.drop_table("notification_delivery_targets")

    op.drop_index(
        "ix_web_push_subscriptions_user_active",
        table_name="web_push_subscriptions",
    )
    op.drop_index(
        "ix_web_push_subscriptions_auth_session_id",
        table_name="web_push_subscriptions",
    )
    op.drop_index(
        "ix_web_push_subscriptions_user_id",
        table_name="web_push_subscriptions",
    )
    op.drop_table("web_push_subscriptions")

    op.drop_column("user_notification_settings", "sound_enabled")
