from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .project import Project
    from .task_discussion import TaskDiscussion
    from .upload_session import UploadSession
    from .user import User


class WebTask(Base):
    __tablename__ = "web_tasks"
    __table_args__ = (
        CheckConstraint(
            "owner_user_id IS NULL OR guest_session_hash IS NULL",
            name="ck_web_tasks_single_owner_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    core_task_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
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
    project_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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

    owner: Mapped[User | None] = relationship(
        "User",
        back_populates="owned_tasks",
        foreign_keys=[owner_user_id],
    )
    project: Mapped[Project | None] = relationship("Project", back_populates="tasks")
    task_discussion: Mapped[TaskDiscussion | None] = relationship(
        "TaskDiscussion",
        back_populates="task",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    upload_session: Mapped[UploadSession | None] = relationship(
        "UploadSession", back_populates="task", uselist=False
    )


__all__ = ["WebTask"]
