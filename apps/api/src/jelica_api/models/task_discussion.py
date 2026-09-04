from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .task_discussion_comment import TaskDiscussionComment
    from .web_task import WebTask


class TaskDiscussion(Base):
    __tablename__ = "task_discussions"

    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("web_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    task: Mapped[WebTask] = relationship("WebTask", back_populates="task_discussion")
    comments: Mapped[list[TaskDiscussionComment]] = relationship(
        "TaskDiscussionComment",
        back_populates="discussion",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="(TaskDiscussionComment.created_at, TaskDiscussionComment.id)",
    )


__all__ = ["TaskDiscussion"]
