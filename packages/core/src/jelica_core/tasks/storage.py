from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from jelica_core.config import ResolvedAnalysisConfig

from .errors import (
    TaskConfigSaveError,
    TaskDirectoryAlreadyExistsError,
    TaskDirectoryCreationError,
    TaskWorkspaceDeleteError,
)
from .models import TaskWorkspacePaths

TASK_CONFIG_FILENAME = "config.json"
TASK_CONFIGS_DIRNAME = "configs"
TASK_JOBS_DIRNAME = "jobs"
TASK_TRASH_DIRNAME = ".trash"


@dataclass(frozen=True, slots=True)
class TaskWorkspaceTrashMove:
    task_dir: Path
    trashed_task_dir: Path | None


class LocalTaskStorage:
    """Create task workspaces in resolved system tasks directory."""

    def __init__(self, *, tasks_dir: Path) -> None:
        self._tasks_dir = tasks_dir

    @property
    def tasks_dir(self) -> Path:
        return self._tasks_dir

    def create_task_workspace(
        self,
        *,
        task_id: str,
        config: ResolvedAnalysisConfig,
    ) -> TaskWorkspacePaths:
        tasks_dir = self._ensure_tasks_dir()
        task_dir = tasks_dir / task_id
        self._create_task_dir(task_dir)

        config_path = task_dir / TASK_CONFIG_FILENAME
        configs_dir = task_dir / TASK_CONFIGS_DIRNAME
        jobs_dir = task_dir / TASK_JOBS_DIRNAME
        try:
            configs_dir.mkdir(parents=False, exist_ok=False)
            jobs_dir.mkdir(parents=False, exist_ok=False)
            serialized_config = config.model_dump(mode="json")
            (
                current_config_revision,
                current_config_relative_path,
                current_config_hash,
                _,
            ) = write_task_config_revision(
                task_dir=task_dir,
                config_document=serialized_config,
                revision=1,
            )
        except TaskConfigSaveError as error:
            self._cleanup_partial_task_dir(task_dir=task_dir, config_path=config_path, cause=error)
            raise
        except OSError as error:
            self._cleanup_partial_task_dir(
                task_dir=task_dir,
                config_path=config_path,
                cause=TaskConfigSaveError(path=config_path, detail=str(error)),
            )
            raise TaskDirectoryCreationError(path=task_dir, detail=str(error)) from error

        return TaskWorkspacePaths(
            task_dir=task_dir,
            config_path=config_path,
            configs_dir=configs_dir,
            jobs_dir=jobs_dir,
            current_config_revision=current_config_revision,
            current_config_relative_path=current_config_relative_path,
            current_config_hash=current_config_hash,
        )

    def _ensure_tasks_dir(self) -> Path:
        tasks_dir = self.tasks_dir
        try:
            tasks_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise TaskDirectoryCreationError(path=tasks_dir, detail=str(error)) from error
        return tasks_dir

    def _create_task_dir(self, task_dir: Path) -> None:
        try:
            task_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError as error:
            raise TaskDirectoryAlreadyExistsError(path=task_dir) from error
        except OSError as error:
            raise TaskDirectoryCreationError(path=task_dir, detail=str(error)) from error

    def _save_config_atomically(
        self,
        *,
        config_path: Path,
        config: ResolvedAnalysisConfig,
    ) -> None:
        try:
            write_text_atomically(
                path=config_path,
                payload=serialize_config_document(config.model_dump(mode="json")),
            )
        except OSError as error:
            raise TaskConfigSaveError(path=config_path, detail=str(error)) from error

    def _cleanup_partial_task_dir(
        self,
        *,
        task_dir: Path,
        config_path: Path,
        cause: TaskConfigSaveError,
    ) -> None:
        try:
            shutil.rmtree(task_dir)
        except OSError as cleanup_error:
            raise TaskConfigSaveError(
                path=config_path,
                detail=f"{cause.detail}; cleanup failed for '{task_dir}': {cleanup_error}",
            ) from cleanup_error


def resolve_task_workspace_dir(
    *,
    tasks_dir: Path,
    task_dir_relative_path: str,
    task_id: str,
) -> Path:
    normalized_task_id = task_id.strip()
    if normalized_task_id == "":
        raise TaskWorkspaceDeleteError(task_id=task_id, detail="task_id must not be empty")

    normalized_relative = task_dir_relative_path.strip()
    if normalized_relative == "":
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail="task_dir_relative_path must not be empty",
        )

    posix_relative = PurePosixPath(normalized_relative)
    windows_relative = PureWindowsPath(normalized_relative)
    if posix_relative.is_absolute() or windows_relative.is_absolute():
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail="task_dir_relative_path must be relative",
        )
    if ".." in posix_relative.parts or ".." in windows_relative.parts:
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail="task_dir_relative_path must stay inside tasks root",
        )
    if normalized_relative in {".", ".."}:
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail="task_dir_relative_path must not be '.' or '..'",
        )

    task_dir = tasks_dir / Path(normalized_relative)
    resolved_tasks_dir = tasks_dir.resolve(strict=False)
    resolved_task_dir = task_dir.resolve(strict=False)
    if resolved_task_dir == resolved_tasks_dir:
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail="task workspace must not equal tasks root",
        )
    try:
        relative_path = resolved_task_dir.relative_to(resolved_tasks_dir)
    except ValueError as error:
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail=(
                f"task workspace '{resolved_task_dir}' is outside tasks root '{resolved_tasks_dir}'"
            ),
        ) from error

    if len(relative_path.parts) == 0 or relative_path.parts[-1] != normalized_task_id:
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail=(
                "task workspace path does not match task_id: "
                f"task_id='{normalized_task_id}', relative_path='{relative_path.as_posix()}'"
            ),
        )

    return task_dir


