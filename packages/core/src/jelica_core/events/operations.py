from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from time import sleep
from typing import Callable, TypeVar
from uuid import UUID, uuid4

from jelica_contracts import (
    Event,
    EventComponent,
    EventDefinition,
    EventType,
    JSONValue,
    PublicError,
)
from jelica_core.analysis import (
    InitializeAnalysisTaskRequest,
    initialize_analysis_task,
    resolve_analysis_execution_selection,
)
from jelica_core.analysis.errors import AnalysisTaskWorkspaceCompensationError
from jelica_core.config import (
    AnalysisConfigInput,
    ConfigParser,
    apply_config_overrides,
    parse_cli_overrides,
    resolve_analysis_config,
)
from jelica_core.runtime import (
    DEFAULT_BACKGROUND_RUNNER_MODULE,
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RUNTIME_EVENT_JOB_CLAIMED,
    RUNTIME_EVENT_JOB_COMPLETED,
    RUNTIME_EVENT_JOB_FAILED,
    RUNTIME_EVENT_LEASE_EXPIRED,
    RUNTIME_EVENT_PREEMPTED_JOB_RETURNED_TO_WAITING,
    RUNTIME_EVENT_PREEMPTION_REQUESTED,
    RUNTIME_EVENT_PREEMPTION_SELECTED,
    RUNTIME_EVENT_PROCESS_SPAWN_FAILURE,
    RUNTIME_EVENT_RECOVERY_COMPLETED,
    RUNTIME_EVENT_RECOVERY_FAILED,
    RUNTIME_EVENT_RECOVERY_STARTED,
    RUNTIME_EVENT_RUNTIME_INTERRUPTED,
    RUNTIME_EVENT_SCHEDULER_STARTED,
    RUNTIME_EVENT_SCHEDULER_STOPPED,
    RUNTIME_EVENT_STAGE_COMMITTED,
    RUNTIME_EVENT_STAGE_STARTED,
    RUNTIME_EVENT_STALE_MESSAGE_REJECTED,
    RUNTIME_EVENT_WORKER_EXITED,
    RUNTIME_EVENT_WORKER_HEARTBEAT_LOST,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_CANCEL,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_DELETION,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PAUSE,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PREEMPTION,
    RUNTIME_EVENT_WORKER_STARTED,
    ExecutionRuntime,
    RuntimeConfig,
    RuntimeContinueResult,
    TaskConfigSynchronizationError,
    TaskDeleteBatchResult,
    TaskDeleteBatchResultType,
    TaskDeleteItemResult,
    TaskDeleteItemResultType,
    TaskReprioritizeResult,
    TaskResumeResult,
    TaskStartResult,
    TaskUpdateResult,
    TaskWatchResult,
    TaskWatchResultType,
    WorkerPipelineControl,
    launch_background_runtime,
    synchronize_task_config_before_start,
)
from jelica_core.runtime.config_sync import _resolved_config_as_strict_input
from jelica_core.runtime.from_phase import (
    FromPhasePreparationError,
    PreparedJobSeed,
    cleanup_prepared_job_seed,
    prepare_from_phase_seed_for_new_job,
)
from jelica_core.runtime.service import (
    ServiceRuntimeControlMonitor,
    ServiceStateStore,
    initial_service_metadata,
)
from jelica_core.system_config import (
    DEFAULT_DATA_DIRECTORY,
    DEFAULT_DIAGNOSTIC_FIELD_LIMIT,
    DEFAULT_INCLUDE_DIAGNOSTICS,
    DEFAULT_LOG_LEVEL,
    CoreConfigError,
    CoreConfigService,
    ResolvedCoreConfig,
)
from jelica_core.tasks import (
    AnalyticalTaskInvalidRecordDataError,
    AnalyticalTaskJobNotFoundError,
    AnalyticalTaskJobRecord,
    AnalyticalTaskMutationResult,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryError,
    AnalyticalTaskRegistryService,
    AnalyticalTaskSnapshot,
    AnalyticalTaskSortOrder,
    AnalyticalTaskState,
    InitializedAnalysisTask,
    TaskWorkspaceDeleteError,
)
from jelica_core.tasks.storage import (
    compute_config_hash,
    move_task_workspace_to_trash,
    purge_trashed_task_workspace,
    restore_task_workspace_from_trash,
    serialize_config_document,
    write_text_atomically,
)
from jelica_core.tasks.timestamps import utc_now

from .context import CoreExecutionContext
from .definitions import (
    CORE_ALIGNMENT_COMPLETED,
    CORE_ALIGNMENT_MAFFT_AVAILABILITY_CONFIRMED,
    CORE_ALIGNMENT_MAFFT_PROCESS_COMPLETED,
    CORE_ALIGNMENT_MAFFT_PROCESS_FAILED,
    CORE_ALIGNMENT_MAFFT_PROCESS_STARTED,
    CORE_ALIGNMENT_MAFFT_STOPPED_FOR_CANCEL,
    CORE_ALIGNMENT_MAFFT_STOPPED_FOR_PAUSE,
    CORE_ALIGNMENT_MAFFT_STOPPED_FOR_SHUTDOWN,
    CORE_ALIGNMENT_PREALIGNED_VALIDATION_STARTED,
    CORE_ALIGNMENT_RESULT_PUBLISHED,
    CORE_ALIGNMENT_RESULT_VALIDATION_FAILED,
    CORE_ALIGNMENT_SKIPPED,
    CORE_ALIGNMENT_STARTED,
    CORE_ANALYTICAL_JOB_REPRIORITIZE_ALREADY_SATISFIED,
    CORE_ANALYTICAL_JOB_REPRIORITIZE_APPLIED,
    CORE_ANALYTICAL_JOB_REPRIORITIZE_REJECTED,
    CORE_ANALYTICAL_JOB_REPRIORITIZE_REQUESTED,
    CORE_ANALYTICAL_TASK_CANCEL_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_CANCEL_APPLIED,
    CORE_ANALYTICAL_TASK_CANCEL_REJECTED,
    CORE_ANALYTICAL_TASK_CANCEL_REQUESTED,
    CORE_ANALYTICAL_TASK_DELETE_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_DELETE_APPLIED,
    CORE_ANALYTICAL_TASK_DELETE_REJECTED,
    CORE_ANALYTICAL_TASK_DELETE_REQUESTED,
    CORE_ANALYTICAL_TASK_FETCHED,
    CORE_ANALYTICAL_TASK_JOBS_LISTED,
    CORE_ANALYTICAL_TASK_PAUSE_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_PAUSE_APPLIED,
    CORE_ANALYTICAL_TASK_PAUSE_REJECTED,
    CORE_ANALYTICAL_TASK_PAUSE_REQUESTED,
    CORE_ANALYTICAL_TASK_REGISTERED,
    CORE_ANALYTICAL_TASK_RESUME_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_RESUME_APPLIED,
    CORE_ANALYTICAL_TASK_RESUME_REJECTED,
    CORE_ANALYTICAL_TASK_RESUME_REQUESTED,
    CORE_ANALYTICAL_TASK_START_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_START_APPLIED,
    CORE_ANALYTICAL_TASK_START_REJECTED,
    CORE_ANALYTICAL_TASK_START_REQUESTED,
    CORE_ANALYTICAL_TASK_UPDATE_ALREADY_SATISFIED,
    CORE_ANALYTICAL_TASK_UPDATE_APPLIED,
    CORE_ANALYTICAL_TASK_UPDATE_REJECTED,
    CORE_ANALYTICAL_TASK_UPDATE_REQUESTED,
    CORE_ANALYTICAL_TASK_WATCH_COMPLETED,
    CORE_ANALYTICAL_TASK_WATCH_INTERRUPTED,
    CORE_ANALYTICAL_TASK_WATCH_REJECTED,
    CORE_ANALYTICAL_TASK_WATCH_STARTED,
    CORE_ANALYTICAL_TASKS_DELETE_COMPLETED,
    CORE_ANALYTICAL_TASKS_DELETE_PARTIALLY_COMPLETED,
    CORE_ANALYTICAL_TASKS_DELETE_REQUESTED,
    CORE_ANALYTICAL_TASKS_LISTED,
    CORE_ANALYZE_CONFIG_PARSED,
    CORE_ANALYZE_CONFIG_SAVED,
    CORE_ANALYZE_REQUEST_STARTED,
    CORE_ANALYZE_TASK_DIRECTORY_CREATED,
    CORE_ANALYZE_TASK_INITIALIZED,
    CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED,
    CORE_CLADE_DETECTION_COMPLETED,
    CORE_CLADE_DETECTION_FAILED,
    CORE_CLADE_DETECTION_PROGRESS,
    CORE_CLADE_DETECTION_RESULT_PUBLISHED,
    CORE_CLADE_DETECTION_SKIPPED,
    CORE_CLADE_DETECTION_STARTED,
    CORE_COMPARATIVE_ANALYSIS_COMPLETED,
    CORE_COMPARATIVE_ANALYSIS_FAILED,
    CORE_COMPARATIVE_ANALYSIS_OPERATION_FAILED,
    CORE_COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS,
    CORE_COMPARATIVE_ANALYSIS_PHASE_STARTED,
    CORE_COMPARATIVE_ANALYSIS_PROGRESS,
    CORE_COMPARATIVE_ANALYSIS_RESULT_PUBLISHED,
    CORE_COMPARATIVE_ANALYSIS_SKIPPED,
    CORE_COMPARATIVE_ANALYSIS_STARTED,
    CORE_DISTANCE_MATRIX_COMPLETED,
    CORE_DISTANCE_MATRIX_FAILED,
    CORE_DISTANCE_MATRIX_PARTIAL_SUCCESS,
    CORE_DISTANCE_MATRIX_PROGRESS,
    CORE_DISTANCE_MATRIX_RESULT_PUBLISHED,
    CORE_DISTANCE_MATRIX_SKIPPED,
    CORE_DISTANCE_MATRIX_STARTED,
    CORE_INLINE_SEQUENCE_INVALID,
    CORE_INPUT_ACQUISITION_COMPLETED,
    CORE_INPUT_COPY_FAILED,
    CORE_INPUT_DIRECTORY_DEPTH_LIMIT_REACHED,
    CORE_INPUT_DIRECTORY_EMPTY,
    CORE_INPUT_DIRECTORY_NO_SUPPORTED_FILES,
    CORE_INPUT_DUPLICATES_SKIPPED,
    CORE_INPUT_FILE_EMPTY,
    CORE_INPUT_FILE_TYPE_UNSUPPORTED,
    CORE_INPUT_FILE_UNREADABLE,
    CORE_INPUT_NO_DATA_ACQUIRED,
    CORE_INPUT_PATH_NOT_FOUND,
    CORE_INPUT_PROCESSING_COMPLETED,
    CORE_INPUT_PROCESSING_FAILED,
    CORE_INPUT_PROCESSING_FILE_PROCESSED,
    CORE_INPUT_PROCESSING_STARTED,
    CORE_INPUT_PROCESSING_VALIDATION_FAILED,
    CORE_INPUT_SOURCE_UNSUPPORTED,
    CORE_INPUT_SYMLINK_UNSUPPORTED,
    CORE_INPUT_SYMLINKS_SKIPPED,
    CORE_INPUT_UNSUPPORTED_FILES_SKIPPED,
    CORE_LOCAL_NOTIFICATION_DIAGNOSTIC,
    CORE_NCBI_ACCESSION_INVALID,
    CORE_NCBI_PARTIAL_RESPONSE,
    CORE_NCBI_RECORD_NOT_FOUND,
    CORE_NCBI_REQUEST_FAILED,
    CORE_NCBI_REQUEST_TIMEOUT,
    CORE_NCBI_RESPONSE_EMPTY,
    CORE_NCBI_RESPONSE_INVALID,
    CORE_NCBI_URL_UNSUPPORTED,
    CORE_PHYLOGENETIC_TREE_COMPLETED,
    CORE_PHYLOGENETIC_TREE_FAILED,
    CORE_PHYLOGENETIC_TREE_PROGRESS,
    CORE_PHYLOGENETIC_TREE_RESULT_PUBLISHED,
    CORE_PHYLOGENETIC_TREE_SKIPPED,
    CORE_PHYLOGENETIC_TREE_STARTED,
    CORE_RUNTIME_INTERRUPTED,
    CORE_RUNTIME_JOB_CLAIMED,
    CORE_RUNTIME_JOB_COMPLETED,
    CORE_RUNTIME_JOB_FAILED,
    CORE_RUNTIME_LEASE_ACQUIRED,
    CORE_RUNTIME_LEASE_CONFLICT,
    CORE_RUNTIME_LEASE_EXPIRED,
    CORE_RUNTIME_LEASE_RELEASED,
    CORE_RUNTIME_PREEMPTED_JOB_RETURNED_TO_WAITING,
    CORE_RUNTIME_PREEMPTION_REQUESTED,
    CORE_RUNTIME_PREEMPTION_SELECTED,
    CORE_RUNTIME_PROCESS_SPAWN_FAILED,
    CORE_RUNTIME_RECOVERY_COMPLETED,
    CORE_RUNTIME_RECOVERY_FAILED,
    CORE_RUNTIME_RECOVERY_STARTED,
    CORE_RUNTIME_SCHEDULER_STARTED,
    CORE_RUNTIME_SCHEDULER_STOPPED,
    CORE_RUNTIME_STAGE_COMMITTED,
    CORE_RUNTIME_STAGE_STARTED,
    CORE_RUNTIME_STALE_WORKER_MESSAGE_REJECTED,
    CORE_RUNTIME_WORKER_EXITED,
    CORE_RUNTIME_WORKER_HEARTBEAT_LOST,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_CANCEL,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_DELETION,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PAUSE,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION,
    CORE_RUNTIME_WORKER_STARTED,
    CORE_SYSTEM_CONFIG_INITIALIZED,
    CORE_SYSTEM_CONFIG_LOADED,
    CORE_SYSTEM_CONFIG_PATH_RESOLVED,
    CORE_SYSTEM_CONFIG_VALIDATED,
    CORE_SYSTEM_CONFIG_VALUE_SET,
    CORE_SYSTEM_CONFIG_VALUE_UNSET,
    CORE_TASK_REGISTRY_SCHEMA_INITIALIZED,
    CORE_TASK_REGISTRY_SCHEMA_VALIDATED,
)
from .factory import CoreEventFactory
from .results import CoreOperationResult
from .service import EventService, MandatoryEventSinkWriteError
from .sinks import SYSTEM_EVENTS_LOG_FILENAME, TASK_EVENTS_LOG_FILENAME, JsonlFileEventSink
from .structured_errors import CoreTaskLifecycleError
from .translator import CoreExceptionTranslator

T = TypeVar("T")
RuntimeEventListener = Callable[[str, dict[str, JSONValue] | None], None]
DEFAULT_ANALYTICAL_TASKS_LIST_LIMIT = 50
MAX_ANALYTICAL_TASKS_LIST_LIMIT = 500


class CoreOperationRuntime:
    def __init__(
        self,
        *,
        event_service: EventService,
        translator: CoreExceptionTranslator,
        system_log_path: Path | None,
    ) -> None:
        self.event_service = event_service
        self.translator = translator
        self.system_log_path = system_log_path


