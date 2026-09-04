"""Add registered-user Project invitations.

Revision ID: 20260825_0006
Revises: 20260825_0005
Create Date: 2026-08-25 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260825_0006"
down_revision: str | Sequence[str] | None = "20260825_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_invitations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("invited_user_id", sa.String(length=36), nullable=False),
        sa.Column("invited_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "invited_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "role IN ('viewer', 'commenter', 'member', 'supervisor')",
            name="ck_project_invitations_role",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'revoked')",
            name="ck_project_invitations_status",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name="fk_project_invitations_invited_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invited_user_id"],
            ["users.id"],
            name="fk_project_invitations_invited_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_invitations_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_invitations_project_id",
        "project_invitations",
        ["project_id"],
    )
    op.create_index(
        "ix_project_invitations_invited_user_id",
        "project_invitations",
        ["invited_user_id"],
    )
    op.create_index(
        "ix_project_invitations_project_id_invited_user_id",
        "project_invitations",
        ["project_id", "invited_user_id"],
    )
    op.create_index(
        "ix_project_invitations_status",
        "project_invitations",
        ["status"],
    )
    op.create_index(
        "ix_project_invitations_expires_at",
        "project_invitations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_invitations_expires_at", table_name="project_invitations")
    op.drop_index("ix_project_invitations_status", table_name="project_invitations")
    op.drop_index(
        "ix_project_invitations_project_id_invited_user_id",
        table_name="project_invitations",
    )
    op.drop_index(
        "ix_project_invitations_invited_user_id",
        table_name="project_invitations",
    )
    op.drop_index("ix_project_invitations_project_id", table_name="project_invitations")
    op.drop_table("project_invitations")
