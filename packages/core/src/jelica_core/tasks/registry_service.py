from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

from .names import validate_task_name
from .registry_errors import (
    AnalyticalTaskAlreadyExistsError,
    AnalyticalTaskInvalidRecordDataError,
)
from .registry_models import (
    AnalyticalTaskJobRecord,
    AnalyticalTaskMutationResult,
    AnalyticalTaskRecord,
    AnalyticalTaskSnapshot,
    AnalyticalTaskSortOrder,
    AnalyticalTaskState,
    ExecutionRuntimeLeaseRecord,
)
from .registry_repository import AnalyticalTaskRegistry
from .state_machine import AnalyticalTaskLifecycleIntent
from .storage import resolve_task_workspace_dir
from .timestamps import utc_now


class AnalyticalTaskRegistryService:
    """Core service for analytical task registry operations."""

    def __init__(
        self,
        *,
        database_path: Path,
        registry: AnalyticalTaskRegistry | None = None,
    ) -> None:
        self._registry = registry or AnalyticalTaskRegistry(database_path=database_path)

    @property
    def registry(self) -> AnalyticalTaskRegistry:
        return self._registry

    def register_task(
        self,
        *,
        task_id: str,
        name: str | None = None,
        automatic_name_base: str | None = None,
        task_dir_relative_path: str,
        default_priority: int = 1,
        current_config_revision: int = 1,
        current_config_relative_path: str,
        current_config_hash: str,
    ) -> AnalyticalTaskRecord:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")
        if name is not None and automatic_name_base is not None:
            raise AnalyticalTaskInvalidRecordDataError(
                detail="name and automatic_name_base must not both be provided"
            )
        try:
            normalized_name = None if name is None else validate_task_name(name)
            normalized_automatic_name_base = (
                None
                if automatic_name_base is None
                else validate_task_name(automatic_name_base)
            )
        except ValueError as error:
            raise AnalyticalTaskInvalidRecordDataError(detail=str(error)) from error

        normalized_task_dir_relative_path = _normalize_relative_path_argument(
            task_dir_relative_path,
            field_name="task_dir_relative_path",
        )
        if default_priority < 1:
            raise AnalyticalTaskInvalidRecordDataError(detail="default_priority must be >= 1")
        if current_config_revision < 1:
            raise AnalyticalTaskInvalidRecordDataError(
                detail="current_config_revision must be >= 1"
            )
        normalized_current_config_relative_path = _normalize_relative_path_argument(
            current_config_relative_path,
            field_name="current_config_relative_path",
        )
        normalized_current_config_hash = _normalize_non_empty_text_argument(
            current_config_hash,
            field_name="current_config_hash",
        )

        now = utc_now()
        automatic_suffix = 0
        while True:
            candidate_name = normalized_name
            if normalized_automatic_name_base is not None:
                candidate_name = normalized_automatic_name_base
                if automatic_suffix > 0:
                    candidate_name = f"{candidate_name}-{automatic_suffix}"
                try:
                    validate_task_name(candidate_name)
                except ValueError as error:
                    raise AnalyticalTaskInvalidRecordDataError(detail=str(error)) from error

            initial_record = AnalyticalTaskRecord(
                task_id=normalized_task_id,
                name=candidate_name,
                state=AnalyticalTaskState.WAITING,
                default_priority=default_priority,
                current_config_revision=current_config_revision,
                current_config_relative_path=normalized_current_config_relative_path,
                current_config_hash=normalized_current_config_hash,
                active_job_id=None,
                latest_job_id=None,
                created_at=now,
                updated_at=now,
                task_dir_relative_path=normalized_task_dir_relative_path,
                record_version=1,
            )
            try:
                return self._registry.insert(record=initial_record)
            except AnalyticalTaskAlreadyExistsError as error:
                if normalized_automatic_name_base is None or error.field_name != "name":
                    raise
                automatic_suffix += 1

    def get_task(self, *, task_id: str) -> AnalyticalTaskRecord:
        return self._registry.get(task_id=task_id)

    def get_task_trace_id(self, *, task_id: str) -> UUID | None:
        """Read the lifecycle trace from the authoritative pinned task config."""

        task = self.get_task(task_id=task_id)
        tasks_dir = self._registry.database_path.parent / "tasks"
        task_dir = resolve_task_workspace_dir(
            tasks_dir=tasks_dir,
            task_dir_relative_path=task.task_dir_relative_path,
            task_id=task.task_id,
        )
        config_path = task_dir / Path(task.current_config_relative_path)
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AnalyticalTaskInvalidRecordDataError(
                detail=f"cannot read current task config trace_id: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise AnalyticalTaskInvalidRecordDataError(
                detail="current task config must be a JSON object"
            )
        raw_trace_id = payload.get("trace_id")
        if raw_trace_id is None:
            return None
        try:
            return UUID(str(raw_trace_id))
        except ValueError as error:
            raise AnalyticalTaskInvalidRecordDataError(
                detail="current task config trace_id must be a valid UUID"
            ) from error

    def get_task_by_name(self, *, name: str) -> AnalyticalTaskRecord:
        return self._registry.get_by_name(name=name)

    def resolve_task_reference(self, *, task_reference: str) -> AnalyticalTaskRecord:
        return self._registry.resolve_task_reference(task_reference=task_reference)

    def resolve_task_id(self, *, task_reference: str) -> str:
        return self.resolve_task_reference(task_reference=task_reference).task_id

    def get_task_snapshot(self, *, task_id: str) -> AnalyticalTaskSnapshot:
        return self._registry.get_task_snapshot(task_id=task_id)

    def get_job(self, *, job_id: str) -> AnalyticalTaskJobRecord:
        return self._registry.get_job(job_id=job_id)

    def task_exists(self, *, task_id: str) -> bool:
        return self._registry.exists(task_id=task_id)

    def list_tasks(
        self,
        *,
        states: Sequence[AnalyticalTaskState] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: AnalyticalTaskSortOrder = (
            AnalyticalTaskSortOrder.DEFAULT_PRIORITY_DESC_CREATED_AT_ASC
        ),
    ) -> list[AnalyticalTaskRecord]:
        return self._registry.list(states=states, limit=limit, offset=offset, order=order)

    def list_task_snapshots(
        self,
        *,
        states: Sequence[AnalyticalTaskState] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: AnalyticalTaskSortOrder = (
            AnalyticalTaskSortOrder.DEFAULT_PRIORITY_DESC_CREATED_AT_ASC
        ),
    ) -> list[AnalyticalTaskSnapshot]:
        return list(
            self._registry.list_task_snapshots(
                states=states,
                limit=limit,
                offset=offset,
                order=order,
            )
        )

    def list_task_jobs(
        self,
        *,
        task_id: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AnalyticalTaskJobRecord]:
        return list(self._registry.list_jobs(task_id=task_id, limit=limit, offset=offset))

    def set_default_priority(
        self,
        *,
        task_id: str,
        default_priority: int,
        expected_version: int,
    ) -> AnalyticalTaskRecord:
        return self._registry.update_default_priority(
            task_id=task_id,
            default_priority=default_priority,
            expected_version=expected_version,
        )

    def set_priority(
        self,
        *,
        task_id: str,
        priority: int,
        expected_version: int,
    ) -> AnalyticalTaskRecord:
        return self.set_default_priority(
            task_id=task_id,
            default_priority=priority,
            expected_version=expected_version,
        )

    def start(
        self,
        *,
        task_id: str,
        priority: int | None = None,
        expected_task_version: int | None = None,
        requested_job_id: str | None = None,
        runtime_state: Mapping[str, object] | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.start_task(
            task_id=task_id,
            priority=priority,
            expected_task_version=expected_task_version,
            requested_job_id=requested_job_id,
            runtime_state=runtime_state,
        )

    def request_deletion(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
        expected_active_job_id: str | None = None,
        expected_worker_instance_id: str | None = None,
        expected_lease_token: str | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.request_task_deletion(
            task_id=task_id,
            expected_task_version=expected_task_version,
            expected_active_job_id=expected_active_job_id,
            expected_worker_instance_id=expected_worker_instance_id,
            expected_lease_token=expected_lease_token,
        )

    def delete_task_and_jobs(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.delete_task_and_jobs(
            task_id=task_id,
            expected_task_version=expected_task_version,
        )

    def resume(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.request_job_intent(
            task_id=task_id,
            intent=AnalyticalTaskLifecycleIntent.RESUME,
            expected_task_version=expected_task_version,
            expected_job_version=expected_job_version,
        )

    def pause(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.request_job_intent(
            task_id=task_id,
            intent=AnalyticalTaskLifecycleIntent.PAUSE,
            expected_task_version=expected_task_version,
            expected_job_version=expected_job_version,
        )

    def cancel(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.request_job_intent(
            task_id=task_id,
            intent=AnalyticalTaskLifecycleIntent.CANCEL,
            expected_task_version=expected_task_version,
            expected_job_version=expected_job_version,
        )

    def scheduler_preempt(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.request_job_intent(
            task_id=task_id,
            intent=AnalyticalTaskLifecycleIntent.SCHEDULER_PREEMPT,
            expected_task_version=expected_task_version,
            expected_job_version=expected_job_version,
        )

    def reprioritize_active_job(
        self,
        *,
        task_id: str,
        priority: int,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.reprioritize_active_job(
            task_id=task_id,
            priority=priority,
            expected_task_version=expected_task_version,
            expected_job_version=expected_job_version,
        )

    def transition_active_job_state(
        self,
        *,
        task_id: str,
        to_state: AnalyticalTaskState,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
        finished_reason: str | None = None,
        error_event_code: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.transition_active_job_state(
            task_id=task_id,
            to_state=to_state,
            expected_task_version=expected_task_version,
            expected_job_version=expected_job_version,
            finished_reason=finished_reason,
            error_event_code=error_event_code,
        )

    def transition_state(
        self,
        *,
        task_id: str,
        to_state: AnalyticalTaskState,
        expected_version: int,
        finished_reason: str | None = None,
        error_event_code: int | None = None,
    ) -> AnalyticalTaskRecord:
        return self._registry.transition_state(
            task_id=task_id,
            to_state=to_state,
            expected_version=expected_version,
            finished_reason=finished_reason,
            error_event_code=error_event_code,
        )

    def set_progress(
        self,
        *,
        task_id: str,
        progress: int,
        current_stage: str | None,
        expected_version: int,
    ) -> AnalyticalTaskRecord:
        return self._registry.update_progress_and_stage(
            task_id=task_id,
            progress=progress,
            current_stage=current_stage,
            expected_version=expected_version,
        )

    def update_active_job_progress(
        self,
        *,
        task_id: str,
        progress: int,
        current_stage: str | None,
        runtime_state: Mapping[str, object] | None = None,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.update_active_job_progress(
            task_id=task_id,
            progress=progress,
            current_stage=current_stage,
            runtime_state=runtime_state,
            expected_task_version=expected_task_version,
            expected_job_version=expected_job_version,
        )

    def update_task_config(
        self,
        *,
        task_id: str,
        config_document: Mapping[str, object],
        expected_task_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.update_task_config(
            task_id=task_id,
            config_document=config_document,
            expected_task_version=expected_task_version,
        )

    def acquire_execution_runtime_lease(
        self,
        *,
        runtime_instance_id: str,
        owner_pid: int,
        lease_token: str,
        lease_timeout_seconds: float,
    ) -> tuple[ExecutionRuntimeLeaseRecord | None, ExecutionRuntimeLeaseRecord | None]:
        return self._registry.acquire_execution_runtime_lease(
            runtime_instance_id=runtime_instance_id,
            owner_pid=owner_pid,
            lease_token=lease_token,
            lease_timeout_seconds=lease_timeout_seconds,
        )

    def heartbeat_execution_runtime_lease(
        self,
        *,
        runtime_instance_id: str,
        lease_token: str,
        lease_timeout_seconds: float,
    ) -> ExecutionRuntimeLeaseRecord | None:
        return self._registry.heartbeat_execution_runtime_lease(
            runtime_instance_id=runtime_instance_id,
            lease_token=lease_token,
            lease_timeout_seconds=lease_timeout_seconds,
        )

    def release_execution_runtime_lease(
        self,
        *,
        runtime_instance_id: str,
        lease_token: str,
    ) -> bool:
        return self._registry.release_execution_runtime_lease(
            runtime_instance_id=runtime_instance_id,
            lease_token=lease_token,
        )

    def get_execution_runtime_lease(self) -> ExecutionRuntimeLeaseRecord | None:
        return self._registry.get_execution_runtime_lease()

    def count_running_jobs(self) -> int:
        return self._registry.count_running_jobs()

    def count_queued_jobs(self) -> int:
        return self._registry.count_queued_jobs()

    def claim_next_queued_job_for_worker(
        self,
        *,
        worker_instance_id: str,
        lease_token: str,
        lease_timeout_seconds: float,
    ) -> tuple[AnalyticalTaskRecord, AnalyticalTaskJobRecord] | None:
        return self._registry.claim_next_queued_job_for_worker(
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
            lease_timeout_seconds=lease_timeout_seconds,
        )

    def attach_worker_pid(
        self,
        *,
        job_id: str,
        worker_instance_id: str,
        lease_token: str,
        worker_pid: int,
    ) -> AnalyticalTaskJobRecord | None:
        return self._registry.attach_worker_pid(
            job_id=job_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
            worker_pid=worker_pid,
        )

    def heartbeat_job_worker(
        self,
        *,
        job_id: str,
        worker_instance_id: str,
        lease_token: str,
        lease_timeout_seconds: float,
    ) -> AnalyticalTaskJobRecord | None:
        return self._registry.heartbeat_job_worker(
            job_id=job_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
            lease_timeout_seconds=lease_timeout_seconds,
        )

    def clear_job_worker_lease(
        self,
        *,
        job_id: str,
        worker_instance_id: str,
        lease_token: str,
    ) -> AnalyticalTaskJobRecord | None:
        return self._registry.clear_job_worker_lease(
            job_id=job_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
        )

    def increment_active_job_recovery_count(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        return self._registry.increment_active_job_recovery_count(
            task_id=task_id,
            expected_task_version=expected_task_version,
            expected_job_version=expected_job_version,
        )


def _normalize_non_empty_text_argument(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise AnalyticalTaskInvalidRecordDataError(detail=f"{field_name} must not be empty")
    return normalized


def _normalize_relative_path_argument(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise AnalyticalTaskInvalidRecordDataError(detail=f"{field_name} must not be empty")

    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if posix_path.is_absolute() or windows_path.is_absolute():
        raise AnalyticalTaskInvalidRecordDataError(detail=f"{field_name} must be a relative path")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise AnalyticalTaskInvalidRecordDataError(
            detail=f"{field_name} must not escape its base directory"
        )
    if normalized in {".", ".."}:
        raise AnalyticalTaskInvalidRecordDataError(detail=f"{field_name} must not be '.' or '..'")

    return normalized
