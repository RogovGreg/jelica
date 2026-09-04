from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .project import Project
    from .user import User


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        CheckConstraint(
            "role IN ('viewer', 'commenter', 'member', 'supervisor')",
            name="ck_project_members_role",
        ),
        Index("ix_project_members_project_id_role", "project_id", "role"),
    )

    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    project: Mapped[Project] = relationship("Project", back_populates="members")
    user: Mapped[User] = relationship("User", back_populates="project_members")


__all__ = ["ProjectMember"]
