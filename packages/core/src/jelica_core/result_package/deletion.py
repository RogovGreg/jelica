from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import AnalyticalTaskRegistryService

from .artifacts import (
    ResultPackageLibraryError,
    ResultPackageLibraryErrorCode,
    _resolve_result_packages_directory_from_core_config_service,
    delete_result_package,
    resolve_result_package_path,
)
from .references import TaskResultReferenceAnalysis, analyze_task_result_references


class ResultPackageDeletionValidationError(RuntimeError):
    """Raised when result deletion cannot safely enter its destructive phase."""


@dataclass(frozen=True, slots=True)
class ResultDeletionTarget:
    path: Path
    content_id: str | None


@dataclass(frozen=True, slots=True)
class ResultDeletionPlan:
    targets: tuple[ResultDeletionTarget, ...]
    analysis: TaskResultReferenceAnalysis


@dataclass(frozen=True, slots=True)
class ResultDeletionItem:
    path: Path
    result: str
    content_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ResultDeletionBatch:
    items: tuple[ResultDeletionItem, ...]
    dangling_task_links: int

    @property
    def deleted_count(self) -> int:
        return sum(item.result in {"deleted", "already_missing"} for item in self.items)

    @property
    def failed_count(self) -> int:
        return sum(item.result == "failed" for item in self.items)


def delete_result_packages(
    *,
    references: Sequence[str] = (),
    delete_all: bool = False,
    force: bool = False,
    yes: bool = False,
    core_config_service: CoreConfigService,
) -> ResultDeletionBatch:
    """Resolve and delete result packages with complete pre-validation."""

    _ = yes  # Confirmation is a CLI concern; retained for call-site clarity.
    plan = validate_result_package_deletion(
        references=references,
        delete_all=delete_all,
        force=force,
        core_config_service=core_config_service,
    )
    return _delete_result_deletion_plan(
        plan=plan,
        force=force,
        core_config_service=core_config_service,
    )


def validate_result_package_deletion(
    *,
    references: Sequence[str] = (),
    delete_all: bool = False,
    force: bool = False,
    core_config_service: CoreConfigService,
) -> ResultDeletionPlan:
    if delete_all and references:
        raise ResultPackageDeletionValidationError(
            "--all cannot be combined with result references"
        )
    if delete_all and not force:
        raise ResultPackageDeletionValidationError(
            "results delete --all requires --force"
        )
    if not delete_all and not references:
        raise ResultPackageDeletionValidationError(
            "Provide at least one result reference or use --all --force"
        )

    resolved_config = core_config_service.require_initialized_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved_config.database_path)
    store_dir = _resolve_result_packages_directory_from_core_config_service(
        core_config_service=core_config_service
    )
    analysis = analyze_task_result_references(
        registry_service=registry,
        tasks_dir=resolved_config.tasks_dir,
        result_packages_dir=store_dir,
    )
    targets = _resolve_targets(
        references=references,
        delete_all=delete_all,
        force=force,
        core_config_service=core_config_service,
        store_dir=store_dir,
    )

    for target in targets:
        refs = _references_for_target(target=target, analysis=analysis)
        if len(refs) > 1 and not force:
            names = ", ".join(
                reference.task_name or reference.task_id for reference in refs
            )
            raise ResultPackageDeletionValidationError(
                f"Result package '{target.path.name}' is referenced by "
                f"{len(refs)} tasks ({names}); use --force to delete it"
            )

    return ResultDeletionPlan(targets=targets, analysis=analysis)


