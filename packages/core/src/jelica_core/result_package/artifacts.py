from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO, Final, Iterable, Mapping
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from jelica_core.system_config import (
    CONFIG_FILENAME,
    JELICA_HOME_ENV_VAR,
    CoreConfigError,
    CoreConfigService,
)
from jelica_core.tasks import (
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryError,
    AnalyticalTaskRegistryService,
    TaskWorkspaceDeleteError,
    validate_human_readable_name,
    validate_task_name,
)
from jelica_core.tasks.storage import resolve_task_workspace_dir, write_text_atomically

RESULT_PACKAGE_STAGE_ID: Final = "result_package"
RESULT_PACKAGE_STAGE_MANIFEST_SCHEMA_VERSION: Final = 1
RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH: Final = (
    "result_package/result_package_manifest.json"
)
RESULT_PACKAGE_LINK_FILENAME: Final = "result_package.json"
RESULT_PACKAGE_PREPARED_DIRNAME: Final = ".result_package_prepared"
RESULT_PACKAGE_DIRECTORY_NAME: Final = "result_packages"

JELICA_PACKAGE_FORMAT: Final = "jelica-result-package"
JELICA_PACKAGE_FORMAT_VERSION: Final = "1.0"
JELICA_PACKAGE_MANIFEST_PATH: Final = "manifest.json"
JELICA_PACKAGE_TASK_PATH: Final = "task.json"
JELICA_PACKAGE_CONFIGURATION_PATH: Final = "configuration.json"
JELICA_PACKAGE_INPUT_MANIFEST_PATH: Final = "input/input_manifest.json"
JELICA_PACKAGE_NORMALIZED_FASTA_PATH: Final = "input/normalized_sequences.fasta"
JELICA_PACKAGE_NOTES_PATH: Final = "NOTES.txt"

_REQUIRED_PROTECTED_PATHS: Final[frozenset[str]] = frozenset(
    {
        JELICA_PACKAGE_TASK_PATH,
        JELICA_PACKAGE_CONFIGURATION_PATH,
        JELICA_PACKAGE_INPUT_MANIFEST_PATH,
        JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
    }
)
_CHUNK_SIZE: Final = 1024 * 1024
_CONTENT_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^sha256:([0-9a-f]{64})$")
_RESULT_PACKAGE_FILENAME_STEM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
)
_SUPPORTED_ZIP_COMPRESSION_METHODS: Final[frozenset[int]] = frozenset(
    method
    for method in (
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        getattr(zipfile, "ZIP_BZIP2", None),
        getattr(zipfile, "ZIP_LZMA", None),
    )
    if isinstance(method, int)
)

_MEDIA_TYPES_BY_SUFFIX: Final[dict[str, str]] = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".fasta": "text/x-fasta",
    ".fa": "text/x-fasta",
    ".fna": "text/x-fasta",
    ".mfa": "text/x-fasta",
    ".afa": "text/x-fasta",
    ".txt": "text/plain",
    ".tsv": "text/tab-separated-values",
    ".csv": "text/csv",
    ".nwk": "text/x-newick",
    ".newick": "text/x-newick",
    ".log": "text/plain",
}
_RESULT_PACKAGE_IMPORT_LOCK_FILENAME: Final = ".result_packages.lock"
_RESULT_PACKAGE_IMPORT_LOCK_WAIT_SECONDS: Final = 5.0
_RESULT_PACKAGE_IMPORT_LOCK_POLL_SECONDS: Final = 0.05


class ResultPackageTaskStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"


class ResultPackageValidationError(RuntimeError):
    """Raised when a `.jelica` container fails structural or integrity checks."""


class ResultPackagePublicationError(RuntimeError):
    """Raised when atomic publication to the central package store fails."""


@dataclass(frozen=True, slots=True)
class _ContentRecord:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedResultPackage:
    manifest: JelicaPackageManifest
    content_id: str
    has_notes: bool


class JelicaPackageValidationIssueCode(StrEnum):
    FILE_NOT_FOUND = "file_not_found"
    NOT_A_FILE = "not_a_file"
    INVALID_ZIP = "invalid_zip"
    ENCRYPTED_ENTRY = "encrypted_entry"
    UNSUPPORTED_COMPRESSION = "unsupported_compression"
    UNSAFE_PATH = "unsafe_path"
    DUPLICATE_ENTRY = "duplicate_entry"
    NON_REGULAR_ENTRY = "non_regular_entry"
    MISSING_REQUIRED_FILE = "missing_required_file"
    UNEXPECTED_FILE = "unexpected_file"
    INVALID_MANIFEST_JSON = "invalid_manifest_json"
    INVALID_MANIFEST_SCHEMA = "invalid_manifest_schema"
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNSUPPORTED_FORMAT_VERSION = "unsupported_format_version"
    INVALID_JSON = "invalid_json"
    INVALID_UTF8 = "invalid_utf8"
    INVALID_NOTES = "invalid_notes"
    MISSING_ARTIFACT = "missing_artifact"
    SIZE_MISMATCH = "size_mismatch"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    CONTENT_ID_MISMATCH = "content_id_mismatch"
    STAGE_ARTIFACT_MISMATCH = "stage_artifact_mismatch"
    TASK_METADATA_MISMATCH = "task_metadata_mismatch"


@dataclass(frozen=True, slots=True)
class JelicaPackageValidationIssue:
    code: JelicaPackageValidationIssueCode
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class JelicaPackageValidationResult:
    valid: bool
    format: str | None
    format_version: str | None
    content_id: str | None
    errors: tuple[JelicaPackageValidationIssue, ...]
    warnings: tuple[JelicaPackageValidationIssue, ...]


class ResultPackageLibraryErrorCode(StrEnum):
    INVALID_SOURCE_PACKAGE = "invalid_source_package"
    INVALID_EXISTING_PACKAGE = "invalid_existing_package"
    NOTES_CONFLICT = "notes_conflict"
    PACKAGE_NOT_FOUND = "package_not_found"
    TASK_NOT_FOUND = "task_not_found"
    TASK_HAS_NO_RESULT_PACKAGE = "task_has_no_result_package"
    INVALID_RESULT_PACKAGE_LINK = "invalid_result_package_link"
    UNSAFE_RESULT_PACKAGE_LINK = "unsafe_result_package_link"
    AMBIGUOUS_RESULT_PACKAGE = "ambiguous_result_package"
    IMPORT_IO_ERROR = "import_io_error"


class ResultPackageLibraryError(RuntimeError):
    def __init__(self, *, code: ResultPackageLibraryErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ImportedResultPackage:
    content_id: str
    path: Path
    already_exists: bool


@dataclass(frozen=True, slots=True)
class DeletedResultPackage:
    path: Path
    already_missing: bool = False


@dataclass(frozen=True, slots=True)
class ParsedResultPackageFilename:
    result_name: str | None
    content_digest: str


@dataclass(frozen=True, slots=True)
class ListedResultPackage:
    file_name: str
    path: Path
    content_id: str | None
    task_id: str | None
    status: str
    format_version: str | None
    valid: bool
    result_name: str | None = None
    task_name: str | None = None
    issue_code: ResultPackageLibraryErrorCode | None = None


@dataclass(frozen=True, slots=True)
class ListedResultPackages:
    packages: tuple[ListedResultPackage, ...]
    has_invalid_entries: bool


@dataclass(frozen=True, slots=True)
class ResolvedResultPackagePath:
    content_id: str
    path: Path


class JelicaPackageReaderError(RuntimeError):
    def __init__(
        self,
        *,
        code: JelicaPackageValidationIssueCode,
        message: str,
        path: str | None = None,
    ) -> None:
        self.code = code
        self.path = path
        super().__init__(message)


class JelicaPackageReader:
    def __init__(self, *, path: Path | str) -> None:
        self._path = Path(path)
        self._archive: zipfile.ZipFile | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._archive is not None

    def __enter__(self) -> JelicaPackageReader:
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = (exc_type, exc, tb)
        self.close()

    def open(self) -> None:
        if self._archive is not None:
            return
        self._archive = zipfile.ZipFile(self._path, mode="r")

    def close(self) -> None:
        if self._archive is None:
            return
        self._archive.close()
        self._archive = None

    def list_entries(self) -> tuple[str, ...]:
        archive = self._require_archive()
        return tuple(info.filename for info in archive.infolist())

    def read_manifest(self) -> JelicaPackageManifest:
        payload = self.read_json_file(path=JELICA_PACKAGE_MANIFEST_PATH)
        try:
            return JelicaPackageManifest.model_validate(payload)
        except ValidationError as error:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.INVALID_MANIFEST_SCHEMA,
                message="manifest.json does not match JELICA result package schema",
                path=JELICA_PACKAGE_MANIFEST_PATH,
            ) from error

    def read_task_json(self) -> dict[str, object]:
        return self.read_json_file(path=JELICA_PACKAGE_TASK_PATH)

    def read_configuration_json(self) -> dict[str, object]:
        return self.read_json_file(path=JELICA_PACKAGE_CONFIGURATION_PATH)

    def read_input_manifest_json(self) -> dict[str, object]:
        return self.read_json_file(path=JELICA_PACKAGE_INPUT_MANIFEST_PATH)

    def read_json_file(self, *, path: str) -> dict[str, object]:
        normalized = self._normalize_entry_path(path)
        payload = self.read_bytes(path=normalized)
        try:
            decoded = payload.decode("utf-8")
        except UnicodeError as error:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.INVALID_UTF8,
                message=f"ZIP entry '{normalized}' is not UTF-8 text",
                path=normalized,
            ) from error
        try:
            loaded = json.loads(
                decoded, parse_constant=_reject_non_finite_json_constant
            )
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.INVALID_JSON,
                message=f"ZIP entry '{normalized}' is not valid JSON",
                path=normalized,
            ) from error
        if not isinstance(loaded, dict):
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.INVALID_JSON,
                message=f"ZIP entry '{normalized}' must be a JSON object",
                path=normalized,
            )
        return {str(key): value for key, value in loaded.items()}

    def read_bytes(self, *, path: str) -> bytes:
        normalized = self._normalize_entry_path(path)
        archive = self._require_archive()
        try:
            return archive.read(normalized)
        except KeyError as error:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.MISSING_ARTIFACT,
                message=f"ZIP entry '{normalized}' is missing",
                path=normalized,
            ) from error
        except OSError as error:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.INVALID_ZIP,
                message=f"ZIP entry '{normalized}' cannot be read",
                path=normalized,
            ) from error

    def open_entry(self, *, path: str) -> IO[bytes]:
        normalized = self._normalize_entry_path(path)
        archive = self._require_archive()
        if normalized not in {info.filename for info in archive.infolist()}:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.MISSING_ARTIFACT,
                message=f"ZIP entry '{normalized}' is missing",
                path=normalized,
            )
        try:
            return archive.open(normalized, mode="r")
        except OSError as error:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.INVALID_ZIP,
                message=f"ZIP entry '{normalized}' cannot be opened",
                path=normalized,
            ) from error

    def open_artifact(self, *, path: str) -> IO[bytes]:
        normalized = self._normalize_entry_path(path)
        manifest = self.read_manifest()
        declared_paths = {artifact.path for artifact in manifest.artifacts}
        if normalized not in declared_paths:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.MISSING_ARTIFACT,
                message=f"Protected artifact '{normalized}' is not declared in manifest",
                path=normalized,
            )
        return self.open_entry(path=normalized)

    def open_notes(self) -> IO[bytes] | None:
        archive = self._require_archive()
        if JELICA_PACKAGE_NOTES_PATH not in {
            info.filename for info in archive.infolist()
        }:
            return None
        return self.open_entry(path=JELICA_PACKAGE_NOTES_PATH)

    def _require_archive(self) -> zipfile.ZipFile:
        if self._archive is None:
            self.open()
        assert self._archive is not None
        return self._archive

    @staticmethod
    def _normalize_entry_path(path: str) -> str:
        try:
            normalized = _normalize_package_internal_path(path)
        except ValueError as error:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.UNSAFE_PATH,
                message=f"ZIP path '{path}' is not safe",
                path=path,
            ) from error
        if normalized != path:
            raise JelicaPackageReaderError(
                code=JelicaPackageValidationIssueCode.UNSAFE_PATH,
                message=f"ZIP path '{path}' is not normalized",
                path=path,
            )
        return normalized


