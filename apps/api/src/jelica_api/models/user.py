from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .project import Project
    from .project_invitation import ProjectInvitation
    from .project_member import ProjectMember
    from .upload_session import UploadSession
    from .web_task import WebTask


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    email_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    language: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'en'"),
    )
    theme: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'light'"),
    )
    interface_scale: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("100"),
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

    owned_tasks: Mapped[list[WebTask]] = relationship(
        "WebTask",
        back_populates="owner",
        foreign_keys="WebTask.owner_user_id",
        passive_deletes=True,
    )
    owned_projects: Mapped[list[Project]] = relationship(
        "Project",
        back_populates="owner",
        foreign_keys="Project.owner_user_id",
        passive_deletes=True,
    )
    project_members: Mapped[list[ProjectMember]] = relationship(
        "ProjectMember",
        back_populates="user",
        passive_deletes=True,
    )
    received_project_invitations: Mapped[list[ProjectInvitation]] = relationship(
        "ProjectInvitation",
        back_populates="invited_user",
        foreign_keys="ProjectInvitation.invited_user_id",
        passive_deletes=True,
    )
    upload_sessions: Mapped[list[UploadSession]] = relationship(
        "UploadSession",
        back_populates="owner",
        passive_deletes=True,
    )


__all__ = ["User"]
