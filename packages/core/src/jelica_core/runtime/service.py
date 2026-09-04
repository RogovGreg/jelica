from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jelica_core import get_core_info
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import (
    AnalyticalTaskMutationResultType,
    AnalyticalTaskRegistryService,
    AnalyticalTaskState,
)
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import utc_now

from .background import DEFAULT_BACKGROUND_RUNNER_MODULE, launch_background_runtime
from .models import RuntimeShutdownMode

SERVICE_METADATA_FILENAME = "service-state.json"
SERVICE_CONTROL_FILENAME = "service-control.json"
DEFAULT_SERVICE_START_TIMEOUT_SECONDS = 10.0
DEFAULT_SERVICE_STOP_TIMEOUT_SECONDS = 30.0
_SYSTEM_EVENTS_LOG_FILENAME = "system-events.jsonl"


class ServiceState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class ServiceControlResult(StrEnum):
    ACCEPTED = "accepted"
    REJECTED_RUNNING_TASKS = "rejected_running_tasks"


class ServiceError(RuntimeError):
    """Base error for the user-space Service lifecycle."""


class ServiceStartTimeoutError(ServiceError):
    pass


class ServiceStopTimeoutError(ServiceError):
    pass


class ServiceRunningTasksError(ServiceError):
    def __init__(self, *, task_ids: tuple[str, ...]) -> None:
        self.task_ids = task_ids
        joined = ", ".join(task_ids)
        super().__init__(
            f"Service has {len(task_ids)} running task(s): {joined}. "
            "Use --force to interrupt them safely."
        )


class ServiceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_id: str = Field(min_length=1)
    pid: int = Field(gt=0)
    started_at: datetime
    last_heartbeat: datetime
    state: ServiceState
    jelica_version: str = Field(min_length=1)
    last_control_request_id: str | None = None
    last_control_result: ServiceControlResult | None = None
    interrupted_task_ids: tuple[str, ...] = tuple()
    error_detail: str | None = None


class ServiceControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    service_id: str = Field(min_length=1)
    force: bool
    requested_at: datetime


class ServiceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    running: bool
    service_id: str | None
    pid: int | None
    jelica_version: str | None
    cli_jelica_version: str
    version_compatible: bool | None
    started_at: datetime | None
    last_heartbeat: datetime | None
    state: ServiceState
    configured_workers: int = Field(ge=1)
    active_workers: int = Field(ge=0)
    queued_tasks: int = Field(ge=0)
    running_tasks: int = Field(ge=0)
    queued_task_ids: tuple[str, ...] = tuple()
    running_task_ids: tuple[str, ...] = tuple()
    active_worker_task_ids: tuple[str, ...] = tuple()
    log_path: Path


class ServiceStartResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ServiceStatus
    already_running: bool
    launched_pid: int | None = None


class ServiceStopResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ServiceStatus
    already_stopped: bool
    interrupted_task_ids: tuple[str, ...] = tuple()


class ServiceRestartResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ServiceStatus
    interrupted_task_ids: tuple[str, ...] = tuple()
    resumed_task_ids: tuple[str, ...] = tuple()


class ServiceLogs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    lines: tuple[str, ...]


class ServiceStateStore:
    """Atomic auxiliary state for Service identity and control requests."""

    def __init__(self, *, data_dir: Path) -> None:
        self.metadata_path = data_dir / SERVICE_METADATA_FILENAME
        self.control_path = data_dir / SERVICE_CONTROL_FILENAME

    def read_metadata(self) -> ServiceMetadata | None:
        return _read_model(path=self.metadata_path, model_type=ServiceMetadata)

    def write_metadata(self, metadata: ServiceMetadata) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(
            path=self.metadata_path,
            payload=f"{metadata.model_dump_json(indent=2)}\n",
        )

    def read_control_request(self) -> ServiceControlRequest | None:
        return _read_model(path=self.control_path, model_type=ServiceControlRequest)

    def write_control_request(self, request: ServiceControlRequest) -> None:
        self.control_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(
            path=self.control_path,
            payload=f"{request.model_dump_json(indent=2)}\n",
        )


