"""Add typed user-owned Web UI preferences."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0016"
down_revision: str | Sequence[str] | None = "20260902_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("theme", sa.String(length=16), server_default=sa.text("'light'"), nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("interface_scale", sa.Integer(), server_default=sa.text("100"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "interface_scale")
    op.drop_column("users", "theme")
