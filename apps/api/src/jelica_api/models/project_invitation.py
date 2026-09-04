from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .project import Project
    from .user import User


class ProjectInvitation(Base):
    __tablename__ = "project_invitations"
    __table_args__ = (
        CheckConstraint(
            "role IN ('viewer', 'commenter', 'member', 'supervisor')",
            name="ck_project_invitations_role",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'revoked')",
            name="ck_project_invitations_status",
        ),
        Index(
            "ix_project_invitations_project_id_invited_user_id",
            "project_id",
            "invited_user_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invited_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    invited_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'pending'"),
        index=True,
    )
    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    project: Mapped[Project] = relationship("Project", back_populates="invitations")
    invited_user: Mapped[User] = relationship(
        "User",
        back_populates="received_project_invitations",
        foreign_keys=[invited_user_id],
    )
    invited_by: Mapped[User] = relationship(
        "User",
        foreign_keys=[invited_by_user_id],
    )


__all__ = ["ProjectInvitation"]