class ServiceRuntimeControlMonitor:
    """Translate persisted Service stop requests into existing runtime controls."""

    def __init__(
        self,
        *,
        store: ServiceStateStore,
        registry_service: AnalyticalTaskRegistryService,
        metadata: ServiceMetadata,
    ) -> None:
        self._store = store
        self._registry_service = registry_service
        self._metadata = metadata

    @property
    def metadata(self) -> ServiceMetadata:
        return self._metadata

    def poll(self) -> RuntimeShutdownMode | None:
        request = self._store.read_control_request()
        if request is None:
            return None
        if request.service_id != self._metadata.service_id:
            # A newer Service may have replaced this runner's lease.  Once the
            # canonical registry proves that this process is no longer the
            # owner, let the runner exit cooperatively instead of leaving an
            # orphan that can never consume the new control request.
            lease = self._registry_service.get_execution_runtime_lease()
            if lease is None or lease.runtime_instance_id != self._metadata.service_id:
                return RuntimeShutdownMode.GRACEFUL
            return None
        if request.request_id == self._metadata.last_control_request_id:
            return None

        running_task_ids = _task_ids_for_states(
            registry_service=self._registry_service,
            states=(AnalyticalTaskState.RUNNING,),
        )
        if len(running_task_ids) > 0 and not request.force:
            self._replace_metadata(
                state=ServiceState.RUNNING,
                last_control_request_id=request.request_id,
                last_control_result=ServiceControlResult.REJECTED_RUNNING_TASKS,
                interrupted_task_ids=running_task_ids,
            )
            return None

        interrupted_task_ids: list[str] = []
        if request.force:
            for task_id in running_task_ids:
                mutation = self._registry_service.pause(task_id=task_id)
                if (
                    mutation.result_type
                    in {
                        AnalyticalTaskMutationResultType.APPLIED,
                        AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
                    }
                    and mutation.job is not None
                    and mutation.job.state
                    in {
                        AnalyticalTaskState.PAUSE_REQUESTED,
                        AnalyticalTaskState.PAUSED,
                    }
                ):
                    interrupted_task_ids.append(task_id)

        self._replace_metadata(
            state=ServiceState.STOPPING,
            last_control_request_id=request.request_id,
            last_control_result=ServiceControlResult.ACCEPTED,
            interrupted_task_ids=tuple(interrupted_task_ids),
        )
        return RuntimeShutdownMode.GRACEFUL

    def mark_running(self) -> None:
        self._replace_metadata(state=ServiceState.RUNNING)

    def mark_stopped(self) -> None:
        self._replace_metadata(state=ServiceState.STOPPED)

    def mark_error(self, detail: str) -> None:
        self._replace_metadata(state=ServiceState.ERROR, error_detail=detail)

    def _replace_metadata(self, **updates: object) -> None:
        updates["last_heartbeat"] = utc_now()
        self._metadata = self._metadata.model_copy(update=updates)
        self._store.write_metadata(self._metadata)


