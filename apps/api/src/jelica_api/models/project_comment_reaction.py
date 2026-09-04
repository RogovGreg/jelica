from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .project_comment import ProjectComment
    from .user import User


class ProjectCommentReaction(Base):
    __tablename__ = "project_comment_reactions"
    __table_args__ = (
        CheckConstraint(
            "reaction IN ('support', 'oppose')",
            name="ck_project_comment_reactions_reaction",
        ),
    )

    comment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("project_comments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
    reaction: Mapped[str] = mapped_column(String(16), nullable=False)

    comment: Mapped[ProjectComment] = relationship(
        "ProjectComment",
        back_populates="reactions",
    )
    user: Mapped[User] = relationship("User", foreign_keys=[user_id])


__all__ = ["ProjectCommentReaction"]
