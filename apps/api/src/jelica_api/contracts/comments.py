from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProjectCommentReaction = Literal["support", "oppose"]


class ProjectCommentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    body: str = Field(min_length=1, max_length=10_000)


class ProjectCommentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    body: str = Field(min_length=1, max_length=10_000)


class ProjectCommentMentionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1)
    username: str = Field(min_length=1, max_length=64)


class ProjectCommentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    author_user_id: str = Field(min_length=1)
    author_username: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=10_000)
    created_at: datetime
    edited_at: datetime | None
    mentions: tuple["ProjectCommentMentionResponse", ...] = ()


class ProjectCommentReactionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    support: int = Field(ge=0)
    oppose: int = Field(ge=0)
    current_user_reaction: ProjectCommentReaction | None


class ProjectCommentListItemResponse(ProjectCommentResponse):
    reaction_summary: ProjectCommentReactionSummaryResponse


class ProjectCommentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProjectCommentListItemResponse, ...] = ()


class ProjectCommentReactionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reaction: ProjectCommentReaction


__all__ = [
    "ProjectCommentCreateRequest",
    "ProjectCommentListResponse",
    "ProjectCommentListItemResponse",
    "ProjectCommentMentionResponse",
    "ProjectCommentReaction",
    "ProjectCommentReactionSummaryResponse",
    "ProjectCommentReactionUpdateRequest",
    "ProjectCommentResponse",
    "ProjectCommentUpdateRequest",
]
