from __future__ import annotations

import hashlib
import json
import os
import tempfile
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import TracebackType
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jelica_core.config.models import ResolvedComparativeAnalysisConfig

from .aligned_comparator import (
    AlignedDifferenceEvent,
    ComparisonCoordinateSystem,
    ComparisonIdentity,
    DifferenceEventType,
)
from .planning import ComparisonPlanCounts, ComparisonSourceKind

COMPARATIVE_ANALYSIS_STAGE_ID = "comparative_analysis"
COMPARATIVE_ANALYSIS_SCHEMA_VERSION = 1
COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH = (
    "comparative_analysis/comparative_analysis_manifest.json"
)
DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH = (
    "comparative_analysis/dataset_statistical_summary.json"
)
STATISTICAL_DIFFERENCES_RELATIVE_PATH = (
    "comparative_analysis/statistical_differences.jsonl"
)
REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH = (
    "comparative_analysis/reference_comparison_summary.jsonl"
)
REFERENCE_DIFFERENCES_RELATIVE_PATH = (
    "comparative_analysis/reference_differences.jsonl"
)
PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH = (
    "comparative_analysis/pairwise_comparison_summary.jsonl"
)
PAIRWISE_DIFFERENCES_RELATIVE_PATH = (
    "comparative_analysis/pairwise_differences.jsonl"
)
COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH = "comparative_analysis/failures.jsonl"


class ComparativeAnalysisStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class ComparativeCategoryStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    SKIPPED = "skipped"
    COMPLETED = "completed"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class ComparativeResultStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


def _validate_relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if normalized == "" or posix.is_absolute() or windows.is_absolute():
        raise ValueError("artifact path must be relative")
    if ".." in posix.parts or ".." in windows.parts:
        raise ValueError("artifact path must not escape the stage directory")
    return posix.as_posix()


class ComparativeArtifactMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    record_count: int | None = Field(default=None, ge=0)

    @field_validator("relative_path")
    @classmethod
    def _normalize_relative_path(cls, value: str) -> str:
        return _validate_relative_path(value)


class ComparativeCategoryExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ComparativeCategoryStatus
    requested: bool
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    successful: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(default=0, ge=0)
    unavailable: int = Field(default=0, ge=0)
    available: bool = False
    artifact_paths: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("artifact_paths")
    @classmethod
    def _normalize_artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_relative_path(item) for item in value)

    @model_validator(mode="after")
    def _validate_counts(self) -> ComparativeCategoryExecution:
        if self.completed != self.successful + self.failed:
            raise ValueError("completed must equal successful plus failed")
        if self.completed + self.skipped + self.unavailable != self.total:
            raise ValueError(
                "completed, skipped, and unavailable counts must equal total"
            )
        if self.available != (self.successful > 0):
            raise ValueError("available must reflect whether a result succeeded")
        return self


