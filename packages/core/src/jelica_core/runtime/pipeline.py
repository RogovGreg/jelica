from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jelica_core.tasks.storage import compute_config_hash, write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    WorkerLaunchSpec,
    WorkerPipelineControl,
)
from .progress import ProgressReporter

_FULL_PIPELINE_TARGETS = frozenset({"full_analysis", "result_package"})
_TERMINAL_STAGE_BY_ANALYSIS_TARGET = {
    "input_processing": "input_processing",
    "validation": "input_processing",
    "sequence_statistics": "input_processing",
    "alignment": "alignment",
    "comparative_analysis": "comparative_analysis",
    "distance_matrix": "distance_matrix",
    "phylogenetic_tree": "phylogenetic_tree",
    "clade_detection": "clade_detection",
}


@dataclass(frozen=True, slots=True)
class StageContext:
    launch_spec: WorkerLaunchSpec
    stage_index: int
    stage_staging_directory: Path
    event_reporter: StageEventReporter | None = None
    control_check: ControlCheck | None = None
    external_process_pid_state: Any | None = None

    def emit_event(self, event_name: str, context: dict[str, object] | None = None) -> None:
        if self.event_reporter is None:
            return
        self.event_reporter(event_name, context or {})

    def check_control(self) -> None:
        if self.control_check is None:
            return
        self.control_check()

    def register_external_process(self, process_id: int) -> None:
        if self.external_process_pid_state is None:
            return
        with self.external_process_pid_state.get_lock():
            self.external_process_pid_state.value = process_id

    def unregister_external_process(self, process_id: int) -> None:
        if self.external_process_pid_state is None:
            return
        with self.external_process_pid_state.get_lock():
            if self.external_process_pid_state.value == process_id:
                self.external_process_pid_state.value = 0


@dataclass(frozen=True, slots=True)
class StageFailure:
    reason: str
    detail: str
    failure_event_name: str | None = None
    failure_context: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class StageRunResult:
    artifacts: tuple[str, ...]
    failure: StageFailure | None = None
    check_control_before_commit: bool = False


class PipelineStage(Protocol):
    @property
    def stage_id(self) -> str: ...

    @property
    def weight(self) -> float: ...

    def preflight(self, context: StageContext) -> None: ...

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult: ...


class StageEventReporter(Protocol):
    def __call__(self, event_name: str, context: dict[str, object]) -> None: ...


class ControlCheck(Protocol):
    def __call__(self) -> None: ...