def _delete_result_deletion_plan(
    *,
    plan: ResultDeletionPlan,
    force: bool,
    core_config_service: CoreConfigService,
) -> ResultDeletionBatch:
    targets = plan.targets
    analysis = plan.analysis

    items: list[ResultDeletionItem] = []
    dangling_task_links = 0
    for target in targets:
        refs = _references_for_target(target=target, analysis=analysis)
        dangling_task_links += len(refs)
        try:
            outcome = delete_result_package(
                package_path=target.path,
                core_config_service=core_config_service,
                force=force,
            )
        except ResultPackageLibraryError as error:
            if error.code is ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND:
                items.append(
                    ResultDeletionItem(
                        path=target.path,
                        result="already_missing",
                        content_id=target.content_id,
                        detail=str(error),
                    )
                )
                continue
            items.append(
                ResultDeletionItem(
                    path=target.path,
                    result="failed",
                    content_id=target.content_id,
                    detail=f"[{error.code.value}] {error}",
                )
            )
            continue
        items.append(
            ResultDeletionItem(
                path=outcome.path,
                result="already_missing" if outcome.already_missing else "deleted",
                content_id=target.content_id,
            )
        )
    return ResultDeletionBatch(
        items=tuple(items),
        dangling_task_links=dangling_task_links,
    )


def _resolve_targets(
    *,
    references: Sequence[str],
    delete_all: bool,
    force: bool,
    core_config_service: CoreConfigService,
    store_dir: Path,
) -> tuple[ResultDeletionTarget, ...]:
    if delete_all:
        if not store_dir.exists():
            return tuple()
        if store_dir.is_symlink() or not store_dir.is_dir():
            raise ResultPackageDeletionValidationError(
                "Result package store must be a regular directory"
            )
        entries = sorted(store_dir.iterdir(), key=lambda item: item.name)
        return tuple(
            ResultDeletionTarget(path=entry, content_id=None)
            for entry in entries
            if entry.name.endswith(".jelica")
            and (entry.is_file() or entry.is_symlink())
        )

    targets: list[ResultDeletionTarget] = []
    seen: dict[str, ResultDeletionTarget] = {}
    for reference in references:
        try:
            resolved = resolve_result_package_path(
                task_or_content_ref=reference,
                core_config_service=core_config_service,
            )
        except ResultPackageLibraryError as error:
            fallback = _resolve_exact_filename_forced_cleanup(
                reference=reference,
                store_dir=store_dir,
                force=force,
            )
            if fallback is None:
                raise ResultPackageDeletionValidationError(
                    f"Cannot resolve result reference '{reference}': "
                    f"[{error.code.value}] {error}"
                ) from error
            target = fallback
        else:
            target = ResultDeletionTarget(
                path=resolved.path,
                content_id=resolved.content_id,
            )
        key = target.content_id or str(target.path.resolve(strict=False))
        previous = seen.get(key)
        if previous is not None:
            if previous.path.resolve(strict=False) != target.path.resolve(strict=False):
                raise ResultPackageDeletionValidationError(
                    f"Content ID {target.content_id} resolves to multiple physical "
                    f"packages: {previous.path.name}, {target.path.name}"
                )
            continue
        seen[key] = target
        targets.append(target)
    return tuple(targets)


def _resolve_exact_filename_forced_cleanup(
    *,
    reference: str,
    store_dir: Path,
    force: bool,
) -> ResultDeletionTarget | None:
    if not force or not reference.endswith(".jelica") or Path(reference).name != reference:
        return None
    candidate = store_dir.resolve(strict=False) / reference
    if candidate.parent != store_dir.resolve(strict=False):
        return None
    if not candidate.exists() and not candidate.is_symlink():
        return None
    if not candidate.is_file() and not candidate.is_symlink():
        return None
    return ResultDeletionTarget(path=candidate, content_id=None)


def _references_for_target(
    *,
    target: ResultDeletionTarget,
    analysis: TaskResultReferenceAnalysis,
):
    if target.content_id is not None:
        return analysis.for_content_id(target.content_id)
    return analysis.for_package_path(target.path)


__all__ = [
    "ResultDeletionBatch",
    "ResultDeletionItem",
    "ResultDeletionPlan",
    "ResultDeletionTarget",
    "ResultPackageDeletionValidationError",
    "delete_result_packages",
    "validate_result_package_deletion",
]
