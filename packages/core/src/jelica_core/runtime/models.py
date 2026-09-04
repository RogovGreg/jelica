from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from jelica_contracts import JSONValue
from jelica_core.system_config import ResolvedCoreConfig
from jelica_core.tasks import (
    AnalyticalTaskJobRecord,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskRecord,
    AnalyticalTaskState,
)

RUNTIME_STATE_SCHEMA_VERSION = 1
DEFAULT_PIPELINE_NAME = "initialize_only"
DEFAULT_PIPELINE_VERSION = "v1"


class RuntimeShutdownMode(StrEnum):
    GRACEFUL = "graceful"
    FORCE = "force"


RuntimeShutdownPoll = Callable[[], RuntimeShutdownMode | None]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    max_parallel_tasks: int
    scheduler_poll_interval_seconds: float
    heartbeat_interval_seconds: float
    lease_timeout_seconds: float
    progress_flush_interval_seconds: float
    max_recovery_attempts: int
    ncbi_api_key: str
    mafft_executable: str | None = None

    @classmethod
    def from_resolved_config(cls, resolved_config: ResolvedCoreConfig) -> RuntimeConfig:
        return cls(
            max_parallel_tasks=resolved_config.max_parallel_tasks,
            scheduler_poll_interval_seconds=resolved_config.scheduler_poll_interval_seconds,
            heartbeat_interval_seconds=resolved_config.heartbeat_interval_seconds,
            lease_timeout_seconds=resolved_config.lease_timeout_seconds,
            progress_flush_interval_seconds=resolved_config.progress_flush_interval_seconds,
            max_recovery_attempts=resolved_config.max_recovery_attempts,
            ncbi_api_key=resolved_config.ncbi_api_key,
            mafft_executable=resolved_config.mafft_executable,
        )


@dataclass(frozen=True, slots=True)
class WorkerLaunchSpec:
    task_id: str
    job_id: str
    worker_instance_id: str
    lease_token: str
    database_path: Path
    task_dir: Path
    job_dir: Path
    config_revision_path: Path
    config_hash: str
    runtime_state_json: str
    pipeline_name: str
    pipeline_version: str
    ncbi_api_key: str = ""
    mafft_executable: str | None = None
    trace_id: str | None = None
    pipeline_control: WorkerPipelineControl | None = None


@dataclass(frozen=True, slots=True)
class WorkerPipelineControl:
    stage_started_event: Any | None = None
    stage_started_semaphore: Any | None = None
    stage_started_queue: Any | None = None
    stage_release_event: Any | None = None
    stage_release_semaphore: Any | None = None
    stage_release_semaphores_by_job_id: Any | None = None
    stage_barrier: Any | None = None


