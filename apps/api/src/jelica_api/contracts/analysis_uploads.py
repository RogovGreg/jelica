from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UploadItemKind = Literal["input_file", "input_directory", "config_file"]
UploadSubmissionStatus = Literal["open", "submitting", "consumed"]


class UploadItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    kind: UploadItemKind
    display_name: str = Field(min_length=1)
    file_count: int = Field(gt=0)
    total_bytes: int = Field(ge=0)
    ready: bool = True
    created_at: datetime


class UploadSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    items: tuple[UploadItemResponse, ...]
    submission_status: UploadSubmissionStatus = "open"
    task_id: str | None = None


class UploadItemsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[UploadItemResponse, ...]


__all__ = [
    "UploadItemKind",
    "UploadItemResponse",
    "UploadItemsResponse",
    "UploadSessionResponse",
    "UploadSubmissionStatus",
]
