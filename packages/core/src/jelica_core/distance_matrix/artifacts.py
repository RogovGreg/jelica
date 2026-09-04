from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import TracebackType
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jelica_core.config.models import (
    AnalysisDistanceMatrixModel,
    ResolvedDistanceMatrixConfig,
)

DISTANCE_MATRIX_STAGE_ID = "distance_matrix"
DISTANCE_MATRIX_MANIFEST_SCHEMA_VERSION = 1
DISTANCE_MATRIX_RESULT_SCHEMA_VERSION = 1
DISTANCE_MATRIX_PAIR_SCHEMA_VERSION = 1

DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH = "distance_matrix/distance_matrix_manifest.json"
DISTANCE_MATRIX_JSON_RELATIVE_PATH = "distance_matrix/distance_matrix.json"
DISTANCE_PAIRS_JSONL_RELATIVE_PATH = "distance_matrix/distance_pairs.jsonl"
DISTANCE_MATRIX_TSV_RELATIVE_PATH = "distance_matrix/distance_matrix.tsv"

GAP_POLICY_PAIRWISE_DELETION = "pairwise_deletion"
AMBIGUITY_POLICY_PAIRWISE_DELETION = "pairwise_deletion"
URACIL_THYMINE_POLICY_EQUIVALENT = "t_equals_u"


class DistanceMatrixStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class DistancePairState(StrEnum):
    DEFINED = "defined"
    UNDEFINED_NO_COMPARABLE_SITES = "undefined_no_comparable_sites"


def _validate_relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if normalized == "" or posix.is_absolute() or windows.is_absolute():
        raise ValueError("artifact path must be relative")
    if ".." in posix.parts or ".." in windows.parts:
        raise ValueError("artifact path must not escape the stage directory")
    return posix.as_posix()


def _expected_pair_count(sequence_count: int) -> int:
    return (sequence_count * (sequence_count - 1)) // 2


class DistanceMatrixArtifactMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    record_count: int | None = Field(default=None, ge=0)

    @field_validator("relative_path")
    @classmethod
    def _normalize_relative_path(cls, value: str) -> str:
        return _validate_relative_path(value)


class DistanceMatrixSequenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    sequence_id: str = Field(min_length=1)
    logical_sample_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("logical_sample_ids")
    @classmethod
    def _normalize_logical_sample_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            text = item.strip()
            if text == "":
                raise ValueError("logical_sample_ids must not contain empty values")
            normalized.append(text)
        if len(set(normalized)) != len(normalized):
            raise ValueError("logical_sample_ids must be unique per sequence reference")
        return tuple(normalized)


class DistanceMatrixAggregateCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mismatch_count_sum: int = Field(ge=0)
    comparable_site_count_sum: int = Field(ge=0)
    excluded_gap_site_count_sum: int = Field(ge=0)
    excluded_ambiguous_site_count_sum: int = Field(ge=0)


class DistancePairRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = DISTANCE_MATRIX_PAIR_SCHEMA_VERSION
    left_sequence_id: str = Field(min_length=1)
    right_sequence_id: str = Field(min_length=1)
    left_index: int = Field(ge=0)
    right_index: int = Field(ge=0)
    mismatch_count: int = Field(ge=0)
    comparable_site_count: int = Field(ge=0)
    excluded_gap_site_count: int = Field(ge=0)
    excluded_ambiguous_site_count: int = Field(ge=0)
    distance: float | None = Field(default=None, ge=0.0, le=1.0)
    state: DistancePairState

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != DISTANCE_MATRIX_PAIR_SCHEMA_VERSION:
            raise ValueError("unsupported pair record schema version")
        return value

    @model_validator(mode="after")
    def _validate_counts(self) -> DistancePairRecord:
        if self.left_index >= self.right_index:
            raise ValueError("left_index must be smaller than right_index")
        if self.left_sequence_id == self.right_sequence_id:
            raise ValueError("pair record sequence identities must be distinct")
        if self.mismatch_count > self.comparable_site_count:
            raise ValueError("mismatch_count cannot exceed comparable_site_count")
        classified_count = (
            self.comparable_site_count
            + self.excluded_gap_site_count
            + self.excluded_ambiguous_site_count
        )
        if classified_count <= 0:
            raise ValueError("pair record must classify at least one alignment site")
        if self.comparable_site_count == 0:
            if self.distance is not None:
                raise ValueError("distance must be null without comparable sites")
            if self.state is not DistancePairState.UNDEFINED_NO_COMPARABLE_SITES:
                raise ValueError(
                    "pair state must be undefined_no_comparable_sites without comparable sites"
                )
            return self
        if self.distance is None:
            raise ValueError("distance must be defined when comparable sites exist")
        expected_distance = self.mismatch_count / self.comparable_site_count
        if not math.isclose(
            self.distance,
            expected_distance,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("distance does not match mismatch/comparable ratio")
        if self.state is not DistancePairState.DEFINED:
            raise ValueError("pair state must be defined when distance exists")
        return self


class DistanceMatrixResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = DISTANCE_MATRIX_RESULT_SCHEMA_VERSION
    stage_id: str = DISTANCE_MATRIX_STAGE_ID
    model: AnalysisDistanceMatrixModel = AnalysisDistanceMatrixModel.P_DISTANCE
    gap_policy: str = GAP_POLICY_PAIRWISE_DELETION
    ambiguity_policy: str = AMBIGUITY_POLICY_PAIRWISE_DELETION
    uracil_thymine_policy: str = URACIL_THYMINE_POLICY_EQUIVALENT
    sequence_references: tuple[DistanceMatrixSequenceReference, ...] = Field(
        default_factory=tuple
    )
    matrix: tuple[tuple[float | None, ...], ...] = Field(default_factory=tuple)
    unique_sequence_count: int = Field(ge=1)
    expected_pair_count: int = Field(ge=0)
    processed_pair_count: int = Field(ge=0)
    defined_distance_count: int = Field(ge=0)
    undefined_distance_count: int = Field(ge=0)
    aggregate_counts: DistanceMatrixAggregateCounts

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != DISTANCE_MATRIX_RESULT_SCHEMA_VERSION:
            raise ValueError("unsupported distance-matrix result schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != DISTANCE_MATRIX_STAGE_ID:
            raise ValueError("invalid distance-matrix stage identity")
        return value

    @model_validator(mode="after")
    def _validate_matrix(self) -> DistanceMatrixResult:
        sequence_count = len(self.sequence_references)
        if self.unique_sequence_count != sequence_count:
            raise ValueError("unique_sequence_count must match sequence_references length")
        if len(self.matrix) != sequence_count:
            raise ValueError("matrix row count must match sequence_references length")
        for index, reference in enumerate(self.sequence_references):
            if reference.index != index:
                raise ValueError("sequence reference indexes must match canonical order")
        expected_pair_total = _expected_pair_count(sequence_count)
        if self.expected_pair_count != expected_pair_total:
            raise ValueError("expected_pair_count is inconsistent with matrix dimensions")
        if self.processed_pair_count != self.expected_pair_count:
            raise ValueError("processed_pair_count must equal expected_pair_count")
        if self.defined_distance_count + self.undefined_distance_count != self.expected_pair_count:
            raise ValueError("defined/undefined pair totals are inconsistent")

        defined = 0
        undefined = 0
        for row_index, row in enumerate(self.matrix):
            if len(row) != sequence_count:
                raise ValueError("matrix must be square")
            diagonal = row[row_index]
            if diagonal is None or not math.isclose(
                diagonal,
                0.0,
                rel_tol=0.0,
                abs_tol=0.0,
            ):
                raise ValueError("matrix diagonal must be zero")
            for column_index, value in enumerate(row):
                mirror = self.matrix[column_index][row_index]
                if value is None:
                    if mirror is not None:
                        raise ValueError("matrix must be symmetric")
                else:
                    if value < 0.0 or value > 1.0:
                        raise ValueError("matrix values must be within [0, 1]")
                    if mirror is None or not math.isclose(
                        value,
                        mirror,
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    ):
                        raise ValueError("matrix must be symmetric")
                if row_index >= column_index:
                    continue
                if value is None:
                    undefined += 1
                else:
                    defined += 1
        if defined != self.defined_distance_count:
            raise ValueError("defined_distance_count does not match matrix data")
        if undefined != self.undefined_distance_count:
            raise ValueError("undefined_distance_count does not match matrix data")
        return self


class DistanceMatrixManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = DISTANCE_MATRIX_MANIFEST_SCHEMA_VERSION
    stage_id: str = DISTANCE_MATRIX_STAGE_ID
    task_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    enabled: bool
    normalized_settings: ResolvedDistanceMatrixConfig = Field(
        default_factory=ResolvedDistanceMatrixConfig
    )
    skipped_reason: str | None = None
    status: DistanceMatrixStatus
    model: AnalysisDistanceMatrixModel = AnalysisDistanceMatrixModel.P_DISTANCE
    gap_policy: str = GAP_POLICY_PAIRWISE_DELETION
    ambiguity_policy: str = AMBIGUITY_POLICY_PAIRWISE_DELETION
    uracil_thymine_policy: str = URACIL_THYMINE_POLICY_EQUIVALENT
    unique_sequence_count: int = Field(ge=0)
    expected_pair_count: int = Field(ge=0)
    processed_pair_count: int = Field(ge=0)
    defined_distance_count: int = Field(ge=0)
    undefined_distance_count: int = Field(ge=0)
    matrix_dimensions: tuple[int, int] = (0, 0)
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)
    source_artifacts: tuple[str, ...] = Field(default_factory=tuple)
    artifacts: tuple[DistanceMatrixArtifactMetadata, ...] = Field(default_factory=tuple)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != DISTANCE_MATRIX_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported distance-matrix manifest schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != DISTANCE_MATRIX_STAGE_ID:
            raise ValueError("invalid distance-matrix stage identity")
        return value

    @field_validator("source_artifacts")
    @classmethod
    def _normalize_source_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_relative_path(item) for item in value)

    @model_validator(mode="after")
    def _validate_content(self) -> DistanceMatrixManifest:
        if self.enabled != self.normalized_settings.enabled:
            raise ValueError("enabled must match normalized distance-matrix settings")
        if self.model is not self.normalized_settings.model:
            raise ValueError("model must match normalized distance-matrix settings")

        matrix_rows, matrix_columns = self.matrix_dimensions
        if matrix_rows != matrix_columns:
            raise ValueError("matrix_dimensions must describe a square matrix")

        if not self.enabled:
            if self.skipped_reason is None:
                raise ValueError("disabled distance matrix requires a skipped reason")
            if self.status is not DistanceMatrixStatus.COMPLETED:
                raise ValueError("disabled distance matrix status must be completed")
            if any(
                count != 0
                for count in (
                    self.unique_sequence_count,
                    self.expected_pair_count,
                    self.processed_pair_count,
                    self.defined_distance_count,
                    self.undefined_distance_count,
                )
            ):
                raise ValueError("disabled distance matrix must publish zero counters")
            if self.matrix_dimensions != (0, 0):
                raise ValueError("disabled distance matrix must publish 0x0 dimensions")
            if len(self.artifacts) != 0:
                raise ValueError("disabled distance matrix must not list result artifacts")
            return self

        if self.skipped_reason is not None:
            raise ValueError("enabled distance matrix cannot have a skipped reason")
        if self.unique_sequence_count < 1:
            raise ValueError("enabled distance matrix requires at least one sequence")
        if self.matrix_dimensions != (
            self.unique_sequence_count,
            self.unique_sequence_count,
        ):
            raise ValueError("matrix_dimensions are inconsistent with sequence counters")
        expected_pair_total = _expected_pair_count(self.unique_sequence_count)
        if self.expected_pair_count != expected_pair_total:
            raise ValueError("expected_pair_count is inconsistent with sequence counters")
        if self.processed_pair_count != self.expected_pair_count:
            raise ValueError("processed_pair_count must equal expected_pair_count")
        if self.defined_distance_count + self.undefined_distance_count != self.expected_pair_count:
            raise ValueError("defined and undefined distance counters are inconsistent")
        if (
            self.status is DistanceMatrixStatus.COMPLETED
            and self.undefined_distance_count != 0
        ):
            raise ValueError("completed status requires all pair distances to be defined")
        if (
            self.status is DistanceMatrixStatus.PARTIAL_SUCCESS
            and self.undefined_distance_count == 0
        ):
            raise ValueError("partial_success requires at least one undefined pair distance")

        expected_artifacts = (
            DISTANCE_MATRIX_JSON_RELATIVE_PATH,
            DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
            DISTANCE_MATRIX_TSV_RELATIVE_PATH,
        )
        published_artifacts = tuple(item.relative_path for item in self.artifacts)
        if published_artifacts != expected_artifacts:
            raise ValueError("distance-matrix artifacts are missing or out of order")
        if len(set(published_artifacts)) != len(published_artifacts):
            raise ValueError("distance-matrix artifact paths must be unique")
        return self


