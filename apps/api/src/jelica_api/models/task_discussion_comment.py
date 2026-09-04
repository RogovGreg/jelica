from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .task_discussion import TaskDiscussion
    from .task_discussion_comment_mention import TaskDiscussionCommentMention
    from .task_discussion_comment_reaction import TaskDiscussionCommentReaction
    from .user import User


class TaskDiscussionComment(Base):
    __tablename__ = "task_discussion_comments"
    __table_args__ = (
        Index("ix_task_discussion_comments_task_id_created_at", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("task_discussions.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    discussion: Mapped[TaskDiscussion] = relationship("TaskDiscussion", back_populates="comments")
    author: Mapped[User] = relationship("User", foreign_keys=[author_user_id])
    reactions: Mapped[list[TaskDiscussionCommentReaction]] = relationship(
        "TaskDiscussionCommentReaction",
        back_populates="comment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    mentions: Mapped[list[TaskDiscussionCommentMention]] = relationship(
        "TaskDiscussionCommentMention",
        back_populates="comment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


__all__ = ["TaskDiscussionComment"]