class JelicaPackageValidator:
    def validate(
        self,
        source: Path | str | JelicaPackageReader,
    ) -> JelicaPackageValidationResult:
        if isinstance(source, JelicaPackageReader):
            return self._validate_reader(source)
        return self._validate_path(Path(source))

    def _validate_path(self, path: Path) -> JelicaPackageValidationResult:
        try:
            if path.is_symlink():
                return self._result_with_single_error(
                    code=JelicaPackageValidationIssueCode.NOT_A_FILE,
                    message="Result package path must reference a regular file",
                )
        except OSError:
            return self._result_with_single_error(
                code=JelicaPackageValidationIssueCode.NOT_A_FILE,
                message="Result package path cannot be inspected",
            )
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            return self._result_with_single_error(
                code=JelicaPackageValidationIssueCode.FILE_NOT_FOUND,
                message="Result package file was not found",
            )
        except OSError:
            return self._result_with_single_error(
                code=JelicaPackageValidationIssueCode.FILE_NOT_FOUND,
                message="Result package file path cannot be resolved",
            )

        if not resolved.is_file() or resolved.is_symlink():
            return self._result_with_single_error(
                code=JelicaPackageValidationIssueCode.NOT_A_FILE,
                message="Result package path must reference a regular file",
            )

        reader = JelicaPackageReader(path=resolved)
        return self._validate_reader(reader)

    def _validate_reader(
        self, reader: JelicaPackageReader
    ) -> JelicaPackageValidationResult:
        issues: list[JelicaPackageValidationIssue] = []
        warnings: list[JelicaPackageValidationIssue] = []
        package_format: str | None = None
        format_version: str | None = None
        content_id: str | None = None
        was_open = reader.is_open
        try:
            if not was_open:
                reader.open()
            archive = reader._require_archive()
        except (OSError, zipfile.BadZipFile):
            return self._result_with_single_error(
                code=JelicaPackageValidationIssueCode.INVALID_ZIP,
                message="Result package is not a readable ZIP archive",
            )

        try:
            info_by_name = self._validate_zip_entries(archive=archive, issues=issues)
            if issues:
                return self._result(
                    format=package_format,
                    format_version=format_version,
                    content_id=content_id,
                    issues=issues,
                    warnings=warnings,
                )

            manifest_payload = self._read_manifest_payload(
                archive=archive,
                info_by_name=info_by_name,
                issues=issues,
            )
            if manifest_payload is None:
                return self._result(
                    format=package_format,
                    format_version=format_version,
                    content_id=content_id,
                    issues=issues,
                    warnings=warnings,
                )

            package_format = (
                manifest_payload.get("format")
                if isinstance(manifest_payload.get("format"), str)
                else None
            )
            format_version = (
                manifest_payload.get("format_version")
                if isinstance(manifest_payload.get("format_version"), str)
                else None
            )
            content_id = (
                manifest_payload.get("content_id")
                if isinstance(manifest_payload.get("content_id"), str)
                else None
            )

            if package_format != JELICA_PACKAGE_FORMAT:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.UNSUPPORTED_FORMAT,
                    message="manifest format is not supported",
                    path=JELICA_PACKAGE_MANIFEST_PATH,
                )
            if format_version != JELICA_PACKAGE_FORMAT_VERSION:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.UNSUPPORTED_FORMAT_VERSION,
                    message="manifest format_version is not supported",
                    path=JELICA_PACKAGE_MANIFEST_PATH,
                )
            if issues:
                return self._result(
                    format=package_format,
                    format_version=format_version,
                    content_id=content_id,
                    issues=issues,
                    warnings=warnings,
                )

            if self._manifest_declares_notes(loaded_manifest=manifest_payload):
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.INVALID_NOTES,
                    message="NOTES.txt must not be declared in manifest.artifacts",
                    path=JELICA_PACKAGE_MANIFEST_PATH,
                )
                return self._result(
                    format=package_format,
                    format_version=format_version,
                    content_id=content_id,
                    issues=issues,
                    warnings=warnings,
                )

            manifest = self._validate_manifest_model(
                payload=manifest_payload, issues=issues
            )
            if manifest is None:
                return self._result(
                    format=package_format,
                    format_version=format_version,
                    content_id=content_id,
                    issues=issues,
                    warnings=warnings,
                )

            self._validate_allowed_file_set(
                archive=archive,
                manifest=manifest,
                info_by_name=info_by_name,
                issues=issues,
            )
            self._validate_declared_artifacts(
                archive=archive,
                manifest=manifest,
                info_by_name=info_by_name,
                issues=issues,
            )
            self._validate_task_metadata_consistency(
                archive=archive,
                manifest=manifest,
                info_by_name=info_by_name,
                issues=issues,
            )
            self._validate_stage_artifact_consistency(manifest=manifest, issues=issues)
            return self._result(
                format=manifest.format,
                format_version=manifest.format_version,
                content_id=manifest.content_id,
                issues=issues,
                warnings=warnings,
            )
        finally:
            if not was_open:
                reader.close()

    def _validate_zip_entries(
        self,
        *,
        archive: zipfile.ZipFile,
        issues: list[JelicaPackageValidationIssue],
    ) -> dict[str, zipfile.ZipInfo]:
        seen: set[str] = set()
        info_by_name: dict[str, zipfile.ZipInfo] = {}
        for info in archive.infolist():
            raw_name = info.filename
            if raw_name == "":
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.UNSAFE_PATH,
                    message="ZIP entry path must not be empty",
                )
                continue
            if "\0" in raw_name:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.UNSAFE_PATH,
                    message="ZIP entry path contains NUL",
                    path=raw_name,
                )
                continue
            try:
                normalized = _normalize_package_internal_path(raw_name)
            except ValueError:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.UNSAFE_PATH,
                    message="ZIP entry path is not safe",
                    path=raw_name,
                )
                continue
            if normalized != raw_name:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.UNSAFE_PATH,
                    message="ZIP entry path is not normalized",
                    path=raw_name,
                )
                continue
            if normalized in seen:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.DUPLICATE_ENTRY,
                    message="ZIP archive contains duplicate entry",
                    path=normalized,
                )
                continue
            seen.add(normalized)

            if info.flag_bits & 0x1:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.ENCRYPTED_ENTRY,
                    message="Encrypted ZIP entries are not supported",
                    path=normalized,
                )

            if info.compress_type not in _SUPPORTED_ZIP_COMPRESSION_METHODS:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.UNSUPPORTED_COMPRESSION,
                    message="ZIP entry uses unsupported compression method",
                    path=normalized,
                )

            if info.is_dir() or _zip_info_is_non_regular_entry(info):
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.NON_REGULAR_ENTRY,
                    message="ZIP entry must be a regular file",
                    path=normalized,
                )
                continue
            info_by_name[normalized] = info
        return info_by_name

    def _read_manifest_payload(
        self,
        *,
        archive: zipfile.ZipFile,
        info_by_name: dict[str, zipfile.ZipInfo],
        issues: list[JelicaPackageValidationIssue],
    ) -> dict[str, object] | None:
        if JELICA_PACKAGE_MANIFEST_PATH not in info_by_name:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.MISSING_REQUIRED_FILE,
                message="Required file is missing",
                path=JELICA_PACKAGE_MANIFEST_PATH,
            )
            return None
        try:
            payload_bytes = archive.read(JELICA_PACKAGE_MANIFEST_PATH)
        except (KeyError, OSError):
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_ZIP,
                message="manifest.json cannot be read",
                path=JELICA_PACKAGE_MANIFEST_PATH,
            )
            return None
        try:
            payload = payload_bytes.decode("utf-8")
        except UnicodeError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_UTF8,
                message="manifest.json must be UTF-8",
                path=JELICA_PACKAGE_MANIFEST_PATH,
            )
            return None
        try:
            loaded = json.loads(
                payload, parse_constant=_reject_non_finite_json_constant
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_MANIFEST_JSON,
                message="manifest.json is not valid JSON",
                path=JELICA_PACKAGE_MANIFEST_PATH,
            )
            return None
        if not isinstance(loaded, dict):
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_MANIFEST_JSON,
                message="manifest.json must be a JSON object",
                path=JELICA_PACKAGE_MANIFEST_PATH,
            )
            return None
        return {str(key): value for key, value in loaded.items()}

    def _validate_manifest_model(
        self,
        *,
        payload: dict[str, object],
        issues: list[JelicaPackageValidationIssue],
    ) -> JelicaPackageManifest | None:
        try:
            return JelicaPackageManifest.model_validate(payload)
        except ValidationError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_MANIFEST_SCHEMA,
                message="manifest.json does not match JELICA result package schema",
                path=JELICA_PACKAGE_MANIFEST_PATH,
            )
            return None

    def _validate_allowed_file_set(
        self,
        *,
        archive: zipfile.ZipFile,
        manifest: JelicaPackageManifest,
        info_by_name: dict[str, zipfile.ZipInfo],
        issues: list[JelicaPackageValidationIssue],
    ) -> None:
        _ = archive
        expected = {
            JELICA_PACKAGE_MANIFEST_PATH,
            *(item.path for item in manifest.artifacts),
        }
        names = set(info_by_name)
        required = {
            JELICA_PACKAGE_MANIFEST_PATH,
            JELICA_PACKAGE_TASK_PATH,
            JELICA_PACKAGE_CONFIGURATION_PATH,
            JELICA_PACKAGE_INPUT_MANIFEST_PATH,
            JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
        }
        for required_path in sorted(required):
            if required_path not in names:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.MISSING_REQUIRED_FILE,
                    message="Required file is missing",
                    path=required_path,
                )

        if JELICA_PACKAGE_NOTES_PATH in names:
            expected.add(JELICA_PACKAGE_NOTES_PATH)
            self._validate_notes_text_encoding(
                archive=archive,
                issues=issues,
            )

        for path in sorted(names.difference(expected)):
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.UNEXPECTED_FILE,
                message="ZIP entry is not allowed by manifest",
                path=path,
            )

    def _validate_declared_artifacts(
        self,
        *,
        archive: zipfile.ZipFile,
        manifest: JelicaPackageManifest,
        info_by_name: dict[str, zipfile.ZipInfo],
        issues: list[JelicaPackageValidationIssue],
    ) -> None:
        observed_records: list[_ContentRecord] = []
        has_integrity_errors = False
        for artifact in manifest.artifacts:
            info = info_by_name.get(artifact.path)
            if info is None:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.MISSING_ARTIFACT,
                    message="Protected artifact listed in manifest is missing",
                    path=artifact.path,
                )
                has_integrity_errors = True
                continue
            if info.file_size != artifact.size:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.SIZE_MISMATCH,
                    message="Protected artifact size does not match manifest",
                    path=artifact.path,
                )
                has_integrity_errors = True
            try:
                sha256 = _sha256_for_zip_entry(
                    archive=archive, entry_path=artifact.path
                )
            except ResultPackageValidationError:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.CHECKSUM_MISMATCH,
                    message="Protected artifact cannot be hashed",
                    path=artifact.path,
                )
                has_integrity_errors = True
                continue
            if sha256 != artifact.sha256:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.CHECKSUM_MISMATCH,
                    message="Protected artifact checksum does not match manifest",
                    path=artifact.path,
                )
                has_integrity_errors = True
            observed_records.append(
                _ContentRecord(path=artifact.path, size=info.file_size, sha256=sha256)
            )
            self._validate_json_artifact_content(
                archive=archive,
                artifact=artifact,
                issues=issues,
            )

        if not has_integrity_errors and len(observed_records) == len(
            manifest.artifacts
        ):
            computed = compute_content_id(
                artifacts=tuple(
                    ResultPackageArtifactInfo(
                        path=item.path,
                        stage=None,
                        media_type="application/octet-stream",
                        size=item.size,
                        sha256=item.sha256,
                    )
                    for item in observed_records
                )
            )
            if computed != manifest.content_id:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.CONTENT_ID_MISMATCH,
                    message="manifest content_id does not match protected artifact content",
                    path=JELICA_PACKAGE_MANIFEST_PATH,
                )

    def _validate_json_artifact_content(
        self,
        *,
        archive: zipfile.ZipFile,
        artifact: ResultPackageArtifactInfo,
        issues: list[JelicaPackageValidationIssue],
    ) -> None:
        if artifact.media_type == "application/json":
            self._validate_json_file_in_archive(
                archive=archive,
                path=artifact.path,
                issues=issues,
            )
            return
        if artifact.media_type == "application/x-ndjson":
            self._validate_jsonl_file_in_archive(
                archive=archive,
                path=artifact.path,
                issues=issues,
            )

    def _validate_task_metadata_consistency(
        self,
        *,
        archive: zipfile.ZipFile,
        manifest: JelicaPackageManifest,
        info_by_name: Mapping[str, zipfile.ZipInfo],
        issues: list[JelicaPackageValidationIssue],
    ) -> None:
        for required_path in (
            JELICA_PACKAGE_TASK_PATH,
            JELICA_PACKAGE_CONFIGURATION_PATH,
            JELICA_PACKAGE_INPUT_MANIFEST_PATH,
        ):
            if required_path not in info_by_name:
                return

        task_payload = self._load_json_object_from_archive(
            archive=archive,
            path=JELICA_PACKAGE_TASK_PATH,
            issues=issues,
        )
        configuration_payload = self._load_json_object_from_archive(
            archive=archive,
            path=JELICA_PACKAGE_CONFIGURATION_PATH,
            issues=issues,
        )
        _ = self._load_json_object_from_archive(
            archive=archive,
            path=JELICA_PACKAGE_INPUT_MANIFEST_PATH,
            issues=issues,
        )
        if task_payload is None or configuration_payload is None:
            return
        try:
            task_info = ResultPackageTaskInfo.model_validate(task_payload)
        except ValidationError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.TASK_METADATA_MISMATCH,
                message="task.json metadata is invalid",
                path=JELICA_PACKAGE_TASK_PATH,
            )
            return
        raw_configuration_trace_id = configuration_payload.get("trace_id")
        try:
            configuration_trace_id = (
                None
                if raw_configuration_trace_id is None
                else UUID(str(raw_configuration_trace_id))
            )
        except ValueError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.TASK_METADATA_MISMATCH,
                message="configuration trace_id is invalid",
                path=JELICA_PACKAGE_CONFIGURATION_PATH,
            )
            return
        if (
            task_info.task_id != manifest.task.task_id
            or task_info.trace_id != manifest.task.trace_id
            or task_info.trace_id != configuration_trace_id
            or task_info.status is not manifest.task.status
            or task_info.created_at != manifest.task.created_at
            or task_info.completed_at != manifest.task.completed_at
        ):
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.TASK_METADATA_MISMATCH,
                message="task.json metadata does not match manifest.task",
                path=JELICA_PACKAGE_TASK_PATH,
            )

    def _validate_stage_artifact_consistency(
        self,
        *,
        manifest: JelicaPackageManifest,
        issues: list[JelicaPackageValidationIssue],
    ) -> None:
        owner_by_path: dict[str, str] = {}
        for stage in manifest.stages:
            for path in stage.artifacts:
                existing_owner = owner_by_path.get(path)
                if existing_owner is not None and existing_owner != stage.name:
                    self._add_issue(
                        issues=issues,
                        code=JelicaPackageValidationIssueCode.STAGE_ARTIFACT_MISMATCH,
                        message="artifact is declared by multiple stages",
                        path=path,
                    )
                    continue
                owner_by_path[path] = stage.name

        for artifact in manifest.artifacts:
            owner = owner_by_path.get(artifact.path)
            if owner is not None and artifact.stage != owner:
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.STAGE_ARTIFACT_MISMATCH,
                    message="artifact.stage does not match stage ownership",
                    path=artifact.path,
                )

            if artifact.path.startswith("results/"):
                parts = artifact.path.split("/")
                if len(parts) < 3:
                    self._add_issue(
                        issues=issues,
                        code=JelicaPackageValidationIssueCode.STAGE_ARTIFACT_MISMATCH,
                        message="results artifact path must include stage segment",
                        path=artifact.path,
                    )
                    continue
                stage_name = parts[1]
                if artifact.stage != stage_name:
                    self._add_issue(
                        issues=issues,
                        code=JelicaPackageValidationIssueCode.STAGE_ARTIFACT_MISMATCH,
                        message="results artifact stage does not match its path",
                        path=artifact.path,
                    )
            elif (
                artifact.path
                in {
                    JELICA_PACKAGE_TASK_PATH,
                    JELICA_PACKAGE_CONFIGURATION_PATH,
                }
                and artifact.stage is not None
            ):
                self._add_issue(
                    issues=issues,
                    code=JelicaPackageValidationIssueCode.STAGE_ARTIFACT_MISMATCH,
                    message="task metadata artifact must not declare stage",
                    path=artifact.path,
                )

    def _validate_json_file_in_archive(
        self,
        *,
        archive: zipfile.ZipFile,
        path: str,
        issues: list[JelicaPackageValidationIssue],
    ) -> None:
        payload = self._read_text_from_archive(
            archive=archive, path=path, issues=issues
        )
        if payload is None:
            return
        try:
            json.loads(payload, parse_constant=_reject_non_finite_json_constant)
        except (ValueError, TypeError, json.JSONDecodeError):
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_JSON,
                message="JSON artifact is malformed",
                path=path,
            )

    def _validate_jsonl_file_in_archive(
        self,
        *,
        archive: zipfile.ZipFile,
        path: str,
        issues: list[JelicaPackageValidationIssue],
    ) -> None:
        try:
            with archive.open(path, mode="r") as handle:
                text_stream = io.TextIOWrapper(handle, encoding="utf-8")
                for raw_line in text_stream:
                    line = raw_line.strip()
                    if line == "":
                        continue
                    try:
                        json.loads(
                            line, parse_constant=_reject_non_finite_json_constant
                        )
                    except (ValueError, TypeError, json.JSONDecodeError):
                        self._add_issue(
                            issues=issues,
                            code=JelicaPackageValidationIssueCode.INVALID_JSON,
                            message="JSONL artifact contains malformed JSON line",
                            path=path,
                        )
                        return
        except UnicodeError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_UTF8,
                message="JSONL artifact is not UTF-8",
                path=path,
            )
        except OSError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_ZIP,
                message="JSONL artifact cannot be read",
                path=path,
            )

    def _load_json_object_from_archive(
        self,
        *,
        archive: zipfile.ZipFile,
        path: str,
        issues: list[JelicaPackageValidationIssue],
    ) -> dict[str, object] | None:
        payload = self._read_text_from_archive(
            archive=archive, path=path, issues=issues
        )
        if payload is None:
            return None
        try:
            loaded = json.loads(
                payload, parse_constant=_reject_non_finite_json_constant
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_JSON,
                message="JSON file is malformed",
                path=path,
            )
            return None
        if not isinstance(loaded, dict):
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_JSON,
                message="JSON file must be an object",
                path=path,
            )
            return None
        return {str(key): value for key, value in loaded.items()}

    def _read_text_from_archive(
        self,
        *,
        archive: zipfile.ZipFile,
        path: str,
        issues: list[JelicaPackageValidationIssue],
    ) -> str | None:
        try:
            with archive.open(path, mode="r") as handle:
                return handle.read().decode("utf-8")
        except KeyError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.MISSING_ARTIFACT,
                message="Declared artifact is missing in archive",
                path=path,
            )
            return None
        except UnicodeError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_UTF8,
                message="File is not UTF-8",
                path=path,
            )
            return None
        except OSError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_ZIP,
                message="ZIP entry cannot be read",
                path=path,
            )
            return None

    def _validate_notes_text_encoding(
        self,
        *,
        archive: zipfile.ZipFile,
        issues: list[JelicaPackageValidationIssue],
    ) -> None:
        try:
            with archive.open(JELICA_PACKAGE_NOTES_PATH, mode="r") as handle:
                handle.read().decode("utf-8")
        except UnicodeError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_NOTES,
                message="NOTES.txt must be UTF-8 text",
                path=JELICA_PACKAGE_NOTES_PATH,
            )
        except OSError:
            self._add_issue(
                issues=issues,
                code=JelicaPackageValidationIssueCode.INVALID_ZIP,
                message="NOTES.txt cannot be read",
                path=JELICA_PACKAGE_NOTES_PATH,
            )

    @staticmethod
    def _manifest_declares_notes(*, loaded_manifest: dict[str, object]) -> bool:
        artifacts = loaded_manifest.get("artifacts")
        if not isinstance(artifacts, list):
            return False
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            if item.get("path") == JELICA_PACKAGE_NOTES_PATH:
                return True
        return False

    @staticmethod
    def _add_issue(
        *,
        issues: list[JelicaPackageValidationIssue],
        code: JelicaPackageValidationIssueCode,
        message: str,
        path: str | None = None,
    ) -> None:
        issue = JelicaPackageValidationIssue(code=code, message=message, path=path)
        if issue not in issues:
            issues.append(issue)

    @staticmethod
    def _result_with_single_error(
        *,
        code: JelicaPackageValidationIssueCode,
        message: str,
    ) -> JelicaPackageValidationResult:
        return JelicaPackageValidationResult(
            valid=False,
            format=None,
            format_version=None,
            content_id=None,
            errors=(JelicaPackageValidationIssue(code=code, message=message),),
            warnings=tuple(),
        )

    @staticmethod
    def _result(
        *,
        format: str | None,
        format_version: str | None,
        content_id: str | None,
        issues: list[JelicaPackageValidationIssue],
        warnings: list[JelicaPackageValidationIssue],
    ) -> JelicaPackageValidationResult:
        return JelicaPackageValidationResult(
            valid=len(issues) == 0,
            format=format,
            format_version=format_version,
            content_id=content_id,
            errors=tuple(issues),
            warnings=tuple(warnings),
        )


class ResultPackageProducerInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = "JELICA"
    version: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != "JELICA":
            raise ValueError("producer.name must be 'JELICA'")
        return normalized

    @field_validator("version")
    @classmethod
    def _normalize_version(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("producer.version must not be empty")
        return normalized


class ResultPackageTaskInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    trace_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    status: ResultPackageTaskStatus
    created_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)

    @field_validator("task_id", "created_at", "completed_at")
    @classmethod
    def _normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("task fields must not be empty")
        return normalized


class ResultPackageStageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    artifacts: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("name", "status")
    @classmethod
    def _normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("stage fields must not be empty")
        return normalized

    @field_validator("artifacts")
    @classmethod
    def _normalize_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_package_internal_path(item) for item in value)
        if tuple(sorted(normalized)) != normalized:
            raise ValueError("stage artifacts must be sorted")
        if len(set(normalized)) != len(normalized):
            raise ValueError("stage artifacts must be unique")
        return normalized


class ResultPackageArtifactInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    stage: str | None = None
    media_type: str = Field(min_length=1)
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        return _normalize_package_internal_path(value)

    @field_validator("stage")
    @classmethod
    def _normalize_stage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized == "":
            raise ValueError("stage must not be empty when provided")
        return normalized

    @field_validator("media_type")
    @classmethod
    def _normalize_media_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized == "":
            raise ValueError("media_type must not be empty")
        return normalized

    @field_validator("sha256")
    @classmethod
    def _normalize_sha256(cls, value: str) -> str:
        return _normalize_sha256_hex(value, field_name="sha256")


class JelicaPackageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: str = JELICA_PACKAGE_FORMAT
    format_version: str = JELICA_PACKAGE_FORMAT_VERSION
    content_id: str = Field(min_length=71, max_length=71)
    producer: ResultPackageProducerInfo
    package_created_at: str = Field(min_length=1)
    task: ResultPackageTaskInfo
    stages: tuple[ResultPackageStageInfo, ...] = Field(default_factory=tuple)
    artifacts: tuple[ResultPackageArtifactInfo, ...] = Field(default_factory=tuple)

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != JELICA_PACKAGE_FORMAT:
            raise ValueError(f"format must be '{JELICA_PACKAGE_FORMAT}'")
        return normalized

    @field_validator("format_version")
    @classmethod
    def _validate_format_version(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != JELICA_PACKAGE_FORMAT_VERSION:
            raise ValueError(
                f"format_version must be '{JELICA_PACKAGE_FORMAT_VERSION}'"
            )
        return normalized

    @field_validator("content_id")
    @classmethod
    def _normalize_content_id(cls, value: str) -> str:
        return _normalize_content_id(value)

    @field_validator("package_created_at")
    @classmethod
    def _normalize_created_at(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("package_created_at must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_content(self) -> JelicaPackageManifest:
        artifact_paths = tuple(item.path for item in self.artifacts)
        if tuple(sorted(artifact_paths)) != artifact_paths:
            raise ValueError("artifacts must be sorted by path")
        if len(set(artifact_paths)) != len(artifact_paths):
            raise ValueError("artifacts must not contain duplicate paths")
        if JELICA_PACKAGE_MANIFEST_PATH in artifact_paths:
            raise ValueError("artifacts must not include manifest.json")
        if JELICA_PACKAGE_NOTES_PATH in artifact_paths:
            raise ValueError("artifacts must not include NOTES.txt")
        missing_required = sorted(_REQUIRED_PROTECTED_PATHS.difference(artifact_paths))
        if missing_required:
            raise ValueError(
                "artifacts are missing required protected files: "
                + ", ".join(missing_required)
            )
        stage_names = tuple(stage.name for stage in self.stages)
        if len(set(stage_names)) != len(stage_names):
            raise ValueError("stages must not contain duplicate names")
        artifact_path_set = set(artifact_paths)
        for stage in self.stages:
            for artifact_path in stage.artifacts:
                if artifact_path not in artifact_path_set:
                    raise ValueError(
                        f"stage '{stage.name}' references an undeclared artifact '{artifact_path}'"
                    )
        return self


class ResultPackageLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_id: str = Field(min_length=71, max_length=71)
    path: str = Field(min_length=1)
    format_version: str = JELICA_PACKAGE_FORMAT_VERSION

    @field_validator("content_id")
    @classmethod
    def _normalize_content_id(cls, value: str) -> str:
        return _normalize_content_id(value)

    @field_validator("path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        return _normalize_filesystem_relative_path(value, allow_parent=True)

    @field_validator("format_version")
    @classmethod
    def _validate_format_version(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != JELICA_PACKAGE_FORMAT_VERSION:
            raise ValueError(
                f"format_version must be '{JELICA_PACKAGE_FORMAT_VERSION}'"
            )
        return normalized


class ResultPackageStageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = RESULT_PACKAGE_STAGE_MANIFEST_SCHEMA_VERSION
    stage_id: str = RESULT_PACKAGE_STAGE_ID
    task_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    format_version: str = JELICA_PACKAGE_FORMAT_VERSION
    task_status: ResultPackageTaskStatus
    content_id: str = Field(min_length=71, max_length=71)
    content_digest: str = Field(min_length=64, max_length=64)
    package_created_at: str = Field(min_length=1)
    prepared_package_relative_path: str = Field(min_length=1)
    published_package_relative_path: str = Field(min_length=1)
    task: ResultPackageTaskInfo
    result_name: str | None = None
    source_stage_ids: tuple[str, ...] = Field(default_factory=tuple)
    artifact_count: int = Field(ge=0)
    stage_count: int = Field(ge=0)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != RESULT_PACKAGE_STAGE_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported result-package stage manifest schema_version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != RESULT_PACKAGE_STAGE_ID:
            raise ValueError("invalid result-package stage identity")
        return normalized

    @field_validator("task_id", "job_id", "package_created_at")
    @classmethod
    def _normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("manifest fields must not be empty")
        return normalized

    @field_validator("config_hash", "content_digest")
    @classmethod
    def _normalize_sha256(cls, value: str) -> str:
        return _normalize_sha256_hex(value, field_name="sha256")

    @field_validator("format_version")
    @classmethod
    def _validate_format_version(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != JELICA_PACKAGE_FORMAT_VERSION:
            raise ValueError(
                f"format_version must be '{JELICA_PACKAGE_FORMAT_VERSION}'"
            )
        return normalized

    @field_validator("content_id")
    @classmethod
    def _normalize_content_id(cls, value: str) -> str:
        return _normalize_content_id(value)

    @field_validator("prepared_package_relative_path")
    @classmethod
    def _normalize_prepared_package_path(cls, value: str) -> str:
        return _normalize_filesystem_relative_path(value, allow_parent=False)

    @field_validator("published_package_relative_path")
    @classmethod
    def _normalize_published_package_path(cls, value: str) -> str:
        return _normalize_filesystem_relative_path(value, allow_parent=True)

    @field_validator("source_stage_ids")
    @classmethod
    def _normalize_source_stage_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value if item.strip() != "")
        if len(normalized) != len(value):
            raise ValueError("source_stage_ids must not contain empty values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("source_stage_ids must be unique")
        return normalized

    @model_validator(mode="after")
    def _validate_content(self) -> ResultPackageStageManifest:
        expected_content_id = f"sha256:{self.content_digest}"
        if self.content_id != expected_content_id:
            raise ValueError("content_id must match content_digest")
        if self.task.status is not self.task_status:
            raise ValueError("task.status must match task_status")
        if self.stage_count != len(self.source_stage_ids):
            raise ValueError("stage_count must match source_stage_ids length")
        return self

    @field_validator("result_name")
    @classmethod
    def _normalize_result_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_task_name(value)


def serialize_stable_json(payload: Mapping[str, object] | list[object]) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def write_model_json(*, path: Path, model: BaseModel) -> None:
    write_text_atomically(
        path=path,
        payload=serialize_stable_json(model.model_dump(mode="json")),
    )


def content_digest_from_content_id(content_id: str) -> str:
    normalized = _normalize_content_id(content_id)
    return normalized.split(":", maxsplit=1)[1]


def compute_content_id(
    *,
    artifacts: Iterable[ResultPackageArtifactInfo],
) -> str:
    records = [
        _ContentRecord(path=item.path, size=item.size, sha256=item.sha256)
        for item in artifacts
    ]
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item.path.encode("utf-8")):
        digest.update(record.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(record.sha256.encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def infer_media_type(path: str) -> str:
    normalized = _normalize_package_internal_path(path)
    suffix = PurePosixPath(normalized).suffix.lower()
    return _MEDIA_TYPES_BY_SUFFIX.get(suffix, "application/octet-stream")


def resolve_result_packages_directory(*, task_dir: Path) -> Path:
    env_value = os.environ.get(JELICA_HOME_ENV_VAR)
    if env_value is not None:
        stripped = env_value.strip()
        if stripped == "":
            raise ResultPackagePublicationError(
                f"environment variable {JELICA_HOME_ENV_VAR} is empty"
            )
        home = Path(stripped).expanduser()
        if not home.is_absolute():
            raise ResultPackagePublicationError(
                f"environment variable {JELICA_HOME_ENV_VAR} must be an absolute path"
            )
        return home / RESULT_PACKAGE_DIRECTORY_NAME

    for candidate in (task_dir, *task_dir.parents):
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate / RESULT_PACKAGE_DIRECTORY_NAME

    return task_dir.parent / RESULT_PACKAGE_DIRECTORY_NAME


def result_package_filename(
    *,
    content_digest: str,
    result_name: str | None = None,
    filename_stem: str | None = None,
) -> str:
    normalized_digest = _normalize_sha256_hex(
        content_digest, field_name="content_digest"
    )
    if filename_stem is not None and result_name is not None:
        raise ValueError("result_name and filename_stem are mutually exclusive")
    if filename_stem is not None:
        normalized_stem = _validate_explicit_result_package_stem(
            filename_stem=filename_stem,
            content_digest=normalized_digest,
        )
        return f"{normalized_stem}.jelica"
    if result_name is None:
        return f"{normalized_digest}.jelica"
    normalized_name = validate_task_name(result_name)
    return f"{normalized_name}__{normalized_digest}.jelica"


def parse_result_package_filename(filename: str) -> ParsedResultPackageFilename | None:
    if Path(filename).name != filename or not filename.endswith(".jelica"):
        return None
    stem = filename[: -len(".jelica")]
    if _CONTENT_DIGEST_PATTERN.fullmatch(stem):
        return ParsedResultPackageFilename(result_name=None, content_digest=stem)
    digest = stem[-64:]
    if stem.endswith(f"__{digest}"):
        result_name = stem[: -(64 + 2)]
    elif stem.endswith(f"_{digest}"):
        result_name = stem[: -(64 + 1)]
    else:
        return None
    if _CONTENT_DIGEST_PATTERN.fullmatch(digest) is None:
        return None
    try:
        normalized_name = validate_human_readable_name(result_name, max_length=None)
    except ValueError:
        return None
    if normalized_name != result_name:
        return None
    return ParsedResultPackageFilename(result_name=result_name, content_digest=digest)


def _is_supported_result_package_filename(filename: str) -> bool:
    if Path(filename).name != filename or not filename.endswith(".jelica"):
        return False
    stem = filename[: -len(".jelica")]
    return _RESULT_PACKAGE_FILENAME_STEM_PATTERN.fullmatch(stem) is not None


def _filename_contains_content_digest(filename: str, content_id: str) -> bool:
    digest = content_digest_from_content_id(content_id)
    stem = filename[: -len(".jelica")]
    return digest in stem


def result_package_target_path(
    *,
    task_dir: Path,
    content_digest: str,
    result_name: str | None = None,
    filename_stem: str | None = None,
) -> Path:
    return resolve_result_packages_directory(
        task_dir=task_dir
    ) / result_package_filename(
        content_digest=content_digest,
        result_name=result_name,
        filename_stem=filename_stem,
    )


def relative_package_path_from_task(*, task_dir: Path, package_path: Path) -> str:
    relative = os.path.relpath(package_path, start=task_dir)
    normalized = relative.replace("\\", "/")
    return _normalize_filesystem_relative_path(normalized, allow_parent=True)


def result_package_artifact_paths(
    manifest: ResultPackageStageManifest,
) -> tuple[str, ...]:
    _ = manifest
    return (RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,)


def load_result_package_stage_manifest(*, path: Path) -> ResultPackageStageManifest:
    payload = _load_json_object_from_file(path)
    try:
        return ResultPackageStageManifest.model_validate(payload)
    except Exception as error:
        raise ResultPackageValidationError(
            "result-package stage manifest is invalid"
        ) from error


def load_result_package_link(*, path: Path) -> ResultPackageLink:
    payload = _load_json_object_from_file(path)
    try:
        return ResultPackageLink.model_validate(payload)
    except Exception as error:
        raise ResultPackageValidationError("result-package link is invalid") from error


def write_result_package_link(*, task_dir: Path, link: ResultPackageLink) -> Path:
    link_path = task_dir / RESULT_PACKAGE_LINK_FILENAME
    write_model_json(path=link_path, model=link)
    return link_path


def validate_result_package_file(
    *,
    path: Path,
    expected_content_id: str | None = None,
    require_notes_absent: bool = False,
) -> ValidatedResultPackage:
    if path.is_symlink():
        raise ResultPackageValidationError(
            f"result package must not be a symbolic link: '{path}'"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ResultPackageValidationError(
            f"result package is missing: '{path}'"
        ) from error
    if not resolved.is_file():
        raise ResultPackageValidationError(
            f"result package is not a regular file: '{resolved}'"
        )
    if resolved.is_symlink():
        raise ResultPackageValidationError(
            f"result package must not be a symbolic link: '{resolved}'"
        )

    try:
        with zipfile.ZipFile(resolved, mode="r") as archive:
            return _validate_archive(
                archive=archive,
                expected_content_id=expected_content_id,
                require_notes_absent=require_notes_absent,
            )
    except ResultPackageValidationError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise ResultPackageValidationError(
            "result package is not a readable ZIP archive"
        ) from error


def publish_prepared_result_package(
    *,
    prepared_package_path: Path,
    task_dir: Path,
    stage_manifest: ResultPackageStageManifest,
) -> Path:
    prepared = prepared_package_path.resolve(strict=False)
    if not prepared.is_file():
        raise ResultPackagePublicationError(
            f"prepared result package is missing: '{prepared_package_path}'"
        )
    if prepared.is_symlink():
        raise ResultPackagePublicationError(
            "prepared result package must not be a symbolic link"
        )

    validate_result_package_file(
        path=prepared,
        expected_content_id=stage_manifest.content_id,
        require_notes_absent=True,
    )

    target_path = result_package_target_path(
        task_dir=task_dir,
        content_digest=stage_manifest.content_digest,
        result_name=stage_manifest.result_name,
    )
    expected_relative_path = relative_package_path_from_task(
        task_dir=task_dir,
        package_path=target_path,
    )
    if expected_relative_path != stage_manifest.published_package_relative_path:
        raise ResultPackagePublicationError(
            "result-package relative link path is inconsistent with publication target"
        )

    result_packages_dir = resolve_result_packages_directory(task_dir=task_dir)
    try:
        result_packages_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ResultPackagePublicationError(
            "result package store directory cannot be created"
        ) from error

    lock_fd: int | None = None
    lock_path: Path | None = None
    published_path: Path
    try:
        try:
            lock_fd, lock_path = _acquire_result_package_store_lock(
                directory=result_packages_dir
            )
        except ResultPackageLibraryError as error:
            raise ResultPackagePublicationError(
                "result package store lock cannot be acquired"
            ) from error

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            try:
                validate_result_package_file(
                    path=target_path,
                    expected_content_id=stage_manifest.content_id,
                    require_notes_absent=False,
                )
            except ResultPackageValidationError as error:
                raise ResultPackagePublicationError(
                    "existing result package is corrupted or inconsistent with expected content"
                ) from error
            published_path = target_path
        else:
            try:
                existing_path = _find_stored_package_by_content_id(
                    result_packages_dir=result_packages_dir,
                    content_id=stage_manifest.content_id,
                    validator=JelicaPackageValidator(),
                )
            except ResultPackageLibraryError as error:
                raise ResultPackagePublicationError(
                    "existing result package store is ambiguous or invalid"
                ) from error
            if existing_path is not None:
                published_path = existing_path
            else:
                try:
                    _copy_file_atomically(source_path=prepared, target_path=target_path)
                except FileExistsError:
                    try:
                        validate_result_package_file(
                            path=target_path,
                            expected_content_id=stage_manifest.content_id,
                            require_notes_absent=False,
                        )
                    except ResultPackageValidationError as error:
                        raise ResultPackagePublicationError(
                            "concurrent publication produced an invalid result package"
                        ) from error
                    published_path = target_path
                except OSError as error:
                    raise ResultPackagePublicationError(
                        "result package could not be atomically published"
                    ) from error

                try:
                    validate_result_package_file(
                        path=target_path,
                        expected_content_id=stage_manifest.content_id,
                        require_notes_absent=False,
                    )
                except ResultPackageValidationError as error:
                    raise ResultPackagePublicationError(
                        "published result package failed integrity validation"
                    ) from error
                published_path = target_path
    finally:
        if lock_path is not None:
            _release_result_package_store_lock(lock_fd=lock_fd, lock_path=lock_path)

    # The prepared package is removed only after successful central publication.
    # On failures it is intentionally preserved for potential retry paths.
    _cleanup_prepared_package_source(prepared_path=prepared)
    return published_path


def import_result_package(
    *,
    source_path: Path,
    core_config_service: CoreConfigService,
    result_name: str | None = None,
    filename_stem: str | None = None,
) -> ImportedResultPackage:
    validator = JelicaPackageValidator()
    source_result = validator.validate(source_path)
    if not source_result.valid or source_result.content_id is None:
        first_code = (
            source_result.errors[0].code.value
            if source_result.errors
            else "validation_failed"
        )
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_SOURCE_PACKAGE,
            message=f"Source package failed validation ({first_code})",
        )

    content_id = source_result.content_id
    content_digest = content_digest_from_content_id(content_id)
    result_packages_dir = _resolve_result_packages_directory_from_core_config_service(
        core_config_service=core_config_service
    )
    try:
        result_packages_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
            message="Result package store directory cannot be created",
        ) from error

    try:
        target_filename = result_package_filename(
            content_digest=content_digest,
            result_name=result_name,
            filename_stem=filename_stem,
        )
    except ValueError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_SOURCE_PACKAGE,
            message=f"Requested result package name is invalid: {error}",
        ) from error
    target_path = result_packages_dir / target_filename
    if target_path.exists() and not _is_existing_regular_file(target_path):
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
            message="Existing local package path is not a regular file",
        )
    if _is_existing_regular_file(target_path):
        return _reuse_existing_imported_package(
            validator=validator,
            source_path=source_path,
            target_path=target_path,
            expected_content_id=content_id,
        )

    existing_path = _find_stored_package_by_content_id(
        result_packages_dir=result_packages_dir,
        content_id=content_id,
        validator=validator,
    )
    if existing_path is not None:
        return _reuse_existing_imported_package(
            validator=validator,
            source_path=source_path,
            target_path=existing_path,
            expected_content_id=content_id,
        )

    temporary_path = _build_import_temporary_path(
        result_packages_dir=result_packages_dir,
        content_digest=content_digest,
    )
    lock_fd: int | None = None
    lock_path: Path | None = None
    try:
        _copy_file_atomically(source_path=source_path, target_path=temporary_path)
        temporary_result = validator.validate(temporary_path)
        if not temporary_result.valid or temporary_result.content_id != content_id:
            first_code = (
                temporary_result.errors[0].code.value
                if temporary_result.errors
                else "validation_failed"
            )
            raise ResultPackageLibraryError(
                code=ResultPackageLibraryErrorCode.INVALID_SOURCE_PACKAGE,
                message=f"Temporary copy failed validation ({first_code})",
            )

        lock_fd, lock_path = _acquire_result_package_store_lock(
            directory=result_packages_dir
        )
        if target_path.exists() and not _is_existing_regular_file(target_path):
            raise ResultPackageLibraryError(
                code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
                message="Existing local package path is not a regular file",
            )
        if _is_existing_regular_file(target_path):
            return _reuse_existing_imported_package(
                validator=validator,
                source_path=source_path,
                target_path=target_path,
                expected_content_id=content_id,
            )
        existing_path = _find_stored_package_by_content_id(
            result_packages_dir=result_packages_dir,
            content_id=content_id,
            validator=validator,
        )
        if existing_path is not None:
            return _reuse_existing_imported_package(
                validator=validator,
                source_path=source_path,
                target_path=existing_path,
                expected_content_id=content_id,
            )
        try:
            os.replace(temporary_path, target_path)
        except FileExistsError:
            return _reuse_existing_imported_package(
                validator=validator,
                source_path=source_path,
                target_path=target_path,
                expected_content_id=content_id,
            )
        except OSError as error:
            raise ResultPackageLibraryError(
                code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
                message="Result package cannot be published into local store",
            ) from error
    finally:
        if lock_path is not None:
            _release_result_package_store_lock(lock_fd=lock_fd, lock_path=lock_path)
        temporary_path.unlink(missing_ok=True)

    published_result = validator.validate(target_path)
    if not published_result.valid or published_result.content_id != content_id:
        target_path.unlink(missing_ok=True)
        first_code = (
            published_result.errors[0].code.value
            if published_result.errors
            else "validation_failed"
        )
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
            message=f"Published package failed post-import validation ({first_code})",
        )
    return ImportedResultPackage(
        content_id=content_id, path=target_path, already_exists=False
    )


def delete_result_package(
    *,
    package_path: Path,
    core_config_service: CoreConfigService,
    force: bool = False,
) -> DeletedResultPackage:
    """Delete one direct child of the central result-package store.

    The same lock used by import/publication is held while the entry is
    rechecked and removed.  Symlinks are never followed; with ``force`` a
    symlink entry itself may be removed, which is useful for corrupt-store
    cleanup.
    """

    result_packages_dir = _resolve_result_packages_directory_from_core_config_service(
        core_config_service=core_config_service
    )
    candidate = _validate_result_store_entry_path(
        result_packages_dir=result_packages_dir,
        package_path=package_path,
    )
    if not candidate.exists() and not candidate.is_symlink():
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message="Result package was not found",
        )
    _validate_deletable_result_package(
        candidate=candidate,
        force=force,
    )

    lock_fd: int | None = None
    lock_path: Path | None = None
    try:
        lock_fd, lock_path = _acquire_result_package_store_lock(
            directory=result_packages_dir
        )
        if not candidate.exists() and not candidate.is_symlink():
            return DeletedResultPackage(path=candidate, already_missing=True)
        _validate_deletable_result_package(candidate=candidate, force=force)
        try:
            candidate.unlink()
        except FileNotFoundError:
            return DeletedResultPackage(path=candidate, already_missing=True)
        except OSError as error:
            raise ResultPackageLibraryError(
                code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
                message=f"Result package could not be deleted: {error}",
            ) from error
        return DeletedResultPackage(path=candidate)
    finally:
        if lock_path is not None:
            _release_result_package_store_lock(lock_fd=lock_fd, lock_path=lock_path)


def delete_all_result_packages(
    *,
    core_config_service: CoreConfigService,
) -> tuple[DeletedResultPackage, ...]:
    """Delete every direct ``.jelica`` store entry, including invalid ones."""

    result_packages_dir = _resolve_result_packages_directory_from_core_config_service(
        core_config_service=core_config_service
    )
    if not result_packages_dir.exists():
        return tuple()
    if result_packages_dir.is_symlink() or not result_packages_dir.is_dir():
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
            message="Result package store is not a regular directory",
        )

    lock_fd: int | None = None
    lock_path: Path | None = None
    deleted: list[DeletedResultPackage] = []
    try:
        lock_fd, lock_path = _acquire_result_package_store_lock(
            directory=result_packages_dir
        )
        for entry in sorted(result_packages_dir.iterdir(), key=lambda item: item.name):
            if entry.suffix != ".jelica":
                continue
            if entry.is_dir() and not entry.is_symlink():
                continue
            try:
                entry.unlink()
            except FileNotFoundError:
                deleted.append(DeletedResultPackage(path=entry, already_missing=True))
            except OSError as error:
                raise ResultPackageLibraryError(
                    code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
                    message=f"Result package could not be deleted: {error}",
                ) from error
            else:
                deleted.append(DeletedResultPackage(path=entry))
    finally:
        if lock_path is not None:
            _release_result_package_store_lock(lock_fd=lock_fd, lock_path=lock_path)
    return tuple(deleted)


def list_result_packages(
    *,
    core_config_service: CoreConfigService,
) -> ListedResultPackages:
    result_packages_dir = _resolve_result_packages_directory_from_core_config_service(
        core_config_service=core_config_service
    )
    if not result_packages_dir.exists():
        return ListedResultPackages(packages=tuple(), has_invalid_entries=False)
    if result_packages_dir.is_symlink() or not result_packages_dir.is_dir():
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
            message="Result package store path is not a directory",
        )

    entries: list[ListedResultPackage] = []
    try:
        directory_entries = sorted(
            result_packages_dir.iterdir(), key=lambda item: item.name
        )
    except OSError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
            message="Result package store cannot be read",
        ) from error
    for entry_path in directory_entries:
        if entry_path.is_dir():
            continue
        if entry_path.suffix != ".jelica":
            continue
        entries.append(_build_result_package_catalog_entry(path=entry_path))

    task_names = _resolve_result_package_task_names(
        core_config_service=core_config_service,
        task_ids={
            entry.task_id for entry in entries if entry.valid and entry.task_id is not None
        },
    )
    entries = [
        replace(entry, task_name=task_names.get(entry.task_id))
        for entry in entries
    ]

    sorted_entries = tuple(
        sorted(
            entries,
            key=lambda item: (
                item.content_id is None,
                item.content_id or "",
                item.file_name,
            ),
        )
    )
    has_invalid_entries = any(not item.valid for item in sorted_entries)
    return ListedResultPackages(
        packages=sorted_entries,
        has_invalid_entries=has_invalid_entries,
    )


