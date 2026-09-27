from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from .registry_errors import AnalyticalTaskNotFoundError
from .registry_models import (
    TERMINAL_ANALYTICAL_TASK_STATES,
    AnalyticalTaskSnapshot,
    AnalyticalTaskState,
)
from .registry_service import AnalyticalTaskRegistryService


class TaskDeletionValidationError(ValueError):
    """Raised before destructive task deletion starts."""


@dataclass(frozen=True, slots=True)
class TaskDeletionSelection:
    task_snapshots: tuple[AnalyticalTaskSnapshot, ...]

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(snapshot.task.task_id for snapshot in self.task_snapshots)


def select_tasks_for_deletion(
    *,
    registry_service: AnalyticalTaskRegistryService,
    task_ids: Sequence[str] | None = None,
    states: Sequence[AnalyticalTaskState] | None = None,
    updated_before: datetime | None = None,
    delete_all: bool = False,
    force: bool = False,
) -> TaskDeletionSelection:
    """Resolve and safety-check the complete task deletion set."""

    identifiers = tuple(dict.fromkeys(item.strip() for item in (task_ids or ())))
    if any(item == "" for item in identifiers):
        raise TaskDeletionValidationError("Task references must not be empty")
    if identifiers and (states or updated_before is not None or delete_all):
        raise TaskDeletionValidationError(
            "Task references cannot be combined with --status, --older-than, or --all"
        )
    if delete_all and (states or updated_before is not None):
        raise TaskDeletionValidationError(
            "--all cannot be combined with --status or --older-than"
        )
    if delete_all and not force:
        raise TaskDeletionValidationError("tasks delete --all requires --force")
    if not identifiers and not delete_all and not states and updated_before is None:
        raise TaskDeletionValidationError(
            "Provide task references, --status, --older-than, or --all --force"
        )

    if identifiers:
        snapshots: list[AnalyticalTaskSnapshot] = []
        for task_id in identifiers:
            try:
                snapshots.append(registry_service.get_task_snapshot(task_id=task_id))
            except AnalyticalTaskNotFoundError as error:
                raise TaskDeletionValidationError(
                    f"Task '{task_id}' was not found"
                ) from error
    else:
        snapshots = list(
            registry_service.list_task_snapshots(
                states=states,
                updated_before=updated_before,
            )
        )

    unsafe = [
        snapshot
        for snapshot in snapshots
        if snapshot.task.state not in TERMINAL_ANALYTICAL_TASK_STATES
    ]
    if unsafe and not force:
        details = ", ".join(
            f"{snapshot.task.task_id} ({snapshot.task.state.value})"
            for snapshot in unsafe
        )
        raise TaskDeletionValidationError(
            "Non-terminal tasks require --force: " + details
        )
    return TaskDeletionSelection(task_snapshots=tuple(snapshots))


__all__ = [
    "TaskDeletionSelection",
    "TaskDeletionValidationError",
    "select_tasks_for_deletion",
]
