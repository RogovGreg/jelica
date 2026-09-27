from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import normalize_human_readable_name, validate_human_readable_name

REPORT_DIRECTORY_NAME = "reports"
REPORT_MAX_FILENAME_LENGTH = 255
REPORT_RECOGNIZED_EXTENSIONS: tuple[str, ...] = (".pdf", ".html", ".xml", ".txt")
REPORT_GENERATED_FORMAT = "pdf"
_CANONICAL_RESULT_REPORT_PATTERN = re.compile(
    r"^(?P<name>.+)__[0-9a-f]{64}(?:_(?P<index>[1-9][0-9]*))?$"
)
_REPORT_LOCK_FILENAME = ".reports.lock"
_REPORT_LOCK_WAIT_SECONDS = 5.0
_REPORT_LOCK_POLL_SECONDS = 0.05


class ReportFormat(StrEnum):
    PDF = "pdf"
    HTML = "html"
    XML = "xml"
    TXT = "txt"

    @property
    def extension(self) -> str:
        return f".{self.value}"


class ReportStoreErrorCode(StrEnum):
    REPORT_NOT_FOUND = "report_not_found"
    AMBIGUOUS_REPORT = "ambiguous_report"
    REPORT_OUTSIDE_MANAGED_STORE = "report_outside_managed_store"
    INVALID_REPORT = "invalid_report"
    REPORT_STORE_ERROR = "report_store_error"
    REPORT_STORE_BUSY = "report_store_busy"


class ReportStoreError(RuntimeError):
    def __init__(self, *, code: ReportStoreErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StoredReport:
    path: Path
    filename: str
    stem: str
    format: ReportFormat
    size_bytes: int
    modified_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "stem": self.stem,
            "format": self.format.value,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "path": str(self.path.resolve(strict=False)),
        }


def resolve_reports_directory(*, core_config_service: CoreConfigService) -> Path:
    return core_config_service.get_jelica_home() / REPORT_DIRECTORY_NAME


def list_stored_reports(*, core_config_service: CoreConfigService) -> tuple[StoredReport, ...]:
    root = resolve_reports_directory(core_config_service=core_config_service)
    if not root.exists():
        return tuple()
    if root.is_symlink() or not root.is_dir():
        raise ReportStoreError(
            code=ReportStoreErrorCode.REPORT_STORE_ERROR,
            message="Managed reports directory is not a regular directory.",
        )

    reports: list[StoredReport] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: (item.name.casefold(), item.name))
    except OSError as error:
        raise ReportStoreError(
            code=ReportStoreErrorCode.REPORT_STORE_ERROR,
            message="Managed reports directory cannot be read.",
        ) from error
    for path in entries:
        if path.is_symlink() or not path.is_file():
            continue
        report_format = report_format_from_filename(path.name)
        if report_format is None:
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        reports.append(
            StoredReport(
                path=path,
                filename=path.name,
                stem=path.name[: -len(path.suffix)],
                format=report_format,
                size_bytes=max(0, int(stat_result.st_size)),
                modified_at=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC).isoformat(),
            )
        )
    return tuple(reports)


def report_format_from_filename(filename: str) -> ReportFormat | None:
    suffix = Path(filename).suffix.casefold()
    try:
        return ReportFormat(suffix.removeprefix("."))
    except ValueError:
        return None


def validate_report_stem(value: str, *, field_name: str = "report name") -> str:
    if "\x00" in value or "/" in value or "\\" in value:
        raise ValueError(f"{field_name} must be a safe filename component")
    normalized = normalize_human_readable_name(value)
    if normalized in {".", ".."}:
        raise ValueError(f"{field_name} must not be '.' or '..'")
    try:
        normalized = validate_human_readable_name(normalized, max_length=None)
    except ValueError as error:
        raise ValueError(
            f"{field_name} must contain only ASCII letters, digits, '_' or '-'."
        ) from error
    return normalized


def validate_report_filename(*, filename: str, report_format: ReportFormat) -> str:
    if Path(filename).name != filename or "\x00" in filename:
        raise ValueError("Report filename must be one safe filename component")
    expected_suffix = report_format.extension
    if not filename.casefold().endswith(expected_suffix):
        raise ValueError(f"Report filename must end with {expected_suffix}")
    stem = filename[: -len(expected_suffix)]
    validate_report_stem(stem, field_name="Report filename stem")
    if len(filename) > REPORT_MAX_FILENAME_LENGTH:
        raise ValueError("Report filename is too long for a filesystem component")
    return filename


def report_filename_for_stem(*, stem: str, report_format: ReportFormat) -> str:
    normalized_stem = validate_report_stem(stem)
    filename = f"{normalized_stem}{report_format.extension}"
    return validate_report_filename(filename=filename, report_format=report_format)