def resolve_result_package_path(
    *,
    task_or_content_ref: str,
    core_config_service: CoreConfigService,
) -> ResolvedResultPackagePath:
    normalized_ref = task_or_content_ref.strip()
    if normalized_ref == "":
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message="Result package reference must not be empty",
        )

    result_packages_dir = _resolve_result_packages_directory_from_core_config_service(
        core_config_service=core_config_service
    )
    digest = _parse_content_digest_reference(reference=normalized_ref)
    if digest is not None:
        return _resolve_stored_package_by_digest(
            result_packages_dir=result_packages_dir,
            digest=digest,
        )
    if normalized_ref.endswith(".jelica"):
        return _resolve_stored_package_by_filename(
            result_packages_dir=result_packages_dir,
            filename=normalized_ref,
        )

    try:
        resolved_core_config = core_config_service.require_initialized_config()
    except CoreConfigError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.TASK_NOT_FOUND,
            message="Task registry is unavailable",
        ) from error

    registry_service = AnalyticalTaskRegistryService(
        database_path=resolved_core_config.database_path
    )
    try:
        task_record = registry_service.resolve_task_reference(
            task_reference=normalized_ref
        )
    except AnalyticalTaskNotFoundError as error:
        try:
            return _resolve_stored_package_by_result_name(
                result_packages_dir=result_packages_dir,
                result_name=normalized_ref,
            )
        except ResultPackageLibraryError as result_error:
            if result_error.code is not ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND:
                raise
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.TASK_NOT_FOUND,
            message=f"Task '{normalized_ref}' was not found",
        ) from error
    except AnalyticalTaskRegistryError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.TASK_NOT_FOUND,
            message="Task registry is unavailable",
        ) from error

    try:
        task_dir = resolve_task_workspace_dir(
            tasks_dir=resolved_core_config.tasks_dir,
            task_dir_relative_path=task_record.task_dir_relative_path,
            task_id=task_record.task_id,
        )
    except TaskWorkspaceDeleteError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.TASK_NOT_FOUND,
            message="Task workspace path is invalid",
        ) from error

    link_path = task_dir / RESULT_PACKAGE_LINK_FILENAME
    if not link_path.is_file():
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.TASK_HAS_NO_RESULT_PACKAGE,
            message="Task has no result package link",
        )
    try:
        link = load_result_package_link(path=link_path)
    except ResultPackageValidationError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_RESULT_PACKAGE_LINK,
            message="Task result package link is invalid",
        ) from error

    raw_link_target = task_dir / Path(link.path)
    if raw_link_target.is_symlink():
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.UNSAFE_RESULT_PACKAGE_LINK,
            message="Task result package link must not target a symbolic link",
        )
    link_target = raw_link_target.resolve(strict=False)
    store_root = result_packages_dir.resolve(strict=False)
    try:
        link_target.relative_to(store_root)
    except ValueError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.UNSAFE_RESULT_PACKAGE_LINK,
            message="Task result package link points outside result package store",
        ) from error

    if link_target.suffix != ".jelica":
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_RESULT_PACKAGE_LINK,
            message="Task result package link must target a .jelica file",
        )

    if not _is_existing_regular_file(link_target):
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message="Result package referenced by task was not found",
        )
    try:
        validate_result_package_file(
            path=link_target, expected_content_id=link.content_id
        )
    except ResultPackageValidationError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_RESULT_PACKAGE_LINK,
            message="Task result package content does not match content_id",
        ) from error
    return ResolvedResultPackagePath(content_id=link.content_id, path=link_target)