def run_config_init(
    *,
    data_directory: str | None,
    max_parallel_tasks: int | None = None,
    max_workers: int | None = None,
    log_level: str | None,
    force: bool,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[ResolvedCoreConfig]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="system_config", operation_id="config.init")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.initialize_system_config(
            data_directory=data_directory,
            max_parallel_tasks=max_parallel_tasks,
            max_workers=max_workers,
            log_level=log_level,
            force=force,
        )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    try:
        runtime.event_service.emit(
            CORE_TASK_REGISTRY_SCHEMA_INITIALIZED,
            execution_context=execution_context,
            message_params={"database_path": str(resolved_config.database_path)},
        )
        event = runtime.event_service.emit(
            CORE_SYSTEM_CONFIG_INITIALIZED,
            execution_context=execution_context,
            message_params={"config_path": str(service.get_config_path())},
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return CoreOperationResult.success(
        event=event,
        value=resolved_config,
        system_log_path=runtime.system_log_path,
    )


def run_config_path(
    *,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[Path]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="system_config", operation_id="config.path")
    runtime = _build_runtime(service=service, resolved_config=None)

    try:
        config_path = service.get_config_path()
        event = runtime.event_service.emit(
            CORE_SYSTEM_CONFIG_PATH_RESOLVED,
            execution_context=execution_context,
            message_params={"config_path": str(config_path)},
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return CoreOperationResult.success(
        event=event,
        value=config_path,
        system_log_path=runtime.system_log_path,
    )


def run_config_show(
    *,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[ResolvedCoreConfig]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="system_config", operation_id="config.show")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.load_resolved_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    try:
        event = runtime.event_service.emit(
            CORE_SYSTEM_CONFIG_LOADED,
            execution_context=execution_context,
            message_params={"config_path": str(service.get_config_path())},
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return CoreOperationResult.success(
        event=event,
        value=resolved_config,
        system_log_path=runtime.system_log_path,
    )


def run_config_validate(
    *,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[ResolvedCoreConfig]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="system_config", operation_id="config.validate")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.validate_current_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    try:
        runtime.event_service.emit(
            CORE_TASK_REGISTRY_SCHEMA_VALIDATED,
            execution_context=execution_context,
            message_params={"database_path": str(resolved_config.database_path)},
        )
        event = runtime.event_service.emit(
            CORE_SYSTEM_CONFIG_VALIDATED,
            execution_context=execution_context,
            message_params={"config_path": str(service.get_config_path())},
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return CoreOperationResult.success(
        event=event,
        value=resolved_config,
        system_log_path=runtime.system_log_path,
    )


def run_config_set(
    *,
    parameter: str,
    value: str,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[ResolvedCoreConfig]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="system_config", operation_id="config.set")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.set_parameter(parameter=parameter, value=value)
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    try:
        event = runtime.event_service.emit(
            CORE_SYSTEM_CONFIG_VALUE_SET,
            execution_context=execution_context,
            message_params={"parameter": parameter},
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return CoreOperationResult.success(
        event=event,
        value=resolved_config,
        system_log_path=runtime.system_log_path,
    )


def run_config_unset(
    *,
    parameter: str,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[ResolvedCoreConfig]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="system_config", operation_id="config.unset")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.unset_parameter(parameter=parameter)
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    try:
        event = runtime.event_service.emit(
            CORE_SYSTEM_CONFIG_VALUE_UNSET,
            execution_context=execution_context,
            message_params={"parameter": parameter},
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return CoreOperationResult.success(
        event=event,
        value=resolved_config,
        system_log_path=runtime.system_log_path,
    )


def run_initialize_analysis_task(
    *,
    request: InitializeAnalysisTaskRequest,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[InitializedAnalysisTask]:
    service = core_config_service or CoreConfigService()
    effective_request = (
        request
        if request.trace_id is not None
        else request.model_copy(update={"trace_id": uuid4()})
    )
    execution_context = CoreExecutionContext(
        trace_id=effective_request.trace_id,
        stage="task_initialization",
        operation_id="analyze.initialize",
    )
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        bootstrap_runtime.event_service.emit(
            CORE_ANALYZE_REQUEST_STARTED,
            execution_context=execution_context,
            context={"sources_count": len(effective_request.positional_sources)},
        )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    try:
        runtime.event_service.emit(
            CORE_SYSTEM_CONFIG_LOADED,
            execution_context=execution_context,
            message_params={"config_path": str(service.get_config_path())},
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    initialized_task: InitializedAnalysisTask | None = None
    task_execution_context = execution_context
    task_log_path: Path | None = None
    task_registered = False

    try:
        initialized_task = initialize_analysis_task(
            request=effective_request,
            core_config_service=service,
        )
        initialized_task = _pin_initial_task_config_revision(
            initialized_task=initialized_task,
            resolved_config=resolved_config,
        )
        task_execution_context = execution_context.model_copy(
            update={"task_id": initialized_task.task_id}
        )
        task_log_level = _event_level_from_string(resolved_config.task_log_level)
        task_log_path = initialized_task.task_dir / TASK_EVENTS_LOG_FILENAME
        runtime.event_service.add_sink(
            JsonlFileEventSink(
                path=task_log_path,
                minimum_level=task_log_level,
                required=True,
                task_id=initialized_task.task_id,
            )
        )

        runtime.event_service.emit(
            CORE_ANALYZE_CONFIG_PARSED,
            execution_context=task_execution_context,
        )
        for warning in initialized_task.warnings:
            runtime.event_service.emit(
                CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED,
                execution_context=task_execution_context,
                message_params={"parameter": _extract_warning_parameter(warning)},
                context={"warning": warning},
            )
        runtime.event_service.emit(
            CORE_ANALYZE_TASK_DIRECTORY_CREATED,
            execution_context=task_execution_context,
            message_params={"task_dir": str(initialized_task.task_dir)},
        )
        runtime.event_service.emit(
            CORE_ANALYZE_CONFIG_SAVED,
            execution_context=task_execution_context,
            message_params={"config_path": str(initialized_task.config_path)},
        )
        task_dir_relative_path = _resolve_task_dir_relative_path(
            task_dir=initialized_task.task_dir,
            tasks_dir=resolved_config.tasks_dir,
            task_id=initialized_task.task_id,
        )
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        registered_record = registry_service.register_task(
            task_id=initialized_task.task_id,
            name=effective_request.name,
            automatic_name_base=(initialized_task.name if effective_request.name is None else None),
            task_dir_relative_path=task_dir_relative_path,
            default_priority=initialized_task.config.priority,
            current_config_revision=initialized_task.current_config_revision,
            current_config_relative_path=initialized_task.current_config_relative_path,
            current_config_hash=initialized_task.current_config_hash,
        )
        initialized_task = initialized_task.model_copy(update={"name": registered_record.name})
        task_registered = True
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_REGISTERED,
            execution_context=task_execution_context,
            message_params={"task_id": initialized_task.task_id},
            context={
                "name": registered_record.name,
                "state": registered_record.state.value,
                "default_priority": registered_record.default_priority,
                "task_dir_relative_path": registered_record.task_dir_relative_path,
                "record_version": registered_record.record_version,
            },
        )
        event = runtime.event_service.emit(
            CORE_ANALYZE_TASK_INITIALIZED,
            execution_context=task_execution_context,
            message_params={"task_id": initialized_task.task_id},
        )
    except Exception as error:
        if initialized_task is not None and not task_registered:
            failure_runtime = _build_runtime(service=service, resolved_config=resolved_config)
            compensation_error = _try_compensate_unregistered_task_workspace(
                task=initialized_task,
                tasks_dir=resolved_config.tasks_dir,
                original_error=error,
            )
            if compensation_error is not None:
                return _failure_result(
                    error=compensation_error,
                    runtime=failure_runtime,
                    execution_context=task_execution_context,
                    task_log_path=task_log_path,
                )
            return _failure_result(
                error=error,
                runtime=failure_runtime,
                execution_context=task_execution_context,
            )
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=task_execution_context,
            task_log_path=task_log_path,
        )

    return CoreOperationResult.success(
        event=event,
        value=initialized_task,
        system_log_path=runtime.system_log_path,
        task_log_path=task_log_path,
    )


def run_create_analytical_task(
    *,
    request: InitializeAnalysisTaskRequest,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[InitializedAnalysisTask]:
    return run_initialize_analysis_task(
        request=request,
        core_config_service=core_config_service,
    )


def run_initialize_analysis_task_from_inputs(
    *,
    name: str | None = None,
    trace_id: UUID | str | None = None,
    config_json: str | None,
    raw_overrides: tuple[str, ...],
    positional_sources: tuple[str, ...],
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[InitializedAnalysisTask]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(
        stage="task_initialization",
        operation_id="analyze.initialize",
    )
    runtime = _build_runtime(service=service, resolved_config=None)

    try:
        parsed_overrides = parse_cli_overrides(raw_overrides)
        normalized_trace_id = UUID(trace_id) if isinstance(trace_id, str) else trace_id
        request = InitializeAnalysisTaskRequest(
            name=name,
            trace_id=normalized_trace_id,
            config_json=config_json,
            overrides=tuple(parsed_overrides),
            positional_sources=positional_sources,
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return run_initialize_analysis_task(
        request=request,
        core_config_service=service,
    )


def run_create_analytical_task_from_inputs(
    *,
    name: str | None = None,
    trace_id: UUID | str | None = None,
    config_json: str | None,
    raw_overrides: tuple[str, ...],
    positional_sources: tuple[str, ...],
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[InitializedAnalysisTask]:
    return run_initialize_analysis_task_from_inputs(
        name=name,
        trace_id=trace_id,
        config_json=config_json,
        raw_overrides=raw_overrides,
        positional_sources=positional_sources,
        core_config_service=core_config_service,
    )


def run_list_analytical_tasks(
    *,
    states: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[list[AnalyticalTaskSnapshot]]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_registry", operation_id="tasks.list")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    try:
        parsed_states = _parse_task_states(states)
        effective_limit = _normalize_tasks_list_limit(limit)
        effective_offset = _normalize_tasks_list_offset(offset)
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        tasks = registry_service.list_task_snapshots(
            states=parsed_states,
            limit=effective_limit,
            offset=effective_offset,
            order=AnalyticalTaskSortOrder.UPDATED_AT_DESC,
        )
        event = runtime.event_service.emit(
            CORE_ANALYTICAL_TASKS_LISTED,
            execution_context=execution_context,
            message_params={"count": len(tasks)},
            context={
                "states": [state.value for state in parsed_states] if parsed_states else [],
                "limit": effective_limit,
                "offset": effective_offset,
            },
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return CoreOperationResult.success(
        event=event,
        value=tasks,
        system_log_path=runtime.system_log_path,
    )


def run_get_analytical_task(
    *,
    task_id: str,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[AnalyticalTaskSnapshot]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_registry", operation_id="tasks.show")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")

        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        task_snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=task_snapshot.task.task_id,
        )
        failure_execution_context = task_execution_context
        event = runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_FETCHED,
            execution_context=task_execution_context,
            message_params={"task_id": task_snapshot.task.task_id},
            context={
                "state": task_snapshot.task.state.value,
                "record_version": task_snapshot.task.record_version,
                "active_job_id": task_snapshot.task.active_job_id,
                "latest_job_id": task_snapshot.task.latest_job_id,
            },
        )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )

    return CoreOperationResult.success(
        event=event,
        value=task_snapshot,
        system_log_path=runtime.system_log_path,
    )


def run_list_analytical_task_jobs(
    *,
    task_id: str,
    limit: int | None = None,
    offset: int = 0,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[list[AnalyticalTaskJobRecord]]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_registry", operation_id="tasks.jobs")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")
        effective_limit = _normalize_tasks_list_limit(limit)
        effective_offset = _normalize_tasks_list_offset(offset)
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        jobs = registry_service.list_task_jobs(
            task_id=normalized_task_id,
            limit=effective_limit,
            offset=effective_offset,
        )
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=normalized_task_id,
        )
        failure_execution_context = task_execution_context
        event = runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_JOBS_LISTED,
            execution_context=task_execution_context,
            message_params={"task_id": normalized_task_id, "count": len(jobs)},
            context={
                "limit": effective_limit,
                "offset": effective_offset,
            },
        )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )

    return CoreOperationResult.success(
        event=event,
        value=jobs,
        system_log_path=runtime.system_log_path,
    )


def run_start_analytical_task(
    *,
    task_id: str,
    priority: int | None = None,
    core_config_service: CoreConfigService | None = None,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
    runtime_event_listener: RuntimeEventListener | None = None,
    detached: bool = False,
    background_runner_module: str = DEFAULT_BACKGROUND_RUNNER_MODULE,
) -> CoreOperationResult[TaskStartResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_runtime", operation_id="tasks.start")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    task_log_path: Path | None = None
    failure_execution_context = execution_context
    prepared_job_seed: PreparedJobSeed | None = None
    start_mutation_attempted = False
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")
        if priority is not None and priority < 1:
            raise AnalyticalTaskInvalidRecordDataError(detail="priority must be >= 1")

        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        task_snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=normalized_task_id,
        )
        failure_execution_context = task_execution_context
        task_log_path = _attach_task_log_sink(
            runtime=runtime,
            resolved_config=resolved_config,
            task_id=task_snapshot.task.task_id,
            task_dir_relative_path=task_snapshot.task.task_dir_relative_path,
        )
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_START_REQUESTED,
            execution_context=task_execution_context,
            message_params={"task_id": normalized_task_id},
        )

        try:
            synchronize_task_config_before_start(
                task_snapshot=task_snapshot,
                tasks_dir=resolved_config.tasks_dir,
                registry_service=registry_service,
                input_directory_max_depth=resolved_config.input_directory_max_depth,
                ncbi_max_retries=resolved_config.ncbi_max_retries,
                default_alignment_mode=resolved_config.default_alignment_mode,
            )
        except TaskConfigSynchronizationError as error:
            raise CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_TASK_START_REJECTED,
                message=f"Cannot start analytical task '{normalized_task_id}': {error}",
                message_params={"task_id": normalized_task_id, "detail": str(error)},
                expected=True,
                retryable=False,
                can_continue=True,
            ) from error

        task_snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
        if _task_start_creates_new_job(task_snapshot):
            requested_job_id = str(uuid4())
            try:
                prepared_job_seed = prepare_from_phase_seed_for_new_job(
                    task_id=normalized_task_id,
                    task_dir=(
                        resolved_config.tasks_dir / task_snapshot.task.task_dir_relative_path
                    ),
                    config_relative_path=task_snapshot.task.current_config_relative_path,
                    config_hash=task_snapshot.task.current_config_hash,
                    requested_job_id=requested_job_id,
                    pipeline_name=pipeline_name,
                    pipeline_version=pipeline_version,
                )
            except FromPhasePreparationError as error:
                raise CoreTaskLifecycleError(
                    definition=CORE_ANALYTICAL_TASK_START_REJECTED,
                    message=f"Cannot start analytical task '{normalized_task_id}': {error}",
                    message_params={"task_id": normalized_task_id, "detail": str(error)},
                    expected=True,
                    retryable=False,
                    can_continue=True,
                ) from error

        start_mutation_attempted = True
        start_result = registry_service.start(
            task_id=normalized_task_id,
            priority=priority,
            requested_job_id=(
                prepared_job_seed.requested_job_id if prepared_job_seed is not None else None
            ),
            runtime_state=(
                prepared_job_seed.runtime_state if prepared_job_seed is not None else None
            ),
        )
        if prepared_job_seed is not None and (
            start_result.job is None
            or start_result.job.job_id != prepared_job_seed.requested_job_id
        ):
            _cleanup_prepared_job_seed(prepared_job_seed=prepared_job_seed)
        if start_result.result_type not in {
            AnalyticalTaskMutationResultType.APPLIED,
            AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
        }:
            raise CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_TASK_START_REJECTED,
                message=(
                    f"Cannot start analytical task '{normalized_task_id}': "
                    f"{_format_mutation_rejection_detail(start_result)}"
                ),
                message_params={
                    "task_id": normalized_task_id,
                    "detail": _format_mutation_rejection_detail(start_result),
                },
                expected=True,
                retryable=False,
                can_continue=True,
            )

        if start_result.task is None or start_result.job is None:
            raise RuntimeError("start operation succeeded without task/job payload")

        if start_result.result_type is AnalyticalTaskMutationResultType.APPLIED:
            lifecycle_event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_START_APPLIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": normalized_task_id,
                    "job_id": start_result.job.job_id,
                    "state": start_result.job.state.value,
                },
            )
        else:
            lifecycle_event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_START_ALREADY_SATISFIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": normalized_task_id,
                    "job_id": start_result.job.job_id,
                },
            )

        if detached:
            existing_lease = registry_service.get_execution_runtime_lease()
            if existing_lease is not None and existing_lease.lease_expires_at > utc_now():
                used_existing_runtime = True
                runtime_instance_id = existing_lease.runtime_instance_id
            else:
                used_existing_runtime = False
                runtime_instance_id = None
                launch_background_runtime(
                    jelica_home=service.get_jelica_home(),
                    runner_module=background_runner_module,
                )
            return CoreOperationResult.success(
                event=lifecycle_event,
                value=TaskStartResult(
                    task=registry_service.get_task(task_id=normalized_task_id),
                    job=registry_service.get_job(job_id=start_result.job.job_id),
                    runtime_instance_id=runtime_instance_id,
                    used_existing_runtime=used_existing_runtime,
                ),
                system_log_path=runtime.system_log_path,
                task_log_path=task_log_path,
            )

        runtime_instance_id = str(uuid4())
        runtime_lease_token = str(uuid4())
        acquired_lease, conflicting_lease = registry_service.acquire_execution_runtime_lease(
            runtime_instance_id=runtime_instance_id,
            owner_pid=os.getpid(),
            lease_token=runtime_lease_token,
            lease_timeout_seconds=resolved_config.lease_timeout_seconds,
        )

        used_existing_runtime = False
        structured_job_failures: dict[str, tuple[str, dict[str, JSONValue]]] = {}
        if acquired_lease is not None:
            runtime.event_service.emit(
                CORE_RUNTIME_LEASE_ACQUIRED,
                execution_context=execution_context,
                message_params={"runtime_instance_id": runtime_instance_id},
            )
            runtime_event_callback = _build_runtime_event_callback(
                runtime=runtime,
                execution_context=execution_context,
                resolved_config=resolved_config,
                registry_service=registry_service,
                job_failure_events=structured_job_failures,
                preattached_task_ids={task_snapshot.task.task_id},
                runtime_event_listener=runtime_event_listener,
            )
            execution_runtime = ExecutionRuntime(
                registry_service=registry_service,
                tasks_dir=resolved_config.tasks_dir,
                runtime_config=RuntimeConfig.from_resolved_config(resolved_config),
                runtime_instance_id=runtime_instance_id,
                runtime_lease_token=runtime_lease_token,
                pipeline_name=pipeline_name,
                pipeline_version=pipeline_version,
                event_callback=runtime_event_callback,
            )
            try:
                execution_runtime.run(auto_queue_waiting_jobs=False)
            except RuntimeError as error:
                if "runtime lease was lost" in str(error):
                    raise CoreTaskLifecycleError(
                        definition=CORE_RUNTIME_LEASE_EXPIRED,
                        message=(
                            "Runtime lease expired before the target job reached terminal state. "
                            "Start JELICA Service with 'jelica service start' and retry."
                        ),
                        message_params={"runtime_instance_id": runtime_instance_id},
                        expected=True,
                        retryable=True,
                        can_continue=True,
                    ) from error
                raise
            finally:
                runtime.event_service.emit(
                    CORE_RUNTIME_LEASE_RELEASED,
                    execution_context=execution_context,
                    message_params={"runtime_instance_id": runtime_instance_id},
                )
        else:
            used_existing_runtime = True
            conflict_runtime_id = (
                conflicting_lease.runtime_instance_id
                if conflicting_lease is not None
                else "<unknown>"
            )
            runtime.event_service.emit(
                CORE_RUNTIME_LEASE_CONFLICT,
                execution_context=execution_context,
                message_params={"runtime_instance_id": conflict_runtime_id},
            )
            _watch_job_until_terminal(
                registry_service=registry_service,
                job_id=start_result.job.job_id,
                poll_interval_seconds=resolved_config.scheduler_poll_interval_seconds,
                runtime_instance_id=conflict_runtime_id,
            )

        final_task = registry_service.get_task(task_id=normalized_task_id)
        final_job = registry_service.get_job(job_id=start_result.job.job_id)
        if final_job.state is AnalyticalTaskState.COMPLETED:
            return CoreOperationResult.success(
                event=lifecycle_event,
                value=TaskStartResult(
                    task=final_task,
                    job=final_job,
                    runtime_instance_id=runtime_instance_id if not used_existing_runtime else None,
                    used_existing_runtime=used_existing_runtime,
                ),
                system_log_path=runtime.system_log_path,
                task_log_path=task_log_path,
            )

        if final_job.state is AnalyticalTaskState.FAILED:
            finished_reason = final_job.finished_reason or "unknown runtime error"
            structured_failure = structured_job_failures.get(final_job.job_id)
            if structured_failure is not None:
                failure_event_name, failure_context = structured_failure
                input_failure_definition = _FAILED_JOB_REASON_DEFINITIONS.get(failure_event_name)
                if input_failure_definition is not None:
                    failure_detail = _context_text(failure_context, "detail")
                    raise CoreTaskLifecycleError(
                        definition=input_failure_definition,
                        message=failure_detail,
                        message_params={"detail": failure_detail},
                        expected=True,
                        retryable=False,
                        can_continue=True,
                        safe_details={
                            "task_id": final_task.task_id,
                            "job_id": final_job.job_id,
                            "state": final_job.state.value,
                        },
                    )
                structured_detail = _context_text(failure_context, "detail")
                if structured_detail != "<unknown>":
                    finished_reason = structured_detail
            raise CoreTaskLifecycleError(
                definition=CORE_RUNTIME_JOB_FAILED,
                message=(
                    f"Job '{final_job.job_id}' for analytical task '{final_task.task_id}' failed: "
                    f"{finished_reason}"
                ),
                message_params={
                    "task_id": final_task.task_id,
                    "job_id": final_job.job_id,
                    "detail": finished_reason,
                },
                expected=True,
                retryable=False,
                can_continue=True,
                safe_details={
                    "task_id": final_task.task_id,
                    "job_id": final_job.job_id,
                    "state": final_job.state.value,
                },
            )
        raise CoreTaskLifecycleError(
            definition=CORE_ANALYTICAL_TASK_START_REJECTED,
            message=(
                f"Runtime stopped before job '{final_job.job_id}' reached terminal state "
                f"(current state: {final_job.state.value})."
            ),
            message_params={
                "task_id": final_task.task_id,
                "detail": (
                    "runtime stopped before terminal state; start JELICA Service with "
                    "'jelica service start' and retry"
                ),
            },
            expected=True,
            retryable=True,
            can_continue=True,
        )
    except Exception as error:
        if prepared_job_seed is not None and not start_mutation_attempted:
            _cleanup_prepared_job_seed(prepared_job_seed=prepared_job_seed)
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
            task_log_path=task_log_path,
        )


