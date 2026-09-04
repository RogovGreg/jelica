from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class NotificationEventOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    enabled: StrictBool


class NotificationPreferencesPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: StrictBool | None = None
    sound_enabled: StrictBool | None = None
    channels: dict[str, StrictBool] | None = None
    events: tuple[NotificationEventOverride, ...] | None = None


class NotificationChannelPreference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    channel: str
    enabled: bool
    available: bool


class NotificationEventPreference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str
    category: str
    scope: str
    default_enabled: bool
    channels: tuple[str, ...]
    enabled: dict[str, bool]
    effective: dict[str, bool]


class NotificationPreferencesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool
    sound_enabled: bool
    channels: tuple[NotificationChannelPreference, ...]
    events: tuple[NotificationEventPreference, ...]


class NotificationResourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: str
    project_id: str | None = None
    task_id: str | None = None
    display_name: str | None = None


class NotificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    event_id: str
    category: str
    actor_username: str | None
    resource: NotificationResourceResponse | None
    created_at: datetime
    read_at: datetime | None
    target_path: str | None


class NotificationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[NotificationResponse, ...]


class NotificationReadPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    read: StrictBool


class NotificationUnreadCountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    unread_count: int


class NotificationMarkAllReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    updated: int
    read_at: datetime


__all__ = [
    "NotificationChannelPreference",
    "NotificationEventOverride",
    "NotificationEventPreference",
    "NotificationListResponse",
    "NotificationMarkAllReadResponse",
    "NotificationPreferencesPatch",
    "NotificationPreferencesResponse",
    "NotificationReadPatch",
    "NotificationResourceResponse",
    "NotificationResponse",
    "NotificationUnreadCountResponse",
]
