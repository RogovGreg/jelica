from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .project_comment import ProjectComment
    from .user import User


class ProjectCommentMention(Base):
    __tablename__ = "project_comment_mentions"

    comment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("project_comments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mentioned_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    comment: Mapped[ProjectComment] = relationship(
        "ProjectComment",
        back_populates="mentions",
    )
    mentioned_user: Mapped[User] = relationship("User", foreign_keys=[mentioned_user_id])


__all__ = ["ProjectCommentMention"]
