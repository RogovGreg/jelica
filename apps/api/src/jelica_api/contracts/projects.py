from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProjectStatus = Literal["active", "frozen"]
ProjectRelation = Literal["any", "owned", "participating"]
ProjectMemberRole = Literal["viewer", "commenter", "member", "supervisor"]
ProjectHistoryEventType = Literal[
    "project_created",
    "project_updated",
    "project_frozen",
    "project_unfrozen",
    "member_joined",
    "member_removed",
    "member_role_changed",
    "ownership_transferred",
    "task_attached",
    "task_detached",
]


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: ProjectStatus = "active"


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    status: ProjectStatus | None = None


class ProjectTransferOwnershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    new_owner_user_id: str = Field(min_length=1)


class ProjectMemberUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ProjectMemberRole


class ProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    description: str | None
    status: ProjectStatus
    created_by_user_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    current_user_role: ProjectMemberRole | None = None
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProjectResponse, ...] = ()


class ProjectMemberResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=320)
    role: ProjectMemberRole
    joined_at: datetime


class ProjectMemberListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProjectMemberResponse, ...] = ()


class ProjectTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    name: str | None
    state: str = Field(min_length=1)
    owner_user_id: str | None
    project_id: str | None
    created_at: datetime
    updated_at: datetime


class ProjectTaskListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProjectTaskResponse, ...] = ()


class ProjectHistoryEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    actor_user_id: str | None
    subject_user_id: str | None
    event_type: ProjectHistoryEventType
    data: dict[str, object] | None
    occurred_at: datetime


class ProjectHistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProjectHistoryEventResponse, ...] = ()


__all__ = [
    "ProjectCreateRequest",
    "ProjectHistoryEventResponse",
    "ProjectHistoryEventType",
    "ProjectHistoryListResponse",
    "ProjectListResponse",
    "ProjectMemberListResponse",
    "ProjectMemberResponse",
    "ProjectMemberRole",
    "ProjectMemberUpdateRequest",
    "ProjectRelation",
    "ProjectResponse",
    "ProjectStatus",
    "ProjectTaskListResponse",
    "ProjectTaskResponse",
    "ProjectTransferOwnershipRequest",
    "ProjectUpdateRequest",
]
