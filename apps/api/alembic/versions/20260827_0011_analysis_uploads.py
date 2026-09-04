"""Add temporary Analysis Upload domain tables."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0011"
down_revision: str | Sequence[str] | None = "20260826_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        sa.Column("guest_session_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(owner_user_id IS NOT NULL AND guest_session_hash IS NULL) OR "
            "(owner_user_id IS NULL AND guest_session_hash IS NOT NULL)",
            name="ck_upload_sessions_exactly_one_actor",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_sessions_owner_user_id", "upload_sessions", ["owner_user_id"])
    op.create_index(
        "ix_upload_sessions_guest_session_hash", "upload_sessions", ["guest_session_hash"]
    )
    op.create_index("ix_upload_sessions_expires_at", "upload_sessions", ["expires_at"])

    op.create_table(
        "upload_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('input_file', 'input_directory', 'config_file')",
            name="ck_upload_items_kind",
        ),
        sa.CheckConstraint("file_count > 0", name="ck_upload_items_positive_file_count"),
        sa.CheckConstraint("total_bytes >= 0", name="ck_upload_items_nonnegative_total_bytes"),
        sa.ForeignKeyConstraint(["session_id"], ["upload_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_items_session_id", "upload_items", ["session_id"])
    op.create_index(
        "uq_upload_items_one_config_per_session",
        "upload_items",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'config_file'"),
    )


def downgrade() -> None:
    op.drop_index("uq_upload_items_one_config_per_session", table_name="upload_items")
    op.drop_index("ix_upload_items_session_id", table_name="upload_items")
    op.drop_table("upload_items")
    op.drop_index("ix_upload_sessions_expires_at", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_guest_session_hash", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_owner_user_id", table_name="upload_sessions")
    op.drop_table("upload_sessions")
