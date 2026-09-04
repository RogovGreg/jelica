from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jelica_api.actor_identity import WebActorIdentity
from jelica_api.cli import JelicaCliClient
from jelica_api.contracts import TaskStatusSnapshot
from jelica_api.task_access import WebTaskActor
from jelica_api.web_tasks import WebTaskProjectionStore


class TaskLifecycleNotFoundError(LookupError):
    """Task is absent or not visible to the actor."""


class TaskLifecycleForbiddenError(PermissionError):
    """Actor can see a task but does not own its lifecycle."""


@dataclass(frozen=True, slots=True)
class TaskLifecycleService:
    cli_client: JelicaCliClient
    projection_store: WebTaskProjectionStore

    def execute(
        self,
        *,
        action: Literal["start", "pause", "resume"],
        task_id: str,
        actor: WebActorIdentity,
    ) -> TaskStatusSnapshot:
        normalized_id = task_id.strip()
        if not normalized_id:
            raise TaskLifecycleNotFoundError()
        visible = self.projection_store.get_visible_task(
            core_task_id=normalized_id,
            actor=WebTaskActor(
                user_id=actor.user_id,
                guest_session_hash=actor.guest_session_hash,
            ),
        )
        if visible is None:
            raise TaskLifecycleNotFoundError()
        if actor.user_id is not None and visible.owner_user_id != actor.user_id:
            raise TaskLifecycleForbiddenError()
        if (
            actor.guest_session_hash is not None
            and visible.guest_session_hash != actor.guest_session_hash
        ):
            raise TaskLifecycleForbiddenError()

        snapshot = getattr(self.cli_client, f"{action}_task")(task_id=normalized_id)
        try:
            self.projection_store.upsert_task(
                core_task_id=snapshot.task_id,
                name=None,
                status=snapshot.state,
            )
        except Exception:
            # Core already accepted the action; reconciliation will repair the cache later.
            return snapshot
        return snapshot.model_copy(update={"can_control_lifecycle": True})


__all__ = [
    "TaskLifecycleForbiddenError",
    "TaskLifecycleNotFoundError",
    "TaskLifecycleService",
]
