from __future__ import annotations

import os
import stat
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from jelica_core.reporting import (
    REPORT_DIRECTORY_NAME,
    REPORT_RECOGNIZED_EXTENSIONS,
    list_stored_reports,
)
from jelica_core.result_package import (
    RESULT_PACKAGE_DIRECTORY_NAME,
    list_result_packages,
)
from jelica_core.system_config import CoreConfigService, ResolvedCoreConfig
from jelica_core.tasks import AnalyticalTaskRegistryService
from jelica_core.tasks.storage import TASK_TRASH_DIRNAME, resolve_task_workspace_dir


class StorageCategory(StrEnum):
    TASKS = "tasks"
    RESULTS = "results"
    REPORTS = "reports"
    DATABASE = "database"
    OTHER = "other"
    TOTAL = "total"


_STORAGE_CATEGORY_ORDER: tuple[StorageCategory, ...] = (
    StorageCategory.TASKS,
    StorageCategory.RESULTS,
    StorageCategory.REPORTS,
    StorageCategory.DATABASE,
    StorageCategory.OTHER,
    StorageCategory.TOTAL,
)


@dataclass(frozen=True, slots=True)
class StorageDiagnostic:
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class TaskStorageMetrics:
    bytes: int
    workspaces: int
    registry_tasks: int
    missing_workspaces: int
    orphan_workspaces: int
    trash_bytes: int = 0


@dataclass(frozen=True, slots=True)
class ResultStorageMetrics:
    bytes: int
    packages: int
    referenced_packages: int
    standalone_packages: int
    invalid_packages: int


@dataclass(frozen=True, slots=True)
class ReportStorageMetrics:
    bytes: int
    reports: int
    pdf: int
    html: int
    xml: int
    txt: int
    other_files: int


@dataclass(frozen=True, slots=True)
class DatabaseStorageMetrics:
    bytes: int
    main_bytes: int
    wal_bytes: int
    shm_bytes: int
    journal_bytes: int


@dataclass(frozen=True, slots=True)
class OtherStorageMetrics:
    bytes: int
    breakdown: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class TotalStorageMetrics:
    bytes: int
    managed_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StorageUsageReport:
    tasks: TaskStorageMetrics
    results: ResultStorageMetrics
    reports: ReportStorageMetrics
    database: DatabaseStorageMetrics
    other: OtherStorageMetrics
    total: TotalStorageMetrics
    complete: bool
    diagnostics: tuple[StorageDiagnostic, ...]

    def to_dict(
        self,
        *,
        selected_categories: Iterable[StorageCategory] | None = None,
    ) -> dict[str, object]:
        selected = (
            set(selected_categories)
            if selected_categories is not None
            else set(StorageCategory)
        )
        payload: dict[str, object] = {
            "complete": self.complete,
            "diagnostics": [
                {"message": item.message, "path": item.path}
                for item in self.diagnostics
            ],
            "selected_categories": [
                category.value
                for category in _STORAGE_CATEGORY_ORDER
                if category in selected
            ],
        }
        if StorageCategory.TASKS in selected:
            payload["tasks"] = {
                "bytes": self.tasks.bytes,
                "workspaces": self.tasks.workspaces,
                "registry_tasks": self.tasks.registry_tasks,
                "missing_workspaces": self.tasks.missing_workspaces,
                "orphan_workspaces": self.tasks.orphan_workspaces,
                "trash_bytes": self.tasks.trash_bytes,
            }
        if StorageCategory.RESULTS in selected:
            payload["results"] = {
                "bytes": self.results.bytes,
                "packages": self.results.packages,
                "referenced_packages": self.results.referenced_packages,
                "standalone_packages": self.results.standalone_packages,
                "invalid_packages": self.results.invalid_packages,
            }
        if StorageCategory.REPORTS in selected:
            payload["reports"] = {
                "bytes": self.reports.bytes,
                "reports": self.reports.reports,
                "pdf": self.reports.pdf,
                "html": self.reports.html,
                "xml": self.reports.xml,
                "txt": self.reports.txt,
                "other_files": self.reports.other_files,
            }
        if StorageCategory.DATABASE in selected:
            payload["database"] = {
                "bytes": self.database.bytes,
                "main_bytes": self.database.main_bytes,
                "wal_bytes": self.database.wal_bytes,
                "shm_bytes": self.database.shm_bytes,
                "journal_bytes": self.database.journal_bytes,
            }
        if StorageCategory.OTHER in selected:
            payload["other"] = {
                "bytes": self.other.bytes,
                "breakdown": dict(self.other.breakdown),
            }
        if StorageCategory.TOTAL in selected:
            payload["total"] = {
                "bytes": self.total.bytes,
                "managed_roots": list(self.total.managed_roots),
            }
        return payload


