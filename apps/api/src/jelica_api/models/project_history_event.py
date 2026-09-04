from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .project import Project
    from .user import User


class ProjectHistoryEvent(Base):
    __tablename__ = "project_history_events"
    __table_args__ = (
        Index(
            "ix_project_history_events_project_id_occurred_at",
            "project_id",
            "occurred_at",
        ),
        Index(
            "ix_project_history_events_project_id_event_type",
            "project_id",
            "event_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    subject_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    project: Mapped[Project] = relationship("Project", back_populates="history")
    actor: Mapped[User | None] = relationship("User", foreign_keys=[actor_user_id])
    subject: Mapped[User | None] = relationship("User", foreign_keys=[subject_user_id])


__all__ = ["ProjectHistoryEvent"]