def run_pause_analytical_task(
    *,
    task_id: str,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[AnalyticalTaskMutationResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_runtime", operation_id="tasks.pause")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")

        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=normalized_task_id,
        )
        failure_execution_context = task_execution_context
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_PAUSE_REQUESTED,
            execution_context=task_execution_context,
            message_params={"task_id": normalized_task_id},
        )
        mutation = registry_service.pause(task_id=normalized_task_id)
        if mutation.result_type not in {
            AnalyticalTaskMutationResultType.APPLIED,
            AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
        }:
            raise CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_TASK_PAUSE_REJECTED,
                message=(
                    f"Cannot pause analytical task '{normalized_task_id}': "
                    f"{_format_mutation_rejection_detail(mutation)}"
                ),
                message_params={
                    "task_id": normalized_task_id,
                    "detail": _format_mutation_rejection_detail(mutation),
                },
                expected=True,
                retryable=False,
                can_continue=True,
            )
        if mutation.task is None or mutation.job is None:
            raise RuntimeError("pause operation succeeded without task/job payload")

        if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
            event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_PAUSE_APPLIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": mutation.task.task_id,
                    "job_id": mutation.job.job_id,
                    "state": mutation.job.state.value,
                },
            )
        else:
            event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_PAUSE_ALREADY_SATISFIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": mutation.task.task_id,
                    "job_id": mutation.job.job_id,
                },
            )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )

    return CoreOperationResult.success(
        event=event,
        value=mutation,
        system_log_path=runtime.system_log_path,
    )


def run_resume_analytical_task(
    *,
    task_id: str,
    core_config_service: CoreConfigService | None = None,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
    runtime_event_listener: RuntimeEventListener | None = None,
    detached: bool = False,
    background_runner_module: str = DEFAULT_BACKGROUND_RUNNER_MODULE,
) -> CoreOperationResult[TaskResumeResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_runtime", operation_id="tasks.resume")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")

        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=normalized_task_id,
        )
        failure_execution_context = task_execution_context
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_RESUME_REQUESTED,
            execution_context=task_execution_context,
            message_params={"task_id": normalized_task_id},
        )
        resume_result = registry_service.resume(task_id=normalized_task_id)
        if resume_result.result_type not in {
            AnalyticalTaskMutationResultType.APPLIED,
            AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
        }:
            raise CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_TASK_RESUME_REJECTED,
                message=(
                    f"Cannot resume analytical task '{normalized_task_id}': "
                    f"{_format_mutation_rejection_detail(resume_result)}"
                ),
                message_params={
                    "task_id": normalized_task_id,
                    "detail": _format_mutation_rejection_detail(resume_result),
                },
                expected=True,
                retryable=False,
                can_continue=True,
            )
        if resume_result.task is None or resume_result.job is None:
            raise RuntimeError("resume operation succeeded without task/job payload")

        if resume_result.result_type is AnalyticalTaskMutationResultType.APPLIED:
            lifecycle_event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_RESUME_APPLIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": resume_result.task.task_id,
                    "job_id": resume_result.job.job_id,
                    "state": resume_result.job.state.value,
                },
            )
        else:
            lifecycle_event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_RESUME_ALREADY_SATISFIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": resume_result.task.task_id,
                    "job_id": resume_result.job.job_id,
                },
            )

        if detached:
            existing_lease = registry_service.get_execution_runtime_lease()
            if existing_lease is not None and existing_lease.lease_expires_at > utc_now():
                used_existing_runtime = True
                runtime_instance_id = existing_lease.runtime_instance_id
            else:
                used_existing_runtime = False
                runtime_instance_id = None
                launch_background_runtime(
                    jelica_home=service.get_jelica_home(),
                    runner_module=background_runner_module,
                )
            return CoreOperationResult.success(
                event=lifecycle_event,
                value=TaskResumeResult(
                    task=registry_service.get_task(task_id=normalized_task_id),
                    job=registry_service.get_job(job_id=resume_result.job.job_id),
                    result=resume_result.result_type,
                    runtime_instance_id=runtime_instance_id,
                    used_existing_runtime=used_existing_runtime,
                ),
                system_log_path=runtime.system_log_path,
            )

        runtime_instance_id = str(uuid4())
        runtime_lease_token = str(uuid4())
        acquired_lease, conflicting_lease = registry_service.acquire_execution_runtime_lease(
            runtime_instance_id=runtime_instance_id,
            owner_pid=os.getpid(),
            lease_token=runtime_lease_token,
            lease_timeout_seconds=resolved_config.lease_timeout_seconds,
        )

        used_existing_runtime = False
        structured_job_failures: dict[str, tuple[str, dict[str, JSONValue]]] = {}
        if acquired_lease is not None:
            runtime.event_service.emit(
                CORE_RUNTIME_LEASE_ACQUIRED,
                execution_context=execution_context,
                message_params={"runtime_instance_id": runtime_instance_id},
            )
            runtime_event_callback = _build_runtime_event_callback(
                runtime=runtime,
                execution_context=execution_context,
                resolved_config=resolved_config,
                registry_service=registry_service,
                job_failure_events=structured_job_failures,
                runtime_event_listener=runtime_event_listener,
            )
            execution_runtime = ExecutionRuntime(
                registry_service=registry_service,
                tasks_dir=resolved_config.tasks_dir,
                runtime_config=RuntimeConfig.from_resolved_config(resolved_config),
                runtime_instance_id=runtime_instance_id,
                runtime_lease_token=runtime_lease_token,
                pipeline_name=pipeline_name,
                pipeline_version=pipeline_version,
                event_callback=runtime_event_callback,
            )
            try:
                execution_runtime.run(auto_queue_waiting_jobs=False)
            except RuntimeError as error:
                if "runtime lease was lost" in str(error):
                    raise CoreTaskLifecycleError(
                        definition=CORE_RUNTIME_LEASE_EXPIRED,
                        message=(
                            "Runtime lease expired before the target job reached terminal state. "
                            "Start JELICA Service with 'jelica service start' and retry."
                        ),
                        message_params={"runtime_instance_id": runtime_instance_id},
                        expected=True,
                        retryable=True,
                        can_continue=True,
                    ) from error
                raise
            finally:
                runtime.event_service.emit(
                    CORE_RUNTIME_LEASE_RELEASED,
                    execution_context=execution_context,
                    message_params={"runtime_instance_id": runtime_instance_id},
                )
        else:
            used_existing_runtime = True
            conflict_runtime_id = (
                conflicting_lease.runtime_instance_id
                if conflicting_lease is not None
                else "<unknown>"
            )
            runtime.event_service.emit(
                CORE_RUNTIME_LEASE_CONFLICT,
                execution_context=execution_context,
                message_params={"runtime_instance_id": conflict_runtime_id},
            )
            _watch_job_until_terminal(
                registry_service=registry_service,
                job_id=resume_result.job.job_id,
                poll_interval_seconds=resolved_config.scheduler_poll_interval_seconds,
                runtime_instance_id=conflict_runtime_id,
            )

        final_task = registry_service.get_task(task_id=normalized_task_id)
        final_job = registry_service.get_job(job_id=resume_result.job.job_id)
        if final_job.state is AnalyticalTaskState.COMPLETED:
            return CoreOperationResult.success(
                event=lifecycle_event,
                value=TaskResumeResult(
                    task=final_task,
                    job=final_job,
                    result=resume_result.result_type,
                    runtime_instance_id=runtime_instance_id if not used_existing_runtime else None,
                    used_existing_runtime=used_existing_runtime,
                ),
                system_log_path=runtime.system_log_path,
            )

        if final_job.state is AnalyticalTaskState.FAILED:
            structured_failure = structured_job_failures.get(final_job.job_id)
            finished_reason = final_job.finished_reason or "unknown runtime error"
            if structured_failure is not None:
                failure_event_name, failure_context = structured_failure
                input_failure_definition = _FAILED_JOB_REASON_DEFINITIONS.get(failure_event_name)
                if input_failure_definition is not None:
                    failure_detail = _context_text(failure_context, "detail")
                    raise CoreTaskLifecycleError(
                        definition=input_failure_definition,
                        message=failure_detail,
                        message_params={"detail": failure_detail},
                        expected=True,
                        retryable=False,
                        can_continue=True,
                        safe_details={
                            "task_id": final_task.task_id,
                            "job_id": final_job.job_id,
                            "state": final_job.state.value,
                        },
                    )
                structured_detail = _context_text(failure_context, "detail")
                if structured_detail != "<unknown>":
                    finished_reason = structured_detail
            raise CoreTaskLifecycleError(
                definition=CORE_RUNTIME_JOB_FAILED,
                message=(
                    f"Job '{final_job.job_id}' for analytical task '{final_task.task_id}' failed: "
                    f"{finished_reason}"
                ),
                message_params={
                    "task_id": final_task.task_id,
                    "job_id": final_job.job_id,
                    "detail": finished_reason,
                },
                expected=True,
                retryable=False,
                can_continue=True,
                safe_details={
                    "task_id": final_task.task_id,
                    "job_id": final_job.job_id,
                    "state": final_job.state.value,
                },
            )

        raise CoreTaskLifecycleError(
            definition=CORE_ANALYTICAL_TASK_RESUME_REJECTED,
            message=(
                f"Runtime stopped before resumed job '{final_job.job_id}' reached terminal state "
                f"(current state: {final_job.state.value})."
            ),
            message_params={
                "task_id": final_task.task_id,
                "detail": (
                    "runtime stopped before terminal state; start JELICA Service with "
                    "'jelica service start' and retry"
                ),
            },
            expected=True,
            retryable=True,
            can_continue=True,
        )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )


def run_cancel_analytical_task(
    *,
    task_id: str,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[AnalyticalTaskMutationResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_runtime", operation_id="tasks.cancel")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")

        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=normalized_task_id,
        )
        failure_execution_context = task_execution_context
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_CANCEL_REQUESTED,
            execution_context=task_execution_context,
            message_params={"task_id": normalized_task_id},
        )
        mutation = registry_service.cancel(task_id=normalized_task_id)
        if mutation.result_type not in {
            AnalyticalTaskMutationResultType.APPLIED,
            AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
        }:
            raise CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_TASK_CANCEL_REJECTED,
                message=(
                    f"Cannot cancel analytical task '{normalized_task_id}': "
                    f"{_format_mutation_rejection_detail(mutation)}"
                ),
                message_params={
                    "task_id": normalized_task_id,
                    "detail": _format_mutation_rejection_detail(mutation),
                },
                expected=True,
                retryable=False,
                can_continue=True,
            )
        if mutation.task is None or mutation.job is None:
            raise RuntimeError("cancel operation succeeded without task/job payload")

        if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
            event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_CANCEL_APPLIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": mutation.task.task_id,
                    "job_id": mutation.job.job_id,
                    "state": mutation.job.state.value,
                },
            )
        else:
            event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_CANCEL_ALREADY_SATISFIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": mutation.task.task_id,
                    "job_id": mutation.job.job_id,
                },
            )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )

    return CoreOperationResult.success(
        event=event,
        value=mutation,
        system_log_path=runtime.system_log_path,
    )


def run_delete_analytical_tasks(
    *,
    task_ids: Sequence[str],
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[TaskDeleteBatchResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_runtime", operation_id="tasks.delete")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    try:
        normalized_task_ids = _deduplicate_task_ids(task_ids=task_ids)
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASKS_DELETE_REQUESTED,
            execution_context=execution_context,
            message_params={"count": len(normalized_task_ids)},
            context={
                "requested_count": len(task_ids),
                "unique_count": len(normalized_task_ids),
            },
        )
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )

        item_results: list[TaskDeleteItemResult] = []
        for requested_task_id in normalized_task_ids:
            try:
                item_trace_id = registry_service.get_task_trace_id(task_id=requested_task_id)
            except (AnalyticalTaskRegistryError, TaskWorkspaceDeleteError):
                item_trace_id = None
            item_result = _delete_analytical_task_item(
                registry_service=registry_service,
                tasks_dir=resolved_config.tasks_dir,
                task_id=requested_task_id,
            )
            item_results.append(item_result)
            _emit_task_delete_item_event(
                runtime=runtime,
                execution_context=execution_context,
                item_result=item_result,
                trace_id=item_trace_id,
            )

        applied_count = sum(
            1
            for item in item_results
            if item.result
            in {
                TaskDeleteItemResultType.DELETED,
                TaskDeleteItemResultType.DELETION_REQUESTED,
                TaskDeleteItemResultType.ALREADY_SATISFIED,
            }
        )
        rejected_count = len(item_results) - applied_count
        if rejected_count == 0:
            overall_result = TaskDeleteBatchResultType.APPLIED
            event_definition = CORE_ANALYTICAL_TASKS_DELETE_COMPLETED
        elif applied_count == 0:
            overall_result = TaskDeleteBatchResultType.REJECTED
            event_definition = CORE_ANALYTICAL_TASKS_DELETE_PARTIALLY_COMPLETED
        else:
            overall_result = TaskDeleteBatchResultType.PARTIALLY_APPLIED
            event_definition = CORE_ANALYTICAL_TASKS_DELETE_PARTIALLY_COMPLETED

        event = runtime.event_service.emit(
            event_definition,
            execution_context=execution_context,
            message_params={"applied": applied_count, "rejected": rejected_count},
            context={
                "requested_count": len(task_ids),
                "unique_count": len(normalized_task_ids),
                "applied_count": applied_count,
                "rejected_count": rejected_count,
                "result": overall_result.value,
            },
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return CoreOperationResult.success(
        event=event,
        value=TaskDeleteBatchResult(
            result=overall_result,
            items=tuple(item_results),
            requested_count=len(task_ids),
            unique_count=len(normalized_task_ids),
        ),
        system_log_path=runtime.system_log_path,
    )


def run_watch_analytical_task(
    *,
    task_id: str,
    watch_update_callback: Callable[[AnalyticalTaskJobRecord], None] | None = None,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[TaskWatchResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_runtime", operation_id="tasks.watch")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")

        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=normalized_task_id,
        )
        failure_execution_context = task_execution_context
        watched_job = snapshot.active_or_latest_job
        if watched_job is None:
            raise CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_TASK_WATCH_REJECTED,
                message=(
                    f"Cannot watch analytical task '{normalized_task_id}': "
                    "task does not have any job yet."
                ),
                message_params={
                    "task_id": normalized_task_id,
                    "detail": "task does not have any job yet",
                },
                expected=True,
                retryable=False,
                can_continue=True,
                safe_details={"task_id": normalized_task_id},
            )

        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_WATCH_STARTED,
            execution_context=task_execution_context,
            message_params={"task_id": normalized_task_id, "job_id": watched_job.job_id},
        )
        watch_result = _watch_task_until_terminal(
            registry_service=registry_service,
            task_id=normalized_task_id,
            job_id=watched_job.job_id,
            poll_interval_seconds=resolved_config.scheduler_poll_interval_seconds,
            watch_update_callback=watch_update_callback,
        )
        event = runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_WATCH_COMPLETED,
            execution_context=task_execution_context,
            message_params={
                "task_id": watch_result.task_id,
                "job_id": watch_result.job_id,
                "result": watch_result.result.value,
            },
            context={
                "state": watch_result.state.value,
                "result": watch_result.result.value,
                "progress": watch_result.progress,
                "current_stage": watch_result.current_stage,
            },
        )
    except KeyboardInterrupt:
        return _failure_result(
            error=CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_TASK_WATCH_INTERRUPTED,
                message=(
                    f"Task watch interrupted for analytical task '{task_id.strip() or task_id}'."
                ),
                message_params={"task_id": task_id.strip() or task_id},
                expected=True,
                retryable=True,
                can_continue=True,
                safe_details={"task_id": task_id.strip() or task_id},
            ),
            runtime=runtime,
            execution_context=failure_execution_context,
        )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )

    return CoreOperationResult.success(
        event=event,
        value=watch_result,
        system_log_path=runtime.system_log_path,
    )


def run_update_analytical_task(
    *,
    task_id: str,
    config_json: str | None = None,
    raw_overrides: tuple[str, ...] = (),
    core_config_service: CoreConfigService | None = None,
    operation_id: str = "tasks.update",
    operation_context: dict[str, JSONValue] | None = None,
) -> CoreOperationResult[TaskUpdateResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_registry", operation_id=operation_id)
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    task_log_path: Path | None = None
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")

        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        task_snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=task_snapshot.task.task_id,
        )
        failure_execution_context = task_execution_context
        current_config_document = _load_task_config_revision_document(
            task_snapshot=task_snapshot,
            tasks_dir=resolved_config.tasks_dir,
        )
        task_log_path = _attach_task_log_sink(
            runtime=runtime,
            resolved_config=resolved_config,
            task_id=task_snapshot.task.task_id,
            task_dir_relative_path=task_snapshot.task.task_dir_relative_path,
        )
        requested_context: dict[str, JSONValue] = {
            "uses_external_config": config_json is not None,
            "overrides_count": len(raw_overrides),
        }
        if operation_context is not None:
            requested_context.update(operation_context)
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_UPDATE_REQUESTED,
            execution_context=task_execution_context,
            message_params={"task_id": task_snapshot.task.task_id},
            context=requested_context,
        )

        effective_config_json: str
        if config_json is not None:
            effective_config_json = config_json
        else:
            task_config_path = (
                resolved_config.tasks_dir
                / task_snapshot.task.task_dir_relative_path
                / "config.json"
            )
            effective_config_json = task_config_path.read_text(encoding="utf-8")
            try:
                stored_document = json.loads(effective_config_json)
            except json.JSONDecodeError:
                stored_document = None
            if (
                isinstance(stored_document, dict)
                and compute_config_hash(stored_document) == task_snapshot.task.current_config_hash
            ):
                effective_config_json = json.dumps(
                    _resolved_config_as_strict_input(stored_document),
                    ensure_ascii=False,
                )

        parsed_config = ConfigParser().parse(effective_config_json)
        parsed_overrides = parse_cli_overrides(raw_overrides)
        merged_config = apply_config_overrides(
            base_config=parsed_config,
            overrides=tuple(parsed_overrides),
        )
        resolution = resolve_analysis_config(
            merged_config,
            default_alignment_mode=resolved_config.default_alignment_mode,
        )
        resolve_analysis_execution_selection(
            config=resolution.config,
            allow_explicit_from_phase=True,
        )
        for warning in resolution.warnings:
            runtime.event_service.emit(
                CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED,
                execution_context=task_execution_context,
                message_params={"parameter": _extract_warning_parameter(warning)},
                context={"warning": warning},
            )
        normalized_config_document = _with_pinned_runtime_settings(
            config_document=resolution.config.model_dump(mode="json"),
            resolved_config=resolved_config,
        )
        current_trace_id = _trace_id_from_config_document(current_config_document)
        if current_trace_id is not None:
            normalized_config_document["trace_id"] = str(current_trace_id)

        mutation = registry_service.update_task_config(
            task_id=task_snapshot.task.task_id,
            config_document=normalized_config_document,
            expected_task_version=task_snapshot.task.record_version,
        )
        if mutation.result_type not in {
            AnalyticalTaskMutationResultType.APPLIED,
            AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
        }:
            raise CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_TASK_UPDATE_REJECTED,
                message=(
                    f"Cannot update analytical task '{task_snapshot.task.task_id}': "
                    f"{_format_mutation_rejection_detail(mutation)}"
                ),
                message_params={
                    "task_id": task_snapshot.task.task_id,
                    "detail": _format_mutation_rejection_detail(mutation),
                },
                expected=True,
                retryable=False,
                can_continue=True,
            )
        if mutation.task is None:
            raise RuntimeError("update operation succeeded without task payload")

        if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
            event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_UPDATE_APPLIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": mutation.task.task_id,
                    "current_config_revision": mutation.task.current_config_revision,
                },
            )
        else:
            event = runtime.event_service.emit(
                CORE_ANALYTICAL_TASK_UPDATE_ALREADY_SATISFIED,
                execution_context=task_execution_context,
                message_params={"task_id": mutation.task.task_id},
            )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
            task_log_path=task_log_path,
        )

    return CoreOperationResult.success(
        event=event,
        value=TaskUpdateResult(
            task_id=mutation.task.task_id,
            result=mutation.result_type,
            current_config_revision=mutation.task.current_config_revision,
            current_config_hash=mutation.task.current_config_hash,
            default_priority=mutation.task.default_priority,
        ),
        system_log_path=runtime.system_log_path,
        task_log_path=task_log_path,
    )


def run_list_analytical_task_samples(
    *,
    task_id: str,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[list[str | None]]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(
        stage="task_registry",
        operation_id="tasks.samples.list",
    )
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=snapshot.task.task_id,
        )
        failure_execution_context = task_execution_context
        config_document = _load_task_config_revision_document(
            task_snapshot=snapshot,
            tasks_dir=resolved_config.tasks_dir,
        )
        samples = _samples_from_config_document(config_document=config_document)
        event = runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_FETCHED,
            execution_context=task_execution_context,
            message_params={"task_id": snapshot.task.task_id},
            context={"operation": "tasks.samples.list", "samples_count": len(samples)},
        )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )

    return CoreOperationResult.success(
        event=event,
        value=samples,
        system_log_path=runtime.system_log_path,
    )


def run_add_analytical_task_samples(
    *,
    task_id: str,
    sources: Sequence[str],
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[TaskUpdateResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(
        stage="task_registry",
        operation_id="tasks.samples.add",
    )
    runtime = _build_runtime(service=service, resolved_config=None)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")
        normalized_sources = _normalize_added_sources(sources=sources)
        if len(normalized_sources) == 0:
            raise AnalyticalTaskInvalidRecordDataError(
                detail="at least one source must be provided"
            )
        resolved_config = service.require_initialized_config()
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
        failure_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=snapshot.task.task_id,
        )
        config_document = _load_task_config_revision_document(
            task_snapshot=snapshot,
            tasks_dir=resolved_config.tasks_dir,
        )
        updated_samples = _samples_from_config_document(config_document=config_document)
        updated_samples.extend(normalized_sources)
        config_json = _build_samples_update_config_json(
            base_config_document=config_document,
            updated_samples=updated_samples,
        )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )

    return run_update_analytical_task(
        task_id=normalized_task_id,
        config_json=config_json,
        raw_overrides=(),
        core_config_service=service,
        operation_id="tasks.samples.add",
        operation_context={
            "samples_action": "add",
            "added_sources_count": len(normalized_sources),
        },
    )


def run_remove_analytical_task_samples(
    *,
    task_id: str,
    indices: Sequence[int],
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[TaskUpdateResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(
        stage="task_registry",
        operation_id="tasks.samples.remove",
    )
    runtime = _build_runtime(service=service, resolved_config=None)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")
        if len(indices) == 0:
            raise AnalyticalTaskInvalidRecordDataError(
                detail="at least one sample index must be provided"
            )

        resolved_config = service.require_initialized_config()
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
        failure_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=snapshot.task.task_id,
        )
        config_document = _load_task_config_revision_document(
            task_snapshot=snapshot,
            tasks_dir=resolved_config.tasks_dir,
        )
        updated_samples = _samples_from_config_document(config_document=config_document)
        unique_indices = sorted(set(indices), reverse=True)
        max_index = len(updated_samples) - 1
        for index in unique_indices:
            if index < 0 or index > max_index:
                raise AnalyticalTaskInvalidRecordDataError(
                    detail=(
                        f"sample index {index} is out of range "
                        f"(valid range: 0..{max_index if max_index >= 0 else 0})"
                    )
                )
        for index in unique_indices:
            del updated_samples[index]

        config_json = _build_samples_update_config_json(
            base_config_document=config_document,
            updated_samples=updated_samples,
        )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )

    requested_indices: list[JSONValue] = [index for index in sorted(set(indices))]
    return run_update_analytical_task(
        task_id=normalized_task_id,
        config_json=config_json,
        raw_overrides=(),
        core_config_service=service,
        operation_id="tasks.samples.remove",
        operation_context={
            "samples_action": "remove",
            "requested_indices": requested_indices,
            "removed_indices_count": len(unique_indices),
        },
    )


