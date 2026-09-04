from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .projects import ProjectMemberRole

ProjectInvitationStatus = Literal[
    "pending",
    "accepted",
    "declined",
    "revoked",
    "expired",
]


class ProjectInvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invited_user_id: str = Field(min_length=1)
    role: ProjectMemberRole


class ProjectInvitationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invitation_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    project_name: str = Field(min_length=1, max_length=200)
    invited_user_id: str = Field(min_length=1)
    invited_username: str = Field(min_length=1, max_length=64)
    invited_by_user_id: str = Field(min_length=1)
    inviter_username: str = Field(min_length=1, max_length=64)
    role: ProjectMemberRole
    status: ProjectInvitationStatus
    invited_at: datetime
    expires_at: datetime
    resolved_at: datetime | None


class ProjectInvitationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProjectInvitationResponse, ...] = ()


class ProjectInvitationCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1)
    username: str = Field(min_length=1, max_length=64)


class ProjectInvitationCandidateListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProjectInvitationCandidateResponse, ...] = ()


__all__ = [
    "ProjectInvitationCandidateListResponse",
    "ProjectInvitationCandidateResponse",
    "ProjectInvitationCreateRequest",
    "ProjectInvitationListResponse",
    "ProjectInvitationResponse",
    "ProjectInvitationStatus",
]
