from __future__ import annotations

import multiprocessing as mp
import signal
import threading
import time
from dataclasses import dataclass
from multiprocessing.process import BaseProcess
from pathlib import Path
from queue import Empty
from typing import Any, Callable
from uuid import uuid4

from jelica_contracts import JSONValue
from jelica_core.alignment import ALIGNMENT_MANIFEST_RELATIVE_PATH, ALIGNMENT_STAGE_ID
from jelica_core.alignment.mafft import terminate_process_tree_by_pid
from jelica_core.result_package import (
    RESULT_PACKAGE_STAGE_ID,
    RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,
    ResultPackageLink,
    ResultPackagePublicationError,
    ResultPackageValidationError,
    load_result_package_stage_manifest,
    publish_prepared_result_package,
    write_result_package_link,
)
from jelica_core.tasks import (
    AnalyticalTaskJobNotFoundError,
    AnalyticalTaskJobRecord,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRecord,
    AnalyticalTaskRegistryError,
    AnalyticalTaskRegistryService,
    AnalyticalTaskState,
)
from jelica_core.tasks.storage import (
    TaskWorkspaceDeleteError,
    move_task_workspace_to_trash,
    purge_trashed_task_workspace,
    restore_task_workspace_from_trash,
)
from jelica_core.tasks.timestamps import utc_now

from .alignment_stage import (
    ALIGNMENT_COMPLETED_EVENT,
    ALIGNMENT_MAFFT_STOPPED_SHUTDOWN_EVENT,
    ALIGNMENT_RESULT_PUBLISHED_EVENT,
)
from .artifacts import (
    StageCommitError,
    cleanup_worker_staging,
    commit_stage_directory,
    validate_committed_stage_snapshot,
)
from .clade_detection_stage import (
    CLADE_DETECTION_COMPLETED_EVENT,
    CLADE_DETECTION_FAILED_EVENT,
    CLADE_DETECTION_RESULT_PUBLISHED_EVENT,
)
from .distance_matrix_stage import (
    DISTANCE_MATRIX_COMPLETED_EVENT,
    DISTANCE_MATRIX_FAILED_EVENT,
    DISTANCE_MATRIX_PARTIAL_SUCCESS_EVENT,
    DISTANCE_MATRIX_RESULT_PUBLISHED_EVENT,
)
from .messages import (
    JobCompletedMessage,
    JobFailedMessage,
    JobStoppedMessage,
    ProgressUpdatedMessage,
    StageCompletedMessage,
    StageEventMessage,
    StageReadyToCommitMessage,
    StageStartedMessage,
    WorkerHeartbeatMessage,
    WorkerMessage,
    WorkerStartedMessage,
    WorkerStopReason,
)
from .models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RuntimeConfig,
    RuntimeContinueResult,
    RuntimeShutdownMode,
    RuntimeShutdownPoll,
    RuntimeStateCheckpoint,
    WorkerLaunchSpec,
    WorkerPipelineControl,
)
from .phylogenetic_tree_stage import (
    PHYLOGENETIC_TREE_COMPLETED_EVENT,
    PHYLOGENETIC_TREE_FAILED_EVENT,
    PHYLOGENETIC_TREE_RESULT_PUBLISHED_EVENT,
)
from .pipeline import PipelineDefinition, build_pipeline_definition
from .worker import run_worker_process

RuntimeEventCallback = Callable[[str, dict[str, JSONValue] | None], None]

RUNTIME_EVENT_LEASE_EXPIRED = "runtime_lease_expired"
RUNTIME_EVENT_SCHEDULER_STARTED = "scheduler_started"
RUNTIME_EVENT_SCHEDULER_STOPPED = "scheduler_stopped"
RUNTIME_EVENT_JOB_CLAIMED = "job_claimed"
RUNTIME_EVENT_WORKER_STARTED = "worker_started"
RUNTIME_EVENT_WORKER_HEARTBEAT_LOST = "worker_heartbeat_lost"
RUNTIME_EVENT_WORKER_EXITED = "worker_exited"
RUNTIME_EVENT_STAGE_STARTED = "stage_started"
RUNTIME_EVENT_STAGE_COMMITTED = "stage_committed"
RUNTIME_EVENT_JOB_COMPLETED = "job_completed"
RUNTIME_EVENT_JOB_FAILED = "job_failed"
RUNTIME_EVENT_RECOVERY_STARTED = "recovery_started"
RUNTIME_EVENT_RECOVERY_COMPLETED = "recovery_completed"
RUNTIME_EVENT_RECOVERY_FAILED = "recovery_failed"
RUNTIME_EVENT_STALE_MESSAGE_REJECTED = "stale_worker_message_rejected"
RUNTIME_EVENT_PROCESS_SPAWN_FAILURE = "process_spawn_failure"
RUNTIME_EVENT_RUNTIME_INTERRUPTED = "runtime_interrupted"
RUNTIME_EVENT_PREEMPTION_SELECTED = "preemption_selected"
RUNTIME_EVENT_PREEMPTION_REQUESTED = "preemption_requested"
RUNTIME_EVENT_PREEMPTED_JOB_RETURNED_TO_WAITING = "preempted_job_returned_to_waiting"
RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PAUSE = "worker_safely_stopped_for_pause"
RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_CANCEL = "worker_safely_stopped_for_cancel"
RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PREEMPTION = "worker_safely_stopped_for_preemption"
RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_DELETION = "worker_safely_stopped_for_deletion"
RUNTIME_PROGRESS_UPDATED = "progress_updated"
_COMPARATIVE_ANALYSIS_STAGE_ID = "comparative_analysis"
_COMPARATIVE_ANALYSIS_COMPLETED_EVENT = "COMPARATIVE_ANALYSIS_COMPLETED"
_COMPARATIVE_ANALYSIS_FAILED_EVENT = "COMPARATIVE_ANALYSIS_FAILED"
_COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS_EVENT = "COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS"
_COMPARATIVE_ANALYSIS_RESULT_PUBLISHED_EVENT = "COMPARATIVE_ANALYSIS_RESULT_PUBLISHED"
_DISTANCE_MATRIX_STAGE_ID = "distance_matrix"
_PHYLOGENETIC_TREE_STAGE_ID = "phylogenetic_tree"
_CLADE_DETECTION_STAGE_ID = "clade_detection"

_RECOVERY_SOURCE_STATES: frozenset[AnalyticalTaskState] = frozenset(
    {
        AnalyticalTaskState.RUNNING,
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
        AnalyticalTaskState.CANCEL_REQUESTED,
    }
)


@dataclass(slots=True)
class _WorkerHandle:
    task_id: str
    job_id: str
    worker_instance_id: str
    lease_token: str
    task_record_version: int
    job_record_version: int
    config_hash: str
    checkpoint: RuntimeStateCheckpoint
    pipeline_definition: PipelineDefinition
    process: BaseProcess
    runtime_shutdown_event: Any
    deletion_requested_event: Any
    pause_requested_event: Any
    cancel_requested_event: Any
    external_process_pid_state: Any
    job_dir: Path
    initial_start: bool
    current_stage: str | None = None
    current_stage_progress: float = 0.0
    last_progress_flush_monotonic: float = 0.0
    last_worker_heartbeat_monotonic: float = 0.0
    terminal_message_wait_started_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class _TaskJobSnapshot:
    task: AnalyticalTaskRecord
    job: AnalyticalTaskJobRecord


