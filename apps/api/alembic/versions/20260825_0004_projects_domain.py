"""Create Web projects domain and task ownership linkage.

Revision ID: 20260825_0004
Revises: 20260824_0003
Create Date: 2026-08-25 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260825_0004"
down_revision: str | Sequence[str] | None = "20260824_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
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
        sa.CheckConstraint(
            "status IN ('active', 'frozen')",
            name="ck_projects_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_projects_created_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_projects_owner_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"])
    op.create_index("ix_projects_status", "projects", ["status"])

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('viewer', 'commenter', 'member', 'supervisor')",
            name="ck_project_members_role",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_members_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_project_members_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
    )
    op.create_index(
        "ix_project_members_project_id_role",
        "project_members",
        ["project_id", "role"],
    )
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])

    op.create_table(
        "project_history_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("subject_user_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_project_history_events_actor_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_history_events_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"],
            ["users.id"],
            name="fk_project_history_events_subject_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_history_events_project_id_event_type",
        "project_history_events",
        ["project_id", "event_type"],
    )
    op.create_index(
        "ix_project_history_events_project_id_occurred_at",
        "project_history_events",
        ["project_id", "occurred_at"],
    )

    op.add_column(
        "web_tasks",
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "web_tasks",
        sa.Column("project_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_web_tasks_owner_user_id_users",
        "web_tasks",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_web_tasks_project_id_projects",
        "web_tasks",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_web_tasks_owner_user_id", "web_tasks", ["owner_user_id"])
    op.create_index("ix_web_tasks_project_id", "web_tasks", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_web_tasks_project_id", table_name="web_tasks")
    op.drop_index("ix_web_tasks_owner_user_id", table_name="web_tasks")
    op.drop_constraint(
        "fk_web_tasks_project_id_projects",
        "web_tasks",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_web_tasks_owner_user_id_users",
        "web_tasks",
        type_="foreignkey",
    )
    op.drop_column("web_tasks", "project_id")
    op.drop_column("web_tasks", "owner_user_id")

    op.drop_index(
        "ix_project_history_events_project_id_occurred_at",
        table_name="project_history_events",
    )
    op.drop_index(
        "ix_project_history_events_project_id_event_type",
        table_name="project_history_events",
    )
    op.drop_table("project_history_events")

    op.drop_index("ix_project_members_user_id", table_name="project_members")
    op.drop_index(
        "ix_project_members_project_id_role",
        table_name="project_members",
    )
    op.drop_table("project_members")

    op.drop_index("ix_projects_status", table_name="projects")
    op.drop_index("ix_projects_owner_user_id", table_name="projects")
    op.drop_table("projects")
