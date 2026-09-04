from __future__ import annotations

import os
import time
from typing import Any, Callable

from jelica_core.tasks import (
    AnalyticalTaskJobNotFoundError,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryService,
    AnalyticalTaskState,
)
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .alignment_stage import AlignmentStageError
from .artifacts import (
    STAGE_MANIFEST_FILENAME,
    StageArtifactManifest,
    StageSnapshotErrorCode,
    StageSnapshotValidationError,
    validate_committed_stage_snapshot,
    write_stage_manifest,
)
from .input_acquisition import InputAcquisitionError
from .input_processing_stage import INPUT_PROCESSING_FAILED_EVENT
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
    WorkerStartedMessage,
    WorkerStopReason,
)
from .models import RuntimeStateCheckpoint, WorkerLaunchSpec
from .pipeline import StageContext, build_pipeline_definition

_STAGE_COMMIT_BARRIER_TIMEOUT_SECONDS = 5.0
_STAGE_COMMIT_BARRIER_POLL_SECONDS = 0.05


class _WorkerControlRequested(RuntimeError):
    def __init__(self, reason: WorkerStopReason) -> None:
        self.reason = reason
        super().__init__(f"worker control requested: {reason.value}")


class _QueueProgressReporter:
    def __init__(
        self,
        *,
        launch_spec: WorkerLaunchSpec,
        stage_id: str,
        message_queue: Any,
    ) -> None:
        self._launch_spec = launch_spec
        self._stage_id = stage_id
        self._message_queue = message_queue
        self._last_progress = -1.0
        self._description: str | None = None

    def start(self, *, description: str, total: float | None = None) -> None:
        self._description = description
        self.update(description=description, progress=0.0)

    def update(
        self,
        *,
        description: str | None = None,
        progress: float | None = None,
    ) -> None:
        next_description = self._description if description is None else description
        bounded_progress = self._last_progress if progress is None else min(1.0, max(0.0, progress))
        if bounded_progress < self._last_progress:
            return
        if bounded_progress == self._last_progress and next_description == self._description:
            return
        self._last_progress = bounded_progress
        self._description = next_description
        _send_message(
            self._message_queue,
            ProgressUpdatedMessage(
                task_id=self._launch_spec.task_id,
                job_id=self._launch_spec.job_id,
                worker_instance_id=self._launch_spec.worker_instance_id,
                lease_token=self._launch_spec.lease_token,
                stage_id=self._stage_id,
                stage_progress=bounded_progress,
                description=next_description,
            ),
        )

    def complete(self, *, description: str | None = None) -> None:
        self.update(description=description, progress=1.0)

    def __call__(self, progress: float) -> None:
        self.update(progress=progress)


class _QueueStageEventReporter:
    def __init__(
        self,
        *,
        launch_spec: WorkerLaunchSpec,
        stage_id: str,
        message_queue: Any,
    ) -> None:
        self._launch_spec = launch_spec
        self._stage_id = stage_id
        self._message_queue = message_queue

    def __call__(self, event_name: str, context: dict[str, object]) -> None:
        _send_message(
            self._message_queue,
            StageEventMessage(
                task_id=self._launch_spec.task_id,
                job_id=self._launch_spec.job_id,
                worker_instance_id=self._launch_spec.worker_instance_id,
                lease_token=self._launch_spec.lease_token,
                stage_id=self._stage_id,
                event_name=event_name,
                context=context,
            ),
        )