class ExecutionRuntime:
    def __init__(
        self,
        *,
        registry_service: AnalyticalTaskRegistryService,
        tasks_dir: Path,
        runtime_config: RuntimeConfig,
        runtime_instance_id: str,
        runtime_lease_token: str,
        pipeline_name: str = DEFAULT_PIPELINE_NAME,
        pipeline_version: str = DEFAULT_PIPELINE_VERSION,
        pipeline_control: WorkerPipelineControl | None = None,
        event_callback: RuntimeEventCallback | None = None,
        shutdown_poll: RuntimeShutdownPoll | None = None,
    ) -> None:
        self._registry_service = registry_service
        self._tasks_dir = tasks_dir
        self._runtime_config = runtime_config
        self._runtime_instance_id = runtime_instance_id
        self._runtime_lease_token = runtime_lease_token
        self._pipeline_name = pipeline_name
        self._pipeline_version = pipeline_version
        self._pipeline_control = pipeline_control
        self._event_callback = event_callback
        self._shutdown_poll = shutdown_poll

        self._spawn_context = mp.get_context("spawn")
        self._message_queue: Any = self._spawn_context.Queue()
        self._running_workers: dict[str, _WorkerHandle] = {}
        self._task_trace_ids: dict[str, str | None] = {}
        self._accept_new_jobs = True
        self._interrupt_count = 0
        self._graceful_stop_requested = False
        self._force_stop_requested = False
        self._runtime_lease_lost = False
        self._interrupted = False

        self._claimed_jobs = 0
        self._completed_jobs = 0
        self._failed_jobs = 0
        self._recovered_jobs = 0

        self._last_runtime_heartbeat_monotonic = 0.0

    def run(
        self,
        *,
        auto_queue_waiting_jobs: bool,
        persistent: bool = False,
    ) -> RuntimeContinueResult:
        previous_signal_handlers = self._install_signal_handlers()
        try:
            self._recover_expired_jobs()
            if auto_queue_waiting_jobs:
                self._enqueue_waiting_jobs()

            self._emit(
                RUNTIME_EVENT_SCHEDULER_STARTED,
                {"runtime_instance_id": self._runtime_instance_id},
            )
            self._last_runtime_heartbeat_monotonic = time.monotonic()
            while True:
                self._poll_shutdown_request()
                if self._force_stop_requested:
                    self._force_stop_workers()
                    break

                self._heartbeat_runtime_lease_if_due()
                self._sync_worker_control_requests()
                self._process_worker_messages()
                self._heartbeat_workers_if_due()
                self._reap_exited_workers()

                if self._graceful_stop_requested:
                    self._request_worker_stop()
                elif self._accept_new_jobs:
                    self._claim_and_start_workers()
                    self._request_preemption_if_needed()

                if self._graceful_stop_requested and len(self._running_workers) == 0:
                    break
                if not persistent and self._is_queue_drained():
                    break

                time.sleep(self._runtime_config.scheduler_poll_interval_seconds)
        finally:
            self._restore_signal_handlers(previous_signal_handlers)
            self._release_runtime_lease()
            self._emit(
                RUNTIME_EVENT_SCHEDULER_STOPPED,
                {
                    "runtime_instance_id": self._runtime_instance_id,
                    "interrupted": self._interrupted,
                },
            )

        return RuntimeContinueResult(
            runtime_instance_id=self._runtime_instance_id,
            recovered_jobs=self._recovered_jobs,
            claimed_jobs=self._claimed_jobs,
            completed_jobs=self._completed_jobs,
            failed_jobs=self._failed_jobs,
            interrupted=self._interrupted,
        )

    def _handle_sigint(self, _signum: int, _frame: Any) -> None:
        self._interrupt_count += 1
        mode = (
            RuntimeShutdownMode.GRACEFUL
            if self._interrupt_count == 1
            else RuntimeShutdownMode.FORCE
        )
        self._request_runtime_shutdown(mode)

    def _poll_shutdown_request(self) -> None:
        if self._shutdown_poll is None:
            return
        requested_mode = self._shutdown_poll()
        if requested_mode is None:
            return
        if not isinstance(requested_mode, RuntimeShutdownMode):
            raise TypeError("shutdown_poll must return RuntimeShutdownMode or None")
        self._request_runtime_shutdown(requested_mode)

    def _request_runtime_shutdown(self, mode: RuntimeShutdownMode) -> None:
        if mode is RuntimeShutdownMode.FORCE:
            if self._force_stop_requested:
                return
            self._interrupted = True
            self._accept_new_jobs = False
            self._force_stop_requested = True
            self._emit(
                RUNTIME_EVENT_RUNTIME_INTERRUPTED,
                {
                    "runtime_instance_id": self._runtime_instance_id,
                    "mode": RuntimeShutdownMode.FORCE.value,
                },
            )
            return
        if self._graceful_stop_requested or self._force_stop_requested:
            return
        self._interrupted = True
        self._accept_new_jobs = False
        self._graceful_stop_requested = True
        self._request_worker_stop()
        self._emit(
            RUNTIME_EVENT_RUNTIME_INTERRUPTED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "mode": RuntimeShutdownMode.GRACEFUL.value,
            },
        )

    def _install_signal_handlers(self) -> dict[int, Any]:
        if threading.current_thread() is not threading.main_thread():
            return {}
        signal_numbers = {int(signal.SIGINT)}
        sigterm = getattr(signal, "SIGTERM", None)
        if sigterm is not None:
            signal_numbers.add(int(sigterm))
        previous_handlers: dict[int, Any] = {}
        for signal_number in signal_numbers:
            try:
                previous_handlers[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, self._handle_sigint)
            except (OSError, ValueError):
                previous_handlers.pop(signal_number, None)
        return previous_handlers

    def _restore_signal_handlers(self, previous_handlers: dict[int, Any]) -> None:
        for signal_number, previous_handler in previous_handlers.items():
            try:
                signal.signal(signal_number, previous_handler)
            except (OSError, ValueError):
                continue

    def _emit(self, event_name: str, context: dict[str, JSONValue] | None) -> None:
        if self._event_callback is None:
            return
        effective_context = dict(context or {})
        task_id = effective_context.get("task_id")
        if isinstance(task_id, str) and "trace_id" not in effective_context:
            trace_id = self._trace_id_for_task(task_id)
            if trace_id is not None:
                effective_context["trace_id"] = trace_id
        self._event_callback(event_name, effective_context or None)

    def _trace_id_for_task(self, task_id: str) -> str | None:
        if task_id in self._task_trace_ids:
            return self._task_trace_ids[task_id]
        trace_id: str | None = None
        try:
            resolved_trace_id = self._registry_service.get_task_trace_id(task_id=task_id)
            if resolved_trace_id is not None:
                trace_id = str(resolved_trace_id)
        except (AnalyticalTaskRegistryError, TaskWorkspaceDeleteError):
            trace_id = None
        self._task_trace_ids[task_id] = trace_id
        return trace_id

    def _recover_expired_jobs(self) -> None:
        snapshots = self._registry_service.list_task_snapshots(
            states=tuple(
                [
                    AnalyticalTaskState.RUNNING,
                    AnalyticalTaskState.PAUSE_REQUESTED,
                    AnalyticalTaskState.PREEMPTION_REQUESTED,
                    AnalyticalTaskState.CANCEL_REQUESTED,
                ]
            ),
            limit=None,
        )
        if len(snapshots) == 0:
            return

        self._emit(
            RUNTIME_EVENT_RECOVERY_STARTED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "candidate_count": len(snapshots),
            },
        )
        for snapshot in snapshots:
            active_job = snapshot.active_or_latest_job
            if active_job is None:
                continue
            if snapshot.task.active_job_id != active_job.job_id:
                continue
            if active_job.state not in _RECOVERY_SOURCE_STATES:
                continue
            if not self._is_worker_lease_expired(active_job):
                continue
            try:
                self._recover_single_job(task_record=snapshot.task, job_record=active_job)
            except (StageCommitError, OSError, ValueError) as error:
                self._fail_recovery_job_snapshot(
                    task_record=snapshot.task,
                    job_record=active_job,
                    error=error,
                )
        self._emit(
            RUNTIME_EVENT_RECOVERY_COMPLETED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "recovered_jobs": self._recovered_jobs,
            },
        )

    def _recover_single_job(
        self,
        *,
        task_record: AnalyticalTaskRecord,
        job_record: AnalyticalTaskJobRecord,
    ) -> None:
        job_dir = self._job_dir(task_record=task_record, job_record=job_record)
        if job_record.worker_instance_id is not None:
            cleanup_worker_staging(
                job_dir=job_dir,
                worker_instance_id=job_record.worker_instance_id,
            )

        pipeline_definition = build_pipeline_definition(
            pipeline_name=self._pipeline_name,
            pipeline_version=self._pipeline_version,
            config_revision_path=job_dir.parent.parent / job_record.config_relative_path,
        )
        try:
            checkpoint = RuntimeStateCheckpoint.from_runtime_state(
                job_record.runtime_state,
                pipeline_version=pipeline_definition.version,
            )
        except ValueError as error:
            self._emit(
                RUNTIME_EVENT_RECOVERY_FAILED,
                {
                    "task_id": task_record.task_id,
                    "job_id": job_record.job_id,
                    "reason": f"invalid_runtime_state:{error}",
                },
            )
            return
        reconciled_checkpoint = self._reconcile_committed_stages(
            checkpoint=checkpoint,
            pipeline_definition=pipeline_definition,
            job_dir=job_dir,
            task_id=task_record.task_id,
            job_id=job_record.job_id,
            config_hash=job_record.config_hash,
        )
        if reconciled_checkpoint != checkpoint:
            progress = self._compute_progress(
                checkpoint=reconciled_checkpoint,
                pipeline_definition=pipeline_definition,
                active_stage_progress=0.0,
                active_stage=None,
            )
            progress_result = self._registry_service.update_active_job_progress(
                task_id=task_record.task_id,
                progress=progress,
                current_stage=None,
                runtime_state=reconciled_checkpoint.to_runtime_state(),
                expected_task_version=task_record.record_version,
                expected_job_version=job_record.record_version,
            )
            if progress_result.result_type is not AnalyticalTaskMutationResultType.APPLIED:
                self._emit(
                    RUNTIME_EVENT_RECOVERY_FAILED,
                    {
                        "task_id": task_record.task_id,
                        "job_id": job_record.job_id,
                        "reason": "checkpoint_reconcile_conflict",
                    },
                )
                return
            if progress_result.task is not None:
                task_record = progress_result.task
            if progress_result.job is not None:
                job_record = progress_result.job

        if _committed_comparative_analysis_failed(
            job_dir=job_dir,
            task_id=task_record.task_id,
            job_id=job_record.job_id,
            config_hash=job_record.config_hash,
            pipeline_version=pipeline_definition.version,
        ):
            failure_result = self._registry_service.transition_active_job_state(
                task_id=task_record.task_id,
                to_state=AnalyticalTaskState.FAILED,
                expected_task_version=task_record.record_version,
                expected_job_version=job_record.record_version,
                finished_reason="comparative_analysis_failed",
            )
            if failure_result.result_type is AnalyticalTaskMutationResultType.APPLIED:
                self._failed_jobs += 1
                failure_detail = (
                    "Recovered a committed comparative-analysis snapshot with failed status."
                )
                self._emit(
                    _COMPARATIVE_ANALYSIS_FAILED_EVENT,
                    {
                        "task_id": task_record.task_id,
                        "job_id": job_record.job_id,
                        "stage_id": _COMPARATIVE_ANALYSIS_STAGE_ID,
                        "detail": failure_detail,
                    },
                )
                self._emit(
                    RUNTIME_EVENT_JOB_FAILED,
                    {
                        "task_id": task_record.task_id,
                        "job_id": job_record.job_id,
                        "reason": "comparative_analysis_failed",
                        "detail": failure_detail,
                        "failure_event_name": _COMPARATIVE_ANALYSIS_FAILED_EVENT,
                        "failure_context": {"detail": failure_detail},
                    },
                )
            return

        recovery_result = self._registry_service.increment_active_job_recovery_count(
            task_id=task_record.task_id,
            expected_task_version=task_record.record_version,
            expected_job_version=job_record.record_version,
        )
        if recovery_result.result_type is not AnalyticalTaskMutationResultType.APPLIED:
            self._emit(
                RUNTIME_EVENT_RECOVERY_FAILED,
                {
                    "task_id": task_record.task_id,
                    "job_id": job_record.job_id,
                    "reason": f"recovery_count_{recovery_result.result_type.value}",
                },
            )
            return

        if recovery_result.task is None or recovery_result.job is None:
            self._emit(
                RUNTIME_EVENT_RECOVERY_FAILED,
                {
                    "task_id": task_record.task_id,
                    "job_id": job_record.job_id,
                    "reason": "missing_recovery_payload",
                },
            )
            return

        task_record = recovery_result.task
        job_record = recovery_result.job
        if job_record.recovery_count > self._runtime_config.max_recovery_attempts:
            failure_result = self._registry_service.transition_active_job_state(
                task_id=task_record.task_id,
                to_state=AnalyticalTaskState.FAILED,
                expected_task_version=task_record.record_version,
                expected_job_version=job_record.record_version,
                finished_reason="recovery_attempt_limit_exceeded",
            )
            if failure_result.result_type is AnalyticalTaskMutationResultType.APPLIED:
                self._failed_jobs += 1
                self._emit(
                    RUNTIME_EVENT_RECOVERY_FAILED,
                    {
                        "task_id": task_record.task_id,
                        "job_id": job_record.job_id,
                        "reason": "recovery_attempt_limit_exceeded",
                    },
                )
            return

        target_state, finished_reason = _recovery_target_state(job_record.state)
        transition_result = self._registry_service.transition_active_job_state(
            task_id=task_record.task_id,
            to_state=target_state,
            expected_task_version=task_record.record_version,
            expected_job_version=job_record.record_version,
            finished_reason=finished_reason,
        )
        if transition_result.result_type is AnalyticalTaskMutationResultType.APPLIED:
            self._recovered_jobs += 1
            self._emit(
                RUNTIME_EVENT_RECOVERY_COMPLETED,
                {
                    "task_id": task_record.task_id,
                    "job_id": job_record.job_id,
                    "target_state": target_state.value,
                },
            )
            return

        self._emit(
            RUNTIME_EVENT_RECOVERY_FAILED,
            {
                "task_id": task_record.task_id,
                "job_id": job_record.job_id,
                "reason": f"transition_{transition_result.result_type.value}",
            },
        )

    def _fail_recovery_job_snapshot(
        self,
        *,
        task_record: AnalyticalTaskRecord,
        job_record: AnalyticalTaskJobRecord,
        error: BaseException,
    ) -> None:
        error_code = getattr(error, "code", "RECOVERY_JOB_SNAPSHOT_INVALID")
        stage_id = getattr(error, "stage_id", None)
        relative_path = getattr(error, "relative_path", None)
        recovery_context: dict[str, JSONValue] = {
            "task_id": task_record.task_id,
            "job_id": job_record.job_id,
            "reason": "recovery_job_snapshot_invalid",
            "error_code": str(error_code),
        }
        if isinstance(stage_id, str):
            recovery_context["stage_id"] = stage_id
        if isinstance(relative_path, str):
            recovery_context["relative_path"] = relative_path
        self._emit(RUNTIME_EVENT_RECOVERY_FAILED, recovery_context)

        mutation = self._registry_service.transition_active_job_state(
            task_id=task_record.task_id,
            to_state=AnalyticalTaskState.FAILED,
            expected_task_version=task_record.record_version,
            expected_job_version=job_record.record_version,
            finished_reason="recovery_job_snapshot_invalid",
        )
        if mutation.result_type is not AnalyticalTaskMutationResultType.APPLIED:
            return
        self._failed_jobs += 1
        self._emit(
            RUNTIME_EVENT_JOB_FAILED,
            {
                "task_id": task_record.task_id,
                "job_id": job_record.job_id,
                "reason": "recovery_job_snapshot_invalid",
                "detail": "A committed stage snapshot failed integrity validation.",
                "failure_context": recovery_context,
            },
        )

    def _reconcile_committed_stages(
        self,
        *,
        checkpoint: RuntimeStateCheckpoint,
        pipeline_definition: PipelineDefinition,
        job_dir: Path,
        task_id: str,
        job_id: str,
        config_hash: str,
    ) -> RuntimeStateCheckpoint:
        next_checkpoint = checkpoint
        validated_stage_ids: set[str] = set()
        for stage in pipeline_definition.stages:
            stage_root = job_dir / "stages" / stage.stage_id
            if stage.stage_id not in checkpoint.completed_stages and not stage_root.exists():
                continue
            snapshot = validate_committed_stage_snapshot(
                job_dir=job_dir,
                stage_id=stage.stage_id,
                expected_job_id=job_id,
                expected_pipeline_version=pipeline_definition.version,
                expected_task_id=task_id,
                expected_config_hash=config_hash,
            )
            validated_stage_ids.add(stage.stage_id)
            if stage.stage_id in next_checkpoint.completed_stages:
                continue
            next_checkpoint = next_checkpoint.with_committed_stage(
                stage_id=stage.stage_id,
                artifacts=snapshot.manifest.artifacts,
            )
        if (
            next_checkpoint.active_stage is not None
            and next_checkpoint.active_stage in validated_stage_ids
        ):
            next_checkpoint = next_checkpoint.with_active_stage(None)
        return next_checkpoint

    def _enqueue_waiting_jobs(self) -> None:
        waiting_snapshots = self._registry_service.list_task_snapshots(
            states=(AnalyticalTaskState.WAITING,),
            limit=None,
        )
        for snapshot in waiting_snapshots:
            active_job = snapshot.active_or_latest_job
            if active_job is None:
                continue
            if snapshot.task.active_job_id != active_job.job_id:
                continue
            self._registry_service.start(task_id=snapshot.task.task_id)

    def _sync_worker_control_requests(self) -> None:
        for handle in list(self._running_workers.values()):
            try:
                snapshot = self._registry_service.get_task_snapshot(task_id=handle.task_id)
            except AnalyticalTaskNotFoundError:
                self._emit(
                    RUNTIME_EVENT_STALE_MESSAGE_REJECTED,
                    {
                        "task_id": handle.task_id,
                        "job_id": handle.job_id,
                        "reason": "task_not_found",
                    },
                )
                self._finalize_worker_handle(job_id=handle.job_id, terminate=True)
                continue
            active_job = snapshot.active_or_latest_job
            if active_job is None:
                continue
            if active_job.job_id != handle.job_id:
                continue
            if active_job.worker_instance_id != handle.worker_instance_id:
                continue
            if active_job.lease_token != handle.lease_token:
                continue
            handle.task_record_version = snapshot.task.record_version
            handle.job_record_version = active_job.record_version

            if snapshot.task.state is AnalyticalTaskState.DELETION_REQUESTED:
                handle.deletion_requested_event.set()
                continue
            if active_job.state is AnalyticalTaskState.CANCEL_REQUESTED:
                handle.cancel_requested_event.set()
                continue
            if active_job.state is AnalyticalTaskState.PAUSE_REQUESTED:
                handle.pause_requested_event.set()
                continue
            if active_job.state is AnalyticalTaskState.PREEMPTION_REQUESTED:
                handle.runtime_shutdown_event.set()

    def _heartbeat_runtime_lease_if_due(self) -> None:
        if self._runtime_lease_lost:
            return
        now_monotonic = time.monotonic()
        if (
            now_monotonic - self._last_runtime_heartbeat_monotonic
            < self._runtime_config.heartbeat_interval_seconds
        ):
            return

        updated_lease = self._registry_service.heartbeat_execution_runtime_lease(
            runtime_instance_id=self._runtime_instance_id,
            lease_token=self._runtime_lease_token,
            lease_timeout_seconds=self._runtime_config.lease_timeout_seconds,
        )
        if updated_lease is None:
            self._runtime_lease_lost = True
            self._accept_new_jobs = False
            self._emit(
                RUNTIME_EVENT_LEASE_EXPIRED,
                {"runtime_instance_id": self._runtime_instance_id},
            )
            self._stop_workers_without_db_updates()
            raise RuntimeError("runtime lease was lost")
        self._last_runtime_heartbeat_monotonic = now_monotonic

    def _claim_and_start_workers(self) -> None:
        while len(self._running_workers) < self._runtime_config.max_parallel_tasks:
            worker_instance_id = str(uuid4())
            worker_lease_token = str(uuid4())
            claimed = self._registry_service.claim_next_queued_job_for_worker(
                worker_instance_id=worker_instance_id,
                lease_token=worker_lease_token,
                lease_timeout_seconds=self._runtime_config.lease_timeout_seconds,
            )
            if claimed is None:
                return
            task_record, job_record = claimed
            self._task_trace_ids.pop(task_record.task_id, None)
            self._claimed_jobs += 1
            self._emit(
                RUNTIME_EVENT_JOB_CLAIMED,
                {"task_id": task_record.task_id, "job_id": job_record.job_id},
            )
            # The registry's first/last start timestamps distinguish the
            # initial execution from an ordinary resume without inferring
            # notification state from a final task status.
            initial_start = job_record.first_started_at == job_record.last_started_at
            self._spawn_worker_for_claim(
                task_record=task_record,
                job_record=job_record,
                worker_instance_id=worker_instance_id,
                lease_token=worker_lease_token,
                initial_start=initial_start,
            )

    def _request_preemption_if_needed(self) -> None:
        if len(self._running_workers) < self._runtime_config.max_parallel_tasks:
            return
        if self._has_preemption_in_flight():
            return

        candidate = self._select_preemption_candidate()
        if candidate is None:
            return
        victim = self._select_preemption_victim()
        if victim is None:
            return
        if candidate.job.priority <= victim.job.priority:
            return

        event_context: dict[str, JSONValue] = {
            "candidate_task_id": candidate.task.task_id,
            "candidate_job_id": candidate.job.job_id,
            "candidate_priority": candidate.job.priority,
            "victim_task_id": victim.task.task_id,
            "victim_job_id": victim.job.job_id,
            "victim_priority": victim.job.priority,
        }
        self._emit(RUNTIME_EVENT_PREEMPTION_SELECTED, event_context)
        preempt_result = self._registry_service.scheduler_preempt(
            task_id=victim.task.task_id,
            expected_task_version=victim.task.record_version,
            expected_job_version=victim.job.record_version,
        )
        if preempt_result.result_type is not AnalyticalTaskMutationResultType.APPLIED:
            return
        handle = self._running_workers.get(victim.job.job_id)
        if handle is not None:
            if preempt_result.task is not None:
                handle.task_record_version = preempt_result.task.record_version
            if preempt_result.job is not None:
                handle.job_record_version = preempt_result.job.record_version
            handle.runtime_shutdown_event.set()
        self._emit(RUNTIME_EVENT_PREEMPTION_REQUESTED, event_context)

    def _has_preemption_in_flight(self) -> bool:
        snapshots = self._registry_service.list_task_snapshots(
            states=(AnalyticalTaskState.PREEMPTION_REQUESTED,),
            limit=1,
        )
        return len(snapshots) > 0

    def _select_preemption_candidate(self) -> _TaskJobSnapshot | None:
        queued_jobs = self._active_jobs_for_state(state=AnalyticalTaskState.QUEUED)
        if len(queued_jobs) == 0:
            return None
        return min(
            queued_jobs,
            key=lambda snapshot: (
                -snapshot.job.priority,
                snapshot.job.queued_at or snapshot.job.created_at,
                snapshot.job.created_at,
                snapshot.job.job_id,
            ),
        )

    def _select_preemption_victim(self) -> _TaskJobSnapshot | None:
        running_jobs = self._active_jobs_for_state(state=AnalyticalTaskState.RUNNING)
        if len(running_jobs) == 0:
            return None
        return min(
            running_jobs,
            key=lambda snapshot: (
                snapshot.job.priority,
                snapshot.job.last_started_at or snapshot.job.created_at,
                snapshot.job.created_at,
                snapshot.job.job_id,
            ),
        )

    def _active_jobs_for_state(self, *, state: AnalyticalTaskState) -> list[_TaskJobSnapshot]:
        snapshots = self._registry_service.list_task_snapshots(
            states=(state,),
            limit=None,
        )
        active_jobs: list[_TaskJobSnapshot] = []
        for snapshot in snapshots:
            active_job = snapshot.active_or_latest_job
            if active_job is None:
                continue
            if snapshot.task.active_job_id != active_job.job_id:
                continue
            if active_job.state is not state:
                continue
            active_jobs.append(_TaskJobSnapshot(task=snapshot.task, job=active_job))
        return active_jobs

    def _spawn_worker_for_claim(
        self,
        *,
        task_record: AnalyticalTaskRecord,
        job_record: AnalyticalTaskJobRecord,
        worker_instance_id: str,
        lease_token: str,
        initial_start: bool,
    ) -> None:
        task_dir = self._task_dir(task_record=task_record)
        pipeline_definition = build_pipeline_definition(
            pipeline_name=self._pipeline_name,
            pipeline_version=self._pipeline_version,
            config_revision_path=task_dir / job_record.config_relative_path,
        )
        checkpoint = RuntimeStateCheckpoint.from_runtime_state(
            job_record.runtime_state,
            pipeline_version=pipeline_definition.version,
        )
        job_dir = self._job_dir(task_record=task_record, job_record=job_record)
        launch_spec = WorkerLaunchSpec(
            task_id=task_record.task_id,
            job_id=job_record.job_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
            database_path=self._registry_service.registry.database_path,
            task_dir=task_dir,
            job_dir=job_dir,
            config_revision_path=task_dir / job_record.config_relative_path,
            config_hash=job_record.config_hash,
            runtime_state_json=checkpoint.to_runtime_state_json(),
            pipeline_name=self._pipeline_name,
            pipeline_version=pipeline_definition.version,
            ncbi_api_key=self._runtime_config.ncbi_api_key,
            mafft_executable=self._runtime_config.mafft_executable,
            trace_id=self._trace_id_for_task(task_record.task_id),
            pipeline_control=self._pipeline_control,
        )
        runtime_shutdown_event = self._spawn_context.Event()
        deletion_requested_event = self._spawn_context.Event()
        pause_requested_event = self._spawn_context.Event()
        cancel_requested_event = self._spawn_context.Event()
        external_process_pid_state = self._spawn_context.Value("q", 0)
        process = self._spawn_context.Process(
            target=run_worker_process,
            args=(
                launch_spec,
                self._message_queue,
                runtime_shutdown_event,
                deletion_requested_event,
                pause_requested_event,
                cancel_requested_event,
                external_process_pid_state,
            ),
            daemon=False,
        )
        try:
            process.start()
        except Exception as error:
            self._emit(
                RUNTIME_EVENT_PROCESS_SPAWN_FAILURE,
                {
                    "task_id": task_record.task_id,
                    "job_id": job_record.job_id,
                    "detail": str(error),
                },
            )
            self._fail_job_immediately_after_spawn_error(
                task_record=task_record,
                job_record=job_record,
                reason="process_spawn_failure",
                detail=str(error),
            )
            return

        attached_job = self._registry_service.attach_worker_pid(
            job_id=job_record.job_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
            worker_pid=process.pid or 0,
        )
        if attached_job is None:
            with external_process_pid_state.get_lock():
                external_process_pid = external_process_pid_state.value
            if isinstance(external_process_pid, int) and external_process_pid > 0:
                terminate_process_tree_by_pid(external_process_pid)
            process.terminate()
            process.join(timeout=1.0)
            self._emit(
                RUNTIME_EVENT_STALE_MESSAGE_REJECTED,
                {
                    "task_id": task_record.task_id,
                    "job_id": job_record.job_id,
                    "reason": "attach_worker_pid_conflict",
                },
            )
            return

        now_monotonic = time.monotonic()
        self._running_workers[job_record.job_id] = _WorkerHandle(
            task_id=task_record.task_id,
            job_id=job_record.job_id,
            worker_instance_id=worker_instance_id,
            lease_token=lease_token,
            task_record_version=task_record.record_version,
            job_record_version=attached_job.record_version,
            config_hash=job_record.config_hash,
            checkpoint=checkpoint,
            pipeline_definition=pipeline_definition,
            process=process,
            runtime_shutdown_event=runtime_shutdown_event,
            deletion_requested_event=deletion_requested_event,
            pause_requested_event=pause_requested_event,
            cancel_requested_event=cancel_requested_event,
            external_process_pid_state=external_process_pid_state,
            job_dir=job_dir,
            initial_start=initial_start,
            last_progress_flush_monotonic=now_monotonic,
            last_worker_heartbeat_monotonic=now_monotonic,
        )

    def _fail_job_immediately_after_spawn_error(
        self,
        *,
        task_record: AnalyticalTaskRecord,
        job_record: AnalyticalTaskJobRecord,
        reason: str,
        detail: str,
    ) -> None:
        mutation = self._registry_service.transition_active_job_state(
            task_id=task_record.task_id,
            to_state=AnalyticalTaskState.FAILED,
            expected_task_version=task_record.record_version,
            expected_job_version=job_record.record_version,
            finished_reason=f"{reason}: {detail}".strip(),
        )
        if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
            self._failed_jobs += 1
            self._emit(
                RUNTIME_EVENT_JOB_FAILED,
                {
                    "task_id": task_record.task_id,
                    "job_id": job_record.job_id,
                    "reason": reason,
                    "detail": detail,
                },
            )

    def _process_worker_messages(self) -> None:
        while True:
            try:
                raw_message = self._message_queue.get_nowait()
            except Empty:
                return
            if not isinstance(
                raw_message,
                (
                    WorkerStartedMessage,
                    StageStartedMessage,
                    StageEventMessage,
                    ProgressUpdatedMessage,
                    StageReadyToCommitMessage,
                    StageCompletedMessage,
                    JobCompletedMessage,
                    JobFailedMessage,
                    WorkerHeartbeatMessage,
                    JobStoppedMessage,
                ),
            ):
                continue
            self._handle_worker_message(raw_message)

    def _handle_worker_message(self, message: WorkerMessage) -> None:
        handle = self._running_workers.get(message.job_id)
        if handle is None:
            self._emit(
                RUNTIME_EVENT_STALE_MESSAGE_REJECTED,
                {
                    "job_id": message.job_id,
                    "reason": "job_not_running",
                },
            )
            return
        if (
            message.worker_instance_id != handle.worker_instance_id
            or message.lease_token != handle.lease_token
            or message.task_id != handle.task_id
        ):
            self._emit(
                RUNTIME_EVENT_STALE_MESSAGE_REJECTED,
                {
                    "task_id": message.task_id,
                    "job_id": message.job_id,
                    "reason": "worker_identity_mismatch",
                },
            )
            return

        if isinstance(message, WorkerStartedMessage):
            self._emit(
                RUNTIME_EVENT_WORKER_STARTED,
                {
                    "task_id": handle.task_id,
                    "job_id": handle.job_id,
                    "worker_pid": message.worker_pid,
                    "initial_start": handle.initial_start,
                },
            )
            return
        if isinstance(message, WorkerHeartbeatMessage):
            handle.last_worker_heartbeat_monotonic = time.monotonic()
            return
        if isinstance(message, StageStartedMessage):
            self._handle_stage_started(handle=handle, message=message)
            return
        if isinstance(message, StageEventMessage):
            self._handle_stage_event(handle=handle, message=message)
            return
        if isinstance(message, ProgressUpdatedMessage):
            self._handle_progress_updated(handle=handle, message=message)
            return
        if isinstance(message, StageReadyToCommitMessage):
            self._handle_stage_ready_to_commit(handle=handle, message=message)
            return
        if isinstance(message, StageCompletedMessage):
            return
        if isinstance(message, JobCompletedMessage):
            self._handle_job_completed(handle=handle)
            return
        if isinstance(message, JobFailedMessage):
            self._handle_job_failed(handle=handle, message=message)
            return
        if isinstance(message, JobStoppedMessage):
            self._handle_job_stopped(handle=handle, message=message)
            return

    def _handle_stage_started(
        self,
        *,
        handle: _WorkerHandle,
        message: StageStartedMessage,
    ) -> None:
        handle.current_stage = message.stage_id
        handle.current_stage_progress = 0.0
        handle.checkpoint = handle.checkpoint.with_active_stage(message.stage_id)
        self._persist_progress(
            handle=handle,
            include_runtime_state=True,
            force=True,
        )
        self._emit(
            RUNTIME_EVENT_STAGE_STARTED,
            {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "stage_id": message.stage_id,
            },
        )

    def _handle_stage_event(
        self,
        *,
        handle: _WorkerHandle,
        message: StageEventMessage,
    ) -> None:
        event_context: dict[str, JSONValue] = {
            "task_id": handle.task_id,
            "job_id": handle.job_id,
            "stage_id": message.stage_id,
        }
        for key, value in message.context.items():
            event_context[key] = _to_json_value(value)
        self._emit(message.event_name, event_context)

    def _handle_progress_updated(
        self,
        *,
        handle: _WorkerHandle,
        message: ProgressUpdatedMessage,
    ) -> None:
        if handle.current_stage is None:
            handle.current_stage = message.stage_id
            handle.checkpoint = handle.checkpoint.with_active_stage(message.stage_id)

        if handle.current_stage != message.stage_id:
            return

        self._emit(
            RUNTIME_PROGRESS_UPDATED,
            {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "stage_id": message.stage_id,
                "stage_progress": message.stage_progress,
                "description": message.description,
            },
        )

        handle.current_stage_progress = max(
            handle.current_stage_progress,
            min(1.0, max(0.0, message.stage_progress)),
        )
        now_monotonic = time.monotonic()
        if (
            now_monotonic - handle.last_progress_flush_monotonic
            >= self._runtime_config.progress_flush_interval_seconds
        ):
            self._persist_progress(
                handle=handle,
                include_runtime_state=False,
                force=False,
            )

    def _handle_stage_ready_to_commit(
        self,
        *,
        handle: _WorkerHandle,
        message: StageReadyToCommitMessage,
    ) -> None:
        staging_directory = Path(message.staging_directory)
        manifest_path = Path(message.manifest_path)
        try:
            manifest = commit_stage_directory(
                job_dir=handle.job_dir,
                stage_id=message.stage_id,
                job_id=handle.job_id,
                worker_instance_id=handle.worker_instance_id,
                pipeline_version=handle.pipeline_definition.version,
                staging_directory=staging_directory,
                manifest_path=manifest_path,
                task_id=handle.task_id,
                config_hash=getattr(handle, "config_hash", None),
            )
        except StageCommitError as error:
            failure_context: dict[str, object] = {
                "error_code": error.code,
                "stage_id": error.stage_id or message.stage_id,
            }
            if error.relative_path is not None:
                failure_context["relative_path"] = error.relative_path
            self._mark_job_failed(
                handle=handle,
                reason="stage_commit_error",
                detail=str(error),
                failure_context=failure_context,
            )
            return

        handle.checkpoint = handle.checkpoint.with_committed_stage(
            stage_id=message.stage_id,
            artifacts=manifest.artifacts,
        )
        handle.current_stage = None
        handle.current_stage_progress = 0.0
        try:
            self._persist_progress(
                handle=handle,
                include_runtime_state=True,
                force=True,
            )
        except Exception:
            self._mark_job_failed(
                handle=handle,
                reason="stage_post_commit_error",
                detail="The committed stage checkpoint could not be persisted.",
                failure_context={"error_code": "STAGE_CHECKPOINT_PERSIST_FAILED"},
            )
            return
        if message.stage_id == RESULT_PACKAGE_STAGE_ID:
            task_dir = handle.job_dir.parent.parent
            stage_root = handle.job_dir / "stages" / RESULT_PACKAGE_STAGE_ID
            stage_manifest_path = stage_root / RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH
            try:
                stage_manifest = load_result_package_stage_manifest(path=stage_manifest_path)
                published_package_path = publish_prepared_result_package(
                    prepared_package_path=stage_root
                    / stage_manifest.prepared_package_relative_path,
                    task_dir=task_dir,
                    stage_manifest=stage_manifest,
                )
                write_result_package_link(
                    task_dir=task_dir,
                    link=ResultPackageLink(
                        content_id=stage_manifest.content_id,
                        path=stage_manifest.published_package_relative_path,
                        format_version=stage_manifest.format_version,
                    ),
                )
                if not published_package_path.is_file():
                    raise ResultPackagePublicationError(
                        "published result package is missing after publication"
                    )
            except (
                OSError,
                ResultPackagePublicationError,
                ResultPackageValidationError,
            ) as error:
                self._mark_job_failed(
                    handle=handle,
                    reason="result_package_publication_failed",
                    detail="The result package could not be published.",
                    failure_context={
                        "error_type": type(error).__name__,
                        "stage_id": RESULT_PACKAGE_STAGE_ID,
                    },
                )
                return
        self._emit(
            RUNTIME_EVENT_STAGE_COMMITTED,
            {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "stage_id": message.stage_id,
            },
        )
        if message.stage_id == ALIGNMENT_STAGE_ID:
            alignment_context: dict[str, JSONValue] = {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "stage_id": message.stage_id,
                "manifest_path": ALIGNMENT_MANIFEST_RELATIVE_PATH,
                "artifact_count": len(manifest.artifacts),
            }
            self._emit(
                ALIGNMENT_RESULT_PUBLISHED_EVENT,
                {
                    **alignment_context,
                    "detail": "Alignment result was atomically published.",
                },
            )
            self._emit(
                ALIGNMENT_COMPLETED_EVENT,
                {
                    **alignment_context,
                    "detail": "Alignment stage completed successfully.",
                },
            )
        elif message.stage_id == _COMPARATIVE_ANALYSIS_STAGE_ID:
            from jelica_core.comparative_analysis.artifacts import (
                COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
                COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
                ComparativeAnalysisManifest,
                ComparativeAnalysisStatus,
            )

            domain_manifest_path = (
                handle.job_dir
                / "stages"
                / _COMPARATIVE_ANALYSIS_STAGE_ID
                / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH
            )
            try:
                domain_manifest = ComparativeAnalysisManifest.model_validate_json(
                    domain_manifest_path.read_text(encoding="utf-8")
                )
            except Exception:
                self._mark_job_failed(
                    handle=handle,
                    reason="comparative_analysis_manifest_invalid",
                    detail="Committed comparative-analysis manifest is invalid.",
                    failure_event_name=_COMPARATIVE_ANALYSIS_FAILED_EVENT,
                    failure_context={
                        "detail": "Comparative-analysis publication validation failed."
                    },
                )
                return
            comparative_context: dict[str, JSONValue] = {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "stage_id": message.stage_id,
                "manifest_path": COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
                "failures_path": COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
                "artifact_count": len(manifest.artifacts),
                "status": domain_manifest.status.value,
                "successful_result_count": domain_manifest.successful_result_count,
                "failed_result_count": domain_manifest.failed_result_count,
                "failure_count": domain_manifest.failure_count,
            }
            category_context_names = {
                "statistics": "statistics",
                "reference_sequence_differences": "reference",
                "pairwise_sequence_differences": "pairwise",
            }
            for category_name, context_prefix in category_context_names.items():
                category = domain_manifest.category_execution[category_name]
                comparative_context[f"{context_prefix}_successful"] = category.successful
                comparative_context[f"{context_prefix}_failed"] = category.failed
            self._emit(
                _COMPARATIVE_ANALYSIS_RESULT_PUBLISHED_EVENT,
                {
                    **comparative_context,
                    "detail": "Comparative-analysis artifacts were atomically published.",
                },
            )
            if domain_manifest.status is ComparativeAnalysisStatus.COMPLETED:
                final_event = _COMPARATIVE_ANALYSIS_COMPLETED_EVENT
                detail = "Comparative analysis completed successfully."
            elif domain_manifest.status is ComparativeAnalysisStatus.PARTIAL_SUCCESS:
                final_event = _COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS_EVENT
                detail = (
                    "Comparative analysis completed with partial results. "
                    f"Successful — statistical metrics: "
                    f"{comparative_context['statistics_successful']}, "
                    f"reference comparisons: "
                    f"{comparative_context['reference_successful']}, "
                    f"pairwise comparisons: "
                    f"{comparative_context['pairwise_successful']}. "
                    f"Failed — statistical metrics: "
                    f"{comparative_context['statistics_failed']}, "
                    f"reference comparisons: "
                    f"{comparative_context['reference_failed']}, "
                    f"pairwise comparisons: "
                    f"{comparative_context['pairwise_failed']}. "
                    "See the failures artifact."
                )
            else:
                final_event = _COMPARATIVE_ANALYSIS_FAILED_EVENT
                detail = (
                    "Comparative analysis failed. Failed — statistical metrics: "
                    f"{comparative_context['statistics_failed']}, "
                    f"reference comparisons: "
                    f"{comparative_context['reference_failed']}, "
                    f"pairwise comparisons: "
                    f"{comparative_context['pairwise_failed']}. "
                    "See the failures artifact."
                )
            self._emit(final_event, {**comparative_context, "detail": detail})
        elif message.stage_id == _DISTANCE_MATRIX_STAGE_ID:
            from jelica_core.distance_matrix import (
                DISTANCE_MATRIX_JSON_RELATIVE_PATH,
                DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
                DISTANCE_MATRIX_TSV_RELATIVE_PATH,
                DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
                DistanceMatrixManifest,
                DistanceMatrixStatus,
            )

            domain_manifest_path = (
                handle.job_dir
                / "stages"
                / _DISTANCE_MATRIX_STAGE_ID
                / DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH
            )
            try:
                domain_manifest = DistanceMatrixManifest.model_validate_json(
                    domain_manifest_path.read_text(encoding="utf-8")
                )
            except Exception:
                self._mark_job_failed(
                    handle=handle,
                    reason="distance_matrix_manifest_invalid",
                    detail="Committed distance-matrix manifest is invalid.",
                    failure_event_name=DISTANCE_MATRIX_FAILED_EVENT,
                    failure_context={"detail": "Distance-matrix publication validation failed."},
                )
                return
            distance_context: dict[str, JSONValue] = {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "stage_id": message.stage_id,
                "manifest_path": DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
                "artifact_count": len(manifest.artifacts),
                "status": domain_manifest.status.value,
                "enabled": domain_manifest.enabled,
                "unique_sequence_count": domain_manifest.unique_sequence_count,
                "total_pairs": domain_manifest.expected_pair_count,
                "defined_distance_count": domain_manifest.defined_distance_count,
                "undefined_distance_count": domain_manifest.undefined_distance_count,
            }
            if domain_manifest.enabled:
                distance_context.update(
                    {
                        "matrix_path": DISTANCE_MATRIX_JSON_RELATIVE_PATH,
                        "pairs_path": DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
                        "tsv_path": DISTANCE_MATRIX_TSV_RELATIVE_PATH,
                    }
                )
            self._emit(
                DISTANCE_MATRIX_RESULT_PUBLISHED_EVENT,
                {
                    **distance_context,
                    "detail": "Distance-matrix artifacts were atomically published.",
                },
            )
            if domain_manifest.status is DistanceMatrixStatus.COMPLETED:
                final_event = DISTANCE_MATRIX_COMPLETED_EVENT
                detail = "Distance matrix completed successfully."
            elif domain_manifest.status is DistanceMatrixStatus.PARTIAL_SUCCESS:
                final_event = DISTANCE_MATRIX_PARTIAL_SUCCESS_EVENT
                detail = (
                    "Distance matrix completed with partial results. "
                    f"Undefined distances: {domain_manifest.undefined_distance_count}."
                )
            else:
                final_event = DISTANCE_MATRIX_FAILED_EVENT
                detail = "Distance matrix failed."
            self._emit(final_event, {**distance_context, "detail": detail})
        elif message.stage_id == _PHYLOGENETIC_TREE_STAGE_ID:
            from jelica_core.phylogenetic_tree import (
                PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
                TREE_DIAGNOSTICS_RELATIVE_PATH,
                TREE_JSON_RELATIVE_PATH,
                TREE_ROOTED_NWK_RELATIVE_PATH,
                TREE_UNROOTED_NWK_RELATIVE_PATH,
                PhylogeneticTreeManifest,
                PhylogeneticTreeStatus,
            )

            domain_manifest_path = (
                handle.job_dir
                / "stages"
                / _PHYLOGENETIC_TREE_STAGE_ID
                / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH
            )
            try:
                domain_manifest = PhylogeneticTreeManifest.model_validate_json(
                    domain_manifest_path.read_text(encoding="utf-8")
                )
            except Exception:
                self._mark_job_failed(
                    handle=handle,
                    reason="phylogenetic_tree_manifest_invalid",
                    detail="Committed phylogenetic-tree manifest is invalid.",
                    failure_event_name=PHYLOGENETIC_TREE_FAILED_EVENT,
                    failure_context={"detail": "Phylogenetic-tree publication validation failed."},
                )
                return
            tree_context: dict[str, JSONValue] = {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "stage_id": message.stage_id,
                "manifest_path": PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
                "artifact_count": len(manifest.artifacts),
                "status": domain_manifest.status.value,
                "enabled": domain_manifest.enabled,
                "leaf_count": domain_manifest.leaf_count,
                "internal_node_count": domain_manifest.internal_node_count,
                "edge_count": domain_manifest.edge_count,
                "construction_mode": domain_manifest.construction_mode.value,
                "inference_performed": domain_manifest.inference_performed,
                "applied_rooting": domain_manifest.applied_rooting,
                "zero_diameter": domain_manifest.zero_diameter,
            }
            if domain_manifest.enabled:
                tree_context.update(
                    {
                        "unrooted_tree_path": TREE_UNROOTED_NWK_RELATIVE_PATH,
                        "rooted_tree_path": TREE_ROOTED_NWK_RELATIVE_PATH,
                        "tree_json_path": TREE_JSON_RELATIVE_PATH,
                        "diagnostics_path": TREE_DIAGNOSTICS_RELATIVE_PATH,
                    }
                )
            self._emit(
                PHYLOGENETIC_TREE_RESULT_PUBLISHED_EVENT,
                {
                    **tree_context,
                    "detail": "Phylogenetic-tree artifacts were atomically published.",
                },
            )
            if domain_manifest.status is PhylogeneticTreeStatus.COMPLETED:
                final_event = PHYLOGENETIC_TREE_COMPLETED_EVENT
                detail = "Phylogenetic tree completed successfully."
            else:
                final_event = PHYLOGENETIC_TREE_FAILED_EVENT
                detail = "Phylogenetic tree failed."
            self._emit(final_event, {**tree_context, "detail": detail})
        elif message.stage_id == _CLADE_DETECTION_STAGE_ID:
            from jelica_core.clade_detection import (
                CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
                CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
                CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
                INFERRED_CLADES_JSON_RELATIVE_PATH,
                CladeDetectionManifest,
                CladeDetectionStatus,
            )

            domain_manifest_path = (
                handle.job_dir
                / "stages"
                / _CLADE_DETECTION_STAGE_ID
                / CLADE_DETECTION_MANIFEST_RELATIVE_PATH
            )
            try:
                domain_manifest = CladeDetectionManifest.model_validate_json(
                    domain_manifest_path.read_text(encoding="utf-8")
                )
            except Exception:
                self._mark_job_failed(
                    handle=handle,
                    reason="clade_detection_manifest_invalid",
                    detail="Committed clade-detection manifest is invalid.",
                    failure_event_name=CLADE_DETECTION_FAILED_EVENT,
                    failure_context={"detail": "Clade-detection publication validation failed."},
                )
                return
            clade_context: dict[str, JSONValue] = {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "stage_id": message.stage_id,
                "manifest_path": CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
                "artifact_count": len(manifest.artifacts),
                "status": domain_manifest.status.value,
                "enabled": domain_manifest.enabled,
                "method": domain_manifest.method.value,
                "threshold": domain_manifest.max_within_clade_distance,
                "leaf_count": domain_manifest.leaf_count,
                "clade_count": domain_manifest.clade_count,
                "singleton_clade_count": domain_manifest.singleton_clade_count,
                "multi_leaf_clade_count": domain_manifest.multi_leaf_clade_count,
            }
            if domain_manifest.enabled:
                clade_context.update(
                    {
                        "result_path": INFERRED_CLADES_JSON_RELATIVE_PATH,
                        "memberships_path": CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
                        "assignments_path": CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
                    }
                )
            self._emit(
                CLADE_DETECTION_RESULT_PUBLISHED_EVENT,
                {
                    **clade_context,
                    "detail": "Clade-detection artifacts were atomically published.",
                },
            )
            if domain_manifest.status is CladeDetectionStatus.COMPLETED:
                final_event = CLADE_DETECTION_COMPLETED_EVENT
                detail = "Clade detection completed successfully."
            else:
                final_event = CLADE_DETECTION_FAILED_EVENT
                detail = "Clade detection failed."
            self._emit(final_event, {**clade_context, "detail": detail})

    def _handle_job_completed(self, *, handle: _WorkerHandle) -> None:
        self._persist_progress(handle=handle, include_runtime_state=True, force=True)
        mutation = self._registry_service.transition_active_job_state(
            task_id=handle.task_id,
            to_state=AnalyticalTaskState.COMPLETED,
            expected_task_version=handle.task_record_version,
            expected_job_version=handle.job_record_version,
        )
        if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
            if mutation.task is not None:
                handle.task_record_version = mutation.task.record_version
            if mutation.job is not None:
                handle.job_record_version = mutation.job.record_version
            self._completed_jobs += 1
            self._emit(
                RUNTIME_EVENT_JOB_COMPLETED,
                {"task_id": handle.task_id, "job_id": handle.job_id},
            )
        elif mutation.result_type in {
            AnalyticalTaskMutationResultType.CONFLICT,
            AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
            AnalyticalTaskMutationResultType.INVALID_TRANSITION,
        }:
            self._finalize_control_requested_transition(handle=handle)
        self._finalize_worker_handle(job_id=handle.job_id)

    def _handle_job_failed(self, *, handle: _WorkerHandle, message: JobFailedMessage) -> None:
        self._mark_job_failed(
            handle=handle,
            reason=message.reason,
            detail=message.detail,
            failure_event_name=message.failure_event_name,
            failure_context=message.failure_context,
        )

    def _handle_job_stopped(self, *, handle: _WorkerHandle, message: JobStoppedMessage) -> None:
        if message.reason is WorkerStopReason.DELETION_REQUESTED:
            cleanup_worker_staging(
                job_dir=handle.job_dir,
                worker_instance_id=handle.worker_instance_id,
            )
            self._finalize_requested_task_deletion(handle=handle)
            self._emit(
                RUNTIME_EVENT_RUNTIME_INTERRUPTED,
                {
                    "runtime_instance_id": self._runtime_instance_id,
                    "task_id": handle.task_id,
                    "job_id": handle.job_id,
                    "reason": message.reason.value,
                },
            )
            self._finalize_worker_handle(job_id=handle.job_id)
            return

        target_state, finished_reason = self._resolve_worker_stop_transition(
            handle=handle,
            stop_reason=message.reason,
        )
        mutation = self._registry_service.transition_active_job_state(
            task_id=handle.task_id,
            to_state=target_state,
            expected_task_version=handle.task_record_version,
            expected_job_version=handle.job_record_version,
            finished_reason=finished_reason,
        )
        if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
            if mutation.task is not None:
                handle.task_record_version = mutation.task.record_version
            if mutation.job is not None:
                handle.job_record_version = mutation.job.record_version
        elif mutation.result_type is AnalyticalTaskMutationResultType.CONCURRENT_UPDATE:
            self._refresh_handle_versions(handle=handle)
            follow_up = self._registry_service.transition_active_job_state(
                task_id=handle.task_id,
                to_state=target_state,
                expected_task_version=handle.task_record_version,
                expected_job_version=handle.job_record_version,
                finished_reason=finished_reason,
            )
            if follow_up.result_type is AnalyticalTaskMutationResultType.APPLIED:
                if follow_up.task is not None:
                    handle.task_record_version = follow_up.task.record_version
                if follow_up.job is not None:
                    handle.job_record_version = follow_up.job.record_version
            elif follow_up.result_type in {
                AnalyticalTaskMutationResultType.CONFLICT,
                AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                AnalyticalTaskMutationResultType.INVALID_TRANSITION,
            }:
                self._finalize_control_requested_transition(handle=handle)
        elif mutation.result_type in {
            AnalyticalTaskMutationResultType.CONFLICT,
            AnalyticalTaskMutationResultType.INVALID_TRANSITION,
        }:
            self._finalize_control_requested_transition(handle=handle)
        cleanup_worker_staging(
            job_dir=handle.job_dir,
            worker_instance_id=handle.worker_instance_id,
        )
        post_stop_job = self._registry_service.get_job(job_id=handle.job_id)
        if post_stop_job.state is AnalyticalTaskState.PAUSED:
            self._emit(
                RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PAUSE,
                {
                    "task_id": handle.task_id,
                    "job_id": handle.job_id,
                    "reason": message.reason.value,
                },
            )
        elif post_stop_job.state is AnalyticalTaskState.CANCELLED:
            self._emit(
                RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_CANCEL,
                {
                    "task_id": handle.task_id,
                    "job_id": handle.job_id,
                    "reason": message.reason.value,
                },
            )
        elif (
            post_stop_job.state is AnalyticalTaskState.WAITING
            and message.reason is WorkerStopReason.PREEMPTION_REQUESTED
        ):
            self._emit(
                RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PREEMPTION,
                {
                    "task_id": handle.task_id,
                    "job_id": handle.job_id,
                    "reason": message.reason.value,
                },
            )
            self._emit(
                RUNTIME_EVENT_PREEMPTED_JOB_RETURNED_TO_WAITING,
                {
                    "task_id": handle.task_id,
                    "job_id": handle.job_id,
                },
            )
            self._requeue_preempted_job(task_id=handle.task_id, job_id=handle.job_id)
        self._emit(
            RUNTIME_EVENT_RUNTIME_INTERRUPTED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "reason": message.reason.value,
            },
        )
        self._finalize_worker_handle(job_id=handle.job_id)

    def _mark_job_failed(
        self,
        *,
        handle: _WorkerHandle,
        reason: str,
        detail: str,
        failure_event_name: str | None = None,
        failure_context: dict[str, object] | None = None,
    ) -> None:
        try:
            mutation = self._registry_service.transition_active_job_state(
                task_id=handle.task_id,
                to_state=AnalyticalTaskState.FAILED,
                expected_task_version=handle.task_record_version,
                expected_job_version=handle.job_record_version,
                finished_reason=f"{reason}: {detail}".strip(),
            )
            if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
                self._failed_jobs += 1
                if mutation.task is not None:
                    handle.task_record_version = mutation.task.record_version
                if mutation.job is not None:
                    handle.job_record_version = mutation.job.record_version
                event_context: dict[str, JSONValue] = {
                    "task_id": handle.task_id,
                    "job_id": handle.job_id,
                    "reason": reason,
                    "detail": detail,
                }
                if failure_event_name is not None:
                    event_context["failure_event_name"] = failure_event_name
                if failure_context is not None:
                    event_context["failure_context"] = {
                        key: _to_json_value(value) for key, value in failure_context.items()
                    }
                self._emit(
                    RUNTIME_EVENT_JOB_FAILED,
                    event_context,
                )
            elif mutation.result_type in {
                AnalyticalTaskMutationResultType.CONFLICT,
                AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                AnalyticalTaskMutationResultType.INVALID_TRANSITION,
            }:
                self._finalize_control_requested_transition(handle=handle)
        finally:
            self._stop_failed_worker(handle=handle)

    def _stop_failed_worker(self, *, handle: _WorkerHandle) -> None:
        # A fatal engine-side failure invalidates further output from this worker.
        # Keep the handle tracked until the process has stopped, then remove staging.
        handle.runtime_shutdown_event.set()
        handle.process.join(timeout=1.0)
        if handle.process.is_alive() or _read_external_process_pid(handle) > 0:
            self._force_stop_worker_processes(handle)
        try:
            cleanup_worker_staging(
                job_dir=handle.job_dir,
                worker_instance_id=handle.worker_instance_id,
            )
        finally:
            self._finalize_worker_handle(job_id=handle.job_id)

    def _resolve_worker_stop_transition(
        self,
        *,
        handle: _WorkerHandle,
        stop_reason: WorkerStopReason,
    ) -> tuple[AnalyticalTaskState, str | None]:
        current_job = self._registry_service.get_job(job_id=handle.job_id)
        if current_job.state is AnalyticalTaskState.CANCEL_REQUESTED:
            return AnalyticalTaskState.CANCELLED, "cancelled"
        if current_job.state is AnalyticalTaskState.PAUSE_REQUESTED:
            return AnalyticalTaskState.PAUSED, None
        if current_job.state is AnalyticalTaskState.PREEMPTION_REQUESTED:
            return AnalyticalTaskState.WAITING, None

        if stop_reason is WorkerStopReason.CANCEL_REQUESTED:
            return AnalyticalTaskState.CANCELLED, "cancelled"
        if stop_reason is WorkerStopReason.PAUSE_REQUESTED:
            return AnalyticalTaskState.PAUSED, None
        if stop_reason is WorkerStopReason.PREEMPTION_REQUESTED:
            return AnalyticalTaskState.WAITING, None
        if stop_reason is WorkerStopReason.DELETION_REQUESTED:
            return AnalyticalTaskState.WAITING, None
        return AnalyticalTaskState.WAITING, None

    def _finalize_control_requested_transition(self, *, handle: _WorkerHandle) -> None:
        try:
            task_record = self._registry_service.get_task(task_id=handle.task_id)
        except AnalyticalTaskNotFoundError:
            return
        if task_record.state is AnalyticalTaskState.DELETION_REQUESTED:
            self._finalize_requested_task_deletion(handle=handle)
            return

        try:
            current_job = self._registry_service.get_job(job_id=handle.job_id)
        except AnalyticalTaskJobNotFoundError:
            return
        if current_job.state is AnalyticalTaskState.CANCEL_REQUESTED:
            self._registry_service.transition_active_job_state(
                task_id=handle.task_id,
                to_state=AnalyticalTaskState.CANCELLED,
                finished_reason="cancelled",
            )
            return
        if current_job.state is AnalyticalTaskState.PAUSE_REQUESTED:
            self._registry_service.transition_active_job_state(
                task_id=handle.task_id,
                to_state=AnalyticalTaskState.PAUSED,
            )
            return
        if current_job.state is AnalyticalTaskState.PREEMPTION_REQUESTED:
            self._registry_service.transition_active_job_state(
                task_id=handle.task_id,
                to_state=AnalyticalTaskState.WAITING,
            )
            return

    def _finalize_requested_task_deletion(self, *, handle: _WorkerHandle) -> None:
        try:
            task_record = self._registry_service.get_task(task_id=handle.task_id)
        except AnalyticalTaskNotFoundError:
            return
        if task_record.state is not AnalyticalTaskState.DELETION_REQUESTED:
            return
        if not self._delete_task_workspace_and_registry(task_record=task_record):
            return
        self._emit(
            RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_DELETION,
            {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
            },
        )

    def _delete_task_workspace_and_registry(self, *, task_record: AnalyticalTaskRecord) -> bool:
        move_result = None
        try:
            move_result = move_task_workspace_to_trash(
                tasks_dir=self._tasks_dir,
                task_dir_relative_path=task_record.task_dir_relative_path,
                task_id=task_record.task_id,
            )
            delete_result = self._registry_service.delete_task_and_jobs(task_id=task_record.task_id)
            if delete_result.result_type is AnalyticalTaskMutationResultType.APPLIED:
                if move_result is not None:
                    purge_trashed_task_workspace(
                        task_id=task_record.task_id,
                        move_result=move_result,
                    )
                return True
            if delete_result.result_type is AnalyticalTaskMutationResultType.NOT_FOUND:
                if move_result is not None:
                    purge_trashed_task_workspace(
                        task_id=task_record.task_id,
                        move_result=move_result,
                    )
                return True

            if move_result is not None:
                restore_task_workspace_from_trash(
                    task_id=task_record.task_id,
                    move_result=move_result,
                )
            self._emit(
                RUNTIME_EVENT_STALE_MESSAGE_REJECTED,
                {
                    "task_id": task_record.task_id,
                    "job_id": task_record.active_job_id or task_record.latest_job_id,
                    "reason": f"deletion_{delete_result.result_type.value}",
                },
            )
            return False
        except TaskWorkspaceDeleteError as error:
            self._emit(
                RUNTIME_EVENT_STALE_MESSAGE_REJECTED,
                {
                    "task_id": task_record.task_id,
                    "job_id": task_record.active_job_id or task_record.latest_job_id,
                    "reason": "deletion_workspace_error",
                    "detail": str(error),
                },
            )
            return False

    def _requeue_preempted_job(self, *, task_id: str, job_id: str) -> None:
        snapshot = self._registry_service.get_task_snapshot(task_id=task_id)
        active_job = snapshot.active_or_latest_job
        if active_job is None:
            return
        if snapshot.task.active_job_id != job_id or active_job.job_id != job_id:
            return
        if active_job.state is not AnalyticalTaskState.WAITING:
            return
        self._registry_service.start(
            task_id=task_id,
            expected_task_version=snapshot.task.record_version,
        )

    def _refresh_handle_versions(self, *, handle: _WorkerHandle) -> bool:
        snapshot = self._registry_service.get_task_snapshot(task_id=handle.task_id)
        active_or_latest = snapshot.active_or_latest_job
        if active_or_latest is None:
            return False
        if active_or_latest.job_id != handle.job_id:
            return False
        if active_or_latest.worker_instance_id != handle.worker_instance_id:
            return False
        if active_or_latest.lease_token != handle.lease_token:
            return False
        handle.task_record_version = snapshot.task.record_version
        handle.job_record_version = active_or_latest.record_version
        return True

    def _persist_progress(
        self,
        *,
        handle: _WorkerHandle,
        include_runtime_state: bool,
        force: bool,
    ) -> None:
        if not force and handle.current_stage is None:
            return
        progress = self._compute_progress(
            checkpoint=handle.checkpoint,
            pipeline_definition=handle.pipeline_definition,
            active_stage_progress=handle.current_stage_progress,
            active_stage=handle.current_stage,
        )
        mutation = self._registry_service.update_active_job_progress(
            task_id=handle.task_id,
            progress=progress,
            current_stage=handle.current_stage,
            runtime_state=(handle.checkpoint.to_runtime_state() if include_runtime_state else None),
            expected_task_version=handle.task_record_version,
            expected_job_version=handle.job_record_version,
        )
        if mutation.result_type in {
            AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
            AnalyticalTaskMutationResultType.CONFLICT,
        } and self._refresh_handle_versions(handle=handle):
            mutation = self._registry_service.update_active_job_progress(
                task_id=handle.task_id,
                progress=progress,
                current_stage=handle.current_stage,
                runtime_state=(
                    handle.checkpoint.to_runtime_state() if include_runtime_state else None
                ),
                expected_task_version=handle.task_record_version,
                expected_job_version=handle.job_record_version,
            )
        if mutation.result_type in {
            AnalyticalTaskMutationResultType.APPLIED,
            AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
        }:
            if mutation.task is not None:
                handle.task_record_version = mutation.task.record_version
            if mutation.job is not None:
                handle.job_record_version = mutation.job.record_version
            handle.last_progress_flush_monotonic = time.monotonic()
            return
        self._emit(
            RUNTIME_EVENT_STALE_MESSAGE_REJECTED,
            {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "reason": f"progress_{mutation.result_type.value}",
            },
        )
        self._finalize_worker_handle(job_id=handle.job_id)

    def _heartbeat_workers_if_due(self) -> None:
        now_monotonic = time.monotonic()
        for handle in list(self._running_workers.values()):
            if (
                now_monotonic - handle.last_worker_heartbeat_monotonic
                < self._runtime_config.heartbeat_interval_seconds
            ):
                continue
            updated_job = self._registry_service.heartbeat_job_worker(
                job_id=handle.job_id,
                worker_instance_id=handle.worker_instance_id,
                lease_token=handle.lease_token,
                lease_timeout_seconds=self._runtime_config.lease_timeout_seconds,
            )
            if updated_job is None:
                if self._is_waiting_for_control_stop_outcome(handle=handle):
                    handle.last_worker_heartbeat_monotonic = now_monotonic
                    continue
                self._emit(
                    RUNTIME_EVENT_WORKER_HEARTBEAT_LOST,
                    {"task_id": handle.task_id, "job_id": handle.job_id},
                )
                self._finalize_worker_handle(job_id=handle.job_id, terminate=True)
                continue
            handle.job_record_version = updated_job.record_version
            handle.last_worker_heartbeat_monotonic = now_monotonic

    def _is_waiting_for_control_stop_outcome(self, *, handle: _WorkerHandle) -> bool:
        try:
            task_record = self._registry_service.get_task(task_id=handle.task_id)
        except AnalyticalTaskNotFoundError:
            return False
        if task_record.state is AnalyticalTaskState.DELETION_REQUESTED:
            return True

        try:
            job_record = self._registry_service.get_job(job_id=handle.job_id)
        except AnalyticalTaskJobNotFoundError:
            return False
        if (
            job_record.worker_instance_id != handle.worker_instance_id
            or job_record.lease_token != handle.lease_token
        ):
            return False
        return job_record.state in {
            AnalyticalTaskState.CANCEL_REQUESTED,
            AnalyticalTaskState.PAUSE_REQUESTED,
            AnalyticalTaskState.PREEMPTION_REQUESTED,
        }

    def _reap_exited_workers(self) -> None:
        now_monotonic = time.monotonic()
        for handle in list(self._running_workers.values()):
            if handle.process.is_alive():
                handle.terminal_message_wait_started_monotonic = None
                continue
            exit_code = handle.process.exitcode
            if exit_code is None:
                continue
            if handle.terminal_message_wait_started_monotonic is None:
                handle.terminal_message_wait_started_monotonic = now_monotonic
                continue
            if (
                now_monotonic - handle.terminal_message_wait_started_monotonic
                < self._runtime_config.heartbeat_interval_seconds
            ):
                continue
            self._mark_job_failed(
                handle=handle,
                reason="worker_terminal_message_missing",
                detail=(
                    f"worker exited with code {exit_code} before terminal lifecycle message arrived"
                ),
            )

    def _request_worker_stop(self) -> None:
        for handle in self._running_workers.values():
            handle.runtime_shutdown_event.set()

    def _force_stop_workers(self) -> None:
        for handle in list(self._running_workers.values()):
            self._force_stop_worker_processes(handle)
            cleanup_worker_staging(
                job_dir=handle.job_dir,
                worker_instance_id=handle.worker_instance_id,
            )
            target_state, finished_reason = self._resolve_worker_stop_transition(
                handle=handle,
                stop_reason=WorkerStopReason.RUNTIME_SHUTDOWN,
            )
            mutation = self._registry_service.transition_active_job_state(
                task_id=handle.task_id,
                to_state=target_state,
                expected_task_version=handle.task_record_version,
                expected_job_version=handle.job_record_version,
                finished_reason=finished_reason,
            )
            if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
                if mutation.task is not None:
                    handle.task_record_version = mutation.task.record_version
                if mutation.job is not None:
                    handle.job_record_version = mutation.job.record_version
            self._emit(
                RUNTIME_EVENT_WORKER_EXITED,
                {
                    "task_id": handle.task_id,
                    "job_id": handle.job_id,
                    "exit_code": handle.process.exitcode or -1,
                },
            )
            self._running_workers.pop(handle.job_id, None)

    def _stop_workers_without_db_updates(self) -> None:
        for handle in list(self._running_workers.values()):
            self._force_stop_worker_processes(handle)
            self._running_workers.pop(handle.job_id, None)

    def _finalize_worker_handle(self, *, job_id: str, terminate: bool = False) -> None:
        handle = self._running_workers.get(job_id)
        if handle is None:
            return
        if terminate:
            self._force_stop_worker_processes(handle)
        else:
            handle.process.join(timeout=1.0)
            if handle.process.is_alive() or _read_external_process_pid(handle) > 0:
                self._force_stop_worker_processes(handle)
        exit_code = handle.process.exitcode if handle.process.exitcode is not None else -1
        self._running_workers.pop(job_id, None)
        self._emit(
            RUNTIME_EVENT_WORKER_EXITED,
            {
                "task_id": handle.task_id,
                "job_id": handle.job_id,
                "exit_code": exit_code,
            },
        )

    def _force_stop_worker_processes(self, handle: _WorkerHandle) -> None:
        external_process_stopped = _force_stop_worker_processes(handle)
        if external_process_stopped and handle.current_stage == ALIGNMENT_STAGE_ID:
            self._emit(
                ALIGNMENT_MAFFT_STOPPED_SHUTDOWN_EVENT,
                {
                    "task_id": handle.task_id,
                    "job_id": handle.job_id,
                    "stage_id": ALIGNMENT_STAGE_ID,
                    "forced": True,
                    "detail": "MAFFT was stopped during forced runtime shutdown.",
                },
            )

    def _is_queue_drained(self) -> bool:
        if len(self._running_workers) > 0:
            return False
        return self._registry_service.count_queued_jobs() == 0

    def _is_worker_lease_expired(self, job_record: AnalyticalTaskJobRecord) -> bool:
        if job_record.lease_expires_at is None:
            return True
        return job_record.lease_expires_at <= utc_now()

    def _release_runtime_lease(self) -> None:
        if self._runtime_lease_lost:
            return
        self._registry_service.release_execution_runtime_lease(
            runtime_instance_id=self._runtime_instance_id,
            lease_token=self._runtime_lease_token,
        )

    def _task_dir(self, *, task_record: AnalyticalTaskRecord) -> Path:
        return self._tasks_dir / task_record.task_dir_relative_path

    def _job_dir(
        self,
        *,
        task_record: AnalyticalTaskRecord,
        job_record: AnalyticalTaskJobRecord,
    ) -> Path:
        return self._task_dir(task_record=task_record) / "jobs" / job_record.job_id

    def _compute_progress(
        self,
        *,
        checkpoint: RuntimeStateCheckpoint,
        pipeline_definition: PipelineDefinition,
        active_stage_progress: float,
        active_stage: str | None,
    ) -> int:
        total_weight = pipeline_definition.total_weight
        if total_weight <= 0:
            return 100
        weights_by_stage = {stage.stage_id: stage.weight for stage in pipeline_definition.stages}
        completed_weight = sum(
            weights_by_stage.get(stage_id, 0.0) for stage_id in checkpoint.completed_stages
        )
        effective_active_stage = active_stage or checkpoint.active_stage
        active_weight = (
            weights_by_stage.get(effective_active_stage, 0.0) if effective_active_stage else 0.0
        )
        bounded_stage_progress = min(1.0, max(0.0, active_stage_progress))
        ratio = (completed_weight + active_weight * bounded_stage_progress) / total_weight
        bounded_ratio = min(1.0, max(0.0, ratio))
        return int(round(bounded_ratio * 100))


