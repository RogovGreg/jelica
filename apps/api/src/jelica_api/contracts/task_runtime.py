from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskStatusSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    project_id: str | None = None
    trace_id: str | None = None
    state: str = Field(min_length=1)
    active_job_state: str | None = None
    current_stage: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    command_id: str | None = Field(default=None, min_length=1)
    state_source: Literal["core", "projection_cache"] = "core"
    authoritative: bool = True
    projection_updated_at: datetime | None = None
    stale_state: bool = False
    detail: str | None = None
    can_control_lifecycle: bool = False


class TaskListItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    owner_user_id: str | None = None
    project_id: str | None = None
    trace_id: str | None = None
    state: str = Field(min_length=1)
    active_job_state: str | None = None
    current_stage: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    command_id: str | None = Field(default=None, min_length=1)
    created_at: datetime
    updated_at: datetime
    state_source: Literal["core", "projection_cache"] = "projection_cache"
    authoritative: bool = False
    projection_updated_at: datetime | None = None
    stale_state: bool = True
    detail: str | None = None


class TaskListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[TaskListItem, ...] = ()


class TaskResultPackageReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_id: str = Field(min_length=1)
    package_path: str = Field(min_length=1)
    command_id: str = Field(min_length=1)


class TaskResultOverviewSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str = Field(min_length=1)
    sequence_id: str | None = None
    original_record_id: str | None = None
    original_description: str | None = None
    source_reference: str = Field(min_length=1)
    validation_status: str = Field(min_length=1)
    eligible_for_analysis: bool
    source_length: int | None = Field(default=None, ge=0)
    gc_content: float | None = Field(default=None, ge=0.0, le=1.0)
    ambiguous_count: int | None = Field(default=None, ge=0)


class TaskResultInputOverview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid_sample_count: int = Field(ge=0)
    invalid_sample_count: int = Field(ge=0)
    unique_sequence_count: int = Field(ge=0)
    duplicate_logical_sample_count: int = Field(ge=0)
    samples: tuple[TaskResultOverviewSample, ...] = ()


class TaskResultAlignmentOverview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alignment_length: int | None = Field(default=None, ge=0)
    logical_sample_count: int = Field(ge=0)
    unique_sequence_count: int = Field(ge=0)
    mode: str = Field(min_length=1)
    engine: str | None = None


class TaskResultSequenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    sequence_id: str = Field(min_length=1)
    logical_sample_ids: tuple[str, ...] = ()


class TaskResultDistancePair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    left_sequence_id: str = Field(min_length=1)
    right_sequence_id: str = Field(min_length=1)
    left_index: int = Field(ge=0)
    right_index: int = Field(ge=0)
    mismatch_count: int = Field(ge=0)
    comparable_site_count: int = Field(ge=0)
    excluded_gap_site_count: int = Field(ge=0)
    excluded_ambiguous_site_count: int = Field(ge=0)
    distance: float | None = Field(default=None, ge=0.0, le=1.0)
    state: str = Field(min_length=1)


class TaskResultDistanceOverview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    expected_pair_count: int = Field(ge=0)
    processed_pair_count: int = Field(ge=0)
    defined_distance_count: int = Field(ge=0)
    undefined_distance_count: int = Field(ge=0)
    sequence_references: tuple[TaskResultSequenceReference, ...] = ()
    matrix: tuple[tuple[float | None, ...], ...] = ()
    pairs: tuple[TaskResultDistancePair, ...] = ()


class TaskResultTreeLeafMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    leaf_label: str = Field(min_length=1)
    sequence_index: int = Field(ge=0)
    sequence_id: str = Field(min_length=1)
    logical_sample_ids: tuple[str, ...] = ()


class TaskResultTreeNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    leaf_label: str | None = None
    sequence_index: int | None = Field(default=None, ge=0)
    sequence_id: str | None = None
    logical_sample_ids: tuple[str, ...] = ()


class TaskResultTreeEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    branch_length: float


class TaskResultRootedTree(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rooted: bool
    root_id: str | None = None
    traversal_root_id: str = Field(min_length=1)
    node_count: int = Field(ge=1)
    edge_count: int = Field(ge=0)
    nodes: tuple[TaskResultTreeNode, ...] = ()
    edges: tuple[TaskResultTreeEdge, ...] = ()


class TaskResultTreeOverview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str = Field(min_length=1)
    applied_rooting: str = Field(min_length=1)
    canonical_leaf_order: tuple[str, ...] = ()
    leaf_mappings: tuple[TaskResultTreeLeafMapping, ...] = ()
    rooted: TaskResultRootedTree


class TaskResultOverview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    input_processing: TaskResultInputOverview | None = None
    alignment: TaskResultAlignmentOverview | None = None
    distance_matrix: TaskResultDistanceOverview | None = None
    phylogenetic_tree: TaskResultTreeOverview | None = None


class TaskResultLookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    trace_id: str | None = None
    state: str = Field(min_length=1)
    available: bool
    status_command_id: str = Field(min_length=1)
    result_reference: TaskResultPackageReference | None = None
    overview: TaskResultOverview | None = None
    detail: str | None = None


__all__ = [
    "TaskListItem",
    "TaskListResponse",
    "TaskResultLookupResponse",
    "TaskResultOverview",
    "TaskResultPackageReference",
    "TaskStatusSnapshot",
]