def _resolve_stored_package_by_digest(
    *,
    result_packages_dir: Path,
    digest: str,
) -> ResolvedResultPackagePath:
    validator = JelicaPackageValidator()
    package_path = _find_stored_package_by_content_id(
        result_packages_dir=result_packages_dir,
        content_id=f"sha256:{digest}",
        validator=validator,
    )
    if package_path is None:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message="Result package was not found",
        )
    return ResolvedResultPackagePath(content_id=f"sha256:{digest}", path=package_path)


def _resolve_stored_package_by_filename(
    *,
    result_packages_dir: Path,
    filename: str,
) -> ResolvedResultPackagePath:
    if Path(filename).name != filename:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message="Result package filename reference must not contain directories",
        )
    if not _is_supported_result_package_filename(filename):
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message="Result package filename is not a supported .jelica name",
        )
    package_path = result_packages_dir / filename
    if not _is_existing_regular_file(package_path):
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message="Result package was not found",
        )
    try:
        validated = validate_result_package_file(path=package_path)
    except ResultPackageValidationError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
            message="Result package is invalid or inconsistent with its manifest",
        ) from error
    if not _filename_contains_content_digest(filename, validated.content_id):
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
            message="Result package filename does not contain its content digest",
        )
    return ResolvedResultPackagePath(
        content_id=validated.content_id,
        path=package_path,
    )