def _committed_comparative_analysis_failed(
    *,
    job_dir: Path,
    task_id: str,
    job_id: str,
    config_hash: str,
    pipeline_version: str,
) -> bool:
    stage_root = job_dir / "stages" / _COMPARATIVE_ANALYSIS_STAGE_ID
    if not stage_root.exists():
        return False
    snapshot = validate_committed_stage_snapshot(
        job_dir=job_dir,
        stage_id=_COMPARATIVE_ANALYSIS_STAGE_ID,
        expected_job_id=job_id,
        expected_pipeline_version=pipeline_version,
        expected_task_id=task_id,
        expected_config_hash=config_hash,
    )
    return snapshot.domain_status == "failed"


def _recovery_target_state(state: AnalyticalTaskState) -> tuple[AnalyticalTaskState, str | None]:
    if state in {AnalyticalTaskState.RUNNING, AnalyticalTaskState.PREEMPTION_REQUESTED}:
        return AnalyticalTaskState.WAITING, None
    if state is AnalyticalTaskState.PAUSE_REQUESTED:
        return AnalyticalTaskState.PAUSED, None
    if state is AnalyticalTaskState.CANCEL_REQUESTED:
        return AnalyticalTaskState.CANCELLED, "cancelled"
    return state, None


def _to_json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_value(item) for item in value]
    return str(value)


