from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, false, or_, select
from sqlalchemy.sql.elements import ColumnElement

from jelica_api.models import ProjectMember, WebTask


@dataclass(frozen=True, slots=True)
class WebTaskActor:
    user_id: str | None = None
    guest_session_hash: str | None = None

    def __post_init__(self) -> None:
        if self.user_id is not None and self.guest_session_hash is not None:
            raise ValueError("A task actor cannot be authenticated and guest at once.")


def task_visibility_predicate(*, actor: WebTaskActor) -> ColumnElement[bool]:
    if actor.user_id is not None:
        participating_project = (
            select(ProjectMember.project_id)
            .where(
                ProjectMember.project_id == WebTask.project_id,
                ProjectMember.user_id == actor.user_id,
            )
            .exists()
        )
        return or_(
            WebTask.owner_user_id == actor.user_id,
            and_(WebTask.project_id.is_not(None), participating_project),
        )
    if actor.guest_session_hash is not None:
        return and_(
            WebTask.owner_user_id.is_(None),
            WebTask.guest_session_hash == actor.guest_session_hash,
        )
    return false()


__all__ = ["WebTaskActor", "task_visibility_predicate"]
