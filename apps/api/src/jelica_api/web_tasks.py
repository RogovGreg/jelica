from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from jelica_api.models import ProjectMember, WebTask
from jelica_api.notifications import NotificationService
from jelica_api.task_access import WebTaskActor, task_visibility_predicate

_TERMINAL_TASK_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "deletion_requested",
    }
)


def is_active_task_status(status: str) -> bool:
    """Return the authoritative Web-projection active/terminal distinction."""
    return status not in _TERMINAL_TASK_STATUSES


@dataclass(frozen=True, slots=True)
class WebTaskProjectionRecord:
    core_task_id: str
    name: str | None
    status: str
    owner_user_id: str | None
    guest_session_hash: str | None
    project_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WebTaskProjectionStore:
    session_factory: sessionmaker[Session]
    notification_service: NotificationService | None = None

    def upsert_task(
        self,
        *,
        core_task_id: str,
        name: str | None,
        status: str,
        owner_user_id: str | None = None,
        guest_session_hash: str | None = None,
    ) -> None:
        normalized_core_task_id = _require_non_empty_text(
            value=core_task_id,
            field_name="core_task_id",
        )
        normalized_status = _require_non_empty_text(value=status, field_name="status")
        normalized_name = _normalize_optional_text(value=name)
        normalized_owner_user_id = _normalize_optional_text(value=owner_user_id)
        normalized_guest_session_hash = _normalize_optional_text(value=guest_session_hash)
        if normalized_owner_user_id is not None and normalized_guest_session_hash is not None:
            raise ValueError(
                "owner_user_id and guest_session_hash cannot both be assigned to a task"
            )

        try:
            self._upsert_once(
                core_task_id=normalized_core_task_id,
                name=normalized_name,
                status=normalized_status,
                owner_user_id=normalized_owner_user_id,
                guest_session_hash=normalized_guest_session_hash,
            )
        except IntegrityError:
            self._upsert_once(
                core_task_id=normalized_core_task_id,
                name=normalized_name,
                status=normalized_status,
                owner_user_id=normalized_owner_user_id,
                guest_session_hash=normalized_guest_session_hash,
            )

    def _upsert_once(
        self,
        *,
        core_task_id: str,
        name: str | None,
        status: str,
        owner_user_id: str | None,
        guest_session_hash: str | None,
    ) -> None:
        with self.session_factory() as session, session.begin():
            projection = session.scalar(
                select(WebTask).where(WebTask.core_task_id == core_task_id).with_for_update()
            )
            previous_status = projection.status if projection is not None else None
            if projection is None:
                projection = WebTask(
                    core_task_id=core_task_id,
                    name=name,
                    status=status,
                    owner_user_id=owner_user_id,
                    guest_session_hash=guest_session_hash,
                )
                session.add(projection)
                session.flush()
            else:
                projection.status = status
                if name is not None:
                    projection.name = name
            if previous_status != status:
                self._enqueue_transition_notifications(
                    session=session,
                    projection=projection,
                    previous_status=previous_status,
                    next_status=status,
                )

    def _enqueue_transition_notifications(
        self,
        *,
        session: Session,
        projection: WebTask,
        previous_status: str | None,
        next_status: str,
    ) -> None:
        notifications = self.notification_service
        if notifications is None:
            return
        lifecycle_event: str | None = None
        source_id = projection.core_task_id
        if next_status == "running" and previous_status in {
            None,
            "created",
            "submitted",
            "waiting",
            "queued",
        }:
            lifecycle_event = "task.started"
        elif next_status == "preemption_requested":
            lifecycle_event = "task.scheduler_paused"
            source_id = f"{projection.core_task_id}:{uuid4()}"
        elif next_status == "completed":
            lifecycle_event = "task.completed"
        elif next_status == "failed":
            lifecycle_event = "task.failed"
        if lifecycle_event is None:
            return

        task_payload = {
            "resource_kind": "task",
            "task_id": projection.core_task_id,
            "display_name": projection.name or projection.core_task_id,
            "target_path": f"/app/tasks/{projection.core_task_id}",
        }
        if projection.owner_user_id is not None:
            notifications.enqueue(
                session=session,
                recipient_user_id=projection.owner_user_id,
                event_id=lifecycle_event,
                source_type="task_transition",
                source_id=source_id,
                payload=task_payload,
            )

        if projection.project_id is None or lifecycle_event == "task.scheduler_paused":
            return
        project_event = {
            "task.started": "project.task.started",
            "task.completed": "project.task.completed",
            "task.failed": "project.task.failed",
        }.get(lifecycle_event)
        if project_event is None:
            return
        member_ids = tuple(
            session.scalars(
                select(ProjectMember.user_id).where(
                    ProjectMember.project_id == projection.project_id,
                    ProjectMember.user_id != projection.owner_user_id,
                )
            )
        )
        project_payload = {
            "resource_kind": "project_tasks",
            "project_id": projection.project_id,
            "task_id": projection.core_task_id,
            "display_name": projection.name or projection.core_task_id,
            "target_path": f"/app/projects/{projection.project_id}/tasks",
        }
        for member_id in member_ids:
            notifications.enqueue(
                session=session,
                recipient_user_id=member_id,
                event_id=project_event,
                source_type="task_transition",
                source_id=source_id,
                payload=project_payload,
            )

    def get_task(self, *, core_task_id: str) -> WebTaskProjectionRecord | None:
        normalized_core_task_id = _require_non_empty_text(
            value=core_task_id,
            field_name="core_task_id",
        )
        with self.session_factory() as session:
            projection = _load_task_projection(
                session=session,
                core_task_id=normalized_core_task_id,
            )
            if projection is None:
                return None
            return _to_projection_record(projection=projection)

    def get_visible_task(
        self,
        *,
        core_task_id: str,
        actor: WebTaskActor,
    ) -> WebTaskProjectionRecord | None:
        normalized_core_task_id = _require_non_empty_text(
            value=core_task_id,
            field_name="core_task_id",
        )
        with self.session_factory() as session:
            statement = select(WebTask).where(
                WebTask.core_task_id == normalized_core_task_id,
                task_visibility_predicate(actor=actor),
            )
            projection = session.execute(statement).scalar_one_or_none()
            if projection is None:
                return None
            return _to_projection_record(projection=projection)

    def list_potentially_active_tasks(self) -> tuple[WebTaskProjectionRecord, ...]:
        with self.session_factory() as session:
            statement = (
                select(WebTask)
                .where(WebTask.status.notin_(tuple(_TERMINAL_TASK_STATUSES)))
                .order_by(WebTask.updated_at.desc())
            )
            rows = session.execute(statement).scalars().all()
            return tuple(_to_projection_record(projection=row) for row in rows)

    def list_recent_tasks(
        self,
        *,
        actor: WebTaskActor,
        project_ids: tuple[str, ...] = (),
        project_none: bool = False,
        owner_user_id: str | None = None,
        states: tuple[str, ...] = (),
    ) -> tuple[WebTaskProjectionRecord, ...]:
        normalized_project_ids = tuple(
            dict.fromkeys(
                _require_non_empty_text(value=value, field_name="project_id")
                for value in project_ids
            )
        )
        normalized_owner_user_id = _normalize_optional_text(value=owner_user_id)
        normalized_states = tuple(
            dict.fromkeys(
                _require_non_empty_text(value=value, field_name="state") for value in states
            )
        )
        with self.session_factory() as session:
            statement = select(WebTask).where(task_visibility_predicate(actor=actor))
            if normalized_project_ids:
                statement = statement.where(WebTask.project_id.in_(normalized_project_ids))
            if project_none:
                statement = statement.where(WebTask.project_id.is_(None))
            if normalized_owner_user_id is not None:
                statement = statement.where(WebTask.owner_user_id == normalized_owner_user_id)
            if normalized_states:
                statement = statement.where(WebTask.status.in_(normalized_states))
            statement = statement.order_by(
                WebTask.updated_at.desc(),
                WebTask.created_at.desc(),
                WebTask.core_task_id.desc(),
            )
            rows = session.execute(statement).scalars().all()
            return tuple(_to_projection_record(projection=row) for row in rows)


def _load_task_projection(*, session: Session, core_task_id: str) -> WebTask | None:
    statement = select(WebTask).where(WebTask.core_task_id == core_task_id)
    return session.execute(statement).scalar_one_or_none()


def _require_non_empty_text(*, value: str, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _normalize_optional_text(*, value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _to_projection_record(*, projection: WebTask) -> WebTaskProjectionRecord:
    return WebTaskProjectionRecord(
        core_task_id=projection.core_task_id,
        name=projection.name,
        status=projection.status,
        owner_user_id=projection.owner_user_id,
        guest_session_hash=projection.guest_session_hash,
        project_id=projection.project_id,
        created_at=projection.created_at,
        updated_at=projection.updated_at,
    )


__all__ = ["WebTaskProjectionRecord", "WebTaskProjectionStore", "is_active_task_status"]
