from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jelica_core.config.models import (
    AnalysisCladeDetectionMethod,
    AnalysisDistanceMatrixModel,
    AnalysisPhylogeneticTreeRooting,
    ResolvedCladeDetectionConfig,
)
from jelica_core.distance_matrix import (
    DISTANCE_MATRIX_JSON_RELATIVE_PATH,
    DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
)
from jelica_core.phylogenetic_tree import (
    PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
    TREE_JSON_RELATIVE_PATH,
)

CLADE_DETECTION_STAGE_ID = "clade_detection"
CLADE_DETECTION_MANIFEST_SCHEMA_VERSION = 1
INFERRED_CLADES_RESULT_SCHEMA_VERSION = 1
CLADE_MEMBERSHIP_SCHEMA_VERSION = 1

CLADE_DETECTION_MANIFEST_RELATIVE_PATH = "clade_detection/clade_detection_manifest.json"
INFERRED_CLADES_JSON_RELATIVE_PATH = "clade_detection/inferred_clades.json"
CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH = "clade_detection/clade_memberships.jsonl"
CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH = "clade_detection/clade_assignments.tsv"

CLADE_SELECTION_POLICY_MAXIMAL_MONOPHYLETIC_SUBTREES = "maximal_monophyletic_subtrees"
CLADE_INTERPRETATION_SCOPE_CURRENT_ROOTED_TREE = "inferred_from_current_rooted_tree"
CLADE_DISTANCE_TOLERANCE = 1e-12
CLADE_ID_SHA256_PREFIX_LENGTH = 24

_EXPECTED_UPSTREAM_SOURCE_ARTIFACTS = (
    f"stages/phylogenetic_tree/{PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH}",
    f"stages/phylogenetic_tree/{TREE_JSON_RELATIVE_PATH}",
    f"stages/distance_matrix/{DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH}",
    f"stages/distance_matrix/{DISTANCE_MATRIX_JSON_RELATIVE_PATH}",
)
_EXPECTED_RESULT_ARTIFACTS = (
    INFERRED_CLADES_JSON_RELATIVE_PATH,
    CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
    CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
)
_CLADE_ASSIGNMENT_FIELD_NAMES = (
    "clade_ordinal",
    "clade_id",
    "leaf_label",
    "sequence_index",
    "sequence_id",
    "logical_sample_ids",
    "clade_leaf_count",
    "clade_max_pairwise_distance",
    "max_within_clade_distance",
)


class CladeDetectionStatus(StrEnum):
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


def _validate_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64:
        raise ValueError("sha256 must be a 64-character lowercase hex digest")
    try:
        int(normalized, 16)
    except ValueError as error:
        raise ValueError("sha256 must be a hexadecimal digest") from error
    return normalized


class CladeDetectionArtifactMetadata(BaseModel):
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


class InferredCladeMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    leaf_label: str = Field(min_length=1)
    sequence_index: int = Field(ge=0)
    sequence_id: str = Field(min_length=1)
    logical_sample_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("leaf_label", "sequence_id")
    @classmethod
    def _normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("member text fields must not be empty")
        return normalized

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
            raise ValueError("logical_sample_ids must be unique per clade member")
        return tuple(normalized)


