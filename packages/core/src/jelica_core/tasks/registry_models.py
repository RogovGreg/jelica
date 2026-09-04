from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from jelica_contracts import JSONValue

from .names import validate_task_name
from .timestamps import serialize_utc_datetime


class AnalyticalTaskState(StrEnum):
    WAITING = "waiting"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PREEMPTION_REQUESTED = "preemption_requested"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETION_REQUESTED = "deletion_requested"


ACTIVE_ANALYTICAL_TASK_JOB_STATES: frozenset[AnalyticalTaskState] = frozenset(
    {
        AnalyticalTaskState.WAITING,
        AnalyticalTaskState.QUEUED,
        AnalyticalTaskState.RUNNING,
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
        AnalyticalTaskState.PAUSED,
        AnalyticalTaskState.CANCEL_REQUESTED,
    }
)

TERMINAL_ANALYTICAL_TASK_JOB_STATES: frozenset[AnalyticalTaskState] = frozenset(
    {
        AnalyticalTaskState.COMPLETED,
        AnalyticalTaskState.FAILED,
        AnalyticalTaskState.CANCELLED,
    }
)

ALLOWED_ANALYTICAL_TASK_JOB_TRANSITIONS: dict[
    AnalyticalTaskState, frozenset[AnalyticalTaskState]
] = {
    AnalyticalTaskState.WAITING: frozenset(
        {AnalyticalTaskState.QUEUED, AnalyticalTaskState.PAUSED, AnalyticalTaskState.CANCELLED}
    ),
    AnalyticalTaskState.QUEUED: frozenset(
        {AnalyticalTaskState.RUNNING, AnalyticalTaskState.PAUSED, AnalyticalTaskState.CANCELLED}
    ),
    AnalyticalTaskState.RUNNING: frozenset(
        {
            AnalyticalTaskState.WAITING,
            AnalyticalTaskState.PAUSE_REQUESTED,
            AnalyticalTaskState.PREEMPTION_REQUESTED,
            AnalyticalTaskState.CANCEL_REQUESTED,
            AnalyticalTaskState.COMPLETED,
            AnalyticalTaskState.FAILED,
        }
    ),
    AnalyticalTaskState.PAUSE_REQUESTED: frozenset(
        {
            AnalyticalTaskState.PAUSED,
            AnalyticalTaskState.CANCEL_REQUESTED,
            AnalyticalTaskState.COMPLETED,
            AnalyticalTaskState.FAILED,
        }
    ),
    AnalyticalTaskState.PREEMPTION_REQUESTED: frozenset(
        {
            AnalyticalTaskState.WAITING,
            AnalyticalTaskState.PAUSE_REQUESTED,
            AnalyticalTaskState.CANCEL_REQUESTED,
            AnalyticalTaskState.COMPLETED,
            AnalyticalTaskState.FAILED,
        }
    ),
    AnalyticalTaskState.PAUSED: frozenset(
        {AnalyticalTaskState.QUEUED, AnalyticalTaskState.CANCELLED}
    ),
    AnalyticalTaskState.CANCEL_REQUESTED: frozenset({AnalyticalTaskState.CANCELLED}),
    AnalyticalTaskState.COMPLETED: frozenset(),
    AnalyticalTaskState.FAILED: frozenset(),
    AnalyticalTaskState.CANCELLED: frozenset(),
    AnalyticalTaskState.DELETION_REQUESTED: frozenset(),
}

TERMINAL_ANALYTICAL_TASK_STATES = TERMINAL_ANALYTICAL_TASK_JOB_STATES
ALLOWED_ANALYTICAL_TASK_TRANSITIONS = ALLOWED_ANALYTICAL_TASK_JOB_TRANSITIONS


class AnalyticalTaskSortOrder(StrEnum):
    DEFAULT_PRIORITY_DESC_CREATED_AT_ASC = "default_priority_desc_created_at_asc"
    PRIORITY_DESC_CREATED_AT_ASC = "default_priority_desc_created_at_asc"
    UPDATED_AT_DESC = "updated_at_desc"


class AnalyticalTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    name: str | None = None
    state: AnalyticalTaskState
    default_priority: int = Field(ge=1)
    current_config_revision: int = Field(ge=1)
    current_config_relative_path: str = Field(min_length=1)
    current_config_hash: str = Field(min_length=1)

    active_job_id: str | None = None
    latest_job_id: str | None = None

    created_at: datetime
    updated_at: datetime
    task_dir_relative_path: str = Field(min_length=1)

    @field_validator("task_id")
    @classmethod
    def _normalize_task_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("task_id must not be empty")
        return normalized

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_task_name(value)

    @field_validator("current_config_relative_path", "current_config_hash")
    @classmethod
    def _normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("text field must not be empty")
        return normalized

    @field_validator("current_config_relative_path")
    @classmethod
    def _validate_current_config_relative_path(cls, value: str) -> str:
        return _normalize_relative_path(value, field_name="current_config_relative_path")

    @field_validator("task_dir_relative_path")
    @classmethod
    def _normalize_task_dir_relative_path(cls, value: str) -> str:
        return _normalize_relative_path(value, field_name="task_dir_relative_path")

    @field_validator("active_job_id", "latest_job_id")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized == "":
            raise ValueError("text field must not be empty when provided")
        return normalized

    @field_validator("created_at", "updated_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("created_at", "updated_at")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return serialize_utc_datetime(value)


class AnalyticalTaskRecord(AnalyticalTask):
    record_version: int = Field(ge=1)


class AnalyticalTaskJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)

    config_revision: int = Field(ge=1)
    config_relative_path: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)

    state: AnalyticalTaskState
    current_stage: str | None = None
    progress: int = Field(ge=0, le=100)
    priority: int = Field(ge=1)

    created_at: datetime
    queued_at: datetime | None = None
    first_started_at: datetime | None = None
    last_started_at: datetime | None = None
    last_stopped_at: datetime | None = None
    finished_at: datetime | None = None

    finished_reason: str | None = None
    error_event_code: int | None = None

    runtime_state: dict[str, JSONValue] = Field(default_factory=dict)

    worker_instance_id: str | None = None
    worker_pid: int | None = None
    lease_token: str | None = None
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None

    recovery_count: int = Field(ge=0)

    @field_validator(
        "job_id",
        "task_id",
        "config_hash",
        "current_stage",
        "finished_reason",
        "worker_instance_id",
        "lease_token",
    )
    @classmethod
    def _normalize_optional_and_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized == "":
            raise ValueError("text field must not be empty when provided")
        return normalized

    @field_validator("config_relative_path")
    @classmethod
    def _validate_config_relative_path(cls, value: str) -> str:
        return _normalize_relative_path(value, field_name="config_relative_path")

    @field_validator("state")
    @classmethod
    def _validate_job_state(cls, value: AnalyticalTaskState) -> AnalyticalTaskState:
        if value is AnalyticalTaskState.DELETION_REQUESTED:
            raise ValueError("deletion_requested is not a valid job state")
        return value

    @field_validator("error_event_code")
    @classmethod
    def _validate_error_event_code(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value <= 0:
            raise ValueError("error_event_code must be a positive integer")
        return value

    @field_validator("worker_pid")
    @classmethod
    def _validate_worker_pid(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value <= 0:
            raise ValueError("worker_pid must be a positive integer")
        return value

    @field_validator(
        "created_at",
        "queued_at",
        "first_started_at",
        "last_started_at",
        "last_stopped_at",
        "finished_at",
        "heartbeat_at",
        "lease_expires_at",
    )
    @classmethod
    def _normalize_job_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("runtime_state")
    @classmethod
    def _validate_runtime_state_json_compatibility(
        cls,
        value: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        try:
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("runtime_state must contain JSON-compatible values") from error
        return value

    @field_serializer(
        "created_at",
        "queued_at",
        "first_started_at",
        "last_started_at",
        "last_stopped_at",
        "finished_at",
        "heartbeat_at",
        "lease_expires_at",
    )
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return serialize_utc_datetime(value)


class AnalyticalTaskJobRecord(AnalyticalTaskJob):
    record_version: int = Field(ge=1)


class AnalyticalTaskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: AnalyticalTaskRecord
    active_or_latest_job: AnalyticalTaskJobRecord | None = None


class ExecutionRuntimeLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_instance_id: str = Field(min_length=1)
    owner_pid: int = Field(gt=0)
    lease_token: str = Field(min_length=1)
    acquired_at: datetime
    heartbeat_at: datetime
    lease_expires_at: datetime

    @field_validator("runtime_instance_id", "lease_token")
    @classmethod
    def _normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("text field must not be empty")
        return normalized

    @field_validator("acquired_at", "heartbeat_at", "lease_expires_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("acquired_at", "heartbeat_at", "lease_expires_at")
    def _serialize_timestamp(self, value: datetime) -> str:
        return serialize_utc_datetime(value)


class ExecutionRuntimeLeaseRecord(ExecutionRuntimeLease):
    record_version: int = Field(ge=1)


class AnalyticalTaskMutationResultType(StrEnum):
    APPLIED = "applied"
    ALREADY_SATISFIED = "already_satisfied"
    INVALID_TRANSITION = "invalid_transition"
    CONFLICT = "conflict"
    CONCURRENT_UPDATE = "concurrent_update"
    NOT_FOUND = "not_found"


class AnalyticalTaskMutationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_type: AnalyticalTaskMutationResultType
    task: AnalyticalTaskRecord | None = None
    job: AnalyticalTaskJobRecord | None = None
    details: dict[str, Any] | None = None


def is_terminal_state(state: AnalyticalTaskState) -> bool:
    return state in TERMINAL_ANALYTICAL_TASK_JOB_STATES


def is_active_job_state(state: AnalyticalTaskState) -> bool:
    return state in ACTIVE_ANALYTICAL_TASK_JOB_STATES


def is_job_transition_allowed(
    *,
    from_state: AnalyticalTaskState,
    to_state: AnalyticalTaskState,
) -> bool:
    return to_state in ALLOWED_ANALYTICAL_TASK_JOB_TRANSITIONS[from_state]


def is_transition_allowed(
    *,
    from_state: AnalyticalTaskState,
    to_state: AnalyticalTaskState,
) -> bool:
    return is_job_transition_allowed(from_state=from_state, to_state=to_state)


def _normalize_relative_path(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{field_name} must not be empty")

    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if posix_path.is_absolute() or windows_path.is_absolute():
        raise ValueError(f"{field_name} must be a relative path")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ValueError(f"{field_name} must not escape its base directory")
    if normalized in {".", ".."}:
        raise ValueError(f"{field_name} must not be '.' or '..'")

    return normalized
