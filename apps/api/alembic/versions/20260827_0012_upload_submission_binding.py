"""Bind upload sessions to submitted web tasks."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260827_0012"
down_revision = "20260827_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("upload_sessions", sa.Column("submission_status", sa.String(16), nullable=False, server_default="open"))
    op.add_column("upload_sessions", sa.Column("submission_trace_id", sa.String(36), nullable=True))
    op.add_column("upload_sessions", sa.Column("task_id", sa.String(36), nullable=True))
    op.add_column("upload_sessions", sa.Column("bound_core_task_id", sa.String(36), nullable=True))
    op.create_index("ix_upload_sessions_submission_status", "upload_sessions", ["submission_status"])
    op.create_index("ix_upload_sessions_submission_trace_id", "upload_sessions", ["submission_trace_id"], unique=True)
    op.create_index("ix_upload_sessions_task_id", "upload_sessions", ["task_id"], unique=True)
    op.create_index("ix_upload_sessions_bound_core_task_id", "upload_sessions", ["bound_core_task_id"], unique=True)
    op.create_check_constraint(
        "ck_upload_sessions_submission_state",
        "upload_sessions",
        "(submission_status IN ('open', 'submitting', 'consumed')) AND ((submission_status = 'consumed' AND task_id IS NOT NULL) OR (submission_status IN ('open', 'submitting') AND task_id IS NULL))",
    )
    op.create_foreign_key(
        "fk_upload_sessions_task_id_web_tasks",
        "upload_sessions",
        "web_tasks",
        ["task_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_upload_sessions_task_id_web_tasks", "upload_sessions", type_="foreignkey")
    op.drop_constraint("ck_upload_sessions_submission_state", "upload_sessions", type_="check")
    op.drop_index("ix_upload_sessions_bound_core_task_id", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_task_id", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_submission_trace_id", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_submission_status", table_name="upload_sessions")
    op.drop_column("upload_sessions", "bound_core_task_id")
    op.drop_column("upload_sessions", "task_id")
    op.drop_column("upload_sessions", "submission_trace_id")
    op.drop_column("upload_sessions", "submission_status")
