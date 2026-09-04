from __future__ import annotations

from pathlib import Path


class TaskStorageError(RuntimeError):
    """Base error for task workspace storage failures."""


class TaskDirectoryCreationError(TaskStorageError):
    """Raised when task directories cannot be created."""

    def __init__(self, *, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"Cannot create task directory '{path}': {detail}.")


class TaskDirectoryAlreadyExistsError(TaskStorageError):
    """Raised when generated task directory already exists."""

    def __init__(self, *, path: Path) -> None:
        self.path = path
        super().__init__(f"Task directory already exists: '{path}'.")


class TaskConfigSaveError(TaskStorageError):
    """Raised when normalized config cannot be saved."""

    def __init__(self, *, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"Cannot save normalized config '{path}': {detail}.")


class TaskWorkspaceDeleteError(TaskStorageError):
    """Raised when task workspace cannot be deleted safely."""

    def __init__(self, *, task_id: str, detail: str) -> None:
        self.task_id = task_id
        self.detail = detail
        super().__init__(f"Cannot delete task workspace for '{task_id}': {detail}.")