def run_reprioritize_analytical_task(
    *,
    task_id: str,
    priority: int,
    core_config_service: CoreConfigService | None = None,
) -> CoreOperationResult[TaskReprioritizeResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(
        stage="task_registry",
        operation_id="tasks.reprioritize",
    )
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    failure_execution_context = execution_context
    try:
        normalized_task_id = task_id.strip()
        if normalized_task_id == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")

        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        task_snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
        task_execution_context = _task_execution_context(
            execution_context=execution_context,
            registry_service=registry_service,
            task_id=task_snapshot.task.task_id,
        )
        failure_execution_context = task_execution_context
        runtime.event_service.emit(
            CORE_ANALYTICAL_JOB_REPRIORITIZE_REQUESTED,
            execution_context=task_execution_context,
            message_params={"task_id": task_snapshot.task.task_id, "priority": priority},
        )

        snapshot_job = task_snapshot.active_or_latest_job
        expected_job_version: int | None = None
        old_priority = priority
        if snapshot_job is not None and task_snapshot.task.active_job_id == snapshot_job.job_id:
            expected_job_version = snapshot_job.record_version
            old_priority = snapshot_job.priority

        mutation = registry_service.reprioritize_active_job(
            task_id=task_snapshot.task.task_id,
            priority=priority,
            expected_task_version=task_snapshot.task.record_version,
            expected_job_version=expected_job_version,
        )
        if mutation.result_type not in {
            AnalyticalTaskMutationResultType.APPLIED,
            AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
        }:
            raise CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_JOB_REPRIORITIZE_REJECTED,
                message=(
                    f"Cannot reprioritize analytical task '{task_snapshot.task.task_id}': "
                    f"{_format_mutation_rejection_detail(mutation)}"
                ),
                message_params={
                    "task_id": task_snapshot.task.task_id,
                    "detail": _format_mutation_rejection_detail(mutation),
                },
                expected=True,
                retryable=False,
                can_continue=True,
            )
        if mutation.task is None or mutation.job is None:
            raise RuntimeError("reprioritize operation succeeded without task/job payload")

        new_priority = mutation.job.priority
        if mutation.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED:
            old_priority = mutation.job.priority

        if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
            event = runtime.event_service.emit(
                CORE_ANALYTICAL_JOB_REPRIORITIZE_APPLIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": mutation.task.task_id,
                    "job_id": mutation.job.job_id,
                    "old_priority": old_priority,
                    "new_priority": new_priority,
                },
            )
        else:
            event = runtime.event_service.emit(
                CORE_ANALYTICAL_JOB_REPRIORITIZE_ALREADY_SATISFIED,
                execution_context=task_execution_context,
                message_params={
                    "task_id": mutation.task.task_id,
                    "job_id": mutation.job.job_id,
                },
            )
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=runtime,
            execution_context=failure_execution_context,
        )

    return CoreOperationResult.success(
        event=event,
        value=TaskReprioritizeResult(
            task_id=mutation.task.task_id,
            job_id=mutation.job.job_id,
            result=mutation.result_type,
            state=mutation.job.state,
            old_priority=old_priority,
            new_priority=new_priority,
        ),
        system_log_path=runtime.system_log_path,
    )


def run_runtime_continue(
    *,
    core_config_service: CoreConfigService | None = None,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
) -> CoreOperationResult[RuntimeContinueResult]:
    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_runtime", operation_id="runtime.continue")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    try:
        registry_service = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        )
        runtime_instance_id = str(uuid4())
        runtime_lease_token = str(uuid4())
        acquired_lease, conflicting_lease = registry_service.acquire_execution_runtime_lease(
            runtime_instance_id=runtime_instance_id,
            owner_pid=os.getpid(),
            lease_token=runtime_lease_token,
            lease_timeout_seconds=resolved_config.lease_timeout_seconds,
        )
        if acquired_lease is None:
            conflict_runtime_id = (
                conflicting_lease.runtime_instance_id
                if conflicting_lease is not None
                else "<unknown>"
            )
            raise CoreTaskLifecycleError(
                definition=CORE_RUNTIME_LEASE_CONFLICT,
                message=(
                    "Cannot start foreground runtime because another runtime lease owner is "
                    "active: "
                    f"'{conflict_runtime_id}'."
                ),
                message_params={"runtime_instance_id": conflict_runtime_id},
                expected=True,
                retryable=True,
                can_continue=True,
                safe_details={"runtime_instance_id": conflict_runtime_id},
            )

        runtime.event_service.emit(
            CORE_RUNTIME_LEASE_ACQUIRED,
            execution_context=execution_context,
            message_params={"runtime_instance_id": runtime_instance_id},
        )
        runtime_event_callback = _build_runtime_event_callback(
            runtime=runtime,
            execution_context=execution_context,
            resolved_config=resolved_config,
            registry_service=registry_service,
        )
        execution_runtime = ExecutionRuntime(
            registry_service=registry_service,
            tasks_dir=resolved_config.tasks_dir,
            runtime_config=RuntimeConfig.from_resolved_config(resolved_config),
            runtime_instance_id=runtime_instance_id,
            runtime_lease_token=runtime_lease_token,
            pipeline_name=pipeline_name,
            pipeline_version=pipeline_version,
            event_callback=runtime_event_callback,
        )
        try:
            runtime_result = execution_runtime.run(auto_queue_waiting_jobs=True)
        except RuntimeError as error:
            if "runtime lease was lost" in str(error):
                raise CoreTaskLifecycleError(
                    definition=CORE_RUNTIME_LEASE_EXPIRED,
                    message=(
                        "Runtime lease expired while processing queue. "
                        "Start JELICA Service with 'jelica service start'."
                    ),
                    message_params={"runtime_instance_id": runtime_instance_id},
                    expected=True,
                    retryable=True,
                    can_continue=True,
                ) from error
            raise
        finally:
            runtime.event_service.emit(
                CORE_RUNTIME_LEASE_RELEASED,
                execution_context=execution_context,
                message_params={"runtime_instance_id": runtime_instance_id},
            )

        event = runtime.event_service.emit(
            CORE_RUNTIME_SCHEDULER_STOPPED,
            execution_context=execution_context,
            message_params={"runtime_instance_id": runtime_instance_id},
            context={
                "claimed_jobs": runtime_result.claimed_jobs,
                "completed_jobs": runtime_result.completed_jobs,
                "failed_jobs": runtime_result.failed_jobs,
                "recovered_jobs": runtime_result.recovered_jobs,
                "interrupted": runtime_result.interrupted,
            },
        )
    except Exception as error:
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)

    return CoreOperationResult.success(
        event=event,
        value=runtime_result,
        system_log_path=runtime.system_log_path,
    )


def run_service_runtime(
    *,
    core_config_service: CoreConfigService | None = None,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
    pipeline_control: WorkerPipelineControl | None = None,
) -> CoreOperationResult[RuntimeContinueResult]:
    """Own the existing ExecutionRuntime until an explicit Service stop request."""

    service = core_config_service or CoreConfigService()
    execution_context = CoreExecutionContext(stage="task_runtime", operation_id="service.run")
    bootstrap_runtime = _build_runtime(service=service, resolved_config=None)

    try:
        resolved_config = service.require_initialized_config()
    except Exception as error:
        return _failure_result(
            error=error,
            runtime=bootstrap_runtime,
            execution_context=execution_context,
        )

    runtime = _build_runtime(service=service, resolved_config=resolved_config)
    from jelica_core.notifications.local import (
        LocalNotificationCoordinator,
        LocalNotificationOccurrence,
    )

    # Keep the coordinator in the Service parent.  The runtime callback is
    # invoked after parent-side terminal persistence and never crosses into a
    # worker process or an events watcher.
    def emit_local_notification_diagnostic(
        phase: str,
        reason: str,
        occurrence: LocalNotificationOccurrence | None,
    ) -> None:
        context: dict[str, JSONValue] = {"phase": phase, "reason": reason}
        if occurrence is not None:
            context.update(
                {
                    "event_id": occurrence.event_id,
                    "task_id": occurrence.task_id,
                    "occurrence_id": occurrence.occurrence_id,
                }
            )
        runtime.event_service.emit(
            CORE_LOCAL_NOTIFICATION_DIAGNOSTIC,
            execution_context=(
                execution_context.model_copy(update={"task_id": occurrence.task_id})
                if occurrence is not None
                else execution_context
            ),
            message_params={"phase": phase, "reason": reason},
            context=context,
        )

    local_notification_coordinator = LocalNotificationCoordinator(
        config_service=service,
        diagnostic_callback=emit_local_notification_diagnostic,
    )
    registry_service = AnalyticalTaskRegistryService(database_path=resolved_config.database_path)
    runtime_instance_id = str(uuid4())
    runtime_lease_token = str(uuid4())
    monitor: ServiceRuntimeControlMonitor | None = None
    lease_acquired = False

    def owns_runtime_lease() -> bool:
        current_lease = registry_service.get_execution_runtime_lease()
        return (
            current_lease is not None
            and current_lease.runtime_instance_id == runtime_instance_id
            and current_lease.lease_token == runtime_lease_token
        )

    try:
        acquired_lease, conflicting_lease = registry_service.acquire_execution_runtime_lease(
            runtime_instance_id=runtime_instance_id,
            owner_pid=os.getpid(),
            lease_token=runtime_lease_token,
            lease_timeout_seconds=resolved_config.lease_timeout_seconds,
        )
        if acquired_lease is None:
            conflict_runtime_id = (
                conflicting_lease.runtime_instance_id
                if conflicting_lease is not None
                else "<unknown>"
            )
            raise CoreTaskLifecycleError(
                definition=CORE_RUNTIME_LEASE_CONFLICT,
                message=(
                    "Cannot start JELICA Service because another runtime lease owner is active: "
                    f"'{conflict_runtime_id}'."
                ),
                message_params={"runtime_instance_id": conflict_runtime_id},
                expected=True,
                retryable=True,
                can_continue=True,
                safe_details={"runtime_instance_id": conflict_runtime_id},
            )
        lease_acquired = True

        runtime.event_service.emit(
            CORE_RUNTIME_LEASE_ACQUIRED,
            execution_context=execution_context,
            message_params={"runtime_instance_id": runtime_instance_id},
        )
        store = ServiceStateStore(data_dir=resolved_config.data_dir)
        metadata = initial_service_metadata(service_id=runtime_instance_id, pid=os.getpid())
        store.write_metadata(metadata)
        monitor = ServiceRuntimeControlMonitor(
            store=store,
            registry_service=registry_service,
            metadata=metadata,
        )
        monitor.mark_running()

        runtime_event_callback = _build_runtime_event_callback(
            runtime=runtime,
            execution_context=execution_context,
            resolved_config=resolved_config,
            registry_service=registry_service,
            local_notification_handler=local_notification_coordinator.emit,
        )
        execution_runtime = ExecutionRuntime(
            registry_service=registry_service,
            tasks_dir=resolved_config.tasks_dir,
            runtime_config=RuntimeConfig.from_resolved_config(resolved_config),
            runtime_instance_id=runtime_instance_id,
            runtime_lease_token=runtime_lease_token,
            pipeline_name=pipeline_name,
            pipeline_version=pipeline_version,
            pipeline_control=pipeline_control,
            event_callback=runtime_event_callback,
            shutdown_poll=monitor.poll,
        )
        runtime_result = execution_runtime.run(
            auto_queue_waiting_jobs=False,
            persistent=True,
        )
        # A stale runner may have exited after another process acquired the
        # canonical lease.  Do not let that runner overwrite the current
        # owner's metadata while marking itself stopped.
        if owns_runtime_lease():
            monitor.mark_stopped()
        event = runtime.event_service.emit(
            CORE_RUNTIME_SCHEDULER_STOPPED,
            execution_context=execution_context,
            message_params={"runtime_instance_id": runtime_instance_id},
            context={
                "claimed_jobs": runtime_result.claimed_jobs,
                "completed_jobs": runtime_result.completed_jobs,
                "failed_jobs": runtime_result.failed_jobs,
                "recovered_jobs": runtime_result.recovered_jobs,
                "interrupted": runtime_result.interrupted,
                "service": True,
            },
        )
    except Exception as error:
        if monitor is not None and owns_runtime_lease():
            try:
                monitor.mark_error(str(error))
            except OSError:
                pass
        return _failure_result(error=error, runtime=runtime, execution_context=execution_context)
    finally:
        if lease_acquired:
            registry_service.release_execution_runtime_lease(
                runtime_instance_id=runtime_instance_id,
                lease_token=runtime_lease_token,
            )
            runtime.event_service.emit(
                CORE_RUNTIME_LEASE_RELEASED,
                execution_context=execution_context,
                message_params={"runtime_instance_id": runtime_instance_id},
            )

    return CoreOperationResult.success(
        event=event,
        value=runtime_result,
        system_log_path=runtime.system_log_path,
    )


def _deduplicate_task_ids(*, task_ids: Sequence[str]) -> tuple[str, ...]:
    deduplicated: list[str] = []
    seen: set[str] = set()
    for raw_task_id in task_ids:
        normalized_task_id = raw_task_id.strip()
        if normalized_task_id in seen:
            continue
        seen.add(normalized_task_id)
        deduplicated.append(normalized_task_id)
    return tuple(deduplicated)


def _task_start_creates_new_job(task_snapshot: AnalyticalTaskSnapshot) -> bool:
    if task_snapshot.task.active_job_id is not None:
        return False
    latest_job = task_snapshot.active_or_latest_job
    if latest_job is None:
        return True
    return latest_job.state in {
        AnalyticalTaskState.FAILED,
        AnalyticalTaskState.CANCELLED,
    }


def _cleanup_prepared_job_seed(*, prepared_job_seed: PreparedJobSeed) -> None:
    cleanup_prepared_job_seed(prepared_seed=prepared_job_seed)