class DistanceMatrixJsonlWriter:
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

    def close(self) -> DistanceMatrixArtifactMetadata:
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

    def __enter__(self) -> DistanceMatrixJsonlWriter:
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
) -> DistanceMatrixArtifactMetadata:
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
    return DistanceMatrixArtifactMetadata(
        relative_path=resolved_relative_path,
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
        record_count=record_count,
    )


def distance_matrix_artifact_paths(manifest: DistanceMatrixManifest) -> tuple[str, ...]:
    if not manifest.enabled:
        return (DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,)
    return (
        DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
        *(metadata.relative_path for metadata in manifest.artifacts),
    )


__all__ = [
    "AMBIGUITY_POLICY_PAIRWISE_DELETION",
    "DISTANCE_MATRIX_JSON_RELATIVE_PATH",
    "DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH",
    "DISTANCE_MATRIX_MANIFEST_SCHEMA_VERSION",
    "DISTANCE_MATRIX_PAIR_SCHEMA_VERSION",
    "DISTANCE_MATRIX_RESULT_SCHEMA_VERSION",
    "DISTANCE_MATRIX_STAGE_ID",
    "DISTANCE_MATRIX_TSV_RELATIVE_PATH",
    "DISTANCE_PAIRS_JSONL_RELATIVE_PATH",
    "GAP_POLICY_PAIRWISE_DELETION",
    "URACIL_THYMINE_POLICY_EQUIVALENT",
    "DistanceMatrixArtifactMetadata",
    "DistanceMatrixAggregateCounts",
    "DistanceMatrixJsonlWriter",
    "DistanceMatrixManifest",
    "DistanceMatrixResult",
    "DistanceMatrixSequenceReference",
    "DistanceMatrixStatus",
    "DistancePairRecord",
    "DistancePairState",
    "artifact_metadata",
    "distance_matrix_artifact_paths",
]