class ComparativePlanExecutionCounts(BaseModel):
    """Planned logical work and the physical work actually attempted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_sample_count: int = Field(ge=0)
    unique_sequence_count: int = Field(ge=0)
    reference_logical_comparison_count: int = Field(ge=0)
    pairwise_logical_comparison_occurrence_count: int = Field(ge=0)
    pairwise_unique_directed_logical_comparison_count: int = Field(ge=0)
    duplicate_occurrence_count: int = Field(ge=0)
    planned_physical_scan_count: int = Field(ge=0)
    attempted_physical_scan_count: int = Field(ge=0)
    successful_physical_scan_count: int = Field(ge=0)
    failed_physical_scan_count: int = Field(ge=0)
    identical_projection_count: int = Field(ge=0)
    attempted_identical_profile_count: int = Field(ge=0)
    successful_identical_profile_count: int = Field(ge=0)
    failed_identical_profile_count: int = Field(ge=0)
    planned_reused_projection_count: int = Field(ge=0)
    executed_reused_projection_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_execution_counts(self) -> ComparativePlanExecutionCounts:
        if self.attempted_physical_scan_count != (
            self.successful_physical_scan_count + self.failed_physical_scan_count
        ):
            raise ValueError("physical scan execution counts are inconsistent")
        if self.attempted_identical_profile_count != (
            self.successful_identical_profile_count
            + self.failed_identical_profile_count
        ):
            raise ValueError("identical-profile execution counts are inconsistent")
        return self


def _empty_execution_counts() -> ComparativePlanExecutionCounts:
    return ComparativePlanExecutionCounts(
        logical_sample_count=0,
        unique_sequence_count=0,
        reference_logical_comparison_count=0,
        pairwise_logical_comparison_occurrence_count=0,
        pairwise_unique_directed_logical_comparison_count=0,
        duplicate_occurrence_count=0,
        planned_physical_scan_count=0,
        attempted_physical_scan_count=0,
        successful_physical_scan_count=0,
        failed_physical_scan_count=0,
        identical_projection_count=0,
        attempted_identical_profile_count=0,
        successful_identical_profile_count=0,
        failed_identical_profile_count=0,
        planned_reused_projection_count=0,
        executed_reused_projection_count=0,
    )


class ComparativeAnalysisManifest(BaseModel):
    """Sequence-safe manifest for one atomically published stage snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = COMPARATIVE_ANALYSIS_SCHEMA_VERSION
    stage_id: str = COMPARATIVE_ANALYSIS_STAGE_ID
    task_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    enabled: bool
    normalized_settings: ResolvedComparativeAnalysisConfig = Field(
        default_factory=ResolvedComparativeAnalysisConfig
    )
    skipped_reason: str | None = None
    status: ComparativeAnalysisStatus
    alignment_mode: str = Field(min_length=1)
    reference_mode: str = Field(min_length=1)
    reference_sample_id: str | None = None
    reference_sequence_id: str | None = None
    uracil_thymine_equivalent: bool
    requested_difference_categories: tuple[DifferenceEventType, ...] = Field(
        default_factory=tuple
    )
    uncertain_category_mandatory: bool = True
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)
    source_artifacts: tuple[str, ...] = Field(default_factory=tuple)
    phase_names: tuple[str, ...] = Field(default_factory=tuple)
    plan_counts: ComparisonPlanCounts
    plan_execution_counts: ComparativePlanExecutionCounts = Field(
        default_factory=_empty_execution_counts
    )
    category_execution: dict[str, ComparativeCategoryExecution] = Field(
        default_factory=dict
    )
    successful_result_count: int = Field(ge=0)
    failed_result_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    artifacts: tuple[ComparativeArtifactMetadata, ...] = Field(default_factory=tuple)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != COMPARATIVE_ANALYSIS_SCHEMA_VERSION:
            raise ValueError("unsupported comparative-analysis manifest schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != COMPARATIVE_ANALYSIS_STAGE_ID:
            raise ValueError("invalid comparative-analysis stage identity")
        return value

    @field_validator("source_artifacts")
    @classmethod
    def _normalize_source_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_relative_path(item) for item in value)

    @model_validator(mode="after")
    def _validate_execution_summary(self) -> ComparativeAnalysisManifest:
        if self.enabled != self.normalized_settings.enabled:
            raise ValueError("enabled must match normalized comparative settings")
        if self.reference_mode != self.normalized_settings.reference.mode.value:
            raise ValueError("reference mode must match normalized comparative settings")
        if self.uracil_thymine_equivalent != (
            self.normalized_settings.sequence_differences.symbol_policy
            .uracil_thymine_equivalent
        ):
            raise ValueError("symbol policy must match normalized comparative settings")
        expected_difference_categories: list[DifferenceEventType] = []
        differences = self.normalized_settings.sequence_differences
        if differences.substitutions:
            expected_difference_categories.append(DifferenceEventType.SUBSTITUTION)
        if differences.insertions:
            expected_difference_categories.append(DifferenceEventType.INSERTION)
        if differences.deletions:
            expected_difference_categories.append(DifferenceEventType.DELETION)
        if tuple(expected_difference_categories) != self.requested_difference_categories:
            raise ValueError("requested difference categories are inconsistent")
        if (self.reference_sample_id is None) != (self.reference_sequence_id is None):
            raise ValueError("reference sample and sequence identities must be paired")
        expected_categories = {
            "statistics",
            "reference_sequence_differences",
            "pairwise_sequence_differences",
        }
        if set(self.category_execution) != expected_categories:
            raise ValueError("comparative category execution is incomplete")
        successful = sum(item.successful for item in self.category_execution.values())
        failed = sum(item.failed for item in self.category_execution.values())
        if successful != self.successful_result_count or failed != self.failed_result_count:
            raise ValueError("comparative result totals are inconsistent")
        expected_status = (
            ComparativeAnalysisStatus.COMPLETED
            if self.failure_count == 0
            else (
                ComparativeAnalysisStatus.PARTIAL_SUCCESS
                if self.successful_result_count > 0
                else ComparativeAnalysisStatus.FAILED
            )
        )
        if self.status is not expected_status:
            raise ValueError("comparative-analysis status is inconsistent")
        if self.enabled:
            if self.skipped_reason is not None:
                raise ValueError("enabled comparative analysis cannot have a skipped reason")
        elif self.skipped_reason is None:
            raise ValueError("disabled comparative analysis requires a skipped reason")
        return self


class RequestedDifferenceCategorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: bool
    event_count: int | None = Field(default=None, ge=0)
    base_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_requested_counts(self) -> RequestedDifferenceCategorySummary:
        has_counts = self.event_count is not None and self.base_count is not None
        if self.requested != has_counts:
            raise ValueError("category counts must exist exactly when requested")
        return self


class PublishedSequenceComparisonSummary(BaseModel):
    """Typed numeric summary that cannot carry sequence-derived strings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    msa_column_count: int = Field(ge=0)
    both_gap_column_count: int = Field(ge=0)
    comparable_base_count: int = Field(ge=0)
    matching_base_count: int = Field(ge=0)
    substitutions: RequestedDifferenceCategorySummary
    insertions: RequestedDifferenceCategorySummary
    deletions: RequestedDifferenceCategorySummary
    uncertain_event_count: int = Field(ge=0)
    uncertain_column_count: int = Field(ge=0)
    identity_on_comparable_bases: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )


class SequenceComparisonSummaryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left: ComparisonIdentity
    right: ComparisonIdentity
    source_kinds: tuple[ComparisonSourceKind, ...] = Field(min_length=1)
    source_occurrence_count: int = Field(ge=1)
    status: ComparativeResultStatus
    failure_id: str | None = None
    computation_index: int | None = Field(default=None, ge=0)
    reverse_projection: bool = False
    reused_physical_computation: bool = False
    identical_sequence_shortcut: bool = False
    requested_categories: tuple[DifferenceEventType, ...] = Field(default_factory=tuple)
    uncertain_category_mandatory: bool = True
    summary: PublishedSequenceComparisonSummary | None = None


class DifferenceArtifactRecord(BaseModel):
    """The only public contract allowed to contain MSA-derived values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left: ComparisonIdentity
    right: ComparisonIdentity
    source_kinds: tuple[ComparisonSourceKind, ...] = Field(min_length=1)
    type: DifferenceEventType
    msa_column_start: int = Field(ge=1)
    msa_column_end: int = Field(ge=1)
    length: int = Field(ge=1)
    left_start: int | None = Field(default=None, ge=1)
    left_end: int | None = Field(default=None, ge=1)
    right_start: int | None = Field(default=None, ge=1)
    right_end: int | None = Field(default=None, ge=1)
    after_left_position: int | None = Field(default=None, ge=1)
    before_left_position: int | None = Field(default=None, ge=1)
    after_right_position: int | None = Field(default=None, ge=1)
    before_right_position: int | None = Field(default=None, ge=1)
    reference_start: int | None = Field(default=None, ge=1)
    reference_end: int | None = Field(default=None, ge=1)
    after_reference_position: int | None = Field(default=None, ge=1)
    before_reference_position: int | None = Field(default=None, ge=1)
    left_value: str | None = None
    right_value: str | None = None
    uncertain_reason: str | None = None
    coordinate_system: ComparisonCoordinateSystem = (
        ComparisonCoordinateSystem.ONE_BASED_INCLUSIVE
    )


class StatisticalDifferenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str = Field(min_length=1)
    metric_name: str = Field(min_length=1)
    left: ComparisonIdentity
    right: ComparisonIdentity
    source_kinds: tuple[ComparisonSourceKind, ...] = Field(min_length=1)
    source_occurrence_count: int = Field(ge=1)
    value: Any


class StatisticalDatasetArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    sample_count: int = Field(ge=0)
    metric_total: int = Field(ge=0)
    metric_successful: int = Field(ge=0)
    metric_failed: int = Field(ge=0)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_metric_counts(self) -> StatisticalDatasetArtifact:
        if self.metric_successful + self.metric_failed != self.metric_total:
            raise ValueError("statistics metric counts are inconsistent")
        return self


