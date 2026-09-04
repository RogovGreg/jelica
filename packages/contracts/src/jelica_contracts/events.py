from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from .constants import CONTRACT_SCHEMA_VERSION
from .enums import EventComponent, EventType
from .json_types import JSONObject


class EventDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnostic_message: str | None = None
    source_exception_type: str | None = None
    traceback: str | None = None
    stdout: str | None = None
    stderr: str | None = None


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = CONTRACT_SCHEMA_VERSION
    event_id: UUID = Field(default_factory=uuid4)
    code: int = Field(ge=1000, le=9999)
    name: str = Field(min_length=1)
    type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    component: EventComponent
    trace_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    command_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    task_id: str | None = None
    run_id: str | None = None
    stage: str | None = None
    worker_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    operation_id: str | None = None
    context: JSONObject | None = None
    diagnostics: EventDiagnostics | None = None

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != CONTRACT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version '{value}'. Expected '{CONTRACT_SCHEMA_VERSION}'."
            )
        return value

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