class EventLike(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class SemaphoreLike(Protocol):
    def acquire(self, block: bool = True, timeout: float | None = None) -> bool: ...

    def release(self) -> None: ...


class QueueLike(Protocol):
    def put(self, value: object) -> None: ...


class BarrierLike(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True, slots=True)
class PipelineDefinition:
    name: str
    version: str
    stages: tuple[PipelineStage, ...]

    @property
    def total_weight(self) -> float:
        return sum(stage.weight for stage in self.stages)


@dataclass(frozen=True, slots=True)
class InitializeJobStage:
    stage_id: str = "initialize_job"
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        if not context.launch_spec.task_dir.is_dir():
            raise RuntimeError(f"task workspace is missing: '{context.launch_spec.task_dir}'")
        if not context.launch_spec.config_revision_path.is_file():
            raise RuntimeError(
                "immutable config revision is missing: "
                f"'{context.launch_spec.config_revision_path}'"
            )
        context.launch_spec.job_dir.mkdir(parents=True, exist_ok=True)
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        config_document = _read_json_object(context.launch_spec.config_revision_path)
        config_hash = compute_config_hash(config_document)
        if config_hash != context.launch_spec.config_hash:
            raise RuntimeError(
                "config hash mismatch for immutable revision: "
                f"expected '{context.launch_spec.config_hash}', got '{config_hash}'"
            )

        execution_manifest_path = context.stage_staging_directory / "execution_manifest.json"
        execution_manifest = {
            "task_id": context.launch_spec.task_id,
            "trace_id": context.launch_spec.trace_id,
            "job_id": context.launch_spec.job_id,
            "worker_instance_id": context.launch_spec.worker_instance_id,
            "pipeline_version": context.launch_spec.pipeline_version,
            "config_revision_path": str(context.launch_spec.config_revision_path),
            "config_hash": context.launch_spec.config_hash,
            "generated_at": serialize_utc_datetime(utc_now()),
        }
        write_text_atomically(
            path=execution_manifest_path,
            payload=json.dumps(
                execution_manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        progress_reporter(1.0)
        return StageRunResult(artifacts=("execution_manifest.json",))


@dataclass(frozen=True, slots=True)
class QuickSuccessStage:
    stage_id: str = "quick_success"
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        marker_path = context.stage_staging_directory / "quick_success.json"
        payload = {
            "task_id": context.launch_spec.task_id,
            "job_id": context.launch_spec.job_id,
            "stage_id": self.stage_id,
        }
        write_text_atomically(
            path=marker_path,
            payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        progress_reporter(1.0)
        return StageRunResult(artifacts=("quick_success.json",))


@dataclass(frozen=True, slots=True)
class SlowProgressStage:
    stage_id: str = "slow_progress"
    weight: float = 1.0
    steps: int = 5
    step_delay_seconds: float = 0.05

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        step_count = max(self.steps, 1)
        for index in range(step_count):
            time.sleep(self.step_delay_seconds)
            progress_reporter((index + 1) / step_count)
        marker_path = context.stage_staging_directory / "slow_progress.json"
        payload = {"steps": step_count, "job_id": context.launch_spec.job_id}
        write_text_atomically(
            path=marker_path,
            payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return StageRunResult(artifacts=("slow_progress.json",))


@dataclass(frozen=True, slots=True)
class RaisesErrorStage:
    stage_id: str = "raises_error"
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        raise RuntimeError("test stage raises an exception")


@dataclass(frozen=True, slots=True)
class AbruptExitStage:
    stage_id: str = "abrupt_exit"
    weight: float = 1.0
    exit_code: int = 93

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        os._exit(self.exit_code)


@dataclass(frozen=True, slots=True)
class ControlledSlowStage:
    stage_id: str = "controlled_slow"
    weight: float = 1.0
    release_timeout_seconds: float = 10.0

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        control = context.launch_spec.pipeline_control
        if control is not None:
            _signal_stage_started(control, job_id=context.launch_spec.job_id)
            _wait_stage_barrier(control)

        progress_reporter(0.5)
        if control is not None:
            _wait_for_stage_release(
                control,
                timeout=self.release_timeout_seconds,
                job_id=context.launch_spec.job_id,
            )

        marker_path = context.stage_staging_directory / "controlled_slow.json"
        payload = {"job_id": context.launch_spec.job_id, "stage_id": self.stage_id}
        write_text_atomically(
            path=marker_path,
            payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        progress_reporter(1.0)
        return StageRunResult(artifacts=("controlled_slow.json",))


def build_pipeline_definition(
    *,
    pipeline_name: str,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
    config_revision_path: Path | None = None,
) -> PipelineDefinition:
    normalized_name = pipeline_name.strip().lower()
    if normalized_name == "":
        raise ValueError("pipeline_name must not be empty")

    if normalized_name == DEFAULT_PIPELINE_NAME:
        from .alignment_stage import AlignmentStage
        from .clade_detection_stage import CladeDetectionStage
        from .comparative_analysis_stage import ComparativeAnalysisStage
        from .distance_matrix_stage import DistanceMatrixStage
        from .input_acquisition import InputAcquisitionStage
        from .input_processing_stage import InputProcessingStage
        from .phylogenetic_tree_stage import PhylogeneticTreeStage
        from .result_package_stage import ResultPackageStage

        stages = _pipeline_stages(
            InitializeJobStage(),
            InputAcquisitionStage(),
            InputProcessingStage(),
            AlignmentStage(),
            ComparativeAnalysisStage(),
            DistanceMatrixStage(),
            PhylogeneticTreeStage(),
            CladeDetectionStage(),
            ResultPackageStage(),
        )
        stages = _select_stages_for_analysis_target(
            stages=stages,
            target=_read_analysis_target(config_revision_path=config_revision_path),
        )
    elif normalized_name == "quick_success":
        stages = _pipeline_stages(InitializeJobStage(), QuickSuccessStage())
    elif normalized_name == "slow_progress":
        stages = _pipeline_stages(InitializeJobStage(), SlowProgressStage())
    elif normalized_name == "raises_error":
        stages = _pipeline_stages(InitializeJobStage(), RaisesErrorStage())
    elif normalized_name == "abrupt_exit":
        stages = _pipeline_stages(InitializeJobStage(), AbruptExitStage())
    elif normalized_name == "multi_stage":
        stages = _pipeline_stages(
            InitializeJobStage(),
            SlowProgressStage(stage_id="stage_a"),
            QuickSuccessStage(),
        )
    elif normalized_name == "test_controlled_multi_stage":
        stages = _pipeline_stages(
            InitializeJobStage(),
            ControlledSlowStage(),
            QuickSuccessStage(stage_id="finalize"),
        )
    else:
        raise ValueError(f"unsupported pipeline_name '{pipeline_name}'")

    return PipelineDefinition(
        name=normalized_name,
        version=pipeline_version,
        stages=stages,
    )


def _read_analysis_target(*, config_revision_path: Path | None) -> str | None:
    if config_revision_path is None:
        return None
    config_document = _read_json_object(config_revision_path)
    execution = config_document.get("execution")
    if execution is None:
        return None
    if not isinstance(execution, dict):
        raise ValueError("analysis config execution must be a JSON object")
    from_phase = execution.get("from_phase")
    if from_phase is not None and not isinstance(from_phase, str):
        raise ValueError("analysis config execution.from_phase must be a string")
    target = execution.get("target")
    if target is None:
        return None
    if not isinstance(target, str):
        raise ValueError("analysis config execution.target must be a string")
    if target == "":
        raise ValueError("analysis config execution.target must not be empty")
    return target


def analysis_target_terminal_stage(target: str) -> str:
    """Map one exact public analysis target to its terminal runtime stage."""

    if target in _FULL_PIPELINE_TARGETS:
        return "result_package"
    terminal_stage_id = _TERMINAL_STAGE_BY_ANALYSIS_TARGET.get(target)
    if terminal_stage_id is None:
        raise ValueError(f"unsupported analysis execution target '{target}'")
    return terminal_stage_id


def _select_stages_for_analysis_target(
    *,
    stages: tuple[PipelineStage, ...],
    target: str | None,
) -> tuple[PipelineStage, ...]:
    if target is None:
        return stages

    terminal_stage_id = analysis_target_terminal_stage(target)
    if terminal_stage_id == "result_package":
        return stages

    terminal_index = next(
        (index for index, stage in enumerate(stages) if stage.stage_id == terminal_stage_id),
        None,
    )
    if terminal_index is None:
        raise ValueError(
            f"analysis execution target '{target}' is unavailable in the selected pipeline"
        )
    result_package_stage = next(
        (stage for stage in stages if stage.stage_id == "result_package"),
        None,
    )
    if result_package_stage is None:
        raise ValueError("analysis pipeline does not include result_package")
    return (*stages[: terminal_index + 1], result_package_stage)


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read JSON config '{path}': {error}") from error
    if not isinstance(loaded, dict):
        raise RuntimeError(f"config '{path}' must be a JSON object")
    return {str(key): value for key, value in loaded.items()}


def _pipeline_stages(*stages: PipelineStage) -> tuple[PipelineStage, ...]:
    return stages


def _signal_stage_started(control: WorkerPipelineControl, *, job_id: str) -> None:
    if control.stage_started_event is not None:
        event = control.stage_started_event
        event_like: EventLike = event
        event_like.set()
    if control.stage_started_semaphore is not None:
        semaphore = control.stage_started_semaphore
        semaphore_like: SemaphoreLike = semaphore
        semaphore_like.release()
    if control.stage_started_queue is not None:
        started_queue = control.stage_started_queue
        queue_like: QueueLike = started_queue
        queue_like.put(job_id)


def _wait_stage_barrier(control: WorkerPipelineControl) -> None:
    if control.stage_barrier is None:
        return
    barrier = control.stage_barrier
    barrier_like: BarrierLike = barrier
    barrier_like.wait(timeout=10.0)


def _wait_for_stage_release(
    control: WorkerPipelineControl,
    *,
    timeout: float,
    job_id: str,
) -> None:
    release_event = control.stage_release_event
    release_semaphore = control.stage_release_semaphore
    release_semaphores_by_job_id = control.stage_release_semaphores_by_job_id
    configured_gate_count = sum(
        gate is not None
        for gate in (
            release_event,
            release_semaphore,
            release_semaphores_by_job_id,
        )
    )
    if configured_gate_count > 1:
        raise RuntimeError("controlled slow stage received multiple release gate controls")
    if release_semaphores_by_job_id is not None:
        release_gate = release_semaphores_by_job_id.get(job_id)
        if release_gate is None:
            raise RuntimeError(
                f"controlled slow stage is missing a release gate for job '{job_id}'"
            )
        job_release_semaphore_like: SemaphoreLike = release_gate
        if not job_release_semaphore_like.acquire(timeout=timeout):
            raise RuntimeError(
                "controlled slow stage timed out while waiting for release permit "
                f"for job '{job_id}'"
            )
        return
    if release_semaphore is not None:
        semaphore = release_semaphore
        shared_release_semaphore_like: SemaphoreLike = semaphore
        if not shared_release_semaphore_like.acquire(timeout=timeout):
            raise RuntimeError("controlled slow stage timed out while waiting for release permit")
        return
    if release_event is not None:
        event = release_event
        event_like: EventLike = event
        if not _wait_event(event_like, timeout=timeout):
            raise RuntimeError("controlled slow stage timed out while waiting for release event")


def _wait_event(event: EventLike, *, timeout: float) -> bool:
    return event.wait(timeout=timeout)
