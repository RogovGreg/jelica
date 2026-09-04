from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MachineErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: int
    name: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class MachineResponseEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    machine_protocol_version: str = Field(min_length=1)
    jelica_version: str = Field(min_length=1)
    trace_id: str | None = None
    command_id: str = Field(min_length=1)
    ok: bool
    data: dict[str, Any] | None = None
    error: MachineErrorPayload | None = None

    @model_validator(mode="after")
    def _validate_payload_shape(self) -> MachineResponseEnvelope:
        if self.ok:
            if self.error is not None:
                raise ValueError("machine response with ok=true must not include error payload")
            if self.data is None:
                raise ValueError("machine response with ok=true must include data payload")
            return self
        if self.error is None:
            raise ValueError("machine response with ok=false must include error payload")
        return self


class AnalyzeTaskPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    task_id: str = Field(min_length=1)


class AnalyzeMachineDataPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    task: AnalyzeTaskPayload
    final_state: str = Field(min_length=1)


class TaskMachineJobPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    state: str = Field(min_length=1)
    progress: int | None = Field(default=None, ge=0, le=100)
    current_stage: str | None = None


class TaskMachinePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    task_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    trace_id: str | None = None
    active_or_latest_job: TaskMachineJobPayload | None = None


class TasksShowMachineDataPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    count: int = Field(ge=0)
    tasks: tuple[TaskMachinePayload, ...] = ()


class ResultPathMachineDataPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    content_id: str = Field(min_length=1)
    path: str = Field(min_length=1)


__all__ = [
    "AnalyzeMachineDataPayload",
    "MachineErrorPayload",
    "MachineResponseEnvelope",
    "ResultPathMachineDataPayload",
    "TaskMachinePayload",
    "TasksShowMachineDataPayload",
]
