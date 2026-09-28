from __future__ import annotations

import csv
import hashlib
import io
import json
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jelica_core.config import LineageDetectionMethod, ResolvedLineageDetectionConfig

LINEAGE_DETECTION_STAGE_ID = "lineage_detection"
LINEAGE_DETECTION_MANIFEST_SCHEMA_VERSION = 1
LINEAGE_GROUPS_SCHEMA_VERSION = 1

LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH = "lineage_detection/lineage_detection_manifest.json"
LINEAGE_ASSIGNMENTS_TSV_RELATIVE_PATH = "lineage_detection/lineage_assignments.tsv"
LINEAGE_GROUPS_JSON_RELATIVE_PATH = "lineage_detection/lineage_groups.json"

_ASSIGNMENT_FIELD_NAMES = (
    "sample_id",
    "sequence_id",
    "lineage",
    "status",
    "diagnostics",
)
_MAX_DIAGNOSTIC_COUNT = 8
_MAX_DIAGNOSTIC_LENGTH = 512


class LineageDetectionStatus(StrEnum):
    COMPLETED = "completed"


class LineageAssignmentStatus(StrEnum):
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"


def _normalize_non_empty_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _validate_relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if normalized == "" or posix_path.is_absolute() or windows_path.is_absolute():
        raise ValueError("artifact path must be relative")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ValueError("artifact path must not escape the stage directory")
    return posix_path.as_posix()


def _validate_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64:
        raise ValueError("sha256 must be a 64-character lowercase hex digest")
    try:
        int(normalized, 16)
    except ValueError as error:
        raise ValueError("sha256 must be a hexadecimal digest") from error
    return normalized


class LineageDetectionArtifactMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    record_count: int | None = Field(default=None, ge=0)

    @field_validator("relative_path")
    @classmethod
    def _normalize_relative_path(cls, value: str) -> str:
        return _validate_relative_path(value)

    @field_validator("sha256")
    @classmethod
    def _normalize_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class LineageAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str = Field(min_length=1)
    sequence_id: str = Field(min_length=1)
    lineage: str | None = None
    status: LineageAssignmentStatus
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("sample_id", "sequence_id")
    @classmethod
    def _normalize_required_text(cls, value: str, info: object) -> str:
        return _normalize_non_empty_text(value, field_name=getattr(info, "field_name", "field"))

    @field_validator("lineage")
    @classmethod
    def _normalize_lineage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_non_empty_text(value, field_name="lineage")

    @field_validator("diagnostics")
    @classmethod
    def _normalize_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > _MAX_DIAGNOSTIC_COUNT:
            raise ValueError("diagnostics exceed the supported count")
        normalized: list[str] = []
        for item in value:
            text = _normalize_non_empty_text(item, field_name="diagnostic")
            if len(text) > _MAX_DIAGNOSTIC_LENGTH:
                raise ValueError("diagnostic exceeds the supported length")
            normalized.append(text)
        return tuple(normalized)

    @model_validator(mode="after")
    def _validate_status(self) -> LineageAssignment:
        if self.status is LineageAssignmentStatus.ASSIGNED and self.lineage is None:
            raise ValueError("assigned lineage assignment requires lineage")
        if self.status is LineageAssignmentStatus.UNASSIGNED and self.lineage is not None:
            raise ValueError("unassigned lineage assignment must not have lineage")
        return self


class LineageGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lineage: str = Field(min_length=1)
    sample_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("lineage")
    @classmethod
    def _normalize_lineage(cls, value: str) -> str:
        return _normalize_non_empty_text(value, field_name="lineage")

    @field_validator("sample_ids")
    @classmethod
    def _normalize_sample_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _normalize_non_empty_text(item, field_name="sample_id") for item in value
        )
        if len(normalized) == 0:
            raise ValueError("lineage group must contain at least one sample")
        if len(set(normalized)) != len(normalized):
            raise ValueError("lineage group sample_ids must be unique")
        return normalized


class LineageGroupsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = LINEAGE_GROUPS_SCHEMA_VERSION
    stage_id: str = LINEAGE_DETECTION_STAGE_ID
    groups: tuple[LineageGroup, ...] = Field(default_factory=tuple)
    assigned_sample_count: int = Field(ge=0)
    unassigned_sample_count: int = Field(ge=0)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != LINEAGE_GROUPS_SCHEMA_VERSION:
            raise ValueError("unsupported lineage-groups schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != LINEAGE_DETECTION_STAGE_ID:
            raise ValueError("invalid lineage-groups stage identity")
        return value

    @model_validator(mode="after")
    def _validate_groups(self) -> LineageGroupsResult:
        lineages = tuple(group.lineage for group in self.groups)
        if len(set(lineages)) != len(lineages):
            raise ValueError("lineage group labels must be unique")
        grouped_sample_count = sum(len(group.sample_ids) for group in self.groups)
        if grouped_sample_count != self.assigned_sample_count:
            raise ValueError("group membership must match assigned_sample_count")
        return self


class LineageDetectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = LINEAGE_DETECTION_MANIFEST_SCHEMA_VERSION
    stage_id: str = LINEAGE_DETECTION_STAGE_ID
    task_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    enabled: bool
    normalized_settings: ResolvedLineageDetectionConfig = Field(
        default_factory=ResolvedLineageDetectionConfig
    )
    skipped_reason: str | None = None
    status: LineageDetectionStatus
    method: LineageDetectionMethod = LineageDetectionMethod.NEXTCLADE_PANGO
    tool_version: str | None = None
    dataset_path: str | None = None
    dataset_name: str | None = None
    dataset_version: str | None = None
    assignments: tuple[LineageAssignment, ...] = Field(default_factory=tuple)
    groups: tuple[LineageGroup, ...] = Field(default_factory=tuple)
    assigned_sample_count: int = Field(ge=0)
    unassigned_sample_count: int = Field(ge=0)
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)
    source_artifacts: tuple[str, ...] = Field(default_factory=tuple)
    artifacts: tuple[LineageDetectionArtifactMetadata, ...] = Field(default_factory=tuple)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != LINEAGE_DETECTION_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported lineage-detection manifest schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != LINEAGE_DETECTION_STAGE_ID:
            raise ValueError("invalid lineage-detection stage identity")
        return value

    @field_validator("config_hash")
    @classmethod
    def _normalize_config_hash(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator(
        "tool_version", "dataset_path", "dataset_name", "dataset_version", "skipped_reason"
    )
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_non_empty_text(value, field_name="manifest text field")

    @field_validator("source_artifacts")
    @classmethod
    def _normalize_source_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_relative_path(item) for item in value)

    @model_validator(mode="after")
    def _validate_content(self) -> LineageDetectionManifest:
        if self.normalized_settings.enabled != self.enabled:
            raise ValueError("enabled must match normalized_settings.enabled")
        if self.normalized_settings.method is not self.method:
            raise ValueError("method must match normalized_settings.method")
        assignment_sample_ids = tuple(item.sample_id for item in self.assignments)
        if len(set(assignment_sample_ids)) != len(assignment_sample_ids):
            raise ValueError("each logical sample may have only one lineage assignment")
        assigned = tuple(
            item for item in self.assignments if item.status is LineageAssignmentStatus.ASSIGNED
        )
        unassigned = tuple(
            item for item in self.assignments if item.status is LineageAssignmentStatus.UNASSIGNED
        )
        if len(assigned) != self.assigned_sample_count:
            raise ValueError("assigned_sample_count must match assignments")
        if len(unassigned) != self.unassigned_sample_count:
            raise ValueError("unassigned_sample_count must match assignments")
        expected_groups = _groups_from_assignments(assigned)
        if self.groups != expected_groups:
            raise ValueError("groups must match canonical assigned lineage assignments")
        if self.enabled:
            if self.skipped_reason is not None:
                raise ValueError("enabled lineage detection cannot be skipped")
            if self.tool_version is None:
                raise ValueError("enabled lineage detection requires tool_version")
            if self.normalized_settings.dataset is None:
                raise ValueError("enabled lineage detection requires dataset")
            if self.dataset_path != self.normalized_settings.dataset:
                raise ValueError("dataset_path must match normalized_settings.dataset")
        elif (
            self.assignments
            or self.groups
            or self.assigned_sample_count != 0
            or self.unassigned_sample_count != 0
            or self.artifacts
        ):
            raise ValueError("disabled lineage detection must not contain computed results")
        return self