def resolve_stored_report(
    *,
    reference: str,
    core_config_service: CoreConfigService,
) -> StoredReport:
    normalized_reference = reference.strip()
    if normalized_reference == "":
        raise ReportStoreError(
            code=ReportStoreErrorCode.REPORT_NOT_FOUND,
            message="Report reference must not be empty.",
        )
    reports = list_stored_reports(core_config_service=core_config_service)
    root = resolve_reports_directory(core_config_service=core_config_service).resolve(strict=False)

    if _looks_like_path(normalized_reference):
        candidate = Path(normalized_reference).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.absolute()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ReportStoreError(
                code=ReportStoreErrorCode.REPORT_OUTSIDE_MANAGED_STORE,
                message="Report path is outside the managed JELICA reports directory.",
            ) from error
        if candidate.is_symlink():
            raise ReportStoreError(
                code=ReportStoreErrorCode.INVALID_REPORT,
                message="Managed report paths must not be symbolic links.",
            )
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ReportStoreError(
                code=ReportStoreErrorCode.REPORT_OUTSIDE_MANAGED_STORE,
                message="Report path is outside the managed JELICA reports directory.",
            ) from error
        matches = [item for item in reports if item.path.resolve(strict=False) == candidate]
        if len(matches) == 1:
            return matches[0]
        raise ReportStoreError(
            code=ReportStoreErrorCode.REPORT_NOT_FOUND,
            message=f"Managed report '{normalized_reference}' was not found.",
        )

    exact_filename = [item for item in reports if item.filename == normalized_reference]
    if len(exact_filename) == 1:
        return exact_filename[0]
    if len(exact_filename) > 1:
        return _ambiguous_report(normalized_reference, exact_filename)

    exact_stem = [item for item in reports if item.stem == normalized_reference]
    if len(exact_stem) == 1:
        return exact_stem[0]
    if len(exact_stem) > 1:
        return _ambiguous_report(normalized_reference, exact_stem)

    bare_candidate = root / normalized_reference
    if bare_candidate.is_symlink():
        raise ReportStoreError(
            code=ReportStoreErrorCode.INVALID_REPORT,
            message="Managed report paths must not be symbolic links.",
        )

    aliases: dict[str, list[StoredReport]] = {}
    for item in reports:
        alias = _hashless_alias(item.stem)
        if alias is not None:
            aliases.setdefault(alias, []).append(item)
    alias_matches = aliases.get(normalized_reference, [])
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        return _ambiguous_report(normalized_reference, alias_matches)
    raise ReportStoreError(
        code=ReportStoreErrorCode.REPORT_NOT_FOUND,
        message=f"Managed report '{normalized_reference}' was not found.",
    )


def delete_stored_reports(
    *,
    references: Iterable[str] = (),
    delete_all: bool = False,
    force: bool = False,
    core_config_service: CoreConfigService,
) -> tuple[StoredReport, ...]:
    refs = tuple(references)
    if delete_all and refs:
        raise ReportStoreError(
            code=ReportStoreErrorCode.INVALID_REPORT,
            message="reports delete --all cannot be combined with report references.",
        )
    if delete_all and not force:
        raise ReportStoreError(
            code=ReportStoreErrorCode.INVALID_REPORT,
            message="reports delete --all requires --force.",
        )
    if not delete_all and not refs:
        raise ReportStoreError(
            code=ReportStoreErrorCode.INVALID_REPORT,
            message="Provide at least one report reference or use --all --force.",
        )

    root = resolve_reports_directory(core_config_service=core_config_service)
    targets: list[StoredReport] = []
    if delete_all:
        targets.extend(list_stored_reports(core_config_service=core_config_service))
    else:
        seen: set[Path] = set()
        for reference in refs:
            target = resolve_stored_report(
                reference=reference,
                core_config_service=core_config_service,
            )
            normalized = target.path.resolve(strict=False)
            if normalized not in seen:
                seen.add(normalized)
                targets.append(target)

    if not targets:
        return tuple()
    lock_fd, lock_path = _acquire_report_store_lock(root=root)
    deleted: list[StoredReport] = []
    try:
        for target in targets:
            path = root / target.filename
            if path.is_symlink() or not path.is_file():
                raise ReportStoreError(
                    code=ReportStoreErrorCode.INVALID_REPORT,
                    message=f"Managed report '{target.filename}' is not a regular file.",
                )
            try:
                path.unlink()
            except OSError as error:
                raise ReportStoreError(
                    code=ReportStoreErrorCode.REPORT_STORE_ERROR,
                    message=f"Report '{target.filename}' could not be deleted.",
                ) from error
            deleted.append(target)
    finally:
        _release_report_store_lock(lock_fd=lock_fd, lock_path=lock_path)
    return tuple(deleted)


def allocate_report_filename(
    *,
    directory: Path,
    base_stem: str,
    report_format: ReportFormat,
    force: bool,
) -> str:
    validate_report_stem(base_stem)
    base_filename = report_filename_for_stem(stem=base_stem, report_format=report_format)
    if force:
        return base_filename
    if not (directory / base_filename).exists():
        return base_filename
    index = 1
    while True:
        candidate = report_filename_for_stem(
            stem=f"{base_stem}_{index}",
            report_format=report_format,
        )
        if not (directory / candidate).exists():
            return candidate
        index += 1