def run_worker_process(
    launch_spec: WorkerLaunchSpec,
    message_queue: Any,
    runtime_shutdown_event: Any,
    deletion_requested_event: Any,
    pause_requested_event: Any,
    cancel_requested_event: Any,
    external_process_pid_state: Any | None = None,
) -> None:
    _send_message(
        message_queue,
        WorkerStartedMessage(
            task_id=launch_spec.task_id,
            job_id=launch_spec.job_id,
            worker_instance_id=launch_spec.worker_instance_id,
            lease_token=launch_spec.lease_token,
            worker_pid=_current_pid(),
        ),
    )
    _send_message(
        message_queue,
        WorkerHeartbeatMessage(
            task_id=launch_spec.task_id,
            job_id=launch_spec.job_id,
            worker_instance_id=launch_spec.worker_instance_id,
            lease_token=launch_spec.lease_token,
        ),
    )

    current_stage_id: str | None = None
    try:
        registry_service = AnalyticalTaskRegistryService(database_path=launch_spec.database_path)
        pipeline = build_pipeline_definition(
            pipeline_name=launch_spec.pipeline_name,
            pipeline_version=launch_spec.pipeline_version,
            config_revision_path=launch_spec.config_revision_path,
        )
        checkpoint = RuntimeStateCheckpoint.from_runtime_state_json(
            launch_spec.runtime_state_json,
            pipeline_version=pipeline.version,
        )
        completed_stage_ids = set(checkpoint.completed_stages)

        def _control_check() -> None:
            stop_reason = _read_stop_reason(
                runtime_shutdown_event=runtime_shutdown_event,
                deletion_requested_event=deletion_requested_event,
                pause_requested_event=pause_requested_event,
                cancel_requested_event=cancel_requested_event,
                registry_service=registry_service,
                launch_spec=launch_spec,
            )
            if stop_reason is None:
                return
            raise _WorkerControlRequested(stop_reason)

        for index, stage in enumerate(pipeline.stages):
            try:
                _control_check()
            except _WorkerControlRequested as stop_requested:
                _send_job_stopped(
                    launch_spec=launch_spec,
                    message_queue=message_queue,
                    reason=stop_requested.reason,
                )
                return

            if stage.stage_id in completed_stage_ids:
                validate_committed_stage_snapshot(
                    job_dir=launch_spec.job_dir,
                    stage_id=stage.stage_id,
                    expected_job_id=launch_spec.job_id,
                    expected_pipeline_version=launch_spec.pipeline_version,
                    expected_task_id=launch_spec.task_id,
                    expected_config_hash=launch_spec.config_hash,
                )
                continue

            current_stage_id = stage.stage_id
            stage_staging_directory = (
                launch_spec.job_dir / "staging" / stage.stage_id / launch_spec.worker_instance_id
            )
            stage_staging_directory.mkdir(parents=True, exist_ok=True)
            event_reporter = _QueueStageEventReporter(
                launch_spec=launch_spec,
                stage_id=stage.stage_id,
                message_queue=message_queue,
            )
            stage_context = StageContext(
                launch_spec=launch_spec,
                stage_index=index,
                stage_staging_directory=stage_staging_directory,
                event_reporter=event_reporter,
                control_check=_control_check,
                external_process_pid_state=external_process_pid_state,
            )
            _send_message(
                message_queue,
                StageStartedMessage(
                    task_id=launch_spec.task_id,
                    job_id=launch_spec.job_id,
                    worker_instance_id=launch_spec.worker_instance_id,
                    lease_token=launch_spec.lease_token,
                    stage_id=stage.stage_id,
                    stage_index=index,
                    stage_weight=stage.weight,
                    total_weight=pipeline.total_weight,
                ),
            )
            reporter = _QueueProgressReporter(
                launch_spec=launch_spec,
                stage_id=stage.stage_id,
                message_queue=message_queue,
            )
            reporter.start(description=stage.stage_id)
            try:
                stage.preflight(stage_context)
                stage_result = stage.run(stage_context, reporter)
                reporter.complete(description=stage.stage_id)
                if stage_result.check_control_before_commit:
                    _control_check()
            except _WorkerControlRequested as stop_requested:
                _send_job_stopped(
                    launch_spec=launch_spec,
                    message_queue=message_queue,
                    reason=stop_requested.reason,
                )
                return
            stage_manifest = StageArtifactManifest(
                stage_id=stage.stage_id,
                job_id=launch_spec.job_id,
                worker_instance_id=launch_spec.worker_instance_id,
                pipeline_version=launch_spec.pipeline_version,
                completed_at=serialize_utc_datetime(utc_now()),
                artifacts=tuple(stage_result.artifacts),
            )
            manifest_path = write_stage_manifest(
                directory=stage_staging_directory,
                manifest=stage_manifest,
            )
            _send_message(
                message_queue,
                StageReadyToCommitMessage(
                    task_id=launch_spec.task_id,
                    job_id=launch_spec.job_id,
                    worker_instance_id=launch_spec.worker_instance_id,
                    lease_token=launch_spec.lease_token,
                    stage_id=stage.stage_id,
                    staging_directory=str(stage_staging_directory),
                    manifest_path=str(manifest_path),
                ),
            )
            if index + 1 < len(pipeline.stages):
                _wait_for_committed_stage_snapshot(
                    launch_spec=launch_spec,
                    stage_id=stage.stage_id,
                    control_check=_control_check,
                )
            _send_message(
                message_queue,
                StageCompletedMessage(
                    task_id=launch_spec.task_id,
                    job_id=launch_spec.job_id,
                    worker_instance_id=launch_spec.worker_instance_id,
                    lease_token=launch_spec.lease_token,
                    stage_id=stage.stage_id,
                ),
            )
            if stage_result.failure is not None:
                _send_message(
                    message_queue,
                    JobFailedMessage(
                        task_id=launch_spec.task_id,
                        job_id=launch_spec.job_id,
                        worker_instance_id=launch_spec.worker_instance_id,
                        lease_token=launch_spec.lease_token,
                        reason=stage_result.failure.reason,
                        detail=stage_result.failure.detail,
                        error_type="StageFailure",
                        failure_event_name=stage_result.failure.failure_event_name,
                        failure_context=stage_result.failure.failure_context,
                    ),
                )
                return
            _send_message(
                message_queue,
                WorkerHeartbeatMessage(
                    task_id=launch_spec.task_id,
                    job_id=launch_spec.job_id,
                    worker_instance_id=launch_spec.worker_instance_id,
                    lease_token=launch_spec.lease_token,
                ),
            )
            try:
                _control_check()
            except _WorkerControlRequested as stop_requested:
                _send_job_stopped(
                    launch_spec=launch_spec,
                    message_queue=message_queue,
                    reason=stop_requested.reason,
                )
                return

            current_stage_id = None

        try:
            _control_check()
        except _WorkerControlRequested as stop_requested:
            _send_job_stopped(
                launch_spec=launch_spec,
                message_queue=message_queue,
                reason=stop_requested.reason,
            )
            return

        _send_message(
            message_queue,
            JobCompletedMessage(
                task_id=launch_spec.task_id,
                job_id=launch_spec.job_id,
                worker_instance_id=launch_spec.worker_instance_id,
                lease_token=launch_spec.lease_token,
            ),
        )
    except _WorkerControlRequested as stop_requested:
        _send_job_stopped(
            launch_spec=launch_spec,
            message_queue=message_queue,
            reason=stop_requested.reason,
        )
        return
    except Exception as error:
        from .clade_detection_stage import CladeDetectionStageError
        from .comparative_analysis_stage import ComparativeAnalysisStageError
        from .distance_matrix_stage import DistanceMatrixStageError
        from .phylogenetic_tree_stage import PhylogeneticTreeStageError
        from .result_package_stage import ResultPackageStageError

        failure_event_name: str | None = None
        failure_context: dict[str, object] | None = None
        if isinstance(error, InputAcquisitionError):
            reason = error.event_name
            detail = error.detail
            failure_event_name = error.event_name
            failure_context = {"detail": error.detail, **error.context}
            if current_stage_id is not None:
                _send_message(
                    message_queue,
                    StageEventMessage(
                        task_id=launch_spec.task_id,
                        job_id=launch_spec.job_id,
                        worker_instance_id=launch_spec.worker_instance_id,
                        lease_token=launch_spec.lease_token,
                        stage_id=current_stage_id,
                        event_name=error.event_name,
                        context={"detail": error.detail, **error.context},
                    ),
                )
        elif isinstance(error, AlignmentStageError):
            reason = error.reason
            detail = error.detail
            failure_event_name = error.event_name
            failure_context = {"detail": error.detail, **error.context}
            if current_stage_id is not None:
                _send_message(
                    message_queue,
                    StageEventMessage(
                        task_id=launch_spec.task_id,
                        job_id=launch_spec.job_id,
                        worker_instance_id=launch_spec.worker_instance_id,
                        lease_token=launch_spec.lease_token,
                        stage_id=current_stage_id,
                        event_name=error.event_name,
                        context=failure_context,
                    ),
                )
        elif isinstance(error, ComparativeAnalysisStageError):
            reason = error.reason
            detail = error.detail
            failure_event_name = error.event_name
            failure_context = {"detail": error.detail, **error.context}
            if current_stage_id is not None:
                _send_message(
                    message_queue,
                    StageEventMessage(
                        task_id=launch_spec.task_id,
                        job_id=launch_spec.job_id,
                        worker_instance_id=launch_spec.worker_instance_id,
                        lease_token=launch_spec.lease_token,
                        stage_id=current_stage_id,
                        event_name=error.event_name,
                        context=failure_context,
                    ),
                )
        elif isinstance(error, DistanceMatrixStageError):
            reason = error.reason
            detail = error.detail
            failure_event_name = error.event_name
            failure_context = {"detail": error.detail, **error.context}
            if current_stage_id is not None:
                _send_message(
                    message_queue,
                    StageEventMessage(
                        task_id=launch_spec.task_id,
                        job_id=launch_spec.job_id,
                        worker_instance_id=launch_spec.worker_instance_id,
                        lease_token=launch_spec.lease_token,
                        stage_id=current_stage_id,
                        event_name=error.event_name,
                        context=failure_context,
                    ),
                )
        elif isinstance(error, PhylogeneticTreeStageError):
            reason = error.reason
            detail = error.detail
            failure_event_name = error.event_name
            failure_context = {"detail": error.detail, **error.context}
            if current_stage_id is not None:
                _send_message(
                    message_queue,
                    StageEventMessage(
                        task_id=launch_spec.task_id,
                        job_id=launch_spec.job_id,
                        worker_instance_id=launch_spec.worker_instance_id,
                        lease_token=launch_spec.lease_token,
                        stage_id=current_stage_id,
                        event_name=error.event_name,
                        context=failure_context,
                    ),
                )
        elif isinstance(error, CladeDetectionStageError):
            reason = error.reason
            detail = error.detail
            failure_event_name = error.event_name
            failure_context = {"detail": error.detail, **error.context}
            if current_stage_id is not None:
                _send_message(
                    message_queue,
                    StageEventMessage(
                        task_id=launch_spec.task_id,
                        job_id=launch_spec.job_id,
                        worker_instance_id=launch_spec.worker_instance_id,
                        lease_token=launch_spec.lease_token,
                        stage_id=current_stage_id,
                        event_name=error.event_name,
                        context=failure_context,
                    ),
                )
        elif isinstance(error, ResultPackageStageError):
            reason = error.reason
            detail = error.detail
            failure_event_name = error.event_name
            failure_context = {"detail": error.detail, **error.context}
            if current_stage_id is not None:
                _send_message(
                    message_queue,
                    StageEventMessage(
                        task_id=launch_spec.task_id,
                        job_id=launch_spec.job_id,
                        worker_instance_id=launch_spec.worker_instance_id,
                        lease_token=launch_spec.lease_token,
                        stage_id=current_stage_id,
                        event_name=error.event_name,
                        context=failure_context,
                    ),
                )
        elif isinstance(error, StageSnapshotValidationError):
            reason = error.code
            detail = str(error)
            failure_context = {
                "error_code": error.code,
                "stage_id": error.stage_id or "unknown",
            }
            if error.relative_path is not None:
                failure_context["relative_path"] = error.relative_path
        elif current_stage_id == "input_processing":
            reason = "input_processing_failed"
            detail = str(error)
            failure_event_name = INPUT_PROCESSING_FAILED_EVENT
            failure_context = {"detail": detail}
            _send_message(
                message_queue,
                StageEventMessage(
                    task_id=launch_spec.task_id,
                    job_id=launch_spec.job_id,
                    worker_instance_id=launch_spec.worker_instance_id,
                    lease_token=launch_spec.lease_token,
                    stage_id=current_stage_id,
                    event_name=INPUT_PROCESSING_FAILED_EVENT,
                    context={"detail": detail},
                ),
            )
        else:
            reason = "worker_exception"
            detail = str(error)
        _send_message(
            message_queue,
            JobFailedMessage(
                task_id=launch_spec.task_id,
                job_id=launch_spec.job_id,
                worker_instance_id=launch_spec.worker_instance_id,
                lease_token=launch_spec.lease_token,
                reason=reason,
                detail=detail,
                error_type=type(error).__name__,
                failure_event_name=failure_event_name,
                failure_context=failure_context,
            ),
        )