def _resolve_stored_package_by_result_name(
    *,
    result_packages_dir: Path,
    result_name: str,
) -> ResolvedResultPackagePath:
    try:
        normalized_name = validate_human_readable_name(result_name, max_length=None)
    except ValueError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message=f"Result package name '{result_name}' is invalid",
        ) from error
    validator = JelicaPackageValidator()
    matches: list[tuple[Path, str]] = []
    for candidate in _iter_result_package_files(
        result_packages_dir=result_packages_dir
    ):
        parsed = parse_result_package_filename(candidate.name)
        if parsed is not None:
            candidate_result_name = parsed.result_name
            expected_content_id = f"sha256:{parsed.content_digest}"
        elif _is_supported_result_package_filename(candidate.name):
            candidate_result_name = candidate.stem
            expected_content_id = None
        else:
            continue
        if (
            candidate_result_name is None
            or candidate_result_name.casefold() != normalized_name.casefold()
        ):
            continue
        result = validator.validate(candidate)
        if result.valid and (
            expected_content_id is None or result.content_id == expected_content_id
        ) and result.content_id is not None:
            if not _filename_contains_content_digest(candidate.name, result.content_id):
                continue
            matches.append((candidate, result.content_id))
    if not matches:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message=f"Result package named '{normalized_name}' was not found",
        )
    if len(matches) > 1:
        details = ", ".join(
            f"{path.name} ({content_id})" for path, content_id in matches
        )
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.AMBIGUOUS_RESULT_PACKAGE,
            message=f"Result package name '{normalized_name}' is ambiguous: {details}",
        )
    package_path, content_id = matches[0]
    return ResolvedResultPackagePath(content_id=content_id, path=package_path)


def _iter_result_package_files(*, result_packages_dir: Path) -> tuple[Path, ...]:
    if not result_packages_dir.is_dir() or result_packages_dir.is_symlink():
        return tuple()
    return tuple(
        sorted(
            (
                entry
                for entry in result_packages_dir.iterdir()
                if entry.suffix == ".jelica" and _is_existing_regular_file(entry)
            ),
            key=lambda item: item.name,
        )
    )


def _find_stored_package_by_content_id(
    *,
    result_packages_dir: Path,
    content_id: str,
    validator: JelicaPackageValidator,
) -> Path | None:
    matches: list[Path] = []
    for candidate in _iter_result_package_files(
        result_packages_dir=result_packages_dir
    ):
        result = validator.validate(candidate)
        if (
            result.valid
            and result.content_id == content_id
            and _filename_contains_content_digest(candidate.name, content_id)
        ):
            matches.append(candidate)
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.AMBIGUOUS_RESULT_PACKAGE,
            message=f"More than one stored package has content_id {content_id}: {names}",
        )
    return matches[0] if matches else None


def _cleanup_prepared_package_source(*, prepared_path: Path) -> None:
    try:
        prepared_path.unlink(missing_ok=True)
    except OSError as error:
        raise ResultPackagePublicationError(
            "prepared result package could not be removed after publication"
        ) from error

    prepared_root = _find_prepared_root(prepared_path=prepared_path)
    if prepared_root is None or not prepared_root.exists():
        return
    try:
        shutil.rmtree(prepared_root, ignore_errors=False)
    except OSError as error:
        raise ResultPackagePublicationError(
            "prepared result package directory cleanup failed"
        ) from error


def _find_prepared_root(*, prepared_path: Path) -> Path | None:
    for parent in prepared_path.parents:
        if parent.name == RESULT_PACKAGE_PREPARED_DIRNAME:
            return parent
    return None


def _validate_result_store_entry_path(
    *,
    result_packages_dir: Path,
    package_path: Path,
) -> Path:
    if result_packages_dir.is_symlink():
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.UNSAFE_RESULT_PACKAGE_LINK,
            message="Result package store must not be a symbolic link",
        )
    store_root = result_packages_dir.resolve(strict=False)
    candidate = Path(package_path)
    if candidate.name == "" or candidate.name != candidate.stem + candidate.suffix:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message="Result package reference must identify one store entry",
        )
    if candidate.name == ".jelica" or candidate.suffix != ".jelica":
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND,
            message="Result package filename must end with .jelica",
        )
    if candidate.parent.resolve(strict=False) != store_root:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.UNSAFE_RESULT_PACKAGE_LINK,
            message="Result package deletion target must be a direct store entry",
        )
    if candidate.name != Path(candidate.name).name:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.UNSAFE_RESULT_PACKAGE_LINK,
            message="Result package deletion target must not contain directories",
        )
    return store_root / candidate.name


def _validate_deletable_result_package(*, candidate: Path, force: bool) -> None:
    if candidate.is_symlink():
        if not force:
            raise ResultPackageLibraryError(
                code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
                message="Symbolic-link result package entries require --force",
            )
        return
    if not candidate.is_file():
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
            message="Result package is not a regular file",
        )
    try:
        validate_result_package_file(path=candidate)
    except ResultPackageValidationError as error:
        if not force:
            raise ResultPackageLibraryError(
                code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
                message="Result package is invalid; use --force to remove it",
            ) from error


def _resolve_result_packages_directory_from_core_config_service(
    *,
    core_config_service: CoreConfigService,
) -> Path:
    try:
        jelica_home = core_config_service.get_jelica_home()
    except CoreConfigError as error:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
            message="JELICA home directory cannot be resolved",
        ) from error
    return jelica_home / RESULT_PACKAGE_DIRECTORY_NAME


def _reuse_existing_imported_package(
    *,
    validator: JelicaPackageValidator,
    source_path: Path,
    target_path: Path,
    expected_content_id: str,
) -> ImportedResultPackage:
    existing_result = validator.validate(target_path)
    if not existing_result.valid or existing_result.content_id != expected_content_id:
        first_code = (
            existing_result.errors[0].code.value
            if existing_result.errors
            else "validation_failed"
        )
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
            message=f"Existing local package is invalid ({first_code})",
        )

    source_notes = _load_optional_notes_bytes(
        package_path=source_path,
        error_code=ResultPackageLibraryErrorCode.INVALID_SOURCE_PACKAGE,
    )
    existing_notes = _load_optional_notes_bytes(
        package_path=target_path,
        error_code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
    )
    if source_notes != existing_notes:
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.NOTES_CONFLICT,
            message="NOTES.txt conflicts with the existing local package",
        )
    return ImportedResultPackage(
        content_id=expected_content_id,
        path=target_path,
        already_exists=True,
    )


def _load_optional_notes_bytes(
    *,
    package_path: Path,
    error_code: ResultPackageLibraryErrorCode,
) -> bytes | None:
    try:
        with JelicaPackageReader(path=package_path) as reader:
            notes_stream = reader.open_notes()
            if notes_stream is None:
                return None
            with notes_stream:
                return notes_stream.read()
    except (JelicaPackageReaderError, OSError, zipfile.BadZipFile) as error:
        raise ResultPackageLibraryError(
            code=error_code,
            message="NOTES.txt cannot be read",
        ) from error


def _build_import_temporary_path(
    *,
    result_packages_dir: Path,
    content_digest: str,
) -> Path:
    suffix = f"{time.monotonic_ns()}-{os.getpid()}"
    return result_packages_dir / f".{content_digest}.{suffix}.import.tmp"


def _acquire_result_package_store_lock(*, directory: Path) -> tuple[int, Path]:
    lock_path = directory / _RESULT_PACKAGE_IMPORT_LOCK_FILENAME
    deadline = time.monotonic() + _RESULT_PACKAGE_IMPORT_LOCK_WAIT_SECONDS
    while True:
        try:
            lock_fd = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            return lock_fd, lock_path
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ResultPackageLibraryError(
                    code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
                    message="Result package store is busy",
                ) from None
            time.sleep(_RESULT_PACKAGE_IMPORT_LOCK_POLL_SECONDS)
        except OSError as error:
            raise ResultPackageLibraryError(
                code=ResultPackageLibraryErrorCode.IMPORT_IO_ERROR,
                message="Result package store lock cannot be acquired",
            ) from error


def _release_result_package_store_lock(*, lock_fd: int | None, lock_path: Path) -> None:
    if lock_fd is not None:
        try:
            os.close(lock_fd)
        except OSError:
            pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _is_existing_regular_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _build_result_package_catalog_entry(*, path: Path) -> ListedResultPackage:
    if (
        path.is_symlink()
        or not path.is_file()
        or not _is_supported_result_package_filename(path.name)
    ):
        return ListedResultPackage(
            file_name=path.name,
            path=path,
            content_id=None,
            task_id=None,
            status="invalid",
            format_version=None,
            valid=False,
            result_name=None,
            issue_code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
        )

    try:
        with JelicaPackageReader(path=path) as reader:
            manifest = reader.read_manifest()
    except (JelicaPackageReaderError, OSError, zipfile.BadZipFile):
        return ListedResultPackage(
            file_name=path.name,
            path=path,
            content_id=None,
            task_id=None,
            status="invalid",
            format_version=None,
            valid=False,
            result_name=None,
            issue_code=ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE,
        )

    parsed_filename = parse_result_package_filename(path.name)
    expected_digest = content_digest_from_content_id(manifest.content_id)
    is_name_consistent = (
        parsed_filename is None
        and _filename_contains_content_digest(path.name, manifest.content_id)
    ) or (
        parsed_filename is not None
        and parsed_filename.content_digest == expected_digest
    )
    return ListedResultPackage(
        file_name=path.name,
        path=path,
        content_id=manifest.content_id,
        task_id=manifest.task.task_id,
        status=manifest.task.status.value if is_name_consistent else "invalid",
        format_version=manifest.format_version,
        valid=is_name_consistent,
        result_name=(
            parsed_filename.result_name
            if parsed_filename is not None and is_name_consistent
            else path.stem
            if is_name_consistent
            else None
        ),
        issue_code=(
            None
            if is_name_consistent
            else ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE
        ),
    )


