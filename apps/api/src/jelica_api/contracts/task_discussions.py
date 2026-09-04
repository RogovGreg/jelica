from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TaskDiscussionReaction = Literal["support", "oppose"]


class TaskDiscussionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    available: bool
    project_id: str | None
    mode: Literal["unavailable", "collaborative", "read_only"]
    is_task_owner: bool = False


class TaskDiscussionCommentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    body: str = Field(min_length=1, max_length=10_000)


class TaskDiscussionCommentUpdateRequest(TaskDiscussionCommentCreateRequest):
    pass


class TaskDiscussionMentionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str
    username: str


class TaskDiscussionReactionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    support: int = Field(ge=0)
    oppose: int = Field(ge=0)
    current_user_reaction: TaskDiscussionReaction | None


class TaskDiscussionCommentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    task_id: str
    author_user_id: str
    author_username: str
    body: str
    created_at: datetime
    edited_at: datetime | None
    mentions: tuple[TaskDiscussionMentionResponse, ...] = ()
    reaction_summary: TaskDiscussionReactionSummaryResponse


class TaskDiscussionCommentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[TaskDiscussionCommentResponse, ...] = ()


class TaskDiscussionReactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reaction: TaskDiscussionReaction


__all__ = [
    "TaskDiscussionCommentCreateRequest",
    "TaskDiscussionCommentListResponse",
    "TaskDiscussionCommentResponse",
    "TaskDiscussionCommentUpdateRequest",
    "TaskDiscussionMentionResponse",
    "TaskDiscussionReaction",
    "TaskDiscussionReactionRequest",
    "TaskDiscussionReactionSummaryResponse",
    "TaskDiscussionResponse",
]