def get_service_status(
    *,
    core_config_service: CoreConfigService | None = None,
) -> ServiceStatus:
    service = core_config_service or CoreConfigService()
    resolved_config = service.require_initialized_config()
    registry_service = AnalyticalTaskRegistryService(database_path=resolved_config.database_path)
    store = ServiceStateStore(data_dir=resolved_config.data_dir)
    metadata = store.read_metadata()
    lease = registry_service.get_execution_runtime_lease()
    now = utc_now()
    lease_is_active = lease is not None and lease.lease_expires_at > now

    active_metadata = (
        metadata
        if lease_is_active
        and lease is not None
        and metadata is not None
        and metadata.service_id == lease.runtime_instance_id
        else None
    )
    current_version = get_core_info()["version"]
    service_version = active_metadata.jelica_version if active_metadata is not None else None
    queued_task_ids = _task_ids_for_states(
        registry_service=registry_service,
        states=(AnalyticalTaskState.QUEUED,),
    )
    running_task_ids = _task_ids_for_states(
        registry_service=registry_service,
        states=(AnalyticalTaskState.RUNNING,),
    )
    active_worker_task_ids = _active_worker_task_ids(registry_service=registry_service)
    if active_metadata is not None:
        service_state = active_metadata.state
    elif lease_is_active:
        service_state = ServiceState.RUNNING
    elif metadata is not None and metadata.state is ServiceState.ERROR:
        service_state = ServiceState.ERROR
    else:
        service_state = ServiceState.STOPPED

    return ServiceStatus(
        running=lease_is_active,
        service_id=(
            lease.runtime_instance_id
            if lease_is_active and lease is not None
            else metadata.service_id
            if metadata is not None
            else None
        ),
        pid=(
            lease.owner_pid
            if lease_is_active and lease is not None
            else metadata.pid
            if metadata is not None
            else None
        ),
        jelica_version=(
            service_version if lease_is_active else metadata.jelica_version if metadata else None
        ),
        cli_jelica_version=current_version,
        version_compatible=(
            service_version == current_version if service_version is not None else None
        ),
        started_at=(
            lease.acquired_at
            if lease_is_active and lease is not None
            else metadata.started_at
            if metadata is not None
            else None
        ),
        last_heartbeat=(
            lease.heartbeat_at
            if lease_is_active and lease is not None
            else metadata.last_heartbeat
            if metadata is not None
            else None
        ),
        state=service_state,
        configured_workers=resolved_config.max_parallel_tasks,
        active_workers=len(active_worker_task_ids),
        queued_tasks=len(queued_task_ids),
        running_tasks=len(running_task_ids),
        queued_task_ids=queued_task_ids,
        running_task_ids=running_task_ids,
        active_worker_task_ids=active_worker_task_ids,
        log_path=resolved_config.logs_dir / _SYSTEM_EVENTS_LOG_FILENAME,
    )


