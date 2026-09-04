from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .project_comment import ProjectComment
    from .project_history_event import ProjectHistoryEvent
    from .project_invitation import ProjectInvitation
    from .project_member import ProjectMember
    from .user import User
    from .web_task import WebTask


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'frozen')",
            name="ck_projects_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'active'"),
        index=True,
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
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

    created_by: Mapped[User] = relationship(
        "User",
        foreign_keys=[created_by_user_id],
    )
    owner: Mapped[User] = relationship(
        "User",
        back_populates="owned_projects",
        foreign_keys=[owner_user_id],
    )
    members: Mapped[list[ProjectMember]] = relationship(
        "ProjectMember",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tasks: Mapped[list[WebTask]] = relationship(
        "WebTask",
        back_populates="project",
        passive_deletes=True,
    )
    history: Mapped[list[ProjectHistoryEvent]] = relationship(
        "ProjectHistoryEvent",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    invitations: Mapped[list[ProjectInvitation]] = relationship(
        "ProjectInvitation",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    comments: Mapped[list[ProjectComment]] = relationship(
        "ProjectComment",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


__all__ = ["Project"]
