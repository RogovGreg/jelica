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


class TaskResultLookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    trace_id: str | None = None
    state: str = Field(min_length=1)
    available: bool
    status_command_id: str = Field(min_length=1)
    result_reference: TaskResultPackageReference | None = None
    detail: str | None = None


__all__ = [
    "TaskListItem",
    "TaskListResponse",
    "TaskResultLookupResponse",
    "TaskResultPackageReference",
    "TaskStatusSnapshot",
]