def _groups_from_assignments(
    assignments: tuple[LineageAssignment, ...],
) -> tuple[LineageGroup, ...]:
    samples_by_lineage: dict[str, list[str]] = {}
    for assignment in assignments:
        if assignment.lineage is None:
            continue
        samples_by_lineage.setdefault(assignment.lineage, []).append(assignment.sample_id)
    return tuple(
        LineageGroup(lineage=lineage, sample_ids=tuple(sorted(sample_ids)))
        for lineage, sample_ids in sorted(samples_by_lineage.items(), key=lambda item: item[0])
    )


def build_lineage_groups(
    assignments: tuple[LineageAssignment, ...],
) -> tuple[LineageGroup, ...]:
    return _groups_from_assignments(
        tuple(item for item in assignments if item.status is LineageAssignmentStatus.ASSIGNED)
    )


def serialize_lineage_assignments_tsv(assignments: tuple[LineageAssignment, ...]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=_ASSIGNMENT_FIELD_NAMES,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    for assignment in assignments:
        writer.writerow(
            {
                "sample_id": assignment.sample_id,
                "sequence_id": assignment.sequence_id,
                "lineage": assignment.lineage or "",
                "status": assignment.status.value,
                "diagnostics": json.dumps(
                    list(assignment.diagnostics), ensure_ascii=False, separators=(",", ":")
                ),
            }
        )
    return output.getvalue()


def parse_lineage_assignments_tsv(payload: str) -> tuple[LineageAssignment, ...]:
    reader = csv.DictReader(io.StringIO(payload, newline=""), delimiter="\t")
    if reader.fieldnames != list(_ASSIGNMENT_FIELD_NAMES):
        raise ValueError("lineage_assignments.tsv has an invalid header")
    assignments: list[LineageAssignment] = []
    for row in reader:
        if row is None or set(row) != set(_ASSIGNMENT_FIELD_NAMES):
            raise ValueError("lineage_assignments.tsv has an invalid row")
        raw_diagnostics = row["diagnostics"]
        try:
            diagnostics_value = json.loads(raw_diagnostics)
        except json.JSONDecodeError as error:
            raise ValueError("lineage_assignments.tsv diagnostics are invalid") from error
        if not isinstance(diagnostics_value, list) or not all(
            isinstance(item, str) for item in diagnostics_value
        ):
            raise ValueError("lineage_assignments.tsv diagnostics must be a JSON string array")
        lineage = row["lineage"].strip() or None
        assignments.append(
            LineageAssignment(
                sample_id=row["sample_id"],
                sequence_id=row["sequence_id"],
                lineage=lineage,
                status=LineageAssignmentStatus(row["status"]),
                diagnostics=tuple(diagnostics_value),
            )
        )
    return tuple(assignments)


def artifact_metadata(
    path: Path,
    *,
    record_count: int | None = None,
    relative_path: str | None = None,
) -> LineageDetectionArtifactMetadata:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return LineageDetectionArtifactMetadata(
        relative_path=(
            _validate_relative_path(relative_path) if relative_path is not None else path.name
        ),
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
        record_count=record_count,
    )


def lineage_detection_artifact_paths(manifest: LineageDetectionManifest) -> tuple[str, ...]:
    if not manifest.enabled:
        return (LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH,)
    return (
        LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH,
        *(metadata.relative_path for metadata in manifest.artifacts),
    )


__all__ = [
    "LINEAGE_ASSIGNMENTS_TSV_RELATIVE_PATH",
    "LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH",
    "LINEAGE_DETECTION_MANIFEST_SCHEMA_VERSION",
    "LINEAGE_DETECTION_STAGE_ID",
    "LINEAGE_GROUPS_JSON_RELATIVE_PATH",
    "LINEAGE_GROUPS_SCHEMA_VERSION",
    "LineageAssignment",
    "LineageAssignmentStatus",
    "LineageDetectionArtifactMetadata",
    "LineageDetectionManifest",
    "LineageDetectionStatus",
    "LineageGroup",
    "LineageGroupsResult",
    "artifact_metadata",
    "build_lineage_groups",
    "lineage_detection_artifact_paths",
    "parse_lineage_assignments_tsv",
    "serialize_lineage_assignments_tsv",
]