@dataclass(frozen=True, slots=True)
class RuntimeStateCheckpoint:
    schema_version: int
    pipeline_version: str
    completed_stages: tuple[str, ...]
    active_stage: str | None
    committed_artifacts: dict[str, tuple[str, ...]]

    @classmethod
    def new(cls, *, pipeline_version: str) -> RuntimeStateCheckpoint:
        return cls(
            schema_version=RUNTIME_STATE_SCHEMA_VERSION,
            pipeline_version=pipeline_version,
            completed_stages=tuple(),
            active_stage=None,
            committed_artifacts={},
        )

    @classmethod
    def from_runtime_state(
        cls,
        runtime_state: dict[str, JSONValue],
        *,
        pipeline_version: str,
    ) -> RuntimeStateCheckpoint:
        schema_version = runtime_state.get("schema_version", RUNTIME_STATE_SCHEMA_VERSION)
        if not isinstance(schema_version, int) or schema_version <= 0:
            raise ValueError("runtime_state.schema_version must be a positive integer")

        raw_pipeline_version = runtime_state.get("pipeline_version", pipeline_version)
        if not isinstance(raw_pipeline_version, str) or raw_pipeline_version.strip() == "":
            raise ValueError("runtime_state.pipeline_version must be a non-empty string")

        completed_stages: list[str] = []
        raw_completed_stages = runtime_state.get("completed_stages", [])
        if not isinstance(raw_completed_stages, list):
            raise ValueError("runtime_state.completed_stages must be a list")
        for raw_stage_id in raw_completed_stages:
            if not isinstance(raw_stage_id, str) or raw_stage_id.strip() == "":
                raise ValueError("runtime_state.completed_stages must contain non-empty strings")
            stage_id = raw_stage_id.strip()
            if stage_id not in completed_stages:
                completed_stages.append(stage_id)

        raw_active_stage = runtime_state.get("active_stage")
        active_stage: str | None
        if raw_active_stage is None:
            active_stage = None
        elif isinstance(raw_active_stage, str) and raw_active_stage.strip() != "":
            active_stage = raw_active_stage.strip()
        else:
            raise ValueError("runtime_state.active_stage must be null or a non-empty string")

        committed_artifacts: dict[str, tuple[str, ...]] = {}
        raw_committed_artifacts = runtime_state.get("committed_artifacts", {})
        if not isinstance(raw_committed_artifacts, dict):
            raise ValueError("runtime_state.committed_artifacts must be an object")
        for stage_id, raw_artifacts in raw_committed_artifacts.items():
            if not isinstance(stage_id, str) or stage_id.strip() == "":
                raise ValueError("runtime_state.committed_artifacts keys must be non-empty strings")
            if not isinstance(raw_artifacts, list):
                raise ValueError("runtime_state.committed_artifacts values must be lists")
            normalized_artifacts: list[str] = []
            for raw_artifact in raw_artifacts:
                if not isinstance(raw_artifact, str) or raw_artifact.strip() == "":
                    raise ValueError("runtime_state artifacts must be non-empty strings")
                normalized_artifacts.append(raw_artifact.strip())
            committed_artifacts[stage_id.strip()] = tuple(normalized_artifacts)

        return cls(
            schema_version=schema_version,
            pipeline_version=raw_pipeline_version.strip(),
            completed_stages=tuple(completed_stages),
            active_stage=active_stage,
            committed_artifacts=committed_artifacts,
        )

    @classmethod
    def from_runtime_state_json(
        cls,
        runtime_state_json: str,
        *,
        pipeline_version: str,
    ) -> RuntimeStateCheckpoint:
        try:
            decoded = json.loads(runtime_state_json)
        except json.JSONDecodeError as error:
            raise ValueError("runtime_state_json must be valid JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("runtime_state_json must decode to an object")
        payload: dict[str, JSONValue] = {str(key): value for key, value in decoded.items()}
        return cls.from_runtime_state(payload, pipeline_version=pipeline_version)

    def to_runtime_state(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.schema_version,
            "pipeline_version": self.pipeline_version,
            "completed_stages": list(self.completed_stages),
            "active_stage": self.active_stage,
            "committed_artifacts": {
                stage_id: list(artifacts)
                for stage_id, artifacts in self.committed_artifacts.items()
            },
        }

    def to_runtime_state_json(self) -> str:
        return json.dumps(
            self.to_runtime_state(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def with_active_stage(self, stage_id: str | None) -> RuntimeStateCheckpoint:
        normalized_stage_id = stage_id
        if normalized_stage_id is not None:
            normalized_stage_id = normalized_stage_id.strip()
            if normalized_stage_id == "":
                normalized_stage_id = None
        return RuntimeStateCheckpoint(
            schema_version=self.schema_version,
            pipeline_version=self.pipeline_version,
            completed_stages=self.completed_stages,
            active_stage=normalized_stage_id,
            committed_artifacts=dict(self.committed_artifacts),
        )

    def with_committed_stage(
        self,
        *,
        stage_id: str,
        artifacts: tuple[str, ...],
    ) -> RuntimeStateCheckpoint:
        normalized_stage_id = stage_id.strip()
        if normalized_stage_id == "":
            raise ValueError("stage_id must not be empty")
        next_completed_stages = list(self.completed_stages)
        if normalized_stage_id not in next_completed_stages:
            next_completed_stages.append(normalized_stage_id)
        next_committed_artifacts = dict(self.committed_artifacts)
        next_committed_artifacts[normalized_stage_id] = artifacts
        return RuntimeStateCheckpoint(
            schema_version=self.schema_version,
            pipeline_version=self.pipeline_version,
            completed_stages=tuple(next_completed_stages),
            active_stage=None,
            committed_artifacts=next_committed_artifacts,
        )


class TaskStartResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: AnalyticalTaskRecord
    job: AnalyticalTaskJobRecord
    runtime_instance_id: str | None = None
    used_existing_runtime: bool = False


class TaskResumeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: AnalyticalTaskRecord
    job: AnalyticalTaskJobRecord
    result: AnalyticalTaskMutationResultType
    runtime_instance_id: str | None = None
    used_existing_runtime: bool = False


class TaskUpdateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    result: AnalyticalTaskMutationResultType
    current_config_revision: int = Field(ge=1)
    current_config_hash: str = Field(min_length=64, max_length=64)
    default_priority: int = Field(ge=1)


class TaskReprioritizeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    job_id: str
    result: AnalyticalTaskMutationResultType
    state: AnalyticalTaskState
    old_priority: int = Field(ge=1)
    new_priority: int = Field(ge=1)


class TaskDeleteItemResultType(StrEnum):
    DELETED = "deleted"
    DELETION_REQUESTED = "deletion_requested"
    ALREADY_SATISFIED = "already_satisfied"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"


class TaskDeleteBatchResultType(StrEnum):
    APPLIED = "applied"
    PARTIALLY_APPLIED = "partially_applied"
    REJECTED = "rejected"


class TaskWatchResultType(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETED = "deleted"


class TaskDeleteItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    result: TaskDeleteItemResultType
    detail: str | None = None


class TaskDeleteBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result: TaskDeleteBatchResultType
    items: tuple[TaskDeleteItemResult, ...]
    requested_count: int = Field(ge=0)
    unique_count: int = Field(ge=0)


class TaskWatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    state: AnalyticalTaskState
    result: TaskWatchResultType
    progress: int = Field(ge=0, le=100)
    current_stage: str | None = None


class RuntimeContinueResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_instance_id: str
    recovered_jobs: int = Field(ge=0)
    claimed_jobs: int = Field(ge=0)
    completed_jobs: int = Field(ge=0)
    failed_jobs: int = Field(ge=0)
    interrupted: bool = False