def publish_report_temporary_file(
    *,
    temporary_path: Path,
    directory: Path,
    base_stem: str,
    report_format: ReportFormat,
    force: bool,
    exact_filename: str | None = None,
    managed: bool = False,
) -> Path:
    """Publish a rendered report with no-clobber or force semantics.

    The temporary file must be a sibling of the target directory.  Managed
    publication serializes allocation under ``.reports.lock``.  External
    directories use hard-link publication, which gives no-clobber semantics
    without leaving a persistent lock file.
    """

    directory.mkdir(parents=True, exist_ok=True)
    lock_fd: int | None = None
    lock_path: Path | None = None
    try:
        if managed:
            lock_fd, lock_path = _acquire_report_store_lock(root=directory)
        while True:
            if exact_filename is not None:
                validate_report_filename(
                    filename=exact_filename,
                    report_format=report_format,
                )
                base_filename = exact_filename
                base_stem_for_collision = exact_filename[: -len(report_format.extension)]
            else:
                base_filename = report_filename_for_stem(
                    stem=base_stem,
                    report_format=report_format,
                )
                base_stem_for_collision = base_stem
            target_filename = (
                base_filename
                if force
                else allocate_report_filename(
                    directory=directory,
                    base_stem=base_stem_for_collision,
                    report_format=report_format,
                    force=False,
                )
            )
            target = directory / target_filename
            try:
                if force and target.is_symlink():
                    raise ReportStoreError(
                        code=ReportStoreErrorCode.INVALID_REPORT,
                        message=f"Report target '{target.name}' is a symlink.",
                    )
                if force:
                    os.replace(temporary_path, target)
                else:
                    os.link(temporary_path, target)
                    temporary_path.unlink(missing_ok=True)
                return target
            except FileExistsError:
                if force:
                    raise
                if target.is_symlink():
                    raise ReportStoreError(
                        code=ReportStoreErrorCode.INVALID_REPORT,
                        message=f"Report target '{target.name}' is a symlink.",
                    ) from None
                if managed:
                    # A cooperating managed writer cannot race while holding
                    # the lock, but a pre-existing symlink or foreign writer
                    # may still make the candidate unavailable.
                    continue
                continue
    except OSError as error:
        raise ReportStoreError(
            code=ReportStoreErrorCode.REPORT_STORE_ERROR,
            message="Rendered report could not be published.",
        ) from error
    finally:
        if lock_path is not None and lock_fd is not None:
            _release_report_store_lock(lock_fd=lock_fd, lock_path=lock_path)


def _hashless_alias(stem: str) -> str | None:
    match = _CANONICAL_RESULT_REPORT_PATTERN.fullmatch(stem)
    if match is None:
        return None
    name = match.group("name")
    index = match.group("index")
    return f"{name}_{index}" if index is not None else name


def _ambiguous_report(reference: str, matches: Iterable[StoredReport]) -> StoredReport:
    names = ", ".join(sorted(item.filename for item in matches))
    raise ReportStoreError(
        code=ReportStoreErrorCode.AMBIGUOUS_REPORT,
        message=f"Report reference '{reference}' is ambiguous: {names}.",
    )


def _looks_like_path(value: str) -> bool:
    return (
        value.startswith((".", "~", "/"))
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
    )


def _acquire_report_store_lock(*, root: Path) -> tuple[int, Path]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _REPORT_LOCK_FILENAME
    deadline = time.monotonic() + _REPORT_LOCK_WAIT_SECONDS
    while True:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            return lock_fd, lock_path
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ReportStoreError(
                    code=ReportStoreErrorCode.REPORT_STORE_BUSY,
                    message="Managed reports directory is busy.",
                ) from None
            time.sleep(_REPORT_LOCK_POLL_SECONDS)
        except OSError as error:
            raise ReportStoreError(
                code=ReportStoreErrorCode.REPORT_STORE_ERROR,
                message="Managed reports directory lock cannot be acquired.",
            ) from error


def _release_report_store_lock(*, lock_fd: int, lock_path: Path) -> None:
    try:
        os.close(lock_fd)
    except OSError:
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "REPORT_DIRECTORY_NAME",
    "REPORT_GENERATED_FORMAT",
    "REPORT_MAX_FILENAME_LENGTH",
    "REPORT_RECOGNIZED_EXTENSIONS",
    "ReportFormat",
    "ReportStoreError",
    "ReportStoreErrorCode",
    "StoredReport",
    "allocate_report_filename",
    "delete_stored_reports",
    "list_stored_reports",
    "publish_report_temporary_file",
    "report_filename_for_stem",
    "report_format_from_filename",
    "resolve_reports_directory",
    "resolve_stored_report",
    "validate_report_filename",
    "validate_report_stem",
]
