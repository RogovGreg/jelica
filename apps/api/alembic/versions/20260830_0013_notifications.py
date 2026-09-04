# ruff: noqa: E501
"""Add notification preferences and durable outbox foundation."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260830_0013"
down_revision = "20260827_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_notification_settings",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "user_notification_channel_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "channel", name="uq_user_notification_channel"),
    )
    op.create_index("ix_user_notification_channel_settings_user_id", "user_notification_channel_settings", ["user_id"])
    op.create_table(
        "user_notification_event_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(160), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "event_id", "channel", name="uq_user_notification_event"),
    )
    op.create_index("ix_user_notification_event_settings_user_id", "user_notification_event_settings", ["user_id"])
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipient_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_id", sa.String(160), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("recipient_user_id", "event_id", "source_type", "source_id", name="uq_notification_source"),
    )
    op.create_index("ix_notifications_recipient_user_id", "notifications", ["recipient_user_id"])
    op.create_index("ix_notifications_recipient_created", "notifications", ["recipient_user_id", "created_at"])
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("notification_id", sa.String(36), sa.ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("notification_id", "channel", name="uq_notification_delivery"),
        sa.CheckConstraint("status IN ('pending', 'retry', 'sent', 'failed')", name="ck_notification_delivery_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_notification_delivery_attempts"),
    )
    op.create_index("ix_notification_deliveries_notification_id", "notification_deliveries", ["notification_id"])
    op.create_index("ix_notification_deliveries_pending", "notification_deliveries", ["status", "available_at"])


def downgrade() -> None:
    op.drop_index("ix_notification_deliveries_pending", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_notification_id", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_index("ix_notifications_recipient_created", table_name="notifications")
    op.drop_index("ix_notifications_recipient_user_id", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_user_notification_event_settings_user_id", table_name="user_notification_event_settings")
    op.drop_table("user_notification_event_settings")
    op.drop_index("ix_user_notification_channel_settings_user_id", table_name="user_notification_channel_settings")
    op.drop_table("user_notification_channel_settings")
    op.drop_table("user_notification_settings")