def _delete_analytical_task_item(
    *,
    registry_service: AnalyticalTaskRegistryService,
    tasks_dir: Path,
    task_id: str,
) -> TaskDeleteItemResult:
    normalized_task_id = task_id.strip()
    if normalized_task_id == "":
        return TaskDeleteItemResult(
            task_id=task_id or "<empty>",
            result=TaskDeleteItemResultType.REJECTED,
            detail="task_id must not be empty",
        )

    try:
        snapshot = registry_service.get_task_snapshot(task_id=normalized_task_id)
    except AnalyticalTaskNotFoundError:
        return TaskDeleteItemResult(
            task_id=normalized_task_id,
            result=TaskDeleteItemResultType.NOT_FOUND,
            detail="task not found",
        )

    active_job = snapshot.active_or_latest_job
    active_job_bound = (
        active_job is not None
        and snapshot.task.active_job_id is not None
        and snapshot.task.active_job_id == active_job.job_id
    )
    if snapshot.task.state is AnalyticalTaskState.DELETION_REQUESTED:
        return TaskDeleteItemResult(
            task_id=normalized_task_id,
            result=TaskDeleteItemResultType.ALREADY_SATISFIED,
            detail=None,
        )

    if active_job is not None and active_job_bound and _job_has_live_worker(active_job):
        # Progress persistence advances task versions; use the live-worker identity
        # as the concurrency guard here.
        mutation = registry_service.request_deletion(
            task_id=normalized_task_id,
            expected_active_job_id=active_job.job_id,
            expected_worker_instance_id=active_job.worker_instance_id,
            expected_lease_token=active_job.lease_token,
        )
        if mutation.result_type is AnalyticalTaskMutationResultType.APPLIED:
            return TaskDeleteItemResult(
                task_id=normalized_task_id,
                result=TaskDeleteItemResultType.DELETION_REQUESTED,
            )
        if mutation.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED:
            return TaskDeleteItemResult(
                task_id=normalized_task_id,
                result=TaskDeleteItemResultType.ALREADY_SATISFIED,
            )
        if mutation.result_type is AnalyticalTaskMutationResultType.NOT_FOUND:
            return TaskDeleteItemResult(
                task_id=normalized_task_id,
                result=TaskDeleteItemResultType.NOT_FOUND,
                detail="task not found",
            )
        return TaskDeleteItemResult(
            task_id=normalized_task_id,
            result=TaskDeleteItemResultType.REJECTED,
            detail=_format_mutation_rejection_detail(mutation),
        )

    move_result = None
    try:
        move_result = move_task_workspace_to_trash(
            tasks_dir=tasks_dir,
            task_dir_relative_path=snapshot.task.task_dir_relative_path,
            task_id=snapshot.task.task_id,
        )
        delete_result = registry_service.delete_task_and_jobs(
            task_id=snapshot.task.task_id,
            expected_task_version=snapshot.task.record_version,
        )
        if delete_result.result_type is AnalyticalTaskMutationResultType.APPLIED:
            purge_trashed_task_workspace(task_id=snapshot.task.task_id, move_result=move_result)
            return TaskDeleteItemResult(
                task_id=normalized_task_id,
                result=TaskDeleteItemResultType.DELETED,
            )
        if delete_result.result_type is AnalyticalTaskMutationResultType.NOT_FOUND:
            purge_trashed_task_workspace(task_id=snapshot.task.task_id, move_result=move_result)
            return TaskDeleteItemResult(
                task_id=normalized_task_id,
                result=TaskDeleteItemResultType.NOT_FOUND,
                detail="task not found",
            )

        if move_result is not None:
            restore_task_workspace_from_trash(
                task_id=snapshot.task.task_id,
                move_result=move_result,
            )
        return TaskDeleteItemResult(
            task_id=normalized_task_id,
            result=TaskDeleteItemResultType.REJECTED,
            detail=delete_result.result_type.value,
        )
    except TaskWorkspaceDeleteError as error:
        if move_result is not None:
            try:
                restore_task_workspace_from_trash(
                    task_id=snapshot.task.task_id,
                    move_result=move_result,
                )
            except TaskWorkspaceDeleteError:
                pass
        return TaskDeleteItemResult(
            task_id=normalized_task_id,
            result=TaskDeleteItemResultType.REJECTED,
            detail=str(error),
        )


def _job_has_live_worker(job_record: AnalyticalTaskJobRecord) -> bool:
    if job_record.worker_instance_id is None or job_record.lease_token is None:
        return False
    return job_record.state in {
        AnalyticalTaskState.RUNNING,
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
        AnalyticalTaskState.CANCEL_REQUESTED,
    }


def _emit_task_delete_item_event(
    *,
    runtime: CoreOperationRuntime,
    execution_context: CoreExecutionContext,
    item_result: TaskDeleteItemResult,
    trace_id: UUID | None,
) -> None:
    scoped_execution_context = execution_context.model_copy(
        update={"task_id": item_result.task_id, "trace_id": trace_id}
    )
    if item_result.result is TaskDeleteItemResultType.DELETION_REQUESTED:
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_DELETE_REQUESTED,
            execution_context=scoped_execution_context,
            message_params={"task_id": item_result.task_id},
        )
        return
    if item_result.result is TaskDeleteItemResultType.DELETED:
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_DELETE_APPLIED,
            execution_context=scoped_execution_context,
            message_params={"task_id": item_result.task_id},
        )
        return
    if item_result.result is TaskDeleteItemResultType.ALREADY_SATISFIED:
        runtime.event_service.emit(
            CORE_ANALYTICAL_TASK_DELETE_ALREADY_SATISFIED,
            execution_context=scoped_execution_context,
            message_params={"task_id": item_result.task_id},
        )
        return
    runtime.event_service.emit(
        CORE_ANALYTICAL_TASK_DELETE_REJECTED,
        execution_context=scoped_execution_context,
        message_params={
            "task_id": item_result.task_id,
            "detail": item_result.detail or item_result.result.value,
        },
    )


def _watch_task_until_terminal(
    *,
    registry_service: AnalyticalTaskRegistryService,
    task_id: str,
    job_id: str,
    poll_interval_seconds: float,
    watch_update_callback: Callable[[AnalyticalTaskJobRecord], None] | None,
) -> TaskWatchResult:
    observed_deletion_requested = False
    last_job_state: tuple[AnalyticalTaskState, str | None, int] | None = None
    last_progress = 0
    last_stage: str | None = None

    while True:
        try:
            task_record = registry_service.get_task(task_id=task_id)
        except AnalyticalTaskNotFoundError:
            if observed_deletion_requested:
                return TaskWatchResult(
                    task_id=task_id,
                    job_id=job_id,
                    state=AnalyticalTaskState.DELETION_REQUESTED,
                    result=TaskWatchResultType.DELETED,
                    progress=last_progress,
                    current_stage=last_stage,
                )
            raise
        if task_record.state is AnalyticalTaskState.DELETION_REQUESTED:
            observed_deletion_requested = True

        try:
            job_record = registry_service.get_job(job_id=job_id)
        except AnalyticalTaskJobNotFoundError:
            if observed_deletion_requested:
                return TaskWatchResult(
                    task_id=task_id,
                    job_id=job_id,
                    state=AnalyticalTaskState.DELETION_REQUESTED,
                    result=TaskWatchResultType.DELETED,
                    progress=last_progress,
                    current_stage=last_stage,
                )
            raise
        if job_record.task_id != task_id:
            raise CoreTaskLifecycleError(
                definition=CORE_ANALYTICAL_TASK_WATCH_REJECTED,
                message=(
                    f"Cannot watch analytical task '{task_id}': "
                    f"job '{job_id}' belongs to another task."
                ),
                message_params={
                    "task_id": task_id,
                    "detail": f"job '{job_id}' belongs to another task",
                },
                expected=True,
                retryable=False,
                can_continue=True,
                safe_details={"task_id": task_id, "job_id": job_id},
            )

        current_job_state = (job_record.state, job_record.current_stage, job_record.progress)
        if watch_update_callback is not None and current_job_state != last_job_state:
            watch_update_callback(job_record)
        last_job_state = current_job_state
        last_progress = job_record.progress
        last_stage = job_record.current_stage

        if job_record.state is AnalyticalTaskState.COMPLETED:
            return _build_task_watch_result(
                task_id=task_id,
                job_id=job_id,
                state=job_record.state,
                progress=job_record.progress,
                current_stage=job_record.current_stage,
            )
        if job_record.state is AnalyticalTaskState.FAILED:
            return _build_task_watch_result(
                task_id=task_id,
                job_id=job_id,
                state=job_record.state,
                progress=job_record.progress,
                current_stage=job_record.current_stage,
            )
        if job_record.state is AnalyticalTaskState.CANCELLED:
            return _build_task_watch_result(
                task_id=task_id,
                job_id=job_id,
                state=job_record.state,
                progress=job_record.progress,
                current_stage=job_record.current_stage,
            )
        sleep(poll_interval_seconds)


def _build_task_watch_result(
    *,
    task_id: str,
    job_id: str,
    state: AnalyticalTaskState,
    progress: int,
    current_stage: str | None,
) -> TaskWatchResult:
    if state is AnalyticalTaskState.COMPLETED:
        outcome = TaskWatchResultType.COMPLETED
    elif state is AnalyticalTaskState.FAILED:
        outcome = TaskWatchResultType.FAILED
    elif state is AnalyticalTaskState.CANCELLED:
        outcome = TaskWatchResultType.CANCELLED
    else:
        raise AnalyticalTaskInvalidRecordDataError(
            detail=f"cannot build terminal watch result from state '{state.value}'"
        )
    return TaskWatchResult(
        task_id=task_id,
        job_id=job_id,
        state=state,
        result=outcome,
        progress=progress,
        current_stage=current_stage,
    )


def _watch_job_until_terminal(
    *,
    registry_service: AnalyticalTaskRegistryService,
    job_id: str,
    poll_interval_seconds: float,
    runtime_instance_id: str,
) -> AnalyticalTaskJobRecord:
    while True:
        job = registry_service.get_job(job_id=job_id)
        if job.state in {
            AnalyticalTaskState.COMPLETED,
            AnalyticalTaskState.FAILED,
            AnalyticalTaskState.CANCELLED,
        }:
            return job
        lease = registry_service.get_execution_runtime_lease()
        if lease is None or lease.lease_expires_at <= utc_now():
            raise CoreTaskLifecycleError(
                definition=CORE_RUNTIME_LEASE_EXPIRED,
                message=(
                    "Observed runtime lease expired before job reached terminal state. "
                    "Start JELICA Service with 'jelica service start'."
                ),
                message_params={"runtime_instance_id": runtime_instance_id},
                expected=True,
                retryable=True,
                can_continue=True,
            )
        sleep(poll_interval_seconds)


def _format_mutation_rejection_detail(mutation: AnalyticalTaskMutationResult) -> str:
    if mutation.details is None or len(mutation.details) == 0:
        return mutation.result_type.value
    details_payload = ", ".join(f"{key}={value}" for key, value in sorted(mutation.details.items()))
    return f"{mutation.result_type.value} ({details_payload})"


def _attach_task_log_sink(
    *,
    runtime: CoreOperationRuntime,
    resolved_config: ResolvedCoreConfig,
    task_id: str,
    task_dir_relative_path: str,
) -> Path:
    task_log_path = resolved_config.tasks_dir / task_dir_relative_path / TASK_EVENTS_LOG_FILENAME
    runtime.event_service.add_sink(
        JsonlFileEventSink(
            path=task_log_path,
            minimum_level=_event_level_from_string(resolved_config.task_log_level),
            required=True,
            task_id=task_id,
        )
    )
    return task_log_path


def _attach_task_log_sink_for_task_id(
    *,
    runtime: CoreOperationRuntime,
    resolved_config: ResolvedCoreConfig,
    registry_service: AnalyticalTaskRegistryService,
    task_id: str,
    attached_task_ids: set[str],
) -> None:
    if task_id in attached_task_ids:
        return
    try:
        task_record = registry_service.get_task(task_id=task_id)
    except AnalyticalTaskNotFoundError:
        attached_task_ids.add(task_id)
        return
    _attach_task_log_sink(
        runtime=runtime,
        resolved_config=resolved_config,
        task_id=task_record.task_id,
        task_dir_relative_path=task_record.task_dir_relative_path,
    )
    attached_task_ids.add(task_id)


def _capture_structured_job_failure(
    *,
    context: dict[str, JSONValue],
    job_failure_events: dict[str, tuple[str, dict[str, JSONValue]]],
) -> None:
    job_id_value = context.get("job_id")
    failure_event_name_value = context.get("failure_event_name")
    if not isinstance(job_id_value, str) or not isinstance(failure_event_name_value, str):
        return
    failure_context_value = context.get("failure_context")
    failure_context: dict[str, JSONValue] = {}
    if isinstance(failure_context_value, dict):
        for key, value in failure_context_value.items():
            if isinstance(key, str):
                failure_context[key] = value
    if "detail" not in failure_context:
        detail_value = context.get("detail")
        if detail_value is not None:
            failure_context["detail"] = detail_value
    job_failure_events[job_id_value] = (failure_event_name_value, failure_context)


def _build_runtime_event_callback(
    *,
    runtime: CoreOperationRuntime,
    execution_context: CoreExecutionContext,
    resolved_config: ResolvedCoreConfig | None = None,
    registry_service: AnalyticalTaskRegistryService | None = None,
    job_failure_events: dict[str, tuple[str, dict[str, JSONValue]]] | None = None,
    preattached_task_ids: set[str] | None = None,
    runtime_event_listener: RuntimeEventListener | None = None,
    local_notification_handler: Callable[[Event], None] | None = None,
) -> Callable[[str, dict[str, JSONValue] | None], None]:
    attached_task_ids: set[str] = set(preattached_task_ids or ())

    def _callback(event_name: str, context: dict[str, JSONValue] | None) -> None:
        effective_context = context or {}
        if runtime_event_listener is not None:
            runtime_event_listener(event_name, effective_context)

        definition = _RUNTIME_EVENT_DEFINITIONS.get(event_name)
        if definition is None:
            return
        task_id_value = effective_context.get("task_id")
        task_id = str(task_id_value) if isinstance(task_id_value, str) else None
        trace_id_value = effective_context.get("trace_id")
        trace_id: UUID | None = None
        if isinstance(trace_id_value, str):
            try:
                trace_id = UUID(trace_id_value)
            except ValueError:
                trace_id = None
        if task_id is not None and resolved_config is not None and registry_service is not None:
            _attach_task_log_sink_for_task_id(
                runtime=runtime,
                resolved_config=resolved_config,
                registry_service=registry_service,
                task_id=task_id,
                attached_task_ids=attached_task_ids,
            )
        if event_name == RUNTIME_EVENT_JOB_FAILED and job_failure_events is not None:
            _capture_structured_job_failure(
                context=effective_context,
                job_failure_events=job_failure_events,
            )
        scoped_execution_context = (
            execution_context.model_copy(update={"task_id": task_id, "trace_id": trace_id})
            if task_id is not None
            else execution_context
        )
        event_type = _resolve_runtime_event_type(
            event_name=event_name,
            context=effective_context,
        )
        context_payload = dict(effective_context)
        context_payload.pop("event_type", None)
        context_payload.pop("trace_id", None)
        event = runtime.event_service.emit(
            definition,
            execution_context=scoped_execution_context,
            message_params=_build_runtime_message_params(
                event_name=event_name,
                context=effective_context,
            ),
            context=context_payload,
            event_type=event_type,
        )
        if local_notification_handler is not None:
            local_notification_handler(event)

    return _callback


