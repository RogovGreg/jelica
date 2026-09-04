from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SupportRequestCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    email: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    message: str = Field(min_length=1)

    @field_validator("name", "subject", "message")
    @classmethod
    def _normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("value must not be empty")
        return normalized

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("email must not be empty")
        if "@" not in normalized:
            raise ValueError("email must contain '@'")
        return normalized


class SupportRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    message: str = Field(min_length=1)
    created_at: datetime
    status: Literal["open", "closed"]


__all__ = ["SupportRequestCreateRequest", "SupportRequestResponse"]