class ComparativeFailureRecord(BaseModel):
    """A bounded, sequence-safe local failure record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    error_code: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    metric_id: str | None = None
    computation_index: int | None = Field(default=None, ge=0)
    affected_logical_result_count: int = Field(default=1, ge=1)
    sample_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=4)


class JsonlArtifactWriter:
    """Stream JSON objects into a temporary file and publish it by rename."""

    def __init__(self, path: Path, *, relative_path: str | None = None) -> None:
        self.path = path
        self.relative_path = _validate_relative_path(relative_path or path.name)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
        )
        self._handle = handle
        self._temporary_path = Path(handle.name)
        self.record_count = 0
        self._closed = False

    def write_model(self, value: BaseModel) -> None:
        self.write(value.model_dump(mode="json"))

    def write(self, value: Mapping[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("JSONL writer is closed")
        serialized = json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._handle.write(serialized)
        self._handle.write("\n")
        self.record_count += 1

    def close(self) -> ComparativeArtifactMetadata:
        if self._closed:
            raise RuntimeError("JSONL writer is already closed")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        os.replace(self._temporary_path, self.path)
        self._closed = True
        return artifact_metadata(
            self.path,
            record_count=self.record_count,
            relative_path=self.relative_path,
        )

    def abort(self) -> None:
        if self._closed:
            return
        self._handle.close()
        self._temporary_path.unlink(missing_ok=True)
        self._closed = True

    def __enter__(self) -> JsonlArtifactWriter:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if exception_type is None:
            self.close()
        else:
            self.abort()
        return False


def artifact_metadata(
    path: Path,
    *,
    record_count: int | None = None,
    relative_to: Path | None = None,
    relative_path: str | None = None,
) -> ComparativeArtifactMetadata:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    resolved_relative_path = (
        _validate_relative_path(relative_path)
        if relative_path is not None
        else (
            path.relative_to(relative_to).as_posix()
            if relative_to is not None
            else path.name
        )
    )
    return ComparativeArtifactMetadata(
        relative_path=resolved_relative_path,
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
        record_count=record_count,
    )


def materialize_difference_record(
    *,
    left: ComparisonIdentity,
    right: ComparisonIdentity,
    source_kinds: tuple[ComparisonSourceKind, ...],
    event: AlignedDifferenceEvent,
    left_aligned_sequence: str,
    right_aligned_sequence: str,
    reference_coordinates: Mapping[str, int | None] | None = None,
) -> DifferenceArtifactRecord:
    """Materialize bounded event values at the dedicated-artifact boundary."""

    start = event.msa_column_start - 1
    stop = event.msa_column_end
    if stop > len(left_aligned_sequence) or stop > len(right_aligned_sequence):
        raise ValueError("difference event span exceeds the aligned row length")
    left_value = left_aligned_sequence[start:stop].replace("-", "") or None
    right_value = right_aligned_sequence[start:stop].replace("-", "") or None
    coordinates = dict(reference_coordinates or {})
    return DifferenceArtifactRecord(
        left=left,
        right=right,
        source_kinds=source_kinds,
        type=event.type,
        msa_column_start=event.msa_column_start,
        msa_column_end=event.msa_column_end,
        length=event.length,
        left_start=event.left_start,
        left_end=event.left_end,
        right_start=event.right_start,
        right_end=event.right_end,
        after_left_position=event.after_left_position,
        before_left_position=event.before_left_position,
        after_right_position=event.after_right_position,
        before_right_position=event.before_right_position,
        reference_start=coordinates.get("reference_start"),
        reference_end=coordinates.get("reference_end"),
        after_reference_position=coordinates.get("after_reference_position"),
        before_reference_position=coordinates.get("before_reference_position"),
        left_value=left_value,
        right_value=right_value,
        uncertain_reason=(
            "ambiguous_or_nondefinite_symbol"
            if event.type is DifferenceEventType.UNCERTAIN
            else None
        ),
    )


__all__ = [
    "COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH",
    "COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH",
    "COMPARATIVE_ANALYSIS_SCHEMA_VERSION",
    "COMPARATIVE_ANALYSIS_STAGE_ID",
    "DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH",
    "PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH",
    "PAIRWISE_DIFFERENCES_RELATIVE_PATH",
    "REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH",
    "REFERENCE_DIFFERENCES_RELATIVE_PATH",
    "STATISTICAL_DIFFERENCES_RELATIVE_PATH",
    "ComparativeAnalysisManifest",
    "ComparativeAnalysisStatus",
    "ComparativeArtifactMetadata",
    "ComparativeCategoryExecution",
    "ComparativeCategoryStatus",
    "ComparativeFailureRecord",
    "ComparativeResultStatus",
    "DifferenceArtifactRecord",
    "JsonlArtifactWriter",
    "SequenceComparisonSummaryRecord",
    "StatisticalDatasetArtifact",
    "StatisticalDifferenceRecord",
    "artifact_metadata",
    "materialize_difference_record",
]
