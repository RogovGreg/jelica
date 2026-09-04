from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TelegramIntegrationStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    integration_available: bool
    linked: bool
    username: str | None = None
    display_name: str | None = None
    linked_at: datetime | None = None


class TelegramLinkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str
    expires_at: datetime


__all__ = ["TelegramIntegrationStateResponse", "TelegramLinkResponse"]
