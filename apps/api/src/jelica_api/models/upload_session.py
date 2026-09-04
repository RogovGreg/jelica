from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .upload_item import UploadItem
    from .user import User
    from .web_task import WebTask


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        CheckConstraint(
            "(owner_user_id IS NOT NULL AND guest_session_hash IS NULL) OR "
            "(owner_user_id IS NULL AND guest_session_hash IS NOT NULL)",
            name="ck_upload_sessions_exactly_one_actor",
        ),
        CheckConstraint(
            "(submission_status IN ('open', 'submitting', 'consumed')) AND "
            "((submission_status = 'consumed' AND task_id IS NOT NULL) OR "
            "(submission_status IN ('open', 'submitting') AND task_id IS NULL))",
            name="ck_upload_sessions_submission_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    guest_session_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    submission_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open", index=True
    )
    submission_trace_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("web_tasks.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    bound_core_task_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )

    owner: Mapped[User | None] = relationship("User", back_populates="upload_sessions")
    items: Mapped[list[UploadItem]] = relationship(
        "UploadItem",
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    task: Mapped[WebTask | None] = relationship("WebTask", back_populates="upload_session")


__all__ = ["UploadSession"]
