from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .task_discussion_comment import TaskDiscussionComment
    from .user import User


class TaskDiscussionCommentReaction(Base):
    __tablename__ = "task_discussion_comment_reactions"
    __table_args__ = (
        CheckConstraint(
            "reaction IN ('support', 'oppose')", name="ck_task_discussion_reactions_reaction"
        ),
    )

    comment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_discussion_comments.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    reaction: Mapped[str] = mapped_column(String(16), nullable=False)

    comment: Mapped[TaskDiscussionComment] = relationship(
        "TaskDiscussionComment", back_populates="reactions"
    )
    user: Mapped[User] = relationship("User", foreign_keys=[user_id])


__all__ = ["TaskDiscussionCommentReaction"]