@dataclass(frozen=True, slots=True)
class _ScannedEntry:
    path: Path
    category: StorageCategory
    bytes: int


def analyze_storage_usage(
    *,
    core_config_service: CoreConfigService,
    resolved_config: ResolvedCoreConfig | None = None,
) -> StorageUsageReport:
    """Return a best-effort, read-only storage report.

    The scanner uses ``lstat``/``scandir(..., follow_symlinks=False)`` and a
    report-wide inode set.  It therefore never follows external symlink
    targets and does not count hardlinked physical files more than once.
    Directory metadata itself is intentionally not counted; regular files,
    symlinks, and special entries are accounted using their lstat size.
    """

    config = resolved_config or core_config_service.require_initialized_config()
    home = core_config_service.get_jelica_home().resolve(strict=False)
    result_root = home / RESULT_PACKAGE_DIRECTORY_NAME
    reports_root = home / REPORT_DIRECTORY_NAME
    database_paths = _database_paths(config.database_path)
    roots = _unique_paths((home, config.data_dir))
    diagnostics: list[StorageDiagnostic] = []
    scanned_entries: list[_ScannedEntry] = []
    seen_inodes: set[tuple[int, int]] = set()
    visited_directories: set[tuple[int, int]] = set()

    for root in roots:
        _scan_path(
            path=root,
            home=home,
            tasks_root=config.tasks_dir,
            result_root=result_root,
            reports_root=reports_root,
            database_paths=database_paths,
            seen_inodes=seen_inodes,
            visited_directories=visited_directories,
            entries=scanned_entries,
            diagnostics=diagnostics,
        )

    category_bytes: dict[StorageCategory, int] = defaultdict(int)
    for entry in scanned_entries:
        category_bytes[entry.category] += entry.bytes

    registry = AnalyticalTaskRegistryService(database_path=config.database_path)
    task_metrics = _task_metrics(
        tasks_root=config.tasks_dir,
        registry=registry,
        task_bytes=category_bytes[StorageCategory.TASKS],
        diagnostics=diagnostics,
    )

    result_metrics = _result_metrics(
        core_config_service=core_config_service,
        registry=registry,
        results_bytes=category_bytes[StorageCategory.RESULTS],
        diagnostics=diagnostics,
    )
    report_metrics = _report_metrics(
        core_config_service=core_config_service,
        reports_root=reports_root,
        reports_bytes=category_bytes[StorageCategory.REPORTS],
        diagnostics=diagnostics,
    )

    database_metrics = _database_metrics(
        database_paths=database_paths,
        entries=scanned_entries,
        database_bytes=category_bytes[StorageCategory.DATABASE],
    )
    other_metrics = _other_metrics(
        config=config,
        home=home,
        entries=scanned_entries,
        other_bytes=category_bytes[StorageCategory.OTHER],
    )
    total_metrics = TotalStorageMetrics(
        bytes=sum(category_bytes.values()),
        managed_roots=tuple(str(root) for root in roots),
    )
    return StorageUsageReport(
        tasks=task_metrics,
        results=result_metrics,
        reports=report_metrics,
        database=database_metrics,
        other=other_metrics,
        total=total_metrics,
        complete=not diagnostics,
        diagnostics=tuple(diagnostics),
    )