class InferredClade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    clade_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    source_node_id: str = Field(min_length=1)
    leaf_count: int = Field(ge=1)
    logical_sample_count: int = Field(ge=0)
    is_singleton: bool
    leaf_labels: tuple[str, ...] = Field(default_factory=tuple)
    sequence_indices: tuple[int, ...] = Field(default_factory=tuple)
    sequence_ids: tuple[str, ...] = Field(default_factory=tuple)
    logical_sample_ids: tuple[str, ...] = Field(default_factory=tuple)
    members: tuple[InferredCladeMember, ...] = Field(default_factory=tuple)
    max_pairwise_distance: float = Field(ge=0.0, le=1.0)
    max_within_clade_distance: float = Field(ge=0.0, le=1.0)
    within_threshold: bool = True

    @field_validator("clade_id", "source_node_id")
    @classmethod
    def _normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("clade identifiers must not be empty")
        return normalized

    @field_validator("leaf_labels", "sequence_ids", "logical_sample_ids")
    @classmethod
    def _normalize_text_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(item == "" for item in normalized):
            raise ValueError("text arrays must not contain empty values")
        return normalized

    @model_validator(mode="after")
    def _validate_content(self) -> InferredClade:
        if self.leaf_count != len(self.leaf_labels):
            raise ValueError("leaf_count must match leaf_labels length")
        if self.leaf_count != len(self.sequence_indices):
            raise ValueError("leaf_count must match sequence_indices length")
        if self.leaf_count != len(self.sequence_ids):
            raise ValueError("leaf_count must match sequence_ids length")
        if self.leaf_count != len(self.members):
            raise ValueError("leaf_count must match members length")
        if self.logical_sample_count != len(self.logical_sample_ids):
            raise ValueError("logical_sample_count must match logical_sample_ids length")
        if len(set(self.leaf_labels)) != len(self.leaf_labels):
            raise ValueError("leaf_labels must be unique")
        if len(set(self.sequence_indices)) != len(self.sequence_indices):
            raise ValueError("sequence_indices must be unique")
        if len(set(self.sequence_ids)) != len(self.sequence_ids):
            raise ValueError("sequence_ids must be unique")
        if len(set(self.logical_sample_ids)) != len(self.logical_sample_ids):
            raise ValueError("logical_sample_ids must be unique")
        if tuple(sorted(self.sequence_indices)) != self.sequence_indices:
            raise ValueError("sequence_indices must be in canonical ascending order")
        if self.is_singleton != (self.leaf_count == 1):
            raise ValueError("is_singleton must match leaf_count")
        if not self.within_threshold:
            raise ValueError("within_threshold must be true for published clades")
        if self.max_pairwise_distance > self.max_within_clade_distance + CLADE_DISTANCE_TOLERANCE:
            raise ValueError("max_pairwise_distance exceeds threshold")

        flattened_logical_ids: list[str] = []
        for index, member in enumerate(self.members):
            if member.leaf_label != self.leaf_labels[index]:
                raise ValueError("members must align with leaf_labels")
            if member.sequence_index != self.sequence_indices[index]:
                raise ValueError("members must align with sequence_indices")
            if member.sequence_id != self.sequence_ids[index]:
                raise ValueError("members must align with sequence_ids")
            flattened_logical_ids.extend(member.logical_sample_ids)
        if tuple(flattened_logical_ids) != self.logical_sample_ids:
            raise ValueError(
                "logical_sample_ids must follow canonical member traversal order"
            )
        return self


class InferredCladesResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = INFERRED_CLADES_RESULT_SCHEMA_VERSION
    stage_id: str = CLADE_DETECTION_STAGE_ID
    method: AnalysisCladeDetectionMethod = AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE
    selection_policy: str = CLADE_SELECTION_POLICY_MAXIMAL_MONOPHYLETIC_SUBTREES
    interpretation_scope: str = CLADE_INTERPRETATION_SCOPE_CURRENT_ROOTED_TREE
    max_within_clade_distance: float = Field(ge=0.0, le=1.0)
    float_tolerance: float = CLADE_DISTANCE_TOLERANCE
    input_distance_model: AnalysisDistanceMatrixModel = AnalysisDistanceMatrixModel.P_DISTANCE
    tree_snapshot_manifest_sha256: str = Field(min_length=64, max_length=64)
    matrix_snapshot_manifest_sha256: str = Field(min_length=64, max_length=64)
    requested_rooting: AnalysisPhylogeneticTreeRooting
    applied_rooting: str = Field(min_length=1)
    canonical_leaf_count: int = Field(ge=1)
    canonical_clade_order: tuple[str, ...] = Field(default_factory=tuple)
    clades: tuple[InferredClade, ...] = Field(default_factory=tuple)
    clade_count: int = Field(ge=1)
    singleton_clade_count: int = Field(ge=0)
    multi_leaf_clade_count: int = Field(ge=0)
    minimum_clade_size: int = Field(ge=1)
    maximum_clade_size: int = Field(ge=1)
    coverage_leaf_count: int = Field(ge=1)
    uncovered_leaf_count: int = Field(ge=0)
    partition_validated: bool

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != INFERRED_CLADES_RESULT_SCHEMA_VERSION:
            raise ValueError("unsupported inferred-clades result schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != CLADE_DETECTION_STAGE_ID:
            raise ValueError("invalid clade-detection stage identity")
        return value

    @field_validator("tree_snapshot_manifest_sha256", "matrix_snapshot_manifest_sha256")
    @classmethod
    def _normalize_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("applied_rooting")
    @classmethod
    def _normalize_applied_rooting(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("applied_rooting must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_content(self) -> InferredCladesResult:
        if self.selection_policy != CLADE_SELECTION_POLICY_MAXIMAL_MONOPHYLETIC_SUBTREES:
            raise ValueError("unsupported clade selection_policy")
        if self.interpretation_scope != CLADE_INTERPRETATION_SCOPE_CURRENT_ROOTED_TREE:
            raise ValueError("unsupported interpretation_scope")
        if not math.isfinite(self.float_tolerance) or self.float_tolerance <= 0.0:
            raise ValueError("float_tolerance must be finite and > 0")
        if self.clade_count != len(self.clades):
            raise ValueError("clade_count must match clades length")
        if self.clade_count != len(self.canonical_clade_order):
            raise ValueError("clade_count must match canonical_clade_order length")
        if self.canonical_clade_order != tuple(clade.clade_id for clade in self.clades):
            raise ValueError("canonical_clade_order must match canonical clade serialization")
        if self.partition_validated is not True:
            raise ValueError("partition_validated must be true")
        if self.uncovered_leaf_count != 0:
            raise ValueError("uncovered_leaf_count must be zero for completed snapshots")
        if tuple(clade.ordinal for clade in self.clades) != tuple(
            range(1, self.clade_count + 1)
        ):
            raise ValueError("clade ordinals must be sequential")
        if len(set(self.canonical_clade_order)) != len(self.canonical_clade_order):
            raise ValueError("clade IDs must be unique")
        if self.singleton_clade_count != sum(1 for clade in self.clades if clade.is_singleton):
            raise ValueError("singleton_clade_count is inconsistent with clades")
        if self.multi_leaf_clade_count != sum(
            1 for clade in self.clades if not clade.is_singleton
        ):
            raise ValueError("multi_leaf_clade_count is inconsistent with clades")
        if self.singleton_clade_count + self.multi_leaf_clade_count != self.clade_count:
            raise ValueError("clade size counters are inconsistent")
        observed_sizes = tuple(clade.leaf_count for clade in self.clades)
        if self.minimum_clade_size != min(observed_sizes):
            raise ValueError("minimum_clade_size is inconsistent with clades")
        if self.maximum_clade_size != max(observed_sizes):
            raise ValueError("maximum_clade_size is inconsistent with clades")
        if self.coverage_leaf_count != sum(observed_sizes):
            raise ValueError("coverage_leaf_count is inconsistent with clades")
        if self.coverage_leaf_count != self.canonical_leaf_count:
            raise ValueError("coverage_leaf_count must equal canonical_leaf_count")
        for clade in self.clades:
            if clade.max_within_clade_distance != self.max_within_clade_distance:
                raise ValueError(
                    "clade thresholds must match result max_within_clade_distance"
                )
        return self


class InferredCladeMembershipRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = CLADE_MEMBERSHIP_SCHEMA_VERSION
    clade_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    source_node_id: str = Field(min_length=1)
    leaf_count: int = Field(ge=1)
    logical_sample_count: int = Field(ge=0)
    is_singleton: bool
    leaf_labels: tuple[str, ...] = Field(default_factory=tuple)
    sequence_indices: tuple[int, ...] = Field(default_factory=tuple)
    sequence_ids: tuple[str, ...] = Field(default_factory=tuple)
    logical_sample_ids: tuple[str, ...] = Field(default_factory=tuple)
    max_pairwise_distance: float = Field(ge=0.0, le=1.0)
    max_within_clade_distance: float = Field(ge=0.0, le=1.0)
    within_threshold: bool

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != CLADE_MEMBERSHIP_SCHEMA_VERSION:
            raise ValueError("unsupported clade-membership schema version")
        return value

    @field_validator("clade_id", "source_node_id")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("membership text fields must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_content(self) -> InferredCladeMembershipRecord:
        if self.leaf_count != len(self.leaf_labels):
            raise ValueError("leaf_count must match leaf_labels length")
        if self.leaf_count != len(self.sequence_indices):
            raise ValueError("leaf_count must match sequence_indices length")
        if self.leaf_count != len(self.sequence_ids):
            raise ValueError("leaf_count must match sequence_ids length")
        if self.logical_sample_count != len(self.logical_sample_ids):
            raise ValueError("logical_sample_count must match logical_sample_ids length")
        if self.is_singleton != (self.leaf_count == 1):
            raise ValueError("is_singleton must match leaf_count")
        if not self.within_threshold:
            raise ValueError("within_threshold must be true for published memberships")
        if self.max_pairwise_distance > self.max_within_clade_distance + CLADE_DISTANCE_TOLERANCE:
            raise ValueError("max_pairwise_distance exceeds threshold")
        return self


class CladeAssignmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    clade_ordinal: int = Field(ge=1)
    clade_id: str = Field(min_length=1)
    leaf_label: str = Field(min_length=1)
    sequence_index: int = Field(ge=0)
    sequence_id: str = Field(min_length=1)
    logical_sample_ids: tuple[str, ...] = Field(default_factory=tuple)
    clade_leaf_count: int = Field(ge=1)
    clade_max_pairwise_distance: float = Field(ge=0.0, le=1.0)
    max_within_clade_distance: float = Field(ge=0.0, le=1.0)

    @field_validator("clade_id", "leaf_label", "sequence_id")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("assignment text fields must not be empty")
        return normalized

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
            raise ValueError("logical_sample_ids must be unique in assignment rows")
        return tuple(normalized)

    @model_validator(mode="after")
    def _validate_threshold(self) -> CladeAssignmentRecord:
        if (
            self.clade_max_pairwise_distance
            > self.max_within_clade_distance + CLADE_DISTANCE_TOLERANCE
        ):
            raise ValueError("assignment row maximum distance exceeds threshold")
        return self


class CladeDetectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = CLADE_DETECTION_MANIFEST_SCHEMA_VERSION
    stage_id: str = CLADE_DETECTION_STAGE_ID
    task_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    enabled: bool
    normalized_settings: ResolvedCladeDetectionConfig = Field(
        default_factory=ResolvedCladeDetectionConfig
    )
    skipped_reason: str | None = None
    status: CladeDetectionStatus
    method: AnalysisCladeDetectionMethod = AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE
    selection_policy: str = CLADE_SELECTION_POLICY_MAXIMAL_MONOPHYLETIC_SUBTREES
    max_within_clade_distance: float | None = Field(default=None, ge=0.0, le=1.0)
    float_tolerance: float = CLADE_DISTANCE_TOLERANCE
    input_distance_model: AnalysisDistanceMatrixModel = AnalysisDistanceMatrixModel.P_DISTANCE
    requested_rooting: AnalysisPhylogeneticTreeRooting | None = None
    applied_rooting: str | None = None
    tree_snapshot_manifest_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    matrix_snapshot_manifest_sha256: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    leaf_count: int = Field(ge=0)
    clade_count: int = Field(ge=0)
    singleton_clade_count: int = Field(ge=0)
    multi_leaf_clade_count: int = Field(ge=0)
    minimum_clade_size: int = Field(ge=0)
    maximum_clade_size: int = Field(ge=0)
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)
    source_artifacts: tuple[str, ...] = Field(default_factory=tuple)
    artifacts: tuple[CladeDetectionArtifactMetadata, ...] = Field(default_factory=tuple)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != CLADE_DETECTION_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported clade-detection manifest schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != CLADE_DETECTION_STAGE_ID:
            raise ValueError("invalid clade-detection stage identity")
        return value

    @field_validator("config_hash")
    @classmethod
    def _validate_config_hash(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("tree_snapshot_manifest_sha256", "matrix_snapshot_manifest_sha256")
    @classmethod
    def _normalize_snapshot_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_sha256(value)

    @field_validator("applied_rooting")
    @classmethod
    def _normalize_applied_rooting(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized == "":
            raise ValueError("applied_rooting must not be empty")
        return normalized

    @field_validator("source_artifacts")
    @classmethod
    def _normalize_source_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_relative_path(item) for item in value)

    @model_validator(mode="after")
    def _validate_content(self) -> CladeDetectionManifest:
        if self.enabled != self.normalized_settings.enabled:
            raise ValueError("enabled must match normalized clade-detection settings")
        if self.method is not self.normalized_settings.method:
            raise ValueError("method must match normalized clade-detection settings")
        if self.selection_policy != CLADE_SELECTION_POLICY_MAXIMAL_MONOPHYLETIC_SUBTREES:
            raise ValueError("unsupported selection_policy")
        if not math.isfinite(self.float_tolerance) or self.float_tolerance <= 0.0:
            raise ValueError("float_tolerance must be finite and > 0")
        if self.input_distance_model is not AnalysisDistanceMatrixModel.P_DISTANCE:
            raise ValueError("unsupported input_distance_model")

        if not self.enabled:
            if self.skipped_reason is None:
                raise ValueError("disabled clade detection requires skipped_reason")
            if self.status is not CladeDetectionStatus.COMPLETED:
                raise ValueError("disabled clade detection status must be completed")
            if (
                self.tree_snapshot_manifest_sha256 is not None
                or self.matrix_snapshot_manifest_sha256 is not None
            ):
                raise ValueError(
                    "disabled clade detection must not publish upstream snapshot digests"
                )
            if len(self.source_artifacts) != 0:
                raise ValueError(
                    "disabled clade detection must not publish upstream source references"
                )
            if len(self.artifacts) != 0:
                raise ValueError("disabled clade detection must not publish result artifacts")
            if any(
                value != 0
                for value in (
                    self.leaf_count,
                    self.clade_count,
                    self.singleton_clade_count,
                    self.multi_leaf_clade_count,
                    self.minimum_clade_size,
                    self.maximum_clade_size,
                )
            ):
                raise ValueError("disabled clade detection must publish zeroed counters")
            if self.applied_rooting is not None or self.requested_rooting is not None:
                raise ValueError(
                    "disabled clade detection must not publish rooting metadata"
                )
            if (
                self.max_within_clade_distance is None
                and self.normalized_settings.max_within_clade_distance is not None
            ):
                raise ValueError(
                    "disabled clade detection must preserve configured threshold if provided"
                )
            if (
                self.max_within_clade_distance is not None
                and self.normalized_settings.max_within_clade_distance is not None
                and not math.isclose(
                    self.max_within_clade_distance,
                    self.normalized_settings.max_within_clade_distance,
                    rel_tol=0.0,
                    abs_tol=CLADE_DISTANCE_TOLERANCE,
                )
            ):
                raise ValueError(
                    "disabled clade detection threshold is inconsistent with normalized settings"
                )
            return self

        if self.skipped_reason is not None:
            raise ValueError("enabled clade detection cannot have skipped_reason")
        if self.status is not CladeDetectionStatus.COMPLETED:
            raise ValueError("enabled clade detection manifest must be completed")
        if self.max_within_clade_distance is None:
            raise ValueError("enabled clade detection requires max_within_clade_distance")
        if self.normalized_settings.max_within_clade_distance is None:
            raise ValueError(
                "enabled normalized clade-detection settings require max_within_clade_distance"
            )
        if not math.isclose(
            self.max_within_clade_distance,
            self.normalized_settings.max_within_clade_distance,
            rel_tol=0.0,
            abs_tol=CLADE_DISTANCE_TOLERANCE,
        ):
            raise ValueError("manifest threshold must match normalized clade-detection settings")
        if (
            self.tree_snapshot_manifest_sha256 is None
            or self.matrix_snapshot_manifest_sha256 is None
        ):
            raise ValueError(
                "enabled clade detection requires tree and matrix snapshot manifest digests"
            )
        if self.source_artifacts != _EXPECTED_UPSTREAM_SOURCE_ARTIFACTS:
            raise ValueError(
                "clade-detection source artifacts must reference committed tree "
                "and matrix artifacts"
            )
        if self.leaf_count < 1:
            raise ValueError("enabled clade detection requires at least one leaf")
        if self.clade_count < 1:
            raise ValueError("enabled clade detection requires at least one clade")
        if self.singleton_clade_count + self.multi_leaf_clade_count != self.clade_count:
            raise ValueError("clade-size counters are inconsistent")
        if self.minimum_clade_size < 1 or self.maximum_clade_size < self.minimum_clade_size:
            raise ValueError("clade-size boundaries are invalid")
        if self.maximum_clade_size > self.leaf_count:
            raise ValueError("maximum_clade_size cannot exceed leaf_count")
        if self.applied_rooting is None or self.requested_rooting is None:
            raise ValueError("enabled clade detection requires rooting metadata")
        expected_artifacts = _EXPECTED_RESULT_ARTIFACTS
        published_artifacts = tuple(item.relative_path for item in self.artifacts)
        if published_artifacts != expected_artifacts:
            raise ValueError("clade-detection artifacts are missing or out of order")
        if len(set(published_artifacts)) != len(published_artifacts):
            raise ValueError("clade-detection artifact paths must be unique")
        return self


def stable_clade_id(
    sequence_indices: tuple[int, ...], *, prefix_length: int = CLADE_ID_SHA256_PREFIX_LENGTH
) -> str:
    if len(sequence_indices) == 0:
        raise ValueError("sequence_indices must not be empty")
    payload = ",".join(str(index) for index in sequence_indices)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"clade_{digest[:prefix_length]}"


def serialize_clade_memberships_jsonl(
    records: tuple[InferredCladeMembershipRecord, ...],
) -> str:
    lines = [
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        for record in records
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def serialize_clade_assignments_tsv(
    rows: tuple[CladeAssignmentRecord, ...],
) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=_CLADE_ASSIGNMENT_FIELD_NAMES,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "clade_ordinal": row.clade_ordinal,
                "clade_id": row.clade_id,
                "leaf_label": row.leaf_label,
                "sequence_index": row.sequence_index,
                "sequence_id": row.sequence_id,
                "logical_sample_ids": json.dumps(
                    list(row.logical_sample_ids),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "clade_leaf_count": row.clade_leaf_count,
                # repr(float) is a canonical lossless representation for round-trip parsing.
                "clade_max_pairwise_distance": repr(row.clade_max_pairwise_distance),
                "max_within_clade_distance": repr(row.max_within_clade_distance),
            }
        )
    return buffer.getvalue()


def parse_clade_assignments_tsv(payload: str) -> tuple[CladeAssignmentRecord, ...]:
    reader = csv.DictReader(io.StringIO(payload), delimiter="\t")
    if reader.fieldnames != list(_CLADE_ASSIGNMENT_FIELD_NAMES):
        raise ValueError("clade_assignments.tsv has an unexpected header")
    rows: list[CladeAssignmentRecord] = []
    for row in reader:
        if row is None:
            continue
        if None in row:
            raise ValueError("clade_assignments.tsv contains extra columns")
        if any(row.get(field_name) is None for field_name in _CLADE_ASSIGNMENT_FIELD_NAMES):
            raise ValueError("clade_assignments.tsv row has missing columns")
        logical_sample_payload = row["logical_sample_ids"]
        try:
            logical_sample_values = json.loads(logical_sample_payload)
        except json.JSONDecodeError as error:
            raise ValueError("logical_sample_ids must be a JSON array") from error
        if not isinstance(logical_sample_values, list) or any(
            not isinstance(item, str) for item in logical_sample_values
        ):
            raise ValueError("logical_sample_ids must be a JSON array of strings")
        try:
            clade_ordinal = int(row["clade_ordinal"])
            sequence_index = int(row["sequence_index"])
            clade_leaf_count = int(row["clade_leaf_count"])
        except ValueError as error:
            raise ValueError("clade_assignments.tsv integer fields are invalid") from error
        try:
            clade_max_pairwise_distance = float(row["clade_max_pairwise_distance"])
            max_within_clade_distance = float(row["max_within_clade_distance"])
        except ValueError as error:
            raise ValueError("clade_assignments.tsv float fields are invalid") from error
        rows.append(
            CladeAssignmentRecord(
                clade_ordinal=clade_ordinal,
                clade_id=str(row["clade_id"]),
                leaf_label=str(row["leaf_label"]),
                sequence_index=sequence_index,
                sequence_id=str(row["sequence_id"]),
                logical_sample_ids=tuple(str(item) for item in logical_sample_values),
                clade_leaf_count=clade_leaf_count,
                clade_max_pairwise_distance=clade_max_pairwise_distance,
                max_within_clade_distance=max_within_clade_distance,
            )
        )
    return tuple(rows)


def artifact_metadata(
    path: Path,
    *,
    record_count: int | None = None,
    relative_to: Path | None = None,
    relative_path: str | None = None,
) -> CladeDetectionArtifactMetadata:
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
    return CladeDetectionArtifactMetadata(
        relative_path=resolved_relative_path,
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
        record_count=record_count,
    )


def clade_detection_artifact_paths(manifest: CladeDetectionManifest) -> tuple[str, ...]:
    if not manifest.enabled:
        return (CLADE_DETECTION_MANIFEST_RELATIVE_PATH,)
    return (
        CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
        *(metadata.relative_path for metadata in manifest.artifacts),
    )


__all__ = [
    "CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH",
    "CLADE_DETECTION_MANIFEST_RELATIVE_PATH",
    "CLADE_DETECTION_MANIFEST_SCHEMA_VERSION",
    "CLADE_DETECTION_STAGE_ID",
    "CLADE_DISTANCE_TOLERANCE",
    "CLADE_ID_SHA256_PREFIX_LENGTH",
    "CLADE_INTERPRETATION_SCOPE_CURRENT_ROOTED_TREE",
    "CLADE_MEMBERSHIP_SCHEMA_VERSION",
    "CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH",
    "CLADE_SELECTION_POLICY_MAXIMAL_MONOPHYLETIC_SUBTREES",
    "INFERRED_CLADES_JSON_RELATIVE_PATH",
    "INFERRED_CLADES_RESULT_SCHEMA_VERSION",
    "CladeAssignmentRecord",
    "CladeDetectionArtifactMetadata",
    "CladeDetectionManifest",
    "CladeDetectionStatus",
    "InferredClade",
    "InferredCladeMember",
    "InferredCladeMembershipRecord",
    "InferredCladesResult",
    "artifact_metadata",
    "clade_detection_artifact_paths",
    "parse_clade_assignments_tsv",
    "serialize_clade_assignments_tsv",
    "serialize_clade_memberships_jsonl",
    "stable_clade_id",
]
