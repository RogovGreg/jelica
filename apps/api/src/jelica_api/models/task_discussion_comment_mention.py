from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .task_discussion_comment import TaskDiscussionComment
    from .user import User


class TaskDiscussionCommentMention(Base):
    __tablename__ = "task_discussion_comment_mentions"

    comment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_discussion_comments.id", ondelete="CASCADE"), primary_key=True
    )
    mentioned_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    comment: Mapped[TaskDiscussionComment] = relationship(
        "TaskDiscussionComment", back_populates="mentions"
    )
    mentioned_user: Mapped[User] = relationship("User", foreign_keys=[mentioned_user_id])


__all__ = ["TaskDiscussionCommentMention"]
