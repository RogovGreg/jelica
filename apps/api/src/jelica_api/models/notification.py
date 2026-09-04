# ruff: noqa: E501
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UserNotificationSettings(Base):
    __tablename__ = "user_notification_settings"
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sound_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserNotificationChannelSetting(Base):
    __tablename__ = "user_notification_channel_settings"
    __table_args__ = (UniqueConstraint("user_id", "channel", name="uq_user_notification_channel"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserNotificationEventSetting(Base):
    __tablename__ = "user_notification_event_settings"
    __table_args__ = (
        UniqueConstraint("user_id", "event_id", "channel", name="uq_user_notification_event"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint(
            "recipient_user_id",
            "event_id",
            "source_type",
            "source_id",
            name="uq_notification_source",
        ),
        Index("ix_notifications_recipient_created", "recipient_user_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    recipient_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("notification_id", "channel", name="uq_notification_delivery"),
        CheckConstraint(
            "status IN ('pending', 'retry', 'sent', 'failed')",
            name="ck_notification_delivery_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_notification_delivery_attempts"),
        Index("ix_notification_deliveries_pending", "status", "available_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    notification_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WebPushSubscription(Base):
    __tablename__ = "web_push_subscriptions"
    __table_args__ = (
        UniqueConstraint("auth_session_id", name="uq_web_push_subscription_auth_session"),
        UniqueConstraint("endpoint_fingerprint", name="uq_web_push_subscription_endpoint"),
        Index("ix_web_push_subscriptions_user_active", "user_id", "active"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    auth_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("auth_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    p256dh_key: Mapped[str] = mapped_column(String(256), nullable=False)
    auth_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expiration_time: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class NotificationDeliveryTarget(Base):
    __tablename__ = "notification_delivery_targets"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "target_fingerprint",
            name="uq_notification_delivery_target",
        ),
        CheckConstraint(
            "status IN ('pending', 'retry', 'sent', 'failed')",
            name="ck_notification_delivery_target_status",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_notification_delivery_target_attempts",
        ),
        Index(
            "ix_notification_delivery_targets_pending",
            "status",
            "available_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    delivery_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("notification_deliveries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("web_push_subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = [
    "Notification",
    "NotificationDelivery",
    "NotificationDeliveryTarget",
    "UserNotificationChannelSetting",
    "UserNotificationEventSetting",
    "UserNotificationSettings",
    "WebPushSubscription",
]