def _scan_path(
    *,
    path: Path,
    home: Path,
    tasks_root: Path,
    result_root: Path,
    reports_root: Path,
    database_paths: dict[str, Path],
    seen_inodes: set[tuple[int, int]],
    visited_directories: set[tuple[int, int]],
    entries: list[_ScannedEntry],
    diagnostics: list[StorageDiagnostic],
) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        diagnostics.append(StorageDiagnostic("storage entry disappeared during scan", str(path)))
        return
    except PermissionError:
        diagnostics.append(StorageDiagnostic("storage entry cannot be read", str(path)))
        return
    except OSError as error:
        diagnostics.append(StorageDiagnostic(f"storage entry cannot be read: {error}", str(path)))
        return

    mode = info.st_mode
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        directory_identity = (info.st_dev, info.st_ino)
        if directory_identity in visited_directories:
            return
        visited_directories.add(directory_identity)
        try:
            with os.scandir(path) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except FileNotFoundError:
            diagnostics.append(StorageDiagnostic("directory disappeared during scan", str(path)))
            return
        except PermissionError:
            diagnostics.append(StorageDiagnostic("directory cannot be read", str(path)))
            return
        except OSError as error:
            diagnostics.append(StorageDiagnostic(f"directory cannot be read: {error}", str(path)))
            return
        for child in children:
            _scan_path(
                path=Path(child.path),
                home=home,
                tasks_root=tasks_root,
                result_root=result_root,
                reports_root=reports_root,
                database_paths=database_paths,
                seen_inodes=seen_inodes,
                visited_directories=visited_directories,
                entries=entries,
                diagnostics=diagnostics,
            )
        return

    identity = (info.st_dev, info.st_ino)
    if identity in seen_inodes:
        return
    seen_inodes.add(identity)
    entries.append(
        _ScannedEntry(
            path=path,
            category=_classify_path(
                path=path,
                tasks_root=tasks_root,
                result_root=result_root,
                reports_root=reports_root,
                database_paths=database_paths,
            ),
            bytes=max(0, int(info.st_size)),
        )
    )


def _classify_path(
    *,
    path: Path,
    tasks_root: Path,
    result_root: Path,
    reports_root: Path,
    database_paths: dict[str, Path],
) -> StorageCategory:
    lexical_path = _absolute_lexical(path)
    if _is_relative_to(lexical_path, _absolute_lexical(tasks_root)):
        return StorageCategory.TASKS
    if _is_relative_to(lexical_path, _absolute_lexical(result_root)):
        return StorageCategory.RESULTS
    if _is_relative_to(lexical_path, _absolute_lexical(reports_root)):
        return StorageCategory.REPORTS
    if any(lexical_path == _absolute_lexical(item) for item in database_paths.values()):
        return StorageCategory.DATABASE
    return StorageCategory.OTHER


