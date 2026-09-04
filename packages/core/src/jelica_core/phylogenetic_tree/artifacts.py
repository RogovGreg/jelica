from __future__ import annotations

import hashlib
import math
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jelica_core.config.models import (
    AnalysisDistanceMatrixModel,
    AnalysisPhylogeneticTreeMethod,
    AnalysisPhylogeneticTreeRooting,
    ResolvedPhylogeneticTreeConfig,
)
from jelica_core.distance_matrix import (
    DISTANCE_MATRIX_JSON_RELATIVE_PATH,
    DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
)

PHYLOGENETIC_TREE_STAGE_ID = "phylogenetic_tree"
PHYLOGENETIC_TREE_MANIFEST_SCHEMA_VERSION = 1
PHYLOGENETIC_TREE_RESULT_SCHEMA_VERSION = 1
PHYLOGENETIC_TREE_DIAGNOSTICS_SCHEMA_VERSION = 1

PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH = (
    "phylogenetic_tree/phylogenetic_tree_manifest.json"
)
TREE_UNROOTED_NWK_RELATIVE_PATH = "phylogenetic_tree/tree_unrooted.nwk"
TREE_ROOTED_NWK_RELATIVE_PATH = "phylogenetic_tree/tree_rooted.nwk"
TREE_JSON_RELATIVE_PATH = "phylogenetic_tree/tree.json"
TREE_DIAGNOSTICS_RELATIVE_PATH = "phylogenetic_tree/tree_diagnostics.json"

NEGATIVE_BRANCH_POLICY_CLAMP_TO_ZERO = "clamp_to_zero_before_midpoint_rooting"
ZERO_DIAMETER_ROOTING_FALLBACK = "deterministic_zero_diameter_fallback"

_EXPECTED_UPSTREAM_SOURCE_ARTIFACTS = (
    f"stages/distance_matrix/{DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH}",
    f"stages/distance_matrix/{DISTANCE_MATRIX_JSON_RELATIVE_PATH}",
)


class PhylogeneticTreeStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class PhylogeneticTreeConstructionMode(StrEnum):
    NEIGHBOR_JOINING = "neighbor_joining"
    TRIVIAL_SINGLETON = "trivial_singleton"
    TRIVIAL_PAIR = "trivial_pair"


class TreeNodeKind(StrEnum):
    LEAF = "leaf"
    INTERNAL = "internal"
    ROOT = "root"


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


class PhylogeneticTreeArtifactMetadata(BaseModel):
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


class PhylogeneticTreeWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)

    @field_validator("code", "detail")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("warning fields must not be empty")
        return normalized


class PhylogeneticTreeLeafMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    leaf_label: str = Field(min_length=1)
    sequence_index: int = Field(ge=0)
    sequence_id: str = Field(min_length=1)
    logical_sample_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("leaf_label")
    @classmethod
    def _normalize_leaf_label(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("leaf_label must not be empty")
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
            raise ValueError("logical_sample_ids must be unique per leaf mapping")
        return tuple(normalized)


class PhylogeneticTreeNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1)
    kind: TreeNodeKind
    leaf_label: str | None = None
    sequence_index: int | None = Field(default=None, ge=0)
    sequence_id: str | None = None
    logical_sample_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("node_id")
    @classmethod
    def _normalize_node_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("node_id must not be empty")
        return normalized

    @field_validator("leaf_label")
    @classmethod
    def _normalize_leaf_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized == "":
            raise ValueError("leaf_label must not be empty")
        return normalized

    @field_validator("sequence_id")
    @classmethod
    def _normalize_sequence_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized == "":
            raise ValueError("sequence_id must not be empty")
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
            raise ValueError("logical_sample_ids must be unique per node")
        return tuple(normalized)

    @model_validator(mode="after")
    def _validate_leaf_fields(self) -> PhylogeneticTreeNode:
        if self.kind is TreeNodeKind.LEAF:
            if (
                self.leaf_label is None
                or self.sequence_index is None
                or self.sequence_id is None
            ):
                raise ValueError(
                    "leaf nodes must include leaf_label, sequence_index, and sequence_id"
                )
            return self
        if (
            self.leaf_label is not None
            or self.sequence_index is not None
            or self.sequence_id is not None
            or len(self.logical_sample_ids) > 0
        ):
            raise ValueError("non-leaf nodes must not include leaf mapping data")
        return self


class PhylogeneticTreeEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    branch_length: float

    @field_validator("parent_id", "child_id")
    @classmethod
    def _normalize_node_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("edge node references must not be empty")
        return normalized

    @field_validator("branch_length")
    @classmethod
    def _validate_branch_length(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("branch_length must be finite")
        return value

    @model_validator(mode="after")
    def _validate_parent_child_distinct(self) -> PhylogeneticTreeEdge:
        if self.parent_id == self.child_id:
            raise ValueError("parent_id and child_id must be distinct")
        return self


class PhylogeneticTreeRepresentation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rooted: bool
    root_id: str | None = None
    traversal_root_id: str = Field(min_length=1)
    node_count: int = Field(ge=1)
    edge_count: int = Field(ge=0)
    nodes: tuple[PhylogeneticTreeNode, ...] = Field(default_factory=tuple)
    edges: tuple[PhylogeneticTreeEdge, ...] = Field(default_factory=tuple)

    @field_validator("root_id")
    @classmethod
    def _normalize_root_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized == "":
            raise ValueError("root_id must not be empty")
        return normalized

    @field_validator("traversal_root_id")
    @classmethod
    def _normalize_traversal_root_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("traversal_root_id must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_tree(self) -> PhylogeneticTreeRepresentation:
        if self.node_count != len(self.nodes):
            raise ValueError("node_count must match nodes length")
        if self.edge_count != len(self.edges):
            raise ValueError("edge_count must match edges length")

        node_ids = tuple(node.node_id for node in self.nodes)
        node_id_set = set(node_ids)
        if len(node_id_set) != len(node_ids):
            raise ValueError("node_id values must be unique")
        if self.traversal_root_id not in node_id_set:
            raise ValueError("traversal_root_id must reference an existing node")

        if self.rooted:
            if self.root_id is None:
                raise ValueError("rooted representation must define root_id")
            if self.root_id != self.traversal_root_id:
                raise ValueError(
                    "rooted representation must use the same root for traversal and root_id"
                )
        elif self.root_id is not None:
            raise ValueError("unrooted representation must not define root_id")

        incoming_count = {node_id: 0 for node_id in node_ids}
        for edge in self.edges:
            if edge.parent_id not in node_id_set or edge.child_id not in node_id_set:
                raise ValueError("edges must reference existing node IDs")
            incoming_count[edge.child_id] += 1

        if incoming_count[self.traversal_root_id] != 0:
            raise ValueError("traversal root cannot have incoming edges")
        for node_id, count in incoming_count.items():
            if node_id == self.traversal_root_id:
                continue
            if count != 1:
                raise ValueError(
                    "every non-root node must have exactly one incoming edge"
                )

        if len(self.nodes) == 1 and len(self.edges) != 0:
            raise ValueError("single-node trees cannot contain edges")
        if len(self.nodes) > 1 and len(self.edges) != len(self.nodes) - 1:
            raise ValueError("tree representation must contain node_count - 1 edges")

        if self.rooted:
            root_node = next(node for node in self.nodes if node.node_id == self.root_id)
            if root_node.kind is not TreeNodeKind.ROOT:
                raise ValueError("root_id must reference a root node")
        return self


class PhylogeneticTreeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = PHYLOGENETIC_TREE_RESULT_SCHEMA_VERSION
    stage_id: str = PHYLOGENETIC_TREE_STAGE_ID
    method: AnalysisPhylogeneticTreeMethod = (
        AnalysisPhylogeneticTreeMethod.NEIGHBOR_JOINING
    )
    construction_mode: PhylogeneticTreeConstructionMode
    inference_performed: bool
    requested_rooting: AnalysisPhylogeneticTreeRooting = (
        AnalysisPhylogeneticTreeRooting.MIDPOINT
    )
    applied_rooting: str = Field(min_length=1)
    negative_branch_policy: str = NEGATIVE_BRANCH_POLICY_CLAMP_TO_ZERO
    input_distance_model: AnalysisDistanceMatrixModel = AnalysisDistanceMatrixModel.P_DISTANCE
    input_snapshot_manifest_sha256: str = Field(min_length=64, max_length=64)
    canonical_leaf_order: tuple[str, ...] = Field(default_factory=tuple)
    leaf_mappings: tuple[PhylogeneticTreeLeafMapping, ...] = Field(default_factory=tuple)
    unrooted: PhylogeneticTreeRepresentation
    rooted: PhylogeneticTreeRepresentation
    raw_negative_branch_count: int = Field(ge=0)
    minimum_raw_branch_length: float | None = None
    normalized_negative_branch_count: int = Field(ge=0)
    zero_diameter: bool
    node_count: int = Field(ge=1)
    edge_count: int = Field(ge=0)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != PHYLOGENETIC_TREE_RESULT_SCHEMA_VERSION:
            raise ValueError("unsupported phylogenetic-tree result schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != PHYLOGENETIC_TREE_STAGE_ID:
            raise ValueError("invalid phylogenetic-tree stage identity")
        return value

    @field_validator("input_snapshot_manifest_sha256")
    @classmethod
    def _normalize_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("applied_rooting", "negative_branch_policy")
    @classmethod
    def _normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("text field must not be empty")
        return normalized

    @field_validator("minimum_raw_branch_length")
    @classmethod
    def _validate_optional_branch_length(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("minimum_raw_branch_length must be finite")
        return value

    @field_validator("canonical_leaf_order")
    @classmethod
    def _normalize_leaf_order(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if len(normalized) == 0:
            raise ValueError("canonical_leaf_order must not be empty")
        if any(item == "" for item in normalized):
            raise ValueError("canonical_leaf_order must not contain empty values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("canonical_leaf_order must contain unique labels")
        return normalized

    @model_validator(mode="after")
    def _validate_result(self) -> PhylogeneticTreeResult:
        if self.node_count != self.rooted.node_count:
            raise ValueError("node_count must match rooted.node_count")
        if self.edge_count != self.rooted.edge_count:
            raise ValueError("edge_count must match rooted.edge_count")
        if self.unrooted.rooted:
            raise ValueError("unrooted representation must have rooted=false")
        if not self.rooted.rooted:
            raise ValueError("rooted representation must have rooted=true")

        if len(self.canonical_leaf_order) != len(self.leaf_mappings):
            raise ValueError("leaf_mappings must align with canonical_leaf_order")
        for index, mapping in enumerate(self.leaf_mappings):
            if mapping.sequence_index != index:
                raise ValueError("leaf mapping sequence indexes must follow canonical order")
            if mapping.leaf_label != self.canonical_leaf_order[index]:
                raise ValueError("leaf mappings must follow canonical_leaf_order")

        canonical_leaf_set = set(self.canonical_leaf_order)
        unrooted_leaf_nodes = {
            node.leaf_label: node
            for node in self.unrooted.nodes
            if node.kind is TreeNodeKind.LEAF
        }
        rooted_leaf_nodes = {
            node.leaf_label: node
            for node in self.rooted.nodes
            if node.kind is TreeNodeKind.LEAF
        }
        if set(unrooted_leaf_nodes) != canonical_leaf_set:
            raise ValueError("unrooted leaf labels are inconsistent with canonical order")
        if set(rooted_leaf_nodes) != canonical_leaf_set:
            raise ValueError("rooted leaf labels are inconsistent with canonical order")

        for mapping in self.leaf_mappings:
            unrooted_leaf = unrooted_leaf_nodes[mapping.leaf_label]
            rooted_leaf = rooted_leaf_nodes[mapping.leaf_label]
            if (
                unrooted_leaf.sequence_index != mapping.sequence_index
                or unrooted_leaf.sequence_id != mapping.sequence_id
                or unrooted_leaf.logical_sample_ids != mapping.logical_sample_ids
                or rooted_leaf.sequence_index != mapping.sequence_index
                or rooted_leaf.sequence_id != mapping.sequence_id
                or rooted_leaf.logical_sample_ids != mapping.logical_sample_ids
            ):
                raise ValueError("tree leaf metadata is inconsistent with leaf mappings")

        if (
            self.construction_mode is PhylogeneticTreeConstructionMode.NEIGHBOR_JOINING
        ) != self.inference_performed:
            raise ValueError(
                "inference_performed must be true only for neighbor_joining construction"
            )
        if self.raw_negative_branch_count == 0 and self.normalized_negative_branch_count != 0:
            raise ValueError(
                "normalized_negative_branch_count cannot be positive without raw negatives"
            )
        if self.normalized_negative_branch_count > self.raw_negative_branch_count:
            raise ValueError(
                "normalized_negative_branch_count cannot exceed raw_negative_branch_count"
            )
        if (
            self.applied_rooting == ZERO_DIAMETER_ROOTING_FALLBACK
            and not self.zero_diameter
        ):
            raise ValueError(
                "zero-diameter fallback rooting can only be used for zero-diameter inputs"
            )
        return self


class PhylogeneticTreeDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = PHYLOGENETIC_TREE_DIAGNOSTICS_SCHEMA_VERSION
    stage_id: str = PHYLOGENETIC_TREE_STAGE_ID
    leaf_count: int = Field(ge=1)
    internal_node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    input_matrix_dimensions: tuple[int, int]
    input_distance_min: float = Field(ge=0.0)
    input_distance_max: float = Field(ge=0.0)
    tree_diameter: float = Field(ge=0.0)
    zero_distance_pair_count: int = Field(ge=0)
    zero_diameter: bool
    raw_negative_branch_count: int = Field(ge=0)
    minimum_raw_branch_length: float | None = None
    normalized_negative_branch_count: int = Field(ge=0)
    requested_rooting: AnalysisPhylogeneticTreeRooting = (
        AnalysisPhylogeneticTreeRooting.MIDPOINT
    )
    applied_rooting: str = Field(min_length=1)
    construction_mode: PhylogeneticTreeConstructionMode
    warnings: tuple[PhylogeneticTreeWarning, ...] = Field(default_factory=tuple)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != PHYLOGENETIC_TREE_DIAGNOSTICS_SCHEMA_VERSION:
            raise ValueError("unsupported phylogenetic-tree diagnostics schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != PHYLOGENETIC_TREE_STAGE_ID:
            raise ValueError("invalid phylogenetic-tree stage identity")
        return value

    @field_validator("minimum_raw_branch_length")
    @classmethod
    def _validate_optional_branch_length(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("minimum_raw_branch_length must be finite")
        return value

    @field_validator("applied_rooting")
    @classmethod
    def _normalize_applied_rooting(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("applied_rooting must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_content(self) -> PhylogeneticTreeDiagnostics:
        rows, columns = self.input_matrix_dimensions
        if rows != columns:
            raise ValueError("input_matrix_dimensions must be square")
        if rows != self.leaf_count:
            raise ValueError("input_matrix_dimensions must match leaf_count")
        if self.input_distance_max < self.input_distance_min:
            raise ValueError("input_distance_max cannot be smaller than input_distance_min")
        if (
            self.applied_rooting == ZERO_DIAMETER_ROOTING_FALLBACK
            and not self.zero_diameter
        ):
            raise ValueError("zero-diameter fallback requires zero_diameter=true")
        if self.raw_negative_branch_count == 0 and self.normalized_negative_branch_count != 0:
            raise ValueError(
                "normalized_negative_branch_count requires raw negative branches"
            )
        if self.normalized_negative_branch_count > self.raw_negative_branch_count:
            raise ValueError(
                "normalized_negative_branch_count cannot exceed raw_negative_branch_count"
            )
        return self


class PhylogeneticTreeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = PHYLOGENETIC_TREE_MANIFEST_SCHEMA_VERSION
    stage_id: str = PHYLOGENETIC_TREE_STAGE_ID
    task_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    enabled: bool
    normalized_settings: ResolvedPhylogeneticTreeConfig = Field(
        default_factory=ResolvedPhylogeneticTreeConfig
    )
    skipped_reason: str | None = None
    status: PhylogeneticTreeStatus
    method: AnalysisPhylogeneticTreeMethod = (
        AnalysisPhylogeneticTreeMethod.NEIGHBOR_JOINING
    )
    requested_rooting: AnalysisPhylogeneticTreeRooting = (
        AnalysisPhylogeneticTreeRooting.MIDPOINT
    )
    applied_rooting: str = Field(min_length=1)
    construction_mode: PhylogeneticTreeConstructionMode
    inference_performed: bool
    input_distance_model: AnalysisDistanceMatrixModel = AnalysisDistanceMatrixModel.P_DISTANCE
    input_snapshot_manifest_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    leaf_count: int = Field(ge=0)
    internal_node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    has_negative_branches: bool
    raw_negative_branch_count: int = Field(ge=0)
    zero_diameter: bool
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)
    source_artifacts: tuple[str, ...] = Field(default_factory=tuple)
    artifacts: tuple[PhylogeneticTreeArtifactMetadata, ...] = Field(default_factory=tuple)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != PHYLOGENETIC_TREE_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported phylogenetic-tree manifest schema version")
        return value

    @field_validator("stage_id")
    @classmethod
    def _validate_stage_id(cls, value: str) -> str:
        if value != PHYLOGENETIC_TREE_STAGE_ID:
            raise ValueError("invalid phylogenetic-tree stage identity")
        return value

    @field_validator("config_hash")
    @classmethod
    def _validate_config_hash(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("input_snapshot_manifest_sha256")
    @classmethod
    def _validate_snapshot_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_sha256(value)

    @field_validator("applied_rooting")
    @classmethod
    def _normalize_applied_rooting(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("applied_rooting must not be empty")
        return normalized

    @field_validator("source_artifacts")
    @classmethod
    def _normalize_source_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_relative_path(item) for item in value)

    @model_validator(mode="after")
    def _validate_manifest(self) -> PhylogeneticTreeManifest:
        if self.enabled != self.normalized_settings.enabled:
            raise ValueError("enabled must match normalized phylogenetic-tree settings")
        if self.method is not self.normalized_settings.method:
            raise ValueError("method must match normalized phylogenetic-tree settings")
        if self.requested_rooting is not self.normalized_settings.rooting:
            raise ValueError(
                "requested_rooting must match normalized phylogenetic-tree settings"
            )

        if (
            self.construction_mode is PhylogeneticTreeConstructionMode.NEIGHBOR_JOINING
        ) != self.inference_performed:
            raise ValueError(
                "inference_performed must be true only for neighbor_joining construction"
            )

        if self.has_negative_branches != (self.raw_negative_branch_count > 0):
            raise ValueError(
                "has_negative_branches must match raw_negative_branch_count"
            )

        if not self.enabled:
            if self.skipped_reason is None:
                raise ValueError("disabled phylogenetic-tree stage requires skipped_reason")
            if self.status is not PhylogeneticTreeStatus.COMPLETED:
                raise ValueError("disabled phylogenetic-tree status must be completed")
            if self.input_snapshot_manifest_sha256 is not None:
                raise ValueError(
                    "disabled phylogenetic-tree stage must not include input snapshot digest"
                )
            if (
                self.leaf_count != 0
                or self.internal_node_count != 0
                or self.edge_count != 0
                or self.raw_negative_branch_count != 0
                or self.has_negative_branches
                or self.zero_diameter
            ):
                raise ValueError(
                    "disabled phylogenetic-tree stage must publish zeroed counters"
                )
            if len(self.source_artifacts) != 0:
                raise ValueError(
                    "disabled phylogenetic-tree stage must not list source artifacts"
                )
            if len(self.artifacts) != 0:
                raise ValueError(
                    "disabled phylogenetic-tree stage must not list result artifacts"
                )
            return self

        if self.skipped_reason is not None:
            raise ValueError("enabled phylogenetic-tree stage cannot have skipped_reason")
        if self.status is not PhylogeneticTreeStatus.COMPLETED:
            raise ValueError(
                "enabled phylogenetic-tree stage manifest must be completed"
            )
        if self.input_snapshot_manifest_sha256 is None:
            raise ValueError(
                "enabled phylogenetic-tree stage requires input snapshot digest"
            )
        if self.leaf_count < 1:
            raise ValueError("enabled phylogenetic-tree stage requires at least one leaf")
        if self.edge_count < 0:
            raise ValueError("edge_count must not be negative")
        if self.source_artifacts != _EXPECTED_UPSTREAM_SOURCE_ARTIFACTS:
            raise ValueError(
                "phylogenetic-tree source artifacts must reference committed distance-matrix artifacts"
            )

        expected_artifacts = (
            TREE_UNROOTED_NWK_RELATIVE_PATH,
            TREE_ROOTED_NWK_RELATIVE_PATH,
            TREE_JSON_RELATIVE_PATH,
            TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
        published_artifacts = tuple(item.relative_path for item in self.artifacts)
        if published_artifacts != expected_artifacts:
            raise ValueError("phylogenetic-tree artifacts are missing or out of order")
        if len(set(published_artifacts)) != len(published_artifacts):
            raise ValueError("phylogenetic-tree artifact paths must be unique")
        return self


def artifact_metadata(
    path: Path,
    *,
    record_count: int | None = None,
    relative_to: Path | None = None,
    relative_path: str | None = None,
) -> PhylogeneticTreeArtifactMetadata:
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
    return PhylogeneticTreeArtifactMetadata(
        relative_path=resolved_relative_path,
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
        record_count=record_count,
    )


def phylogenetic_tree_artifact_paths(manifest: PhylogeneticTreeManifest) -> tuple[str, ...]:
    if not manifest.enabled:
        return (PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,)
    return (
        PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
        *(metadata.relative_path for metadata in manifest.artifacts),
    )


__all__ = [
    "NEGATIVE_BRANCH_POLICY_CLAMP_TO_ZERO",
    "PHYLOGENETIC_TREE_DIAGNOSTICS_SCHEMA_VERSION",
    "PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH",
    "PHYLOGENETIC_TREE_MANIFEST_SCHEMA_VERSION",
    "PHYLOGENETIC_TREE_RESULT_SCHEMA_VERSION",
    "PHYLOGENETIC_TREE_STAGE_ID",
    "TREE_DIAGNOSTICS_RELATIVE_PATH",
    "TREE_JSON_RELATIVE_PATH",
    "TREE_ROOTED_NWK_RELATIVE_PATH",
    "TREE_UNROOTED_NWK_RELATIVE_PATH",
    "ZERO_DIAMETER_ROOTING_FALLBACK",
    "PhylogeneticTreeArtifactMetadata",
    "PhylogeneticTreeConstructionMode",
    "PhylogeneticTreeDiagnostics",
    "PhylogeneticTreeEdge",
    "PhylogeneticTreeLeafMapping",
    "PhylogeneticTreeManifest",
    "PhylogeneticTreeNode",
    "PhylogeneticTreeRepresentation",
    "PhylogeneticTreeResult",
    "PhylogeneticTreeStatus",
    "PhylogeneticTreeWarning",
    "TreeNodeKind",
    "artifact_metadata",
    "phylogenetic_tree_artifact_paths",
]