_RUNTIME_EVENT_DEFINITIONS = {
    RUNTIME_EVENT_LEASE_EXPIRED: CORE_RUNTIME_LEASE_EXPIRED,
    RUNTIME_EVENT_SCHEDULER_STARTED: CORE_RUNTIME_SCHEDULER_STARTED,
    RUNTIME_EVENT_SCHEDULER_STOPPED: CORE_RUNTIME_SCHEDULER_STOPPED,
    RUNTIME_EVENT_JOB_CLAIMED: CORE_RUNTIME_JOB_CLAIMED,
    RUNTIME_EVENT_PREEMPTION_SELECTED: CORE_RUNTIME_PREEMPTION_SELECTED,
    RUNTIME_EVENT_PREEMPTION_REQUESTED: CORE_RUNTIME_PREEMPTION_REQUESTED,
    RUNTIME_EVENT_PREEMPTED_JOB_RETURNED_TO_WAITING: CORE_RUNTIME_PREEMPTED_JOB_RETURNED_TO_WAITING,
    RUNTIME_EVENT_WORKER_STARTED: CORE_RUNTIME_WORKER_STARTED,
    RUNTIME_EVENT_WORKER_HEARTBEAT_LOST: CORE_RUNTIME_WORKER_HEARTBEAT_LOST,
    RUNTIME_EVENT_WORKER_EXITED: CORE_RUNTIME_WORKER_EXITED,
    RUNTIME_EVENT_STAGE_STARTED: CORE_RUNTIME_STAGE_STARTED,
    RUNTIME_EVENT_STAGE_COMMITTED: CORE_RUNTIME_STAGE_COMMITTED,
    RUNTIME_EVENT_JOB_COMPLETED: CORE_RUNTIME_JOB_COMPLETED,
    RUNTIME_EVENT_JOB_FAILED: CORE_RUNTIME_JOB_FAILED,
    RUNTIME_EVENT_RECOVERY_STARTED: CORE_RUNTIME_RECOVERY_STARTED,
    RUNTIME_EVENT_RECOVERY_COMPLETED: CORE_RUNTIME_RECOVERY_COMPLETED,
    RUNTIME_EVENT_RECOVERY_FAILED: CORE_RUNTIME_RECOVERY_FAILED,
    RUNTIME_EVENT_STALE_MESSAGE_REJECTED: CORE_RUNTIME_STALE_WORKER_MESSAGE_REJECTED,
    RUNTIME_EVENT_PROCESS_SPAWN_FAILURE: CORE_RUNTIME_PROCESS_SPAWN_FAILED,
    RUNTIME_EVENT_RUNTIME_INTERRUPTED: CORE_RUNTIME_INTERRUPTED,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PAUSE: CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PAUSE,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_CANCEL: CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_CANCEL,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_DELETION: (
        CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_DELETION
    ),
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PREEMPTION: (
        CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION
    ),
    "INPUT_SOURCE_UNSUPPORTED": CORE_INPUT_SOURCE_UNSUPPORTED,
    "INPUT_PATH_NOT_FOUND": CORE_INPUT_PATH_NOT_FOUND,
    "INPUT_FILE_TYPE_UNSUPPORTED": CORE_INPUT_FILE_TYPE_UNSUPPORTED,
    "INPUT_FILE_UNREADABLE": CORE_INPUT_FILE_UNREADABLE,
    "INPUT_FILE_EMPTY": CORE_INPUT_FILE_EMPTY,
    "INPUT_DIRECTORY_EMPTY": CORE_INPUT_DIRECTORY_EMPTY,
    "INPUT_DIRECTORY_NO_SUPPORTED_FILES": CORE_INPUT_DIRECTORY_NO_SUPPORTED_FILES,
    "INPUT_NO_DATA_ACQUIRED": CORE_INPUT_NO_DATA_ACQUIRED,
    "INPUT_UNSUPPORTED_FILES_SKIPPED": CORE_INPUT_UNSUPPORTED_FILES_SKIPPED,
    "INPUT_SYMLINK_UNSUPPORTED": CORE_INPUT_SYMLINK_UNSUPPORTED,
    "INPUT_SYMLINKS_SKIPPED": CORE_INPUT_SYMLINKS_SKIPPED,
    "INPUT_DIRECTORY_DEPTH_LIMIT_REACHED": CORE_INPUT_DIRECTORY_DEPTH_LIMIT_REACHED,
    "INPUT_DUPLICATES_SKIPPED": CORE_INPUT_DUPLICATES_SKIPPED,
    "INPUT_ACQUISITION_COMPLETED": CORE_INPUT_ACQUISITION_COMPLETED,
    "INPUT_COPY_FAILED": CORE_INPUT_COPY_FAILED,
    "INLINE_SEQUENCE_INVALID": CORE_INLINE_SEQUENCE_INVALID,
    "NCBI_URL_UNSUPPORTED": CORE_NCBI_URL_UNSUPPORTED,
    "NCBI_ACCESSION_INVALID": CORE_NCBI_ACCESSION_INVALID,
    "NCBI_RECORD_NOT_FOUND": CORE_NCBI_RECORD_NOT_FOUND,
    "NCBI_REQUEST_FAILED": CORE_NCBI_REQUEST_FAILED,
    "NCBI_REQUEST_TIMEOUT": CORE_NCBI_REQUEST_TIMEOUT,
    "NCBI_RESPONSE_EMPTY": CORE_NCBI_RESPONSE_EMPTY,
    "NCBI_RESPONSE_INVALID": CORE_NCBI_RESPONSE_INVALID,
    "NCBI_PARTIAL_RESPONSE": CORE_NCBI_PARTIAL_RESPONSE,
    "INPUT_PROCESSING_STARTED": CORE_INPUT_PROCESSING_STARTED,
    "INPUT_PROCESSING_FILE_PROCESSED": CORE_INPUT_PROCESSING_FILE_PROCESSED,
    "INPUT_PROCESSING_COMPLETED": CORE_INPUT_PROCESSING_COMPLETED,
    "INPUT_PROCESSING_VALIDATION_FAILED": CORE_INPUT_PROCESSING_VALIDATION_FAILED,
    "INPUT_PROCESSING_FAILED": CORE_INPUT_PROCESSING_FAILED,
    "ALIGNMENT_STARTED": CORE_ALIGNMENT_STARTED,
    "ALIGNMENT_SKIPPED": CORE_ALIGNMENT_SKIPPED,
    "ALIGNMENT_PREALIGNED_VALIDATION_STARTED": CORE_ALIGNMENT_PREALIGNED_VALIDATION_STARTED,
    "ALIGNMENT_MAFFT_PROBED": CORE_ALIGNMENT_MAFFT_AVAILABILITY_CONFIRMED,
    "ALIGNMENT_MAFFT_LAUNCHED": CORE_ALIGNMENT_MAFFT_PROCESS_STARTED,
    "ALIGNMENT_MAFFT_COMPLETED": CORE_ALIGNMENT_MAFFT_PROCESS_COMPLETED,
    "ALIGNMENT_MAFFT_FAILED": CORE_ALIGNMENT_MAFFT_PROCESS_FAILED,
    "ALIGNMENT_MAFFT_STOPPED_PAUSE": CORE_ALIGNMENT_MAFFT_STOPPED_FOR_PAUSE,
    "ALIGNMENT_MAFFT_STOPPED_CANCEL": CORE_ALIGNMENT_MAFFT_STOPPED_FOR_CANCEL,
    "ALIGNMENT_MAFFT_STOPPED_SHUTDOWN": CORE_ALIGNMENT_MAFFT_STOPPED_FOR_SHUTDOWN,
    "ALIGNMENT_RESULT_INVALID": CORE_ALIGNMENT_RESULT_VALIDATION_FAILED,
    "ALIGNMENT_RESULT_PUBLISHED": CORE_ALIGNMENT_RESULT_PUBLISHED,
    "ALIGNMENT_COMPLETED": CORE_ALIGNMENT_COMPLETED,
    "COMPARATIVE_ANALYSIS_STARTED": CORE_COMPARATIVE_ANALYSIS_STARTED,
    "COMPARATIVE_ANALYSIS_SKIPPED": CORE_COMPARATIVE_ANALYSIS_SKIPPED,
    "COMPARATIVE_ANALYSIS_PHASE_STARTED": CORE_COMPARATIVE_ANALYSIS_PHASE_STARTED,
    "COMPARATIVE_ANALYSIS_PROGRESS": CORE_COMPARATIVE_ANALYSIS_PROGRESS,
    "COMPARATIVE_ANALYSIS_OPERATION_FAILED": CORE_COMPARATIVE_ANALYSIS_OPERATION_FAILED,
    "COMPARATIVE_ANALYSIS_RESULT_PUBLISHED": CORE_COMPARATIVE_ANALYSIS_RESULT_PUBLISHED,
    "COMPARATIVE_ANALYSIS_COMPLETED": CORE_COMPARATIVE_ANALYSIS_COMPLETED,
    "COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS": CORE_COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS,
    "COMPARATIVE_ANALYSIS_FAILED": CORE_COMPARATIVE_ANALYSIS_FAILED,
    "DISTANCE_MATRIX_STARTED": CORE_DISTANCE_MATRIX_STARTED,
    "DISTANCE_MATRIX_SKIPPED": CORE_DISTANCE_MATRIX_SKIPPED,
    "DISTANCE_MATRIX_PROGRESS": CORE_DISTANCE_MATRIX_PROGRESS,
    "DISTANCE_MATRIX_RESULT_PUBLISHED": CORE_DISTANCE_MATRIX_RESULT_PUBLISHED,
    "DISTANCE_MATRIX_COMPLETED": CORE_DISTANCE_MATRIX_COMPLETED,
    "DISTANCE_MATRIX_PARTIAL_SUCCESS": CORE_DISTANCE_MATRIX_PARTIAL_SUCCESS,
    "DISTANCE_MATRIX_FAILED": CORE_DISTANCE_MATRIX_FAILED,
    "PHYLOGENETIC_TREE_STARTED": CORE_PHYLOGENETIC_TREE_STARTED,
    "PHYLOGENETIC_TREE_SKIPPED": CORE_PHYLOGENETIC_TREE_SKIPPED,
    "PHYLOGENETIC_TREE_PROGRESS": CORE_PHYLOGENETIC_TREE_PROGRESS,
    "PHYLOGENETIC_TREE_RESULT_PUBLISHED": CORE_PHYLOGENETIC_TREE_RESULT_PUBLISHED,
    "PHYLOGENETIC_TREE_COMPLETED": CORE_PHYLOGENETIC_TREE_COMPLETED,
    "PHYLOGENETIC_TREE_FAILED": CORE_PHYLOGENETIC_TREE_FAILED,
    "CLADE_DETECTION_STARTED": CORE_CLADE_DETECTION_STARTED,
    "CLADE_DETECTION_SKIPPED": CORE_CLADE_DETECTION_SKIPPED,
    "CLADE_DETECTION_PROGRESS": CORE_CLADE_DETECTION_PROGRESS,
    "CLADE_DETECTION_RESULT_PUBLISHED": CORE_CLADE_DETECTION_RESULT_PUBLISHED,
    "CLADE_DETECTION_COMPLETED": CORE_CLADE_DETECTION_COMPLETED,
    "CLADE_DETECTION_FAILED": CORE_CLADE_DETECTION_FAILED,
}


def _build_runtime_message_params(
    *,
    event_name: str,
    context: dict[str, JSONValue],
) -> dict[str, JSONValue]:
    runtime_instance_id = _context_text(context, "runtime_instance_id")
    task_id = _context_text(context, "task_id")
    job_id = _context_text(context, "job_id")
    stage_id = _context_text(context, "stage_id")
    detail = _context_text(context, "detail")
    reason = _context_text(context, "reason")
    state = _context_text(context, "state")
    candidate_task_id = _context_text(context, "candidate_task_id")
    candidate_job_id = _context_text(context, "candidate_job_id")
    victim_task_id = _context_text(context, "victim_task_id")
    victim_job_id = _context_text(context, "victim_job_id")

    if event_name in {
        RUNTIME_EVENT_LEASE_EXPIRED,
        RUNTIME_EVENT_SCHEDULER_STARTED,
        RUNTIME_EVENT_SCHEDULER_STOPPED,
        RUNTIME_EVENT_RUNTIME_INTERRUPTED,
        RUNTIME_EVENT_RECOVERY_STARTED,
        RUNTIME_EVENT_RECOVERY_COMPLETED,
    }:
        return {"runtime_instance_id": runtime_instance_id}
    if event_name in {
        RUNTIME_EVENT_JOB_CLAIMED,
        RUNTIME_EVENT_WORKER_STARTED,
        RUNTIME_EVENT_WORKER_HEARTBEAT_LOST,
        RUNTIME_EVENT_WORKER_EXITED,
        RUNTIME_EVENT_JOB_COMPLETED,
        RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PAUSE,
        RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_CANCEL,
        RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_DELETION,
        RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PREEMPTION,
        RUNTIME_EVENT_PREEMPTED_JOB_RETURNED_TO_WAITING,
    }:
        return {"task_id": task_id, "job_id": job_id}
    if event_name in {RUNTIME_EVENT_PREEMPTION_SELECTED, RUNTIME_EVENT_PREEMPTION_REQUESTED}:
        return {
            "candidate_task_id": candidate_task_id,
            "candidate_job_id": candidate_job_id,
            "victim_task_id": victim_task_id,
            "victim_job_id": victim_job_id,
        }
    if event_name in {RUNTIME_EVENT_STAGE_STARTED, RUNTIME_EVENT_STAGE_COMMITTED}:
        return {"task_id": task_id, "job_id": job_id, "stage_id": stage_id}
    if (
        event_name in _INPUT_ACQUISITION_EVENTS
        or event_name in _INPUT_PROCESSING_EVENTS
        or event_name in _ALIGNMENT_EVENTS
        or event_name in _COMPARATIVE_ANALYSIS_EVENTS
        or event_name in _DISTANCE_MATRIX_EVENTS
        or event_name in _PHYLOGENETIC_TREE_EVENTS
        or event_name in _CLADE_DETECTION_EVENTS
    ):
        return {"detail": detail}
    if event_name == RUNTIME_EVENT_JOB_FAILED:
        effective_detail = detail if detail != "<unknown>" else reason
        return {"task_id": task_id, "job_id": job_id, "detail": effective_detail}
    if event_name == RUNTIME_EVENT_RECOVERY_FAILED:
        return {"task_id": task_id, "job_id": job_id, "reason": reason}
    if event_name == RUNTIME_EVENT_STALE_MESSAGE_REJECTED:
        return {"job_id": job_id}
    if event_name == RUNTIME_EVENT_PROCESS_SPAWN_FAILURE:
        return {"task_id": task_id, "job_id": job_id, "detail": detail}
    return {"task_id": task_id, "job_id": job_id, "state": state}


_INPUT_ACQUISITION_EVENTS = frozenset(
    {
        "INPUT_SOURCE_UNSUPPORTED",
        "INPUT_PATH_NOT_FOUND",
        "INPUT_FILE_TYPE_UNSUPPORTED",
        "INPUT_FILE_UNREADABLE",
        "INPUT_FILE_EMPTY",
        "INPUT_DIRECTORY_EMPTY",
        "INPUT_DIRECTORY_NO_SUPPORTED_FILES",
        "INPUT_NO_DATA_ACQUIRED",
        "INPUT_UNSUPPORTED_FILES_SKIPPED",
        "INPUT_SYMLINK_UNSUPPORTED",
        "INPUT_SYMLINKS_SKIPPED",
        "INPUT_DIRECTORY_DEPTH_LIMIT_REACHED",
        "INPUT_DUPLICATES_SKIPPED",
        "INPUT_ACQUISITION_COMPLETED",
        "INPUT_COPY_FAILED",
        "INLINE_SEQUENCE_INVALID",
        "NCBI_URL_UNSUPPORTED",
        "NCBI_ACCESSION_INVALID",
        "NCBI_RECORD_NOT_FOUND",
        "NCBI_REQUEST_FAILED",
        "NCBI_REQUEST_TIMEOUT",
        "NCBI_RESPONSE_EMPTY",
        "NCBI_RESPONSE_INVALID",
        "NCBI_PARTIAL_RESPONSE",
    }
)

_INPUT_PROCESSING_EVENTS = frozenset(
    {
        "INPUT_PROCESSING_STARTED",
        "INPUT_PROCESSING_FILE_PROCESSED",
        "INPUT_PROCESSING_COMPLETED",
        "INPUT_PROCESSING_VALIDATION_FAILED",
        "INPUT_PROCESSING_FAILED",
    }
)

_ALIGNMENT_EVENTS = frozenset(
    {
        "ALIGNMENT_STARTED",
        "ALIGNMENT_SKIPPED",
        "ALIGNMENT_PREALIGNED_VALIDATION_STARTED",
        "ALIGNMENT_MAFFT_PROBED",
        "ALIGNMENT_MAFFT_LAUNCHED",
        "ALIGNMENT_MAFFT_COMPLETED",
        "ALIGNMENT_MAFFT_FAILED",
        "ALIGNMENT_MAFFT_STOPPED_PAUSE",
        "ALIGNMENT_MAFFT_STOPPED_CANCEL",
        "ALIGNMENT_MAFFT_STOPPED_SHUTDOWN",
        "ALIGNMENT_RESULT_INVALID",
        "ALIGNMENT_RESULT_PUBLISHED",
        "ALIGNMENT_COMPLETED",
    }
)

_COMPARATIVE_ANALYSIS_EVENTS = frozenset(
    {
        "COMPARATIVE_ANALYSIS_STARTED",
        "COMPARATIVE_ANALYSIS_SKIPPED",
        "COMPARATIVE_ANALYSIS_PHASE_STARTED",
        "COMPARATIVE_ANALYSIS_PROGRESS",
        "COMPARATIVE_ANALYSIS_OPERATION_FAILED",
        "COMPARATIVE_ANALYSIS_RESULT_PUBLISHED",
        "COMPARATIVE_ANALYSIS_COMPLETED",
        "COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS",
        "COMPARATIVE_ANALYSIS_FAILED",
    }
)