def _read_external_process_pid(handle: _WorkerHandle) -> int:
    with handle.external_process_pid_state.get_lock():
        value = handle.external_process_pid_state.value
    return value if isinstance(value, int) and value > 0 else 0


def _clear_external_process_pid(handle: _WorkerHandle, *, expected_pid: int) -> None:
    with handle.external_process_pid_state.get_lock():
        if handle.external_process_pid_state.value == expected_pid:
            handle.external_process_pid_state.value = 0


def _force_stop_worker_processes(handle: _WorkerHandle) -> bool:
    external_process_stopped = False
    external_process_pid = _read_external_process_pid(handle)
    if external_process_pid > 0:
        terminate_process_tree_by_pid(external_process_pid)
        external_process_stopped = True
    if handle.process.is_alive():
        handle.process.terminate()
        handle.process.join(timeout=1.0)
    if handle.process.is_alive():
        handle.process.kill()
        handle.process.join()
    if handle.process.is_alive():
        raise RuntimeError("Worker process remained alive after forced termination.")
    external_process_pid = _read_external_process_pid(handle)
    if external_process_pid > 0:
        terminate_process_tree_by_pid(external_process_pid)
        external_process_stopped = True
        _clear_external_process_pid(handle, expected_pid=external_process_pid)
    return external_process_stopped