def _send_message(message_queue: Any, message: object) -> None:
    message_queue.put(message)


def _wait_for_committed_stage_snapshot(
    *,
    launch_spec: WorkerLaunchSpec,
    stage_id: str,
    control_check: Callable[[], None],
) -> None:
    committed_stage_root = launch_spec.job_dir / "stages" / stage_id
    staging_manifest_path = (
        launch_spec.job_dir
        / "staging"
        / stage_id
        / launch_spec.worker_instance_id
        / STAGE_MANIFEST_FILENAME
    )
    deadline = time.monotonic() + _STAGE_COMMIT_BARRIER_TIMEOUT_SECONDS
    while True:
        try:
            validate_committed_stage_snapshot(
                job_dir=launch_spec.job_dir,
                stage_id=stage_id,
                expected_job_id=launch_spec.job_id,
                expected_pipeline_version=launch_spec.pipeline_version,
                expected_task_id=launch_spec.task_id,
                expected_config_hash=launch_spec.config_hash,
            )
            return
        except StageSnapshotValidationError as error:
            pending_commit = (
                error.code == StageSnapshotErrorCode.INVALID.value
                and not committed_stage_root.exists()
                and staging_manifest_path.is_file()
            )
            if not pending_commit or time.monotonic() >= deadline:
                raise
            control_check()
            time.sleep(_STAGE_COMMIT_BARRIER_POLL_SECONDS)


