from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkerMessageKind(StrEnum):
    WORKER_STARTED = "worker_started"
    STAGE_STARTED = "stage_started"
    STAGE_EVENT = "stage_event"
    PROGRESS_UPDATED = "progress_updated"
    STAGE_READY_TO_COMMIT = "stage_ready_to_commit"
    STAGE_COMPLETED = "stage_completed"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    WORKER_HEARTBEAT = "worker_heartbeat"
    JOB_STOPPED = "job_stopped"


class WorkerStopReason(StrEnum):
    RUNTIME_SHUTDOWN = "runtime_shutdown"
    PAUSE_REQUESTED = "pause_requested"
    CANCEL_REQUESTED = "cancel_requested"
    PREEMPTION_REQUESTED = "preemption_requested"
    DELETION_REQUESTED = "deletion_requested"


@dataclass(frozen=True, slots=True)
class WorkerMessageBase:
    task_id: str
    job_id: str
    worker_instance_id: str
    lease_token: str


@dataclass(frozen=True, slots=True)
class WorkerStartedMessage(WorkerMessageBase):
    worker_pid: int
    kind: WorkerMessageKind = WorkerMessageKind.WORKER_STARTED


@dataclass(frozen=True, slots=True)
class StageStartedMessage(WorkerMessageBase):
    stage_id: str
    stage_index: int
    stage_weight: float
    total_weight: float
    kind: WorkerMessageKind = WorkerMessageKind.STAGE_STARTED


@dataclass(frozen=True, slots=True)
class StageEventMessage(WorkerMessageBase):
    stage_id: str
    event_name: str
    context: dict[str, object]
    kind: WorkerMessageKind = WorkerMessageKind.STAGE_EVENT


@dataclass(frozen=True, slots=True)
class ProgressUpdatedMessage(WorkerMessageBase):
    stage_id: str
    stage_progress: float
    description: str | None = None
    kind: WorkerMessageKind = WorkerMessageKind.PROGRESS_UPDATED


@dataclass(frozen=True, slots=True)
class StageReadyToCommitMessage(WorkerMessageBase):
    stage_id: str
    staging_directory: str
    manifest_path: str
    kind: WorkerMessageKind = WorkerMessageKind.STAGE_READY_TO_COMMIT


@dataclass(frozen=True, slots=True)
class StageCompletedMessage(WorkerMessageBase):
    stage_id: str
    kind: WorkerMessageKind = WorkerMessageKind.STAGE_COMPLETED


@dataclass(frozen=True, slots=True)
class JobCompletedMessage(WorkerMessageBase):
    kind: WorkerMessageKind = WorkerMessageKind.JOB_COMPLETED


@dataclass(frozen=True, slots=True)
class JobFailedMessage(WorkerMessageBase):
    reason: str
    detail: str
    error_type: str
    failure_event_name: str | None = None
    failure_context: dict[str, object] | None = None
    kind: WorkerMessageKind = WorkerMessageKind.JOB_FAILED


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatMessage(WorkerMessageBase):
    kind: WorkerMessageKind = WorkerMessageKind.WORKER_HEARTBEAT


@dataclass(frozen=True, slots=True)
class JobStoppedMessage(WorkerMessageBase):
    reason: WorkerStopReason
    kind: WorkerMessageKind = WorkerMessageKind.JOB_STOPPED


WorkerMessage = (
    WorkerStartedMessage
    | StageStartedMessage
    | StageEventMessage
    | ProgressUpdatedMessage
    | StageReadyToCommitMessage
    | StageCompletedMessage
    | JobCompletedMessage
    | JobFailedMessage
    | WorkerHeartbeatMessage
    | JobStoppedMessage
)