def _resolve_result_package_task_names(
    *,
    core_config_service: CoreConfigService,
    task_ids: set[str],
) -> dict[str, str | None]:
    if not task_ids:
        return {}
    try:
        resolved_config = core_config_service.require_initialized_config()
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
    except (CoreConfigError, AnalyticalTaskRegistryError):
        return {}

    task_names: dict[str, str | None] = {}
    for task_id in task_ids:
        try:
            task_names[task_id] = registry_service.get_task(task_id=task_id).name
        except AnalyticalTaskRegistryError:
            task_names[task_id] = None
    return task_names


def _parse_content_digest_reference(*, reference: str) -> str | None:
    if _CONTENT_DIGEST_PATTERN.fullmatch(reference):
        return reference
    match = _CONTENT_ID_PATTERN.fullmatch(reference)
    if match is None:
        return None
    return match.group(1)


def _validate_archive(
    *,
    archive: zipfile.ZipFile,
    expected_content_id: str | None,
    require_notes_absent: bool,
) -> ValidatedResultPackage:
    file_infos = [info for info in archive.infolist() if not info.is_dir()]
    names: list[str] = []
    info_by_name: dict[str, zipfile.ZipInfo] = {}
    for info in file_infos:
        _validate_zip_member_name(info.filename)
        if _zip_info_is_symlink(info):
            raise ResultPackageValidationError(
                f"ZIP entry '{info.filename}' must not be a symbolic link"
            )
        if info.filename in info_by_name:
            raise ResultPackageValidationError(
                f"ZIP archive contains duplicate entries: '{info.filename}'"
            )
        info_by_name[info.filename] = info
        names.append(info.filename)

    if JELICA_PACKAGE_MANIFEST_PATH not in info_by_name:
        raise ResultPackageValidationError("ZIP archive is missing manifest.json")
    try:
        manifest_payload_raw = archive.read(JELICA_PACKAGE_MANIFEST_PATH)
    except KeyError as error:
        raise ResultPackageValidationError(
            "ZIP archive is missing manifest.json"
        ) from error
    manifest = _load_package_manifest(payload=manifest_payload_raw)

    has_notes = JELICA_PACKAGE_NOTES_PATH in info_by_name
    if require_notes_absent and has_notes:
        raise ResultPackageValidationError(
            "automatically generated result packages must not include NOTES.txt"
        )

    expected_files = {
        JELICA_PACKAGE_MANIFEST_PATH,
        *(item.path for item in manifest.artifacts),
    }
    if has_notes:
        expected_files.add(JELICA_PACKAGE_NOTES_PATH)
    missing_files = sorted(expected_files.difference(names))
    if missing_files:
        joined = ", ".join(missing_files)
        raise ResultPackageValidationError(
            f"result package is missing declared files: {joined}"
        )
    unexpected_files = sorted(set(names).difference(expected_files))
    if unexpected_files:
        joined = ", ".join(unexpected_files)
        raise ResultPackageValidationError(
            f"result package contains undeclared files: {joined}"
        )

    observed_records: list[_ContentRecord] = []
    for artifact in manifest.artifacts:
        info = info_by_name.get(artifact.path)
        if info is None:
            raise ResultPackageValidationError(
                f"declared artifact is missing from ZIP archive: '{artifact.path}'"
            )
        if info.file_size != artifact.size:
            raise ResultPackageValidationError(
                f"declared artifact size mismatch for '{artifact.path}'"
            )
        sha256 = _sha256_for_zip_entry(archive=archive, entry_path=artifact.path)
        if sha256 != artifact.sha256:
            raise ResultPackageValidationError(
                f"declared artifact digest mismatch for '{artifact.path}'"
            )
        observed_records.append(
            _ContentRecord(path=artifact.path, size=artifact.size, sha256=sha256)
        )

    computed_content_id = compute_content_id(
        artifacts=tuple(
            ResultPackageArtifactInfo(
                path=item.path,
                stage=None,
                media_type="application/octet-stream",
                size=item.size,
                sha256=item.sha256,
            )
            for item in observed_records
        )
    )
    if computed_content_id != manifest.content_id:
        raise ResultPackageValidationError(
            "manifest content_id does not match protected artifact content"
        )
    if expected_content_id is not None:
        normalized_expected = _normalize_content_id(expected_content_id)
        if computed_content_id != normalized_expected:
            raise ResultPackageValidationError(
                "result package content_id does not match expected content_id"
            )
    return ValidatedResultPackage(
        manifest=manifest,
        content_id=computed_content_id,
        has_notes=has_notes,
    )


def _load_package_manifest(*, payload: bytes) -> JelicaPackageManifest:
    try:
        decoded = payload.decode("utf-8")
    except UnicodeError as error:
        raise ResultPackageValidationError("manifest.json must be UTF-8") from error
    loaded = _load_json_object(decoded)
    try:
        return JelicaPackageManifest.model_validate(loaded)
    except Exception as error:
        raise ResultPackageValidationError("manifest.json is invalid") from error


def _copy_file_atomically(*, source_path: Path, target_path: Path) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=target_path.parent,
            prefix=f"{target_path.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            with source_path.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, temporary_file, length=_CHUNK_SIZE)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if target_path.exists():
            raise FileExistsError(str(target_path))
        os.replace(temporary_path, target_path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _load_json_object_from_file(path: Path) -> dict[str, object]:
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ResultPackageValidationError(
            f"cannot read JSON document '{path}'"
        ) from error
    loaded = _load_json_object(payload)
    return {str(key): value for key, value in loaded.items()}


def _load_json_object(payload: str) -> dict[str, object]:
    try:
        loaded = json.loads(payload, parse_constant=_reject_non_finite_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ResultPackageValidationError("invalid JSON payload") from error
    if not isinstance(loaded, dict):
        raise ResultPackageValidationError("JSON payload must be an object")
    return {str(key): value for key, value in loaded.items()}


def _reject_non_finite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _sha256_for_zip_entry(*, archive: zipfile.ZipFile, entry_path: str) -> str:
    digest = hashlib.sha256()
    try:
        with archive.open(entry_path, mode="r") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as error:
        raise ResultPackageValidationError(
            f"cannot read ZIP entry '{entry_path}'"
        ) from error
    return digest.hexdigest()


def _zip_info_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _zip_info_is_non_regular_entry(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type == 0:
        return False
    return file_type != stat.S_IFREG


def _validate_zip_member_name(value: str) -> str:
    normalized = _normalize_package_internal_path(value)
    if normalized != value:
        raise ResultPackageValidationError(
            f"ZIP entry path is not normalized: '{value}'"
        )
    return normalized


def _normalize_content_id(value: str) -> str:
    normalized = value.strip()
    if not normalized.startswith("sha256:"):
        raise ValueError("content_id must use the 'sha256:' prefix")
    digest = normalized.split(":", maxsplit=1)[1]
    normalized_digest = _normalize_sha256_hex(digest, field_name="content_id digest")
    return f"sha256:{normalized_digest}"


def _normalize_sha256_hex(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{field_name} must not be empty")
    if normalized != normalized.lower():
        raise ValueError(f"{field_name} must use lowercase hexadecimal symbols")
    if len(normalized) != 64:
        raise ValueError(f"{field_name} must be a 64-character SHA-256 digest")
    try:
        int(normalized, 16)
    except ValueError as error:
        raise ValueError(f"{field_name} must be hexadecimal") from error
    return normalized


def _validate_explicit_result_package_stem(
    *,
    filename_stem: str,
    content_digest: str,
) -> str:
    normalized = filename_stem.strip()
    if normalized == "":
        raise ValueError("result package filename stem must not be empty")
    if (
        "/" in normalized
        or "\\" in normalized
        or _RESULT_PACKAGE_FILENAME_STEM_PATTERN.fullmatch(normalized) is None
    ):
        raise ValueError("result package filename stem contains invalid characters")
    if content_digest not in normalized:
        raise ValueError("result package filename stem must include content digest")
    return normalized


def _normalize_package_internal_path(value: str) -> str:
    return _normalize_filesystem_relative_path(value, allow_parent=False)


def _normalize_filesystem_relative_path(value: str, *, allow_parent: bool) -> str:
    normalized = value.strip().replace("\\", "/")
    if normalized == "":
        raise ValueError("path must not be empty")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if posix.is_absolute() or windows.is_absolute():
        raise ValueError("path must be relative")
    if normalized in {".", ".."}:
        raise ValueError("path must not be '.' or '..'")
    if "//" in normalized:
        raise ValueError("path must not contain empty components")
    parts = normalized.split("/")
    for part in parts:
        if part in {"", "."}:
            raise ValueError("path must not contain empty or '.' components")
        if not allow_parent and part == "..":
            raise ValueError("path must not contain '..' components")
    return "/".join(parts)


__all__ = [
    "JELICA_PACKAGE_CONFIGURATION_PATH",
    "JELICA_PACKAGE_FORMAT",
    "JELICA_PACKAGE_FORMAT_VERSION",
    "JELICA_PACKAGE_INPUT_MANIFEST_PATH",
    "JELICA_PACKAGE_MANIFEST_PATH",
    "JELICA_PACKAGE_NORMALIZED_FASTA_PATH",
    "JELICA_PACKAGE_NOTES_PATH",
    "JELICA_PACKAGE_TASK_PATH",
    "RESULT_PACKAGE_DIRECTORY_NAME",
    "RESULT_PACKAGE_LINK_FILENAME",
    "RESULT_PACKAGE_PREPARED_DIRNAME",
    "RESULT_PACKAGE_STAGE_ID",
    "RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH",
    "JelicaPackageReader",
    "JelicaPackageReaderError",
    "JelicaPackageValidationIssue",
    "JelicaPackageValidationIssueCode",
    "JelicaPackageValidationResult",
    "JelicaPackageValidator",
    "ImportedResultPackage",
    "ParsedResultPackageFilename",
    "ListedResultPackage",
    "ListedResultPackages",
    "ResolvedResultPackagePath",
    "ResultPackageArtifactInfo",
    "ResultPackageLibraryError",
    "ResultPackageLibraryErrorCode",
    "ResultPackageLink",
    "ResultPackageProducerInfo",
    "ResultPackagePublicationError",
    "ResultPackageStageInfo",
    "ResultPackageStageManifest",
    "ResultPackageTaskInfo",
    "ResultPackageTaskStatus",
    "ResultPackageValidationError",
    "ValidatedResultPackage",
    "JelicaPackageManifest",
    "compute_content_id",
    "content_digest_from_content_id",
    "infer_media_type",
    "load_result_package_link",
    "load_result_package_stage_manifest",
    "list_result_packages",
    "import_result_package",
    "publish_prepared_result_package",
    "relative_package_path_from_task",
    "resolve_result_package_path",
    "resolve_result_packages_directory",
    "result_package_artifact_paths",
    "result_package_target_path",
    "result_package_filename",
    "parse_result_package_filename",
    "serialize_stable_json",
    "validate_result_package_file",
    "write_model_json",
    "write_result_package_link",
]
