from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from jelica_api.models import ProjectMember
from jelica_api.notifications import NotificationService


@dataclass(frozen=True, slots=True)
class NotificationProducer:
    """Small domain-facing adapter around the Stage 20.1 notification service."""

    service: NotificationService | None

    def enqueue(
        self,
        *,
        session: Session,
        recipient_user_ids: Iterable[str],
        event_id: str,
        source_type: str,
        source_id: str,
        payload: dict[str, Any],
        actor_user_id: str | None = None,
        suppress_actor: bool = True,
    ) -> None:
        if self.service is None:
            return
        recipients = tuple(
            sorted(
                {
                    user_id
                    for user_id in recipient_user_ids
                    if user_id and (not suppress_actor or user_id != actor_user_id)
                }
            )
        )
        for recipient_user_id in recipients:
            self.service.enqueue(
                session=session,
                recipient_user_id=recipient_user_id,
                event_id=event_id,
                source_type=source_type,
                source_id=source_id,
                payload=_presentation_payload(event_id=event_id, payload=payload),
                actor_user_id=actor_user_id,
            )

    @staticmethod
    def project_member_user_ids(*, session: Session, project_id: str) -> tuple[str, ...]:
        return tuple(
            session.scalars(
                select(ProjectMember.user_id)
                .where(ProjectMember.project_id == project_id)
                .order_by(ProjectMember.user_id)
            )
        )


def edited_mention_source_id(
    *,
    comment_id: str,
    mentioned_user_id: str,
    edited_at_iso: str,
) -> str:
    return f"{comment_id}:mention:{mentioned_user_id}:{edited_at_iso}"


def reaction_source_id(
    *,
    comment_id: str,
    actor_user_id: str,
    previous_reaction: str | None,
    reaction: str,
    occurred_at_iso: str,
) -> str:
    previous = previous_reaction or "none"
    return f"{comment_id}:reaction:{actor_user_id}:{previous}:{reaction}:{occurred_at_iso}"


def _presentation_payload(*, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    project_id = payload.get("project_id")
    task_id = payload.get("task_id")
    if event_id.startswith("project_discussion."):
        resource_kind = "project_discussion"
        target_path = f"/app/projects/{project_id}/discussion" if project_id else None
    elif event_id.startswith("task_discussion."):
        resource_kind = "task_discussion"
        target_path = f"/app/tasks/{task_id}/discussion" if task_id else None
    elif event_id.startswith("project.task."):
        resource_kind = "project_tasks"
        target_path = f"/app/projects/{project_id}/tasks" if project_id else None
    elif event_id.startswith("project.invitation."):
        resource_kind = "invitation"
        target_path = "/app/projects"
    elif event_id.startswith("project."):
        resource_kind = "project"
        target_path = f"/app/projects/{project_id}" if project_id else "/app/projects"
    else:
        resource_kind = "task"
        target_path = f"/app/tasks/{task_id}" if task_id else "/app/tasks"
    result.update(
        {
            "resource_kind": resource_kind,
            "display_name": payload.get("task_name") or payload.get("project_name"),
            "target_path": target_path,
        }
    )
    return result


__all__ = [
    "NotificationProducer",
    "edited_mention_source_id",
    "reaction_source_id",
]