def _task_metrics(
    *,
    tasks_root: Path,
    registry: AnalyticalTaskRegistryService,
    task_bytes: int,
    diagnostics: list[StorageDiagnostic],
) -> TaskStorageMetrics:
    workspaces: list[Path] = []
    try:
        if tasks_root.exists() and not tasks_root.is_symlink() and tasks_root.is_dir():
            workspaces = [
                entry
                for entry in sorted(tasks_root.iterdir(), key=lambda item: item.name)
                if entry.name != TASK_TRASH_DIRNAME
                and entry.is_dir()
                and not entry.is_symlink()
            ]
        elif tasks_root.is_symlink() or (tasks_root.exists() and not tasks_root.is_dir()):
            diagnostics.append(
                StorageDiagnostic("tasks root is not a regular directory", str(tasks_root))
            )
    except (FileNotFoundError, PermissionError, OSError) as error:
        diagnostics.append(
            StorageDiagnostic(f"tasks root cannot be inspected: {error}", str(tasks_root))
        )

    try:
        tasks = registry.list_tasks()
    except Exception as error:
        diagnostics.append(StorageDiagnostic(f"task registry cannot be inspected: {error}"))
        tasks = []

    expected_workspace_paths: set[Path] = set()
    missing = 0
    for task in tasks:
        try:
            expected = resolve_task_workspace_dir(
                tasks_dir=tasks_root,
                task_dir_relative_path=task.task_dir_relative_path,
                task_id=task.task_id,
            )
        except Exception as error:
            missing += 1
            diagnostics.append(
                StorageDiagnostic(f"task workspace path is unsafe: {error}", task.task_id)
            )
            continue
        expected_workspace_paths.add(_absolute_lexical(expected))
        if expected.is_symlink() or not expected.is_dir():
            missing += 1

    actual_workspace_paths = {_absolute_lexical(path) for path in workspaces}
    orphan = len(actual_workspace_paths - expected_workspace_paths)
    trash_bytes = _safe_path_size(tasks_root / TASK_TRASH_DIRNAME)
    return TaskStorageMetrics(
        bytes=task_bytes,
        workspaces=len(workspaces),
        registry_tasks=len(tasks),
        missing_workspaces=missing,
        orphan_workspaces=orphan,
        trash_bytes=trash_bytes,
    )


def _result_metrics(
    *,
    core_config_service: CoreConfigService,
    registry: AnalyticalTaskRegistryService,
    results_bytes: int,
    diagnostics: list[StorageDiagnostic],
) -> ResultStorageMetrics:
    try:
        catalog = list_result_packages(core_config_service=core_config_service)
    except Exception as error:
        diagnostics.append(StorageDiagnostic(f"result catalog cannot be inspected: {error}"))
        return ResultStorageMetrics(
            bytes=results_bytes,
            packages=0,
            referenced_packages=0,
            standalone_packages=0,
            invalid_packages=0,
        )

    try:
        resolved_config = core_config_service.require_initialized_config()
        from jelica_core.result_package import analyze_task_result_references

        analysis = analyze_task_result_references(
            registry_service=registry,
            tasks_dir=resolved_config.tasks_dir,
            result_packages_dir=core_config_service.get_jelica_home()
            / RESULT_PACKAGE_DIRECTORY_NAME,
            validate_packages=False,
        )
    except Exception as error:
        diagnostics.append(
            StorageDiagnostic(f"task result references cannot be inspected: {error}")
        )
        analysis = None

    valid_entries = [entry for entry in catalog.packages if entry.valid and entry.content_id]
    stored_keys = {entry.content_id for entry in valid_entries}
    valid_paths = {
        _absolute_lexical(entry.path): entry.content_id
        for entry in valid_entries
        if entry.content_id is not None
    }
    referenced_ids: set[str] = set()
    if analysis is not None:
        for reference in analysis.references:
            if reference.issue is not None and reference.issue != "task has no result package link":
                diagnostics.append(
                    StorageDiagnostic(
                        f"task result reference is unavailable: {reference.issue}",
                        reference.task_id,
                    )
                )
                continue
            if reference.issue is None and (
                reference.package_path is None
                or valid_paths.get(_absolute_lexical(reference.package_path))
                != reference.content_id
            ):
                diagnostics.append(
                    StorageDiagnostic(
                        "task result reference does not resolve to a valid stored package",
                        reference.task_id,
                    )
                )
        referenced_ids = {
            reference.content_id
            for reference in analysis.references
            if reference.issue is None
            and reference.content_id in stored_keys
            and reference.package_path is not None
            and valid_paths.get(_absolute_lexical(reference.package_path))
            == reference.content_id
        }
    return ResultStorageMetrics(
        bytes=results_bytes,
        packages=len(catalog.packages),
        referenced_packages=len(referenced_ids),
        standalone_packages=len(stored_keys - referenced_ids),
        invalid_packages=sum(not entry.valid for entry in catalog.packages),
    )