_DISTANCE_MATRIX_EVENTS = frozenset(
    {
        "DISTANCE_MATRIX_STARTED",
        "DISTANCE_MATRIX_SKIPPED",
        "DISTANCE_MATRIX_PROGRESS",
        "DISTANCE_MATRIX_RESULT_PUBLISHED",
        "DISTANCE_MATRIX_COMPLETED",
        "DISTANCE_MATRIX_PARTIAL_SUCCESS",
        "DISTANCE_MATRIX_FAILED",
    }
)

_PHYLOGENETIC_TREE_EVENTS = frozenset(
    {
        "PHYLOGENETIC_TREE_STARTED",
        "PHYLOGENETIC_TREE_SKIPPED",
        "PHYLOGENETIC_TREE_PROGRESS",
        "PHYLOGENETIC_TREE_RESULT_PUBLISHED",
        "PHYLOGENETIC_TREE_COMPLETED",
        "PHYLOGENETIC_TREE_FAILED",
    }
)


_CLADE_DETECTION_EVENTS = frozenset(
    {
        "CLADE_DETECTION_STARTED",
        "CLADE_DETECTION_SKIPPED",
        "CLADE_DETECTION_PROGRESS",
        "CLADE_DETECTION_RESULT_PUBLISHED",
        "CLADE_DETECTION_COMPLETED",
        "CLADE_DETECTION_FAILED",
    }
)


def _resolve_runtime_event_type(
    *,
    event_name: str,
    context: dict[str, JSONValue],
) -> EventType | None:
    if event_name != "INPUT_PROCESSING_FILE_PROCESSED":
        return None
    event_type_value = context.get("event_type")
    if not isinstance(event_type_value, str):
        return None
    normalized = event_type_value.strip().lower()
    if normalized == "":
        return None
    try:
        return EventType(normalized)
    except ValueError:
        return None


_FAILED_JOB_REASON_DEFINITIONS: dict[str, EventDefinition] = {
    "INPUT_SOURCE_UNSUPPORTED": CORE_INPUT_SOURCE_UNSUPPORTED,
    "INPUT_PATH_NOT_FOUND": CORE_INPUT_PATH_NOT_FOUND,
    "INPUT_FILE_TYPE_UNSUPPORTED": CORE_INPUT_FILE_TYPE_UNSUPPORTED,
    "INPUT_FILE_UNREADABLE": CORE_INPUT_FILE_UNREADABLE,
    "INPUT_FILE_EMPTY": CORE_INPUT_FILE_EMPTY,
    "INPUT_DIRECTORY_EMPTY": CORE_INPUT_DIRECTORY_EMPTY,
    "INPUT_DIRECTORY_NO_SUPPORTED_FILES": CORE_INPUT_DIRECTORY_NO_SUPPORTED_FILES,
    "INPUT_NO_DATA_ACQUIRED": CORE_INPUT_NO_DATA_ACQUIRED,
    "INPUT_UNSUPPORTED_FILES_SKIPPED": CORE_INPUT_UNSUPPORTED_FILES_SKIPPED,
    "INPUT_SYMLINK_UNSUPPORTED": CORE_INPUT_SYMLINK_UNSUPPORTED,
    "INPUT_SYMLINKS_SKIPPED": CORE_INPUT_SYMLINKS_SKIPPED,
    "INPUT_DIRECTORY_DEPTH_LIMIT_REACHED": CORE_INPUT_DIRECTORY_DEPTH_LIMIT_REACHED,
    "INPUT_DUPLICATES_SKIPPED": CORE_INPUT_DUPLICATES_SKIPPED,
    "INPUT_COPY_FAILED": CORE_INPUT_COPY_FAILED,
    "INLINE_SEQUENCE_INVALID": CORE_INLINE_SEQUENCE_INVALID,
    "NCBI_URL_UNSUPPORTED": CORE_NCBI_URL_UNSUPPORTED,
    "NCBI_ACCESSION_INVALID": CORE_NCBI_ACCESSION_INVALID,
    "NCBI_RECORD_NOT_FOUND": CORE_NCBI_RECORD_NOT_FOUND,
    "NCBI_REQUEST_FAILED": CORE_NCBI_REQUEST_FAILED,
    "NCBI_REQUEST_TIMEOUT": CORE_NCBI_REQUEST_TIMEOUT,
    "NCBI_RESPONSE_EMPTY": CORE_NCBI_RESPONSE_EMPTY,
    "NCBI_RESPONSE_INVALID": CORE_NCBI_RESPONSE_INVALID,
    "NCBI_PARTIAL_RESPONSE": CORE_NCBI_PARTIAL_RESPONSE,
    "INPUT_PROCESSING_VALIDATION_FAILED": CORE_INPUT_PROCESSING_VALIDATION_FAILED,
    "COMPARATIVE_ANALYSIS_FAILED": CORE_COMPARATIVE_ANALYSIS_FAILED,
    "DISTANCE_MATRIX_FAILED": CORE_DISTANCE_MATRIX_FAILED,
    "PHYLOGENETIC_TREE_FAILED": CORE_PHYLOGENETIC_TREE_FAILED,
    "CLADE_DETECTION_FAILED": CORE_CLADE_DETECTION_FAILED,
}


def _context_text(context: dict[str, JSONValue], key: str) -> str:
    value = context.get(key)
    if isinstance(value, str) and value.strip() != "":
        return value
    if value is None:
        return "<unknown>"
    return str(value)


def _build_runtime(
    *,
    service: CoreConfigService,
    resolved_config: ResolvedCoreConfig | None,
) -> CoreOperationRuntime:
    factory = CoreEventFactory(component=EventComponent.CORE)

    minimum_level = DEFAULT_LOG_LEVEL
    include_diagnostics = DEFAULT_INCLUDE_DIAGNOSTICS
    diagnostic_field_limit = DEFAULT_DIAGNOSTIC_FIELD_LIMIT
    system_log_path: Path | None = None

    if resolved_config is not None:
        minimum_level = resolved_config.system_log_level
        include_diagnostics = resolved_config.include_diagnostics
        diagnostic_field_limit = resolved_config.diagnostic_field_limit
        system_log_path = resolved_config.logs_dir / SYSTEM_EVENTS_LOG_FILENAME
    else:
        try:
            system_log_path = (
                service.get_jelica_home()
                / DEFAULT_DATA_DIRECTORY
                / "logs"
                / (SYSTEM_EVENTS_LOG_FILENAME)
            )
        except CoreConfigError:
            system_log_path = None

    sinks = []
    if system_log_path is not None:
        sinks.append(
            JsonlFileEventSink(
                path=system_log_path,
                minimum_level=_event_level_from_string(minimum_level),
                required=True,
            )
        )

    event_service = EventService(factory=factory, sinks=sinks)
    translator = CoreExceptionTranslator(
        factory=factory,
        include_diagnostics=include_diagnostics,
        diagnostic_field_limit=diagnostic_field_limit,
    )

    return CoreOperationRuntime(
        event_service=event_service,
        translator=translator,
        system_log_path=system_log_path,
    )


def _failure_result(
    *,
    error: Exception,
    runtime: CoreOperationRuntime,
    execution_context: CoreExecutionContext,
    task_log_path: Path | None = None,
) -> CoreOperationResult[T]:
    public_error = runtime.translator.to_public_error(error, execution_context=execution_context)
    _try_emit_public_error(runtime=runtime, error=public_error)
    return CoreOperationResult.failure(
        error=public_error,
        system_log_path=runtime.system_log_path,
        task_log_path=task_log_path,
    )


def _try_emit_public_error(*, runtime: CoreOperationRuntime, error: PublicError) -> None:
    try:
        runtime.event_service.emit_event(error.event)
    except MandatoryEventSinkWriteError:
        return


def _extract_warning_parameter(warning: str) -> str:
    prefix = "Ignoring unknown analysis config field '"
    suffix = "'."
    if warning.startswith(prefix) and warning.endswith(suffix):
        return warning.removeprefix(prefix).removesuffix(suffix)
    return warning


def _with_pinned_runtime_settings(
    *,
    config_document: dict[str, object],
    resolved_config: ResolvedCoreConfig,
) -> dict[str, object]:
    updated_config = dict(config_document)
    updated_config["input_directory_max_depth"] = resolved_config.input_directory_max_depth
    updated_config["ncbi_max_retries"] = resolved_config.ncbi_max_retries
    return updated_config


def _pin_initial_task_config_revision(
    *,
    initialized_task: InitializedAnalysisTask,
    resolved_config: ResolvedCoreConfig,
) -> InitializedAnalysisTask:
    config_document = _load_json_document(path=initialized_task.config_path)
    pinned_config_document = _with_pinned_runtime_settings(
        config_document=config_document,
        resolved_config=resolved_config,
    )
    if pinned_config_document == config_document:
        return initialized_task

    payload = serialize_config_document(pinned_config_document)
    config_revision_path = initialized_task.task_dir / Path(
        initialized_task.current_config_relative_path
    )
    write_text_atomically(path=config_revision_path, payload=payload)
    write_text_atomically(path=initialized_task.config_path, payload=payload)
    config_hash = compute_config_hash(pinned_config_document)
    return initialized_task.model_copy(update={"current_config_hash": config_hash})


def _load_json_document(*, path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RuntimeError(f"cannot read JSON config '{path}': {error}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"cannot parse JSON config '{path}': {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON config '{path}' must be an object")
    return {str(key): value for key, value in payload.items()}


def _load_task_config_revision_document(
    *,
    task_snapshot: AnalyticalTaskSnapshot,
    tasks_dir: Path,
) -> dict[str, object]:
    config_path = tasks_dir / task_snapshot.task.task_dir_relative_path
    config_path = config_path / Path(task_snapshot.task.current_config_relative_path)
    return _load_json_document(path=config_path)


def _trace_id_from_config_document(config_document: dict[str, object]) -> UUID | None:
    raw_trace_id = config_document.get("trace_id")
    if raw_trace_id is None:
        return None
    return UUID(str(raw_trace_id))


def _task_execution_context(
    *,
    execution_context: CoreExecutionContext,
    registry_service: AnalyticalTaskRegistryService,
    task_id: str,
) -> CoreExecutionContext:
    return execution_context.model_copy(
        update={
            "task_id": task_id,
            "trace_id": registry_service.get_task_trace_id(task_id=task_id),
        }
    )


def _samples_from_config_document(*, config_document: dict[str, object]) -> list[str | None]:
    config_input = AnalysisConfigInput.model_validate(
        _resolved_config_as_strict_input(config_document)
    )
    if config_input.samples is None:
        return []
    return list(config_input.samples)


def _build_samples_update_config_json(
    *,
    base_config_document: dict[str, object],
    updated_samples: list[str | None],
) -> str:
    config_input = AnalysisConfigInput.model_validate(
        _resolved_config_as_strict_input(base_config_document)
    )
    mutable_input = config_input.model_dump(mode="python")
    mutable_input["samples"] = list(updated_samples)
    next_input = AnalysisConfigInput.model_validate(mutable_input)
    return json.dumps(
        next_input.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _normalize_added_sources(*, sources: Sequence[str]) -> list[str]:
    normalized_sources: list[str] = []
    for source in sources:
        normalized_source = source.strip()
        if normalized_source == "":
            raise AnalyticalTaskInvalidRecordDataError(detail="sample sources must not be empty")
        normalized_sources.append(normalized_source)
    return normalized_sources


def _parse_task_states(states: Sequence[str] | None) -> tuple[AnalyticalTaskState, ...] | None:
    if states is None or len(states) == 0:
        return None

    parsed_states: list[AnalyticalTaskState] = []
    for state in states:
        normalized_state = state.strip().lower()
        if normalized_state == "":
            raise AnalyticalTaskInvalidRecordDataError(
                detail="state filter values must not be empty"
            )
        try:
            parsed_state = AnalyticalTaskState(normalized_state)
        except ValueError as error:
            allowed_states = ", ".join(sorted(item.value for item in AnalyticalTaskState))
            raise AnalyticalTaskInvalidRecordDataError(
                detail=f"unknown analytical task state '{state}' (allowed: {allowed_states})"
            ) from error
        if parsed_state not in parsed_states:
            parsed_states.append(parsed_state)
    return tuple(parsed_states)


def _normalize_tasks_list_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_ANALYTICAL_TASKS_LIST_LIMIT
    if limit <= 0:
        raise AnalyticalTaskInvalidRecordDataError(detail="limit must be > 0")
    if limit > MAX_ANALYTICAL_TASKS_LIST_LIMIT:
        raise AnalyticalTaskInvalidRecordDataError(
            detail=f"limit must be <= {MAX_ANALYTICAL_TASKS_LIST_LIMIT}"
        )
    return limit


def _normalize_tasks_list_offset(offset: int) -> int:
    if offset < 0:
        raise AnalyticalTaskInvalidRecordDataError(detail="offset must be >= 0")
    return offset


def _resolve_task_dir_relative_path(*, task_dir: Path, tasks_dir: Path, task_id: str) -> str:
    normalized_task_id = task_id.strip()
    if normalized_task_id == "":
        raise AnalyticalTaskInvalidRecordDataError(detail="task_id must not be empty")

    resolved_tasks_dir = tasks_dir.resolve(strict=False)
    resolved_task_dir = task_dir.resolve(strict=False)
    if resolved_task_dir == resolved_tasks_dir:
        raise AnalyticalTaskInvalidRecordDataError(detail="task_dir must not equal tasks_dir")
    try:
        relative_path = resolved_task_dir.relative_to(resolved_tasks_dir)
    except ValueError as error:
        raise AnalyticalTaskInvalidRecordDataError(
            detail=(
                f"task_dir '{resolved_task_dir}' must be located inside tasks_dir "
                f"'{resolved_tasks_dir}'"
            )
        ) from error

    if relative_path == Path("."):
        raise AnalyticalTaskInvalidRecordDataError(
            detail="task_dir_relative_path must not be empty"
        )
    if relative_path.parts[-1] != normalized_task_id:
        raise AnalyticalTaskInvalidRecordDataError(
            detail=(
                "task_dir must match expected task_id directory: "
                f"task_id='{normalized_task_id}', "
                f"task_dir_relative_path='{relative_path.as_posix()}'"
            )
        )
    return relative_path.as_posix()


def _try_compensate_unregistered_task_workspace(
    *,
    task: InitializedAnalysisTask,
    tasks_dir: Path,
    original_error: Exception,
) -> AnalysisTaskWorkspaceCompensationError | None:
    try:
        _remove_new_task_workspace_directory(
            task_id=task.task_id,
            task_dir=task.task_dir,
            tasks_dir=tasks_dir,
        )
    except Exception as cleanup_error:
        return AnalysisTaskWorkspaceCompensationError(
            task_id=task.task_id,
            task_dir=task.task_dir,
            original_error=original_error,
            cleanup_error=cleanup_error,
        )
    return None


def _remove_new_task_workspace_directory(*, task_id: str, task_dir: Path, tasks_dir: Path) -> None:
    _resolve_task_dir_relative_path(task_dir=task_dir, tasks_dir=tasks_dir, task_id=task_id)

    if not task_dir.exists():
        return
    if task_dir.is_symlink():
        raise ValueError("task_dir must not be a symlink")
    if not task_dir.is_dir():
        raise ValueError("task_dir must be a directory")

    shutil.rmtree(task_dir)


def _event_level_from_string(level: str) -> EventType:
    try:
        return EventType[level.upper()]
    except KeyError:
        return EventType.INFO
