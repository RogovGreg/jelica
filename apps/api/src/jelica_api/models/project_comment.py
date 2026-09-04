from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .project import Project
    from .project_comment_mention import ProjectCommentMention
    from .project_comment_reaction import ProjectCommentReaction
    from .user import User


class ProjectComment(Base):
    __tablename__ = "project_comments"
    __table_args__ = (
        Index(
            "ix_project_comments_project_id_created_at",
            "project_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    project: Mapped[Project] = relationship("Project", back_populates="comments")
    author: Mapped[User] = relationship("User", foreign_keys=[author_user_id])
    reactions: Mapped[list[ProjectCommentReaction]] = relationship(
        "ProjectCommentReaction",
        back_populates="comment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    mentions: Mapped[list[ProjectCommentMention]] = relationship(
        "ProjectCommentMention",
        back_populates="comment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


__all__ = ["ProjectComment"]
