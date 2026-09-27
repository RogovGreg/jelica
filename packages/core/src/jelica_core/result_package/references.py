from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jelica_core.tasks import AnalyticalTaskRecord, AnalyticalTaskRegistryService
from jelica_core.tasks.storage import resolve_task_workspace_dir

from .artifacts import (
    RESULT_PACKAGE_LINK_FILENAME,
    ResultPackageValidationError,
    load_result_package_link,
    validate_result_package_file,
)


@dataclass(frozen=True, slots=True)
class TaskResultReference:
    task_id: str
    task_name: str | None
    content_id: str | None
    package_path: Path | None
    issue: str | None = None


@dataclass(frozen=True, slots=True)
class TaskResultReferenceAnalysis:
    references: tuple[TaskResultReference, ...]

    def for_content_id(self, content_id: str) -> tuple[TaskResultReference, ...]:
        return tuple(
            reference
            for reference in self.references
            if reference.content_id == content_id
        )

    def for_package_path(self, package_path: Path) -> tuple[TaskResultReference, ...]:
        normalized = package_path.resolve(strict=False)
        return tuple(
            reference
            for reference in self.references
            if reference.package_path is not None
            and reference.package_path.resolve(strict=False) == normalized
        )


def analyze_task_result_references(
    *,
    registry_service: AnalyticalTaskRegistryService,
    tasks_dir: Path,
    result_packages_dir: Path,
    validate_packages: bool = True,
) -> TaskResultReferenceAnalysis:
    """Read local task links without trusting embedded package provenance.

    A link's ``content_id`` is retained even when its target is missing or
    corrupt: it is still a local dependency for deletion safety.  Malformed
    or unsafe links are recorded as diagnostics and never followed outside
    the task/result roots.
    """

    references: list[TaskResultReference] = []
    store_root = result_packages_dir.resolve(strict=False)
    for task in registry_service.list_tasks():
        references.append(
            _read_task_result_reference(
                task=task,
                tasks_dir=tasks_dir,
                store_root=store_root,
                validate_packages=validate_packages,
            )
        )
    return TaskResultReferenceAnalysis(references=tuple(references))


def _read_task_result_reference(
    *,
    task: AnalyticalTaskRecord,
    tasks_dir: Path,
    store_root: Path,
    validate_packages: bool,
) -> TaskResultReference:
    try:
        task_dir = resolve_task_workspace_dir(
            tasks_dir=tasks_dir,
            task_dir_relative_path=task.task_dir_relative_path,
            task_id=task.task_id,
        )
    except Exception as error:
        return TaskResultReference(
            task_id=task.task_id,
            task_name=task.name,
            content_id=None,
            package_path=None,
            issue=f"task workspace unavailable: {error}",
        )

    link_path = task_dir / RESULT_PACKAGE_LINK_FILENAME
    if link_path.is_symlink() or not link_path.is_file():
        return TaskResultReference(
            task_id=task.task_id,
            task_name=task.name,
            content_id=None,
            package_path=None,
            issue="task has no result package link",
        )
    try:
        link = load_result_package_link(path=link_path)
    except ResultPackageValidationError as error:
        return TaskResultReference(
            task_id=task.task_id,
            task_name=task.name,
            content_id=None,
            package_path=None,
            issue=str(error),
        )

    raw_target = task_dir / Path(link.path)
    if raw_target.is_symlink():
        return TaskResultReference(
            task_id=task.task_id,
            task_name=task.name,
            content_id=link.content_id,
            package_path=None,
            issue="task result package link targets a symbolic link",
        )
    target = raw_target.resolve(strict=False)
    try:
        target.relative_to(store_root)
    except ValueError:
        return TaskResultReference(
            task_id=task.task_id,
            task_name=task.name,
            content_id=link.content_id,
            package_path=None,
            issue="task result package link points outside result package store",
        )
    if target.suffix != ".jelica":
        return TaskResultReference(
            task_id=task.task_id,
            task_name=task.name,
            content_id=link.content_id,
            package_path=target,
            issue="task result package link does not target a .jelica file",
        )
    issue: str | None = None
    if not target.is_file():
        issue = "result package is missing"
    elif validate_packages:
        try:
            validate_result_package_file(path=target, expected_content_id=link.content_id)
        except ResultPackageValidationError as error:
            issue = str(error)
    return TaskResultReference(
        task_id=task.task_id,
        task_name=task.name,
        content_id=link.content_id,
        package_path=target,
        issue=issue,
    )


__all__ = [
    "TaskResultReference",
    "TaskResultReferenceAnalysis",
    "analyze_task_result_references",
]