def start_service(
    *,
    core_config_service: CoreConfigService | None = None,
    timeout_seconds: float = DEFAULT_SERVICE_START_TIMEOUT_SECONDS,
    runner_module: str = DEFAULT_BACKGROUND_RUNNER_MODULE,
) -> ServiceStartResult:
    service = core_config_service or CoreConfigService()
    initial_status = get_service_status(core_config_service=service)
    if initial_status.running:
        return ServiceStartResult(status=initial_status, already_running=True)

    launched_pid = launch_background_runtime(
        jelica_home=service.get_jelica_home(),
        runner_module=runner_module,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = get_service_status(core_config_service=service)
        if status.running and status.jelica_version is not None:
            return ServiceStartResult(
                status=status,
                already_running=False,
                launched_pid=launched_pid,
            )
        time.sleep(0.05)
    raise ServiceStartTimeoutError(
        f"Service process {launched_pid} did not acquire the runtime lease within "
        f"{timeout_seconds:g} seconds."
    )


def stop_service(
    *,
    force: bool = False,
    core_config_service: CoreConfigService | None = None,
    timeout_seconds: float = DEFAULT_SERVICE_STOP_TIMEOUT_SECONDS,
) -> ServiceStopResult:
    service = core_config_service or CoreConfigService()
    resolved_config = service.require_initialized_config()
    status = get_service_status(core_config_service=service)
    if not status.running:
        return ServiceStopResult(status=status, already_stopped=True)
    if len(status.running_task_ids) > 0 and not force:
        raise ServiceRunningTasksError(task_ids=status.running_task_ids)
    if status.service_id is None:
        raise ServiceError("Active Service lease has no service identifier.")

    request = ServiceControlRequest(
        request_id=str(uuid4()),
        service_id=status.service_id,
        force=force,
        requested_at=utc_now(),
    )
    store = ServiceStateStore(data_dir=resolved_config.data_dir)
    store.write_control_request(request)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        metadata = store.read_metadata()
        if (
            metadata is not None
            and metadata.last_control_request_id == request.request_id
            and metadata.last_control_result is ServiceControlResult.REJECTED_RUNNING_TASKS
        ):
            raise ServiceRunningTasksError(task_ids=metadata.interrupted_task_ids)
        current_status = get_service_status(core_config_service=service)
        if not current_status.running:
            interrupted_task_ids = (
                metadata.interrupted_task_ids
                if metadata is not None and metadata.last_control_request_id == request.request_id
                else tuple()
            )
            return ServiceStopResult(
                status=current_status,
                already_stopped=False,
                interrupted_task_ids=interrupted_task_ids,
            )
        time.sleep(0.05)
    raise ServiceStopTimeoutError(
        f"Service '{status.service_id}' did not stop within {timeout_seconds:g} seconds."
    )


def restart_service(
    *,
    force: bool = False,
    core_config_service: CoreConfigService | None = None,
    runner_module: str = DEFAULT_BACKGROUND_RUNNER_MODULE,
) -> ServiceRestartResult:
    service = core_config_service or CoreConfigService()
    stopped = stop_service(force=force, core_config_service=service)
    resumed_task_ids: list[str] = []
    if len(stopped.interrupted_task_ids) > 0:
        resolved_config = service.require_initialized_config()
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        for task_id in stopped.interrupted_task_ids:
            mutation = registry_service.resume(task_id=task_id)
            if mutation.result_type in {
                AnalyticalTaskMutationResultType.APPLIED,
                AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
            }:
                resumed_task_ids.append(task_id)

    started = start_service(core_config_service=service, runner_module=runner_module)
    return ServiceRestartResult(
        status=started.status,
        interrupted_task_ids=stopped.interrupted_task_ids,
        resumed_task_ids=tuple(resumed_task_ids),
    )


def read_service_logs(
    *,
    tail: int = 200,
    core_config_service: CoreConfigService | None = None,
) -> ServiceLogs:
    if tail < 1:
        raise ValueError("tail must be >= 1")
    service = core_config_service or CoreConfigService()
    resolved_config = service.require_initialized_config()
    path = resolved_config.logs_dir / _SYSTEM_EVENTS_LOG_FILENAME
    if not path.is_file():
        return ServiceLogs(path=path, lines=tuple())
    with path.open("r", encoding="utf-8") as stream:
        lines = tuple(line.rstrip("\n") for line in deque(stream, maxlen=tail))
    return ServiceLogs(path=path, lines=lines)


def initial_service_metadata(*, service_id: str, pid: int) -> ServiceMetadata:
    now = utc_now()
    return ServiceMetadata(
        service_id=service_id,
        pid=pid,
        started_at=now,
        last_heartbeat=now,
        state=ServiceState.STARTING,
        jelica_version=get_core_info()["version"],
    )


def _task_ids_for_states(
    *,
    registry_service: AnalyticalTaskRegistryService,
    states: tuple[AnalyticalTaskState, ...],
) -> tuple[str, ...]:
    snapshots = registry_service.list_task_snapshots(states=states, limit=None)
    return tuple(snapshot.task.task_id for snapshot in snapshots)


def _active_worker_task_ids(
    *,
    registry_service: AnalyticalTaskRegistryService,
) -> tuple[str, ...]:
    states = (
        AnalyticalTaskState.RUNNING,
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
        AnalyticalTaskState.CANCEL_REQUESTED,
    )
    snapshots = registry_service.list_task_snapshots(states=states, limit=None)
    active: list[str] = []
    for snapshot in snapshots:
        job = snapshot.active_or_latest_job
        if job is not None and job.worker_pid is not None:
            active.append(snapshot.task.task_id)
    return tuple(active)


def _read_model[ModelT: BaseModel](
    *,
    path: Path,
    model_type: type[ModelT],
) -> ModelT | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return model_type.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError):
        return None
