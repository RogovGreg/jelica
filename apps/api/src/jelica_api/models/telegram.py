from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TelegramAccountLink(Base):
    __tablename__ = "telegram_account_links"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TelegramLinkToken(Base):
    __tablename__ = "telegram_link_tokens"
    __table_args__ = (Index("ix_telegram_link_tokens_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramMessageContext(Base):
    __tablename__ = "telegram_message_contexts"
    __table_args__ = (
        UniqueConstraint(
            "telegram_chat_id",
            "telegram_message_id",
            name="uq_telegram_message_context_chat_message",
        ),
        Index("ix_telegram_message_contexts_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notification_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("notifications.id", ondelete="SET NULL"), nullable=True, index=True
    )
    delivery_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("notification_deliveries.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    callback_token: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    context_type: Mapped[str] = mapped_column(String(48), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    comment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["TelegramAccountLink", "TelegramLinkToken", "TelegramMessageContext"]