def move_task_workspace_to_trash(
    *,
    tasks_dir: Path,
    task_dir_relative_path: str,
    task_id: str,
) -> TaskWorkspaceTrashMove:
    normalized_task_id = task_id.strip()
    task_dir = resolve_task_workspace_dir(
        tasks_dir=tasks_dir,
        task_dir_relative_path=task_dir_relative_path,
        task_id=normalized_task_id,
    )
    if not task_dir.exists():
        return TaskWorkspaceTrashMove(task_dir=task_dir, trashed_task_dir=None)
    if task_dir.is_symlink():
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail=f"task workspace '{task_dir}' must not be a symlink",
        )
    if not task_dir.is_dir():
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail=f"task workspace '{task_dir}' must be a directory",
        )

    trash_root = tasks_dir / TASK_TRASH_DIRNAME
    try:
        trash_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail=f"cannot create trash directory '{trash_root}': {error}",
        ) from error

    trashed_task_dir = trash_root / f"{normalized_task_id}-{uuid4().hex}"
    try:
        task_dir.replace(trashed_task_dir)
    except OSError as error:
        raise TaskWorkspaceDeleteError(
            task_id=normalized_task_id,
            detail=f"cannot move workspace '{task_dir}' to trash: {error}",
        ) from error

    return TaskWorkspaceTrashMove(task_dir=task_dir, trashed_task_dir=trashed_task_dir)


def restore_task_workspace_from_trash(
    *,
    task_id: str,
    move_result: TaskWorkspaceTrashMove,
) -> None:
    trashed_task_dir = move_result.trashed_task_dir
    if trashed_task_dir is None:
        return
    if not trashed_task_dir.exists():
        return
    if move_result.task_dir.exists():
        raise TaskWorkspaceDeleteError(
            task_id=task_id,
            detail=(
                f"cannot restore workspace because target '{move_result.task_dir}' already exists"
            ),
        )
    try:
        trashed_task_dir.replace(move_result.task_dir)
    except OSError as error:
        raise TaskWorkspaceDeleteError(
            task_id=task_id,
            detail=(
                f"cannot restore workspace from '{trashed_task_dir}' "
                f"to '{move_result.task_dir}': {error}"
            ),
        ) from error


def purge_trashed_task_workspace(
    *,
    task_id: str,
    move_result: TaskWorkspaceTrashMove,
) -> None:
    trashed_task_dir = move_result.trashed_task_dir
    if trashed_task_dir is None or not trashed_task_dir.exists():
        return
    if trashed_task_dir.is_symlink() or not trashed_task_dir.is_dir():
        raise TaskWorkspaceDeleteError(
            task_id=task_id,
            detail=f"trash path '{trashed_task_dir}' must be a regular directory",
        )
    try:
        shutil.rmtree(trashed_task_dir)
    except OSError as error:
        raise TaskWorkspaceDeleteError(
            task_id=task_id,
            detail=f"cannot delete trashed workspace '{trashed_task_dir}': {error}",
        ) from error


def serialize_config_document(config_document: Mapping[str, object]) -> str:
    serialized = json.dumps(
        config_document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return f"{serialized}\n"


def canonicalize_config_document(config_document: Mapping[str, object]) -> str:
    return json.dumps(
        config_document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def compute_config_hash(config_document: Mapping[str, object]) -> str:
    canonical_payload = canonicalize_config_document(config_document)
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def config_revision_relative_path(revision: int) -> str:
    if revision <= 0:
        raise ValueError("revision must be >= 1")
    return f"{TASK_CONFIGS_DIRNAME}/{revision:06d}.json"


def write_task_config_revision(
    *,
    task_dir: Path,
    config_document: Mapping[str, object],
    revision: int,
) -> tuple[int, str, str, Path]:
    config_path = task_dir / TASK_CONFIG_FILENAME
    revision_relative_path = config_revision_relative_path(revision)
    revision_path = task_dir / Path(revision_relative_path)

    if revision_path.exists():
        raise TaskConfigSaveError(
            path=revision_path,
            detail=f"config revision file already exists for revision {revision}",
        )

    try:
        revision_path.parent.mkdir(parents=True, exist_ok=True)
        payload = serialize_config_document(config_document)
        write_text_atomically(path=revision_path, payload=payload)
        write_text_atomically(path=config_path, payload=payload)
    except OSError as error:
        raise TaskConfigSaveError(path=revision_path, detail=str(error)) from error

    return (
        revision,
        revision_relative_path,
        compute_config_hash(config_document),
        revision_path,
    )


def write_text_atomically(*, path: Path, payload: str) -> None:
    temp_file_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
        ) as temp_file:
            temp_file.write(payload)
            temp_file_path = Path(temp_file.name)

        os.replace(temp_file_path, path)
    except OSError:
        if temp_file_path is not None:
            temp_file_path.unlink(missing_ok=True)
        raise