def _send_job_stopped(
    *,
    launch_spec: WorkerLaunchSpec,
    message_queue: Any,
    reason: WorkerStopReason,
) -> None:
    _send_message(
        message_queue,
        JobStoppedMessage(
            task_id=launch_spec.task_id,
            job_id=launch_spec.job_id,
            worker_instance_id=launch_spec.worker_instance_id,
            lease_token=launch_spec.lease_token,
            reason=reason,
        ),
    )


def _read_stop_reason(
    *,
    runtime_shutdown_event: Any,
    deletion_requested_event: Any,
    pause_requested_event: Any,
    cancel_requested_event: Any,
    registry_service: AnalyticalTaskRegistryService,
    launch_spec: WorkerLaunchSpec,
) -> WorkerStopReason | None:
    try:
        task_record = registry_service.get_task(task_id=launch_spec.task_id)
    except AnalyticalTaskNotFoundError:
        return WorkerStopReason.DELETION_REQUESTED
    if (
        task_record.state is AnalyticalTaskState.DELETION_REQUESTED
        or deletion_requested_event.is_set()
    ):
        return WorkerStopReason.DELETION_REQUESTED

    try:
        job_record = registry_service.get_job(job_id=launch_spec.job_id)
    except AnalyticalTaskJobNotFoundError:
        return WorkerStopReason.DELETION_REQUESTED
    identity_matches = (
        job_record.worker_instance_id == launch_spec.worker_instance_id
        and job_record.lease_token == launch_spec.lease_token
    )
    if (
        identity_matches and job_record.state is AnalyticalTaskState.CANCEL_REQUESTED
    ) or cancel_requested_event.is_set():
        return WorkerStopReason.CANCEL_REQUESTED
    if identity_matches and job_record.state is AnalyticalTaskState.PAUSE_REQUESTED:
        return WorkerStopReason.PAUSE_REQUESTED
    if identity_matches and job_record.state is AnalyticalTaskState.PREEMPTION_REQUESTED:
        return WorkerStopReason.PREEMPTION_REQUESTED
    if pause_requested_event.is_set():
        return WorkerStopReason.PAUSE_REQUESTED
    if runtime_shutdown_event.is_set():
        return WorkerStopReason.RUNTIME_SHUTDOWN
    return None


def _current_pid() -> int:
    return os.getpid()