def _report_metrics(
    *,
    core_config_service: CoreConfigService,
    reports_root: Path,
    reports_bytes: int,
    diagnostics: list[StorageDiagnostic],
) -> ReportStorageMetrics:
    try:
        reports = list_stored_reports(core_config_service=core_config_service)
    except Exception as error:
        diagnostics.append(StorageDiagnostic(f"report catalog cannot be inspected: {error}"))
        reports = tuple()

    counts = {suffix.removeprefix("."): 0 for suffix in REPORT_RECOGNIZED_EXTENSIONS}
    for report in reports:
        counts[report.format.value] += 1

    other_files = 0
    if reports_root.exists() and reports_root.is_dir() and not reports_root.is_symlink():
        try:
            other_files = sum(
                1
                for entry in reports_root.iterdir()
                if not entry.is_symlink()
                and entry.is_file()
                and entry.suffix.casefold() not in REPORT_RECOGNIZED_EXTENSIONS
            )
        except OSError as error:
            diagnostics.append(
                StorageDiagnostic(f"report directory cannot be inspected: {error}")
            )
    return ReportStorageMetrics(
        bytes=reports_bytes,
        reports=len(reports),
        pdf=counts["pdf"],
        html=counts["html"],
        xml=counts["xml"],
        txt=counts["txt"],
        other_files=other_files,
    )


def _database_metrics(
    *,
    database_paths: dict[str, Path],
    entries: list[_ScannedEntry],
    database_bytes: int,
) -> DatabaseStorageMetrics:
    sizes: dict[str, int] = {}
    for entry in entries:
        for label, path in database_paths.items():
            if _absolute_lexical(entry.path) == _absolute_lexical(path):
                sizes[label] = entry.bytes
    return DatabaseStorageMetrics(
        bytes=database_bytes,
        main_bytes=sizes.get("main", 0),
        wal_bytes=sizes.get("wal", 0),
        shm_bytes=sizes.get("shm", 0),
        journal_bytes=sizes.get("journal", 0),
    )


def _other_metrics(
    *,
    config: ResolvedCoreConfig,
    home: Path,
    entries: list[_ScannedEntry],
    other_bytes: int,
) -> OtherStorageMetrics:
    known_roots = (
        ("Config", home / "config.toml"),
        ("Logs", config.logs_dir),
        ("Temp", config.temp_dir),
        ("Service state", config.data_dir / "service-state.json"),
        ("Service control", config.data_dir / "service-control.json"),
    )
    breakdown: dict[str, int] = defaultdict(int)
    for entry in entries:
        if entry.category is not StorageCategory.OTHER:
            continue
        matched = False
        for label, root in known_roots:
            if _is_relative_to(_absolute_lexical(entry.path), _absolute_lexical(root)):
                breakdown[label] += entry.bytes
                matched = True
                break
        if not matched:
            breakdown["Other files"] += entry.bytes
    return OtherStorageMetrics(
        bytes=other_bytes,
        breakdown=tuple(sorted(breakdown.items())),
    )


def _database_paths(database_path: Path) -> dict[str, Path]:
    return {
        "main": database_path,
        "wal": Path(f"{database_path}-wal"),
        "shm": Path(f"{database_path}-shm"),
        "journal": Path(f"{database_path}-journal"),
    }


def _safe_path_size(path: Path) -> int:
    if not path.exists() or path.is_symlink() or not path.is_dir():
        return 0
    total = 0
    try:
        for item in path.iterdir():
            try:
                info = item.lstat()
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                total += _safe_path_size(item)
            else:
                total += max(0, int(info.st_size))
    except OSError:
        return total
    return total


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        normalized = path.resolve(strict=False)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "DatabaseStorageMetrics",
    "OtherStorageMetrics",
    "ReportStorageMetrics",
    "ResultStorageMetrics",
    "StorageCategory",
    "StorageDiagnostic",
    "StorageUsageReport",
    "TaskStorageMetrics",
    "TotalStorageMetrics",
    "analyze_storage_usage",
]
