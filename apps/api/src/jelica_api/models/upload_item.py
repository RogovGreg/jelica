from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .upload_session import UploadSession


class UploadItem(Base):
    __tablename__ = "upload_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('input_file', 'input_directory', 'config_file')",
            name="ck_upload_items_kind",
        ),
        CheckConstraint("file_count > 0", name="ck_upload_items_positive_file_count"),
        CheckConstraint("total_bytes >= 0", name="ck_upload_items_nonnegative_total_bytes"),
        Index(
            "uq_upload_items_one_config_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("kind = 'config_file'"),
            sqlite_where=text("kind = 'config_file'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("upload_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    session: Mapped[UploadSession] = relationship("UploadSession", back_populates="items")


__all__ = ["UploadItem"]
