from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import jelica_core.clade_detection as clade_detection_module
import jelica_core.distance_matrix as distance_matrix_module
import jelica_core.phylogenetic_tree as phylogenetic_tree_module
import jelica_core.runtime.result_package_stage as result_package_stage_module
from jelica_core.alignment import (
    ALIGNMENT_MANIFEST_RELATIVE_PATH,
    ALIGNMENT_STAGE_ID,
    AlignmentManifest,
    AlignmentStageOutcome,
)
from jelica_core.comparative_analysis import (
    COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
    COMPARATIVE_ANALYSIS_STAGE_ID,
    ComparativeAnalysisManifest,
    ComparativeAnalysisStatus,
    ComparativeCategoryExecution,
    ComparativeCategoryStatus,
    ComparisonPlanCounts,
)
from jelica_core.result_package import (
    RESULT_PACKAGE_STAGE_ID,
    RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,
    ResultPackageValidationError,
)
from jelica_core.runtime import analysis_target_terminal_stage
from jelica_core.runtime import engine as engine_module
from jelica_core.runtime import worker as worker_module
from jelica_core.runtime.alignment_stage import (
    ALIGNMENT_MAFFT_FAILED_EVENT,
    ALIGNMENT_RESULT_INVALID_EVENT,
    AlignmentStageError,
)
from jelica_core.runtime.artifacts import (
    StageArtifactManifest,
    StageCommitError,
    StageSnapshotErrorCode,
    StageSnapshotValidationError,
    write_stage_manifest,
)
from jelica_core.runtime.engine import RUNTIME_EVENT_JOB_FAILED, ExecutionRuntime
from jelica_core.runtime.input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    INPUT_PROCESSING_STAGE_ID,
    InputProcessingDatasetSummary,
    InputProcessingManifest,
    InputProcessingState,
)
from jelica_core.runtime.messages import (
    JobCompletedMessage,
    JobFailedMessage,
    StageEventMessage,
    StageReadyToCommitMessage,
    StageStartedMessage,
)
from jelica_core.runtime.models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RuntimeStateCheckpoint,
    WorkerLaunchSpec,
)
from jelica_core.runtime.pipeline import PipelineDefinition, StageContext, StageRunResult
from jelica_core.tasks import (
    AnalyticalTaskMutationResult,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskState,
)
from jelica_core.tasks.storage import write_text_atomically


class _Queue:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def put(self, message: object) -> None:
        self.messages.append(message)


class _UnsetEvent:
    def is_set(self) -> bool:
        return False


class _Registry:
    def __init__(self, launch_spec: WorkerLaunchSpec) -> None:
        self._launch_spec = launch_spec

    def get_task(self, *, task_id: str) -> SimpleNamespace:
        assert task_id == self._launch_spec.task_id
        return SimpleNamespace(state=AnalyticalTaskState.RUNNING)

    def get_job(self, *, job_id: str) -> SimpleNamespace:
        assert job_id == self._launch_spec.job_id
        return SimpleNamespace(
            state=AnalyticalTaskState.RUNNING,
            worker_instance_id=self._launch_spec.worker_instance_id,
            lease_token=self._launch_spec.lease_token,
        )


@dataclass
class _NeverRunAlignmentStage:
    stage_id: str = ALIGNMENT_STAGE_ID
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        pytest.fail("a committed alignment stage must be skipped")

    def run(self, context: StageContext, progress_reporter: object) -> StageRunResult:
        pytest.fail("a committed alignment stage must be skipped")


@dataclass
class _NeverRunComparativeAnalysisStage:
    stage_id: str = COMPARATIVE_ANALYSIS_STAGE_ID
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        pytest.fail("a committed comparative-analysis stage must be skipped")

    def run(self, context: StageContext, progress_reporter: object) -> StageRunResult:
        pytest.fail("a committed comparative-analysis stage must be skipped")


@dataclass
class _FailingAlignmentStage:
    reason: str
    event_name: str
    stage_id: str = ALIGNMENT_STAGE_ID
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: object) -> StageRunResult:
        raise AlignmentStageError(
            reason=self.reason,
            detail="Safe alignment stage failure.",
            event_name=self.event_name,
            context={"error_type": self.reason},
        )


@dataclass
class _MarkerStage:
    stage_id: str
    marker: list[str]
    value: str
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: object) -> StageRunResult:
        self.marker.append(self.value)
        return StageRunResult(artifacts=())


def _launch_spec(tmp_path: Path, *, checkpoint: RuntimeStateCheckpoint) -> WorkerLaunchSpec:
    return WorkerLaunchSpec(
        task_id="task-alignment-runtime",
        job_id="job-alignment-runtime",
        worker_instance_id="worker-alignment-runtime",
        lease_token="lease-alignment-runtime",
        database_path=tmp_path / "registry.sqlite3",
        task_dir=tmp_path / "task",
        job_dir=tmp_path / "task" / "jobs" / "job-alignment-runtime",
        config_revision_path=tmp_path / "task" / "config.json",
        config_hash="0" * 64,
        runtime_state_json=checkpoint.to_runtime_state_json(),
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )


def _write_disabled_alignment_snapshot(*, launch_spec: WorkerLaunchSpec) -> None:
    stage_root = launch_spec.job_dir / "stages" / ALIGNMENT_STAGE_ID
    domain_manifest = AlignmentManifest(
        task_id=launch_spec.task_id,
        job_id=launch_spec.job_id,
        config_hash=launch_spec.config_hash,
        mode="none",
        logical_sample_count=0,
        unique_sequence_count=0,
        input_set_sha256="1" * 64,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:00Z",
        duration_seconds=0.0,
        outcome=AlignmentStageOutcome.SKIPPED_DISABLED,
    )
    domain_path = stage_root / ALIGNMENT_MANIFEST_RELATIVE_PATH
    domain_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(path=domain_path, payload=domain_manifest.model_dump_json())
    write_stage_manifest(
        directory=stage_root,
        manifest=StageArtifactManifest(
            stage_id=ALIGNMENT_STAGE_ID,
            job_id=launch_spec.job_id,
            worker_instance_id=launch_spec.worker_instance_id,
            pipeline_version=launch_spec.pipeline_version,
            completed_at="2026-01-01T00:00:00Z",
            artifacts=(ALIGNMENT_MANIFEST_RELATIVE_PATH,),
        ),
    )


def _write_disabled_comparative_snapshot(*, launch_spec: WorkerLaunchSpec) -> None:
    input_root = launch_spec.job_dir / "stages" / INPUT_PROCESSING_STAGE_ID
    input_manifest = InputProcessingManifest(
        task_id=launch_spec.task_id,
        job_id=launch_spec.job_id,
        config_revision_path="configs/000001.json",
        config_hash=launch_spec.config_hash,
        generated_at="2026-01-01T00:00:00Z",
        processing_state=InputProcessingState.COMPLETED,
        dataset_summary=InputProcessingDatasetSummary(
            discovered_record_count=0,
            valid_sample_count=0,
            invalid_sample_count=0,
            unique_sequence_count=0,
            duplicate_logical_sample_count=0,
            comparative_analysis_available=False,
        ),
    )
    input_path = input_root / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    input_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(path=input_path, payload=input_manifest.model_dump_json())
    write_stage_manifest(
        directory=input_root,
        manifest=StageArtifactManifest(
            stage_id=INPUT_PROCESSING_STAGE_ID,
            job_id=launch_spec.job_id,
            worker_instance_id=launch_spec.worker_instance_id,
            pipeline_version=launch_spec.pipeline_version,
            completed_at="2026-01-01T00:00:00Z",
            artifacts=(INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,),
        ),
    )

    stage_root = launch_spec.job_dir / "stages" / COMPARATIVE_ANALYSIS_STAGE_ID
    empty_category = ComparativeCategoryExecution(
        status=ComparativeCategoryStatus.NOT_REQUESTED,
        requested=False,
        total=0,
        completed=0,
        successful=0,
        failed=0,
    )
    domain_manifest = ComparativeAnalysisManifest(
        task_id=launch_spec.task_id,
        job_id=launch_spec.job_id,
        config_hash=launch_spec.config_hash,
        enabled=False,
        skipped_reason="disabled_by_configuration",
        status=ComparativeAnalysisStatus.COMPLETED,
        alignment_mode="none",
        reference_mode="auto",
        uracil_thymine_equivalent=False,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:00Z",
        duration_seconds=0.0,
        plan_counts=ComparisonPlanCounts(
            occurrence_count=0,
            unique_logical_operation_count=0,
            duplicate_occurrence_count=0,
            scan_computation_count=0,
            identical_sequence_projection_count=0,
        ),
        category_execution={
            "statistics": empty_category,
            "reference_sequence_differences": empty_category,
            "pairwise_sequence_differences": empty_category,
        },
        successful_result_count=0,
        failed_result_count=0,
        failure_count=0,
    )
    domain_path = stage_root / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH
    domain_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(path=domain_path, payload=domain_manifest.model_dump_json())
    write_stage_manifest(
        directory=stage_root,
        manifest=StageArtifactManifest(
            stage_id=COMPARATIVE_ANALYSIS_STAGE_ID,
            job_id=launch_spec.job_id,
            worker_instance_id=launch_spec.worker_instance_id,
            pipeline_version=launch_spec.pipeline_version,
            completed_at="2026-01-01T00:00:00Z",
            artifacts=(COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,),
        ),
    )


def _run_worker_with_pipeline(
    *,
    monkeypatch: pytest.MonkeyPatch,
    launch_spec: WorkerLaunchSpec,
    pipeline: PipelineDefinition,
) -> _Queue:
    queue = _Queue()
    registry = _Registry(launch_spec)
    monkeypatch.setattr(worker_module, "build_pipeline_definition", lambda **_kwargs: pipeline)
    monkeypatch.setattr(
        worker_module,
        "AnalyticalTaskRegistryService",
        lambda **_kwargs: registry,
    )
    event = _UnsetEvent()
    worker_module.run_worker_process(
        launch_spec,
        queue,
        event,
        event,
        event,
        event,
    )
    return queue


def test_default_pipeline_places_comparative_analysis_after_alignment() -> None:
    pipeline = worker_module.build_pipeline_definition(
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )

    assert [stage.stage_id for stage in pipeline.stages] == [
        "initialize_job",
        "input_acquisition",
        "input_processing",
        ALIGNMENT_STAGE_ID,
        COMPARATIVE_ANALYSIS_STAGE_ID,
        "distance_matrix",
        "phylogenetic_tree",
        "clade_detection",
        "result_package",
    ]


@pytest.mark.parametrize(
    ("target", "expected_stage_ids"),
    (
        (
            "input_processing",
            ("initialize_job", "input_acquisition", "input_processing", RESULT_PACKAGE_STAGE_ID),
        ),
        (
            "validation",
            ("initialize_job", "input_acquisition", "input_processing", RESULT_PACKAGE_STAGE_ID),
        ),
        (
            "sequence_statistics",
            ("initialize_job", "input_acquisition", "input_processing", RESULT_PACKAGE_STAGE_ID),
        ),
        (
            "alignment",
            (
                "initialize_job",
                "input_acquisition",
                "input_processing",
                ALIGNMENT_STAGE_ID,
                RESULT_PACKAGE_STAGE_ID,
            ),
        ),
    ),
)
def test_analysis_target_selects_runtime_prefix_and_terminal_result_package(
    tmp_path: Path,
    target: str,
    expected_stage_ids: tuple[str, ...],
) -> None:
    config_path = tmp_path / "config.json"
    write_text_atomically(
        path=config_path,
        payload=f'{{"execution":{{"target":"{target}"}}}}\n',
    )

    pipeline = worker_module.build_pipeline_definition(
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        config_revision_path=config_path,
    )

    assert tuple(stage.stage_id for stage in pipeline.stages) == expected_stage_ids
    assert analysis_target_terminal_stage("full_analysis") == RESULT_PACKAGE_STAGE_ID
    with pytest.raises(ValueError, match="unsupported analysis execution target 'full'"):
        analysis_target_terminal_stage("full")


def test_result_package_loads_only_targeted_committed_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION)
    launch_spec = _launch_spec(tmp_path, checkpoint=checkpoint)
    launch_spec.config_revision_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(
        path=launch_spec.config_revision_path,
        payload='{"execution":{"target":"alignment"}}\n',
    )
    validated_stage_ids: list[str] = []

    def _validate_snapshot(**kwargs: object) -> SimpleNamespace:
        validated_stage_ids.append(str(kwargs["stage_id"]))
        return SimpleNamespace()

    monkeypatch.setattr(
        result_package_stage_module,
        "validate_committed_stage_snapshot",
        _validate_snapshot,
    )
    context = StageContext(
        launch_spec=launch_spec,
        stage_index=4,
        stage_staging_directory=launch_spec.job_dir / "staging" / RESULT_PACKAGE_STAGE_ID,
    )

    committed = result_package_stage_module._load_committed_stages(context=context)

    assert [item.stage_id for item in committed] == [
        "initialize_job",
        "input_acquisition",
        "input_processing",
        ALIGNMENT_STAGE_ID,
    ]
    assert validated_stage_ids == [item.stage_id for item in committed]


def test_runtime_allows_explicit_from_phase_in_pipeline_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    write_text_atomically(
        path=config_path,
        payload=('{"execution":{"from_phase":"alignment","target":"full_analysis"}}\n'),
    )

    pipeline = worker_module.build_pipeline_definition(
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        config_revision_path=config_path,
    )

    assert tuple(stage.stage_id for stage in pipeline.stages)[0] == "initialize_job"
    assert tuple(stage.stage_id for stage in pipeline.stages)[-1] == RESULT_PACKAGE_STAGE_ID


def test_worker_skips_atomically_committed_alignment_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = RuntimeStateCheckpoint.new(
        pipeline_version=DEFAULT_PIPELINE_VERSION
    ).with_committed_stage(
        stage_id=ALIGNMENT_STAGE_ID,
        artifacts=(ALIGNMENT_MANIFEST_RELATIVE_PATH,),
    )
    launch_spec = _launch_spec(tmp_path, checkpoint=checkpoint)
    _write_disabled_alignment_snapshot(launch_spec=launch_spec)
    pipeline = PipelineDefinition(
        name=DEFAULT_PIPELINE_NAME,
        version=DEFAULT_PIPELINE_VERSION,
        stages=(_NeverRunAlignmentStage(),),
    )

    queue = _run_worker_with_pipeline(
        monkeypatch=monkeypatch,
        launch_spec=launch_spec,
        pipeline=pipeline,
    )

    assert any(isinstance(message, JobCompletedMessage) for message in queue.messages)
    assert not any(isinstance(message, StageStartedMessage) for message in queue.messages)


def test_worker_skips_atomically_committed_comparative_analysis_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = RuntimeStateCheckpoint.new(
        pipeline_version=DEFAULT_PIPELINE_VERSION
    ).with_committed_stage(
        stage_id=COMPARATIVE_ANALYSIS_STAGE_ID,
        artifacts=(COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,),
    )
    launch_spec = _launch_spec(tmp_path, checkpoint=checkpoint)
    _write_disabled_comparative_snapshot(launch_spec=launch_spec)
    pipeline = PipelineDefinition(
        name=DEFAULT_PIPELINE_NAME,
        version=DEFAULT_PIPELINE_VERSION,
        stages=(_NeverRunComparativeAnalysisStage(),),
    )

    queue = _run_worker_with_pipeline(
        monkeypatch=monkeypatch,
        launch_spec=launch_spec,
        pipeline=pipeline,
    )

    assert any(isinstance(message, JobCompletedMessage) for message in queue.messages)
    assert not any(isinstance(message, StageStartedMessage) for message in queue.messages)


def test_worker_rejects_corrupted_checkpoint_committed_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = RuntimeStateCheckpoint.new(
        pipeline_version=DEFAULT_PIPELINE_VERSION
    ).with_committed_stage(
        stage_id=ALIGNMENT_STAGE_ID,
        artifacts=(ALIGNMENT_MANIFEST_RELATIVE_PATH,),
    )
    launch_spec = _launch_spec(tmp_path, checkpoint=checkpoint)
    pipeline = PipelineDefinition(
        name=DEFAULT_PIPELINE_NAME,
        version=DEFAULT_PIPELINE_VERSION,
        stages=(_NeverRunAlignmentStage(),),
    )

    queue = _run_worker_with_pipeline(
        monkeypatch=monkeypatch,
        launch_spec=launch_spec,
        pipeline=pipeline,
    )

    failure = next(message for message in queue.messages if isinstance(message, JobFailedMessage))
    assert failure.reason == StageSnapshotErrorCode.INVALID.value
    assert failure.failure_context == {
        "error_code": StageSnapshotErrorCode.INVALID.value,
        "stage_id": ALIGNMENT_STAGE_ID,
    }
    assert not any(isinstance(message, StageStartedMessage) for message in queue.messages)
    assert not any(isinstance(message, JobCompletedMessage) for message in queue.messages)


@pytest.mark.parametrize(
    ("reason", "event_name"),
    [
        ("mafft_not_found", ALIGNMENT_MAFFT_FAILED_EVENT),
        ("mafft_version_probe_failed", ALIGNMENT_MAFFT_FAILED_EVENT),
        ("mafft_launch_failed", ALIGNMENT_MAFFT_FAILED_EVENT),
        ("mafft_nonzero_exit", ALIGNMENT_MAFFT_FAILED_EVENT),
        ("mafft_empty_output", ALIGNMENT_MAFFT_FAILED_EVENT),
        ("alignment_result_invalid", ALIGNMENT_RESULT_INVALID_EVENT),
        ("alignment_reference_missing", ALIGNMENT_RESULT_INVALID_EVENT),
        ("alignment_prealigned_mismatch", ALIGNMENT_RESULT_INVALID_EVENT),
    ],
)
def test_worker_preserves_safe_alignment_failure_categories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    event_name: str,
) -> None:
    checkpoint = RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION)
    launch_spec = _launch_spec(tmp_path, checkpoint=checkpoint)
    pipeline = PipelineDefinition(
        name=DEFAULT_PIPELINE_NAME,
        version=DEFAULT_PIPELINE_VERSION,
        stages=(_FailingAlignmentStage(reason=reason, event_name=event_name),),
    )

    queue = _run_worker_with_pipeline(
        monkeypatch=monkeypatch,
        launch_spec=launch_spec,
        pipeline=pipeline,
    )

    failure = next(message for message in queue.messages if isinstance(message, JobFailedMessage))
    stage_event = next(
        message for message in queue.messages if isinstance(message, StageEventMessage)
    )
    assert failure.reason == reason
    assert failure.failure_event_name == event_name
    assert failure.failure_context == {
        "detail": "Safe alignment stage failure.",
        "error_type": reason,
    }
    assert stage_event.event_name == event_name
    assert stage_event.context == failure.failure_context


def test_worker_waits_for_stage_commit_before_starting_next_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION)
    launch_spec = _launch_spec(tmp_path, checkpoint=checkpoint)
    stage_progression: list[str] = []
    first_stage_id = "phylogenetic_tree"
    second_stage_id = "clade_detection"
    pipeline = PipelineDefinition(
        name=DEFAULT_PIPELINE_NAME,
        version=DEFAULT_PIPELINE_VERSION,
        stages=(
            _MarkerStage(
                stage_id=first_stage_id,
                marker=stage_progression,
                value="first",
            ),
            _MarkerStage(
                stage_id=second_stage_id,
                marker=stage_progression,
                value="second",
            ),
        ),
    )
    first_stage_validation_attempts = 0

    def _validate_committed_snapshot(*, stage_id: str, **_kwargs: object) -> object:
        nonlocal first_stage_validation_attempts
        if stage_id == first_stage_id:
            first_stage_validation_attempts += 1
            if first_stage_validation_attempts == 1:
                raise StageSnapshotValidationError(
                    code=StageSnapshotErrorCode.INVALID,
                    stage_id=first_stage_id,
                    detail="pending commit",
                )
        return SimpleNamespace()

    monkeypatch.setattr(
        worker_module,
        "validate_committed_stage_snapshot",
        _validate_committed_snapshot,
    )
    monkeypatch.setattr(worker_module, "_STAGE_COMMIT_BARRIER_POLL_SECONDS", 0.0)

    queue = _run_worker_with_pipeline(
        monkeypatch=monkeypatch,
        launch_spec=launch_spec,
        pipeline=pipeline,
    )

    assert first_stage_validation_attempts == 2
    assert stage_progression == ["first", "second"]
    started_stage_ids = [
        message.stage_id for message in queue.messages if isinstance(message, StageStartedMessage)
    ]
    assert started_stage_ids == [first_stage_id, second_stage_id]
    assert any(isinstance(message, JobCompletedMessage) for message in queue.messages)


def test_alignment_publication_events_are_emitted_only_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = False
    emitted: list[tuple[str, dict[str, object] | None]] = []
    manifest = StageArtifactManifest(
        stage_id=ALIGNMENT_STAGE_ID,
        job_id="job-alignment-runtime",
        worker_instance_id="worker-alignment-runtime",
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        completed_at="2026-01-01T00:00:00Z",
        artifacts=(ALIGNMENT_MANIFEST_RELATIVE_PATH,),
    )

    def _commit(**_kwargs: object) -> StageArtifactManifest:
        nonlocal committed
        committed = True
        return manifest

    monkeypatch.setattr(engine_module, "commit_stage_directory", _commit)
    checkpoint = RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION)
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            task_id="task-alignment-runtime",
            job_id="job-alignment-runtime",
            worker_instance_id="worker-alignment-runtime",
            checkpoint=checkpoint,
            pipeline_definition=PipelineDefinition(
                name=DEFAULT_PIPELINE_NAME,
                version=DEFAULT_PIPELINE_VERSION,
                stages=(),
            ),
            job_dir=tmp_path / "job",
            current_stage=ALIGNMENT_STAGE_ID,
            current_stage_progress=1.0,
        ),
    )

    class _RuntimeHarness:
        def _persist_progress(self, **_kwargs: object) -> None:
            assert committed

        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            assert committed
            emitted.append((event_name, context))

        def _mark_job_failed(self, **_kwargs: object) -> None:
            pytest.fail("commit must not fail")

    message = StageReadyToCommitMessage(
        task_id=handle.task_id,
        job_id=handle.job_id,
        worker_instance_id=handle.worker_instance_id,
        lease_token="lease-alignment-runtime",
        stage_id=ALIGNMENT_STAGE_ID,
        staging_directory=str(tmp_path / "staging"),
        manifest_path=str(tmp_path / "staging" / "stage_manifest.json"),
    )

    ExecutionRuntime._handle_stage_ready_to_commit(
        _RuntimeHarness(),  # type: ignore[arg-type]
        handle=handle,
        message=message,
    )

    event_names = [name for name, _context in emitted]
    assert event_names[-2:] == ["ALIGNMENT_RESULT_PUBLISHED", "ALIGNMENT_COMPLETED"]
    for _name, context in emitted[-2:]:
        assert context is not None
        assert context["manifest_path"] == ALIGNMENT_MANIFEST_RELATIVE_PATH


def test_comparative_publication_and_status_events_are_emitted_only_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = False
    emitted: list[tuple[str, dict[str, object] | None]] = []
    job_dir = tmp_path / "job"
    domain_manifest = ComparativeAnalysisManifest(
        task_id="task-alignment-runtime",
        job_id="job-alignment-runtime",
        config_hash="0" * 64,
        enabled=True,
        normalized_settings={
            "enabled": True,
            "statistics": {"enabled": True},
            "reference": {"mode": "disabled"},
        },
        status=ComparativeAnalysisStatus.PARTIAL_SUCCESS,
        alignment_mode="none",
        reference_mode="disabled",
        uracil_thymine_equivalent=False,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        duration_seconds=1.0,
        plan_counts=ComparisonPlanCounts(
            occurrence_count=0,
            unique_logical_operation_count=0,
            duplicate_occurrence_count=0,
            scan_computation_count=0,
            identical_sequence_projection_count=0,
        ),
        category_execution={
            "statistics": {
                "status": "partial_success",
                "requested": True,
                "total": 2,
                "completed": 2,
                "successful": 1,
                "failed": 1,
                "available": True,
            },
            "reference_sequence_differences": {
                "status": "not_requested",
                "requested": False,
                "total": 0,
                "completed": 0,
                "successful": 0,
                "failed": 0,
            },
            "pairwise_sequence_differences": {
                "status": "not_requested",
                "requested": False,
                "total": 0,
                "completed": 0,
                "successful": 0,
                "failed": 0,
            },
        },
        successful_result_count=1,
        failed_result_count=1,
        failure_count=1,
    )
    domain_path = (
        job_dir
        / "stages"
        / COMPARATIVE_ANALYSIS_STAGE_ID
        / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH
    )
    domain_path.parent.mkdir(parents=True)
    write_text_atomically(
        path=domain_path,
        payload=domain_manifest.model_dump_json(),
    )
    generic_manifest = StageArtifactManifest(
        stage_id=COMPARATIVE_ANALYSIS_STAGE_ID,
        job_id="job-alignment-runtime",
        worker_instance_id="worker-alignment-runtime",
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        completed_at="2026-01-01T00:00:01Z",
        artifacts=(COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,),
    )

    def _commit(**_kwargs: object) -> StageArtifactManifest:
        nonlocal committed
        committed = True
        return generic_manifest

    monkeypatch.setattr(engine_module, "commit_stage_directory", _commit)
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            task_id="task-alignment-runtime",
            job_id="job-alignment-runtime",
            worker_instance_id="worker-alignment-runtime",
            checkpoint=RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION),
            pipeline_definition=PipelineDefinition(
                name=DEFAULT_PIPELINE_NAME,
                version=DEFAULT_PIPELINE_VERSION,
                stages=(),
            ),
            job_dir=job_dir,
            current_stage=COMPARATIVE_ANALYSIS_STAGE_ID,
            current_stage_progress=1.0,
        ),
    )

    class _RuntimeHarness:
        def _persist_progress(self, **_kwargs: object) -> None:
            assert committed

        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            assert committed
            emitted.append((event_name, context))

        def _mark_job_failed(self, **_kwargs: object) -> None:
            pytest.fail("valid comparative manifest must not fail publication")

    message = StageReadyToCommitMessage(
        task_id=handle.task_id,
        job_id=handle.job_id,
        worker_instance_id=handle.worker_instance_id,
        lease_token="lease-alignment-runtime",
        stage_id=COMPARATIVE_ANALYSIS_STAGE_ID,
        staging_directory=str(tmp_path / "staging"),
        manifest_path=str(tmp_path / "staging" / "stage_manifest.json"),
    )

    ExecutionRuntime._handle_stage_ready_to_commit(
        _RuntimeHarness(),  # type: ignore[arg-type]
        handle=handle,
        message=message,
    )

    assert [name for name, _context in emitted][-2:] == [
        "COMPARATIVE_ANALYSIS_RESULT_PUBLISHED",
        "COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS",
    ]


_STAGE_PUBLICATION_CASES = (
    (
        distance_matrix_module.DISTANCE_MATRIX_STAGE_ID,
        distance_matrix_module.DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
        "DISTANCE_MATRIX_RESULT_PUBLISHED",
        "DISTANCE_MATRIX_COMPLETED",
    ),
    (
        phylogenetic_tree_module.PHYLOGENETIC_TREE_STAGE_ID,
        phylogenetic_tree_module.PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
        "PHYLOGENETIC_TREE_RESULT_PUBLISHED",
        "PHYLOGENETIC_TREE_COMPLETED",
    ),
    (
        clade_detection_module.CLADE_DETECTION_STAGE_ID,
        clade_detection_module.CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
        "CLADE_DETECTION_RESULT_PUBLISHED",
        "CLADE_DETECTION_COMPLETED",
    ),
)


def _patch_stage_manifest_validator(
    *,
    monkeypatch: pytest.MonkeyPatch,
    stage_id: str,
) -> None:
    if stage_id == distance_matrix_module.DISTANCE_MATRIX_STAGE_ID:
        stub_manifest = SimpleNamespace(
            status=distance_matrix_module.DistanceMatrixStatus.COMPLETED,
            enabled=True,
            unique_sequence_count=3,
            expected_pair_count=3,
            defined_distance_count=3,
            undefined_distance_count=0,
        )

        def _validate_distance_manifest(_cls: object, _payload: str) -> object:
            return stub_manifest

        monkeypatch.setattr(
            distance_matrix_module.DistanceMatrixManifest,
            "model_validate_json",
            classmethod(_validate_distance_manifest),
        )
        return
    if stage_id == phylogenetic_tree_module.PHYLOGENETIC_TREE_STAGE_ID:
        stub_manifest = SimpleNamespace(
            status=phylogenetic_tree_module.PhylogeneticTreeStatus.COMPLETED,
            enabled=True,
            leaf_count=3,
            internal_node_count=1,
            edge_count=4,
            construction_mode=SimpleNamespace(value="neighbor_joining"),
            inference_performed=True,
            applied_rooting="midpoint",
            zero_diameter=False,
        )

        def _validate_tree_manifest(_cls: object, _payload: str) -> object:
            return stub_manifest

        monkeypatch.setattr(
            phylogenetic_tree_module.PhylogeneticTreeManifest,
            "model_validate_json",
            classmethod(_validate_tree_manifest),
        )
        return
    stub_manifest = SimpleNamespace(
        status=clade_detection_module.CladeDetectionStatus.COMPLETED,
        enabled=True,
        method=SimpleNamespace(value="max_pairwise_distance"),
        max_within_clade_distance=0.2,
        leaf_count=3,
        clade_count=2,
        singleton_clade_count=1,
        multi_leaf_clade_count=1,
    )

    def _validate_clade_manifest(_cls: object, _payload: str) -> object:
        return stub_manifest

    monkeypatch.setattr(
        clade_detection_module.CladeDetectionManifest,
        "model_validate_json",
        classmethod(_validate_clade_manifest),
    )


@pytest.mark.parametrize(
    ("stage_id", "manifest_relative_path", "result_event", "completed_event"),
    _STAGE_PUBLICATION_CASES,
)
def test_post_commit_distance_tree_clade_events_are_unique_and_ordered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_id: str,
    manifest_relative_path: str,
    result_event: str,
    completed_event: str,
) -> None:
    committed = False
    emitted: list[tuple[str, dict[str, object] | None]] = []
    job_dir = tmp_path / "job"
    domain_manifest_path = job_dir / "stages" / stage_id / manifest_relative_path
    domain_manifest_path.parent.mkdir(parents=True)
    write_text_atomically(path=domain_manifest_path, payload="{}")
    _patch_stage_manifest_validator(monkeypatch=monkeypatch, stage_id=stage_id)

    generic_manifest = StageArtifactManifest(
        stage_id=stage_id,
        job_id="job-commit-order",
        worker_instance_id="worker-commit-order",
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        completed_at="2026-01-01T00:00:01Z",
        artifacts=(manifest_relative_path,),
    )

    def _commit(**_kwargs: object) -> StageArtifactManifest:
        nonlocal committed
        committed = True
        return generic_manifest

    monkeypatch.setattr(engine_module, "commit_stage_directory", _commit)
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            task_id="task-commit-order",
            job_id="job-commit-order",
            worker_instance_id="worker-commit-order",
            checkpoint=RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION),
            pipeline_definition=PipelineDefinition(
                name=DEFAULT_PIPELINE_NAME,
                version=DEFAULT_PIPELINE_VERSION,
                stages=(),
            ),
            job_dir=job_dir,
            current_stage=stage_id,
            current_stage_progress=1.0,
        ),
    )

    class _RuntimeHarness:
        def _persist_progress(self, **_kwargs: object) -> None:
            assert committed

        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            assert committed
            emitted.append((event_name, context))

        def _mark_job_failed(self, **_kwargs: object) -> None:
            pytest.fail("commit must not fail")

    message = StageReadyToCommitMessage(
        task_id=handle.task_id,
        job_id=handle.job_id,
        worker_instance_id=handle.worker_instance_id,
        lease_token="lease-commit-order",
        stage_id=stage_id,
        staging_directory=str(tmp_path / "staging"),
        manifest_path=str(tmp_path / "staging" / "stage_manifest.json"),
    )

    ExecutionRuntime._handle_stage_ready_to_commit(
        _RuntimeHarness(),  # type: ignore[arg-type]
        handle=handle,
        message=message,
    )

    event_names = [name for name, _context in emitted]
    assert event_names.count(engine_module.RUNTIME_EVENT_STAGE_COMMITTED) == 1
    assert event_names.count(result_event) == 1
    assert event_names.count(completed_event) == 1
    committed_index = event_names.index(engine_module.RUNTIME_EVENT_STAGE_COMMITTED)
    result_index = event_names.index(result_event)
    completed_index = event_names.index(completed_event)
    assert committed_index < result_index < completed_index


@pytest.mark.parametrize(
    ("stage_id", "result_event", "completed_event"),
    tuple((case[0], case[2], case[3]) for case in _STAGE_PUBLICATION_CASES),
)
def test_commit_failure_does_not_emit_distance_tree_clade_publication_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_id: str,
    result_event: str,
    completed_event: str,
) -> None:
    emitted: list[tuple[str, dict[str, object] | None]] = []
    failures: list[dict[str, object]] = []

    def _commit_failure(**_kwargs: object) -> StageArtifactManifest:
        raise StageCommitError("commit failed", code="TEST_COMMIT_FAILURE", stage_id=stage_id)

    monkeypatch.setattr(engine_module, "commit_stage_directory", _commit_failure)
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            task_id="task-commit-failure",
            job_id="job-commit-failure",
            worker_instance_id="worker-commit-failure",
            checkpoint=RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION),
            pipeline_definition=PipelineDefinition(
                name=DEFAULT_PIPELINE_NAME,
                version=DEFAULT_PIPELINE_VERSION,
                stages=(),
            ),
            job_dir=tmp_path / "job",
            current_stage=stage_id,
            current_stage_progress=1.0,
        ),
    )

    class _RuntimeHarness:
        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            emitted.append((event_name, context))

        def _mark_job_failed(self, **kwargs: object) -> None:
            failures.append(cast(dict[str, object], kwargs))

    message = StageReadyToCommitMessage(
        task_id=handle.task_id,
        job_id=handle.job_id,
        worker_instance_id=handle.worker_instance_id,
        lease_token="lease-commit-failure",
        stage_id=stage_id,
        staging_directory=str(tmp_path / "staging"),
        manifest_path=str(tmp_path / "staging" / "stage_manifest.json"),
    )

    ExecutionRuntime._handle_stage_ready_to_commit(
        _RuntimeHarness(),  # type: ignore[arg-type]
        handle=handle,
        message=message,
    )

    event_names = [name for name, _context in emitted]
    assert engine_module.RUNTIME_EVENT_STAGE_COMMITTED not in event_names
    assert result_event not in event_names
    assert completed_event not in event_names
    assert len(failures) == 1
    assert failures[0]["reason"] == "stage_commit_error"


def test_result_package_publication_is_completed_before_stage_committed_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = False
    emitted: list[str] = []
    calls: list[str] = []
    task_dir = tmp_path / "tasks" / "task-commit-order"
    job_dir = task_dir / "jobs" / "job-commit-order"
    job_dir.mkdir(parents=True)
    published_path = tmp_path / "result_packages" / ("a" * 64 + ".jelica")
    published_path.parent.mkdir(parents=True, exist_ok=True)
    published_path.write_bytes(b"ok")
    generic_manifest = StageArtifactManifest(
        stage_id=RESULT_PACKAGE_STAGE_ID,
        job_id="job-commit-order",
        worker_instance_id="worker-commit-order",
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        completed_at="2026-01-01T00:00:01Z",
        artifacts=(RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,),
    )
    stage_manifest = SimpleNamespace(
        prepared_package_relative_path=".result_package_prepared/" + ("a" * 64) + ".jelica",
        content_id="sha256:" + ("a" * 64),
        published_package_relative_path="../../result_packages/" + ("a" * 64) + ".jelica",
        format_version="1.0",
    )

    def _commit(**_kwargs: object) -> StageArtifactManifest:
        nonlocal committed
        committed = True
        return generic_manifest

    def _load_stage_manifest(*, path: Path) -> SimpleNamespace:
        calls.append("load_stage_manifest")
        expected_path = (
            job_dir
            / "stages"
            / RESULT_PACKAGE_STAGE_ID
            / RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH
        )
        assert path == expected_path
        return stage_manifest

    def _publish_prepared(**_kwargs: object) -> Path:
        calls.append("publish_prepared")
        return published_path

    def _write_link(**_kwargs: object) -> Path:
        calls.append("write_link")
        return task_dir / "result_package.json"

    monkeypatch.setattr(engine_module, "commit_stage_directory", _commit)
    monkeypatch.setattr(engine_module, "load_result_package_stage_manifest", _load_stage_manifest)
    monkeypatch.setattr(engine_module, "publish_prepared_result_package", _publish_prepared)
    monkeypatch.setattr(engine_module, "write_result_package_link", _write_link)
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            task_id="task-commit-order",
            job_id="job-commit-order",
            worker_instance_id="worker-commit-order",
            checkpoint=RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION),
            pipeline_definition=PipelineDefinition(
                name=DEFAULT_PIPELINE_NAME,
                version=DEFAULT_PIPELINE_VERSION,
                stages=(),
            ),
            job_dir=job_dir,
            current_stage=RESULT_PACKAGE_STAGE_ID,
            current_stage_progress=1.0,
        ),
    )

    class _RuntimeHarness:
        def _persist_progress(self, **_kwargs: object) -> None:
            assert committed
            calls.append("persist_progress")

        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            assert context is not None
            emitted.append(event_name)
            calls.append(f"emit:{event_name}")

        def _mark_job_failed(self, **_kwargs: object) -> None:
            pytest.fail("result-package publication must not fail")

    message = StageReadyToCommitMessage(
        task_id=handle.task_id,
        job_id=handle.job_id,
        worker_instance_id=handle.worker_instance_id,
        lease_token="lease-commit-order",
        stage_id=RESULT_PACKAGE_STAGE_ID,
        staging_directory=str(tmp_path / "staging"),
        manifest_path=str(tmp_path / "staging" / "stage_manifest.json"),
    )

    ExecutionRuntime._handle_stage_ready_to_commit(
        _RuntimeHarness(),  # type: ignore[arg-type]
        handle=handle,
        message=message,
    )

    assert emitted == [engine_module.RUNTIME_EVENT_STAGE_COMMITTED]
    assert calls == [
        "persist_progress",
        "load_stage_manifest",
        "publish_prepared",
        "write_link",
        f"emit:{engine_module.RUNTIME_EVENT_STAGE_COMMITTED}",
    ]


def test_result_package_publication_failure_marks_job_failed_without_stage_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[str] = []
    failures: list[dict[str, object]] = []
    job_dir = tmp_path / "tasks" / "task-publish-fail" / "jobs" / "job-publish-fail"
    job_dir.mkdir(parents=True)
    generic_manifest = StageArtifactManifest(
        stage_id=RESULT_PACKAGE_STAGE_ID,
        job_id="job-publish-fail",
        worker_instance_id="worker-publish-fail",
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        completed_at="2026-01-01T00:00:01Z",
        artifacts=(RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,),
    )

    def _commit(**_kwargs: object) -> StageArtifactManifest:
        return generic_manifest

    def _load_stage_manifest(*, path: Path) -> SimpleNamespace:
        raise ResultPackageValidationError(f"invalid manifest at {path}")

    monkeypatch.setattr(engine_module, "commit_stage_directory", _commit)
    monkeypatch.setattr(engine_module, "load_result_package_stage_manifest", _load_stage_manifest)
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            task_id="task-publish-fail",
            job_id="job-publish-fail",
            worker_instance_id="worker-publish-fail",
            checkpoint=RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION),
            pipeline_definition=PipelineDefinition(
                name=DEFAULT_PIPELINE_NAME,
                version=DEFAULT_PIPELINE_VERSION,
                stages=(),
            ),
            job_dir=job_dir,
            current_stage=RESULT_PACKAGE_STAGE_ID,
            current_stage_progress=1.0,
        ),
    )

    class _RuntimeHarness:
        def _persist_progress(self, **_kwargs: object) -> None:
            return

        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            emitted.append(event_name)

        def _mark_job_failed(self, **kwargs: object) -> None:
            failures.append(cast(dict[str, object], kwargs))

    message = StageReadyToCommitMessage(
        task_id=handle.task_id,
        job_id=handle.job_id,
        worker_instance_id=handle.worker_instance_id,
        lease_token="lease-publish-fail",
        stage_id=RESULT_PACKAGE_STAGE_ID,
        staging_directory=str(tmp_path / "staging"),
        manifest_path=str(tmp_path / "staging" / "stage_manifest.json"),
    )

    ExecutionRuntime._handle_stage_ready_to_commit(
        _RuntimeHarness(),  # type: ignore[arg-type]
        handle=handle,
        message=message,
    )

    assert engine_module.RUNTIME_EVENT_STAGE_COMMITTED not in emitted
    assert len(failures) == 1
    assert failures[0]["reason"] == "result_package_publication_failed"
    assert failures[0]["failure_context"] == {
        "error_type": "ResultPackageValidationError",
        "stage_id": RESULT_PACKAGE_STAGE_ID,
    }


def test_recovered_failed_comparative_snapshot_emits_terminal_job_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, object] | None]] = []
    transitions: list[dict[str, object]] = []
    checkpoint = RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION)
    pipeline = PipelineDefinition(
        name=DEFAULT_PIPELINE_NAME,
        version=DEFAULT_PIPELINE_VERSION,
        stages=(),
    )

    class _RecoveryRegistry:
        def transition_active_job_state(self, **kwargs: object) -> AnalyticalTaskMutationResult:
            transitions.append(kwargs)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED
            )

    class _RuntimeHarness:
        _registry_service = _RecoveryRegistry()
        _pipeline_name = DEFAULT_PIPELINE_NAME
        _pipeline_version = DEFAULT_PIPELINE_VERSION
        _failed_jobs = 0

        def _job_dir(self, **_kwargs: object) -> Path:
            return tmp_path / "job"

        def _reconcile_committed_stages(
            self,
            *,
            checkpoint: RuntimeStateCheckpoint,
            **_kwargs: object,
        ) -> RuntimeStateCheckpoint:
            return checkpoint

        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            emitted.append((event_name, context))

    monkeypatch.setattr(
        engine_module,
        "build_pipeline_definition",
        lambda **_kwargs: pipeline,
    )

    def _committed_snapshot_failed(
        *,
        job_dir: Path,
        task_id: str,
        job_id: str,
        config_hash: str,
        pipeline_version: str,
    ) -> bool:
        assert job_dir == tmp_path / "job"
        assert task_id == "task-recovery"
        assert job_id == "job-recovery"
        assert config_hash == "2" * 64
        assert pipeline_version == DEFAULT_PIPELINE_VERSION
        return True

    monkeypatch.setattr(
        engine_module,
        "_committed_comparative_analysis_failed",
        _committed_snapshot_failed,
    )
    runtime = _RuntimeHarness()
    task_record = SimpleNamespace(task_id="task-recovery", record_version=3)
    job_record = SimpleNamespace(
        job_id="job-recovery",
        worker_instance_id=None,
        config_relative_path="config.json",
        config_hash="2" * 64,
        runtime_state=checkpoint.to_runtime_state(),
        record_version=5,
    )

    ExecutionRuntime._recover_single_job(
        runtime,  # type: ignore[arg-type]
        task_record=task_record,  # type: ignore[arg-type]
        job_record=job_record,  # type: ignore[arg-type]
    )

    assert transitions == [
        {
            "task_id": "task-recovery",
            "to_state": AnalyticalTaskState.FAILED,
            "expected_task_version": 3,
            "expected_job_version": 5,
            "finished_reason": "comparative_analysis_failed",
        }
    ]
    assert runtime._failed_jobs == 1
    assert [name for name, _context in emitted] == [
        "COMPARATIVE_ANALYSIS_FAILED",
        RUNTIME_EVENT_JOB_FAILED,
    ]
    failure_detail = "Recovered a committed comparative-analysis snapshot with failed status."
    assert emitted[-1][1] == {
        "task_id": "task-recovery",
        "job_id": "job-recovery",
        "reason": "comparative_analysis_failed",
        "detail": failure_detail,
        "failure_event_name": "COMPARATIVE_ANALYSIS_FAILED",
        "failure_context": {"detail": failure_detail},
    }


def test_recovery_isolates_invalid_snapshot_to_affected_job() -> None:
    recovered_job_ids: list[str] = []
    failed_job_ids: list[str] = []
    emitted: list[str] = []

    def _snapshot(*, suffix: str) -> SimpleNamespace:
        job_id = f"job-{suffix}"
        return SimpleNamespace(
            task=SimpleNamespace(
                task_id=f"task-{suffix}",
                active_job_id=job_id,
                record_version=1,
            ),
            active_or_latest_job=SimpleNamespace(
                job_id=job_id,
                state=AnalyticalTaskState.RUNNING,
                record_version=1,
            ),
        )

    snapshots = (_snapshot(suffix="invalid"), _snapshot(suffix="valid"))

    class _RecoveryRegistry:
        def list_task_snapshots(self, **_kwargs: object) -> tuple[SimpleNamespace, ...]:
            return snapshots

        def transition_active_job_state(self, **kwargs: object) -> AnalyticalTaskMutationResult:
            assert kwargs["to_state"] is AnalyticalTaskState.FAILED
            failed_job_ids.append(cast(str, kwargs["task_id"]))
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED
            )

    class _RuntimeHarness:
        _registry_service = _RecoveryRegistry()
        _runtime_instance_id = "runtime-recovery"
        _recovered_jobs = 0
        _failed_jobs = 0
        _fail_recovery_job_snapshot = ExecutionRuntime._fail_recovery_job_snapshot

        def _is_worker_lease_expired(self, _job: object) -> bool:
            return True

        def _recover_single_job(self, *, task_record: object, job_record: object) -> None:
            job_id = cast(str, getattr(job_record, "job_id"))
            if job_id == "job-invalid":
                raise StageCommitError(
                    "A committed snapshot is invalid.",
                    code=StageSnapshotErrorCode.INVALID.value,
                )
            recovered_job_ids.append(job_id)

        def _emit(self, event_name: str, _context: dict[str, object] | None) -> None:
            emitted.append(event_name)

    ExecutionRuntime._recover_expired_jobs(
        _RuntimeHarness(),  # type: ignore[arg-type]
    )

    assert failed_job_ids == ["task-invalid"]
    assert recovered_job_ids == ["job-valid"]
    assert emitted == [
        "recovery_started",
        "recovery_failed",
        "job_failed",
        "recovery_completed",
    ]


def test_stage_commit_error_stops_worker_before_cleanup_and_handle_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: list[str] = []

    class _StopEvent:
        def set(self) -> None:
            call_order.append("stop:set")

    class _PidState:
        value = 0

        class _Lock:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_args: object) -> None:
                return None

        def get_lock(self) -> _Lock:
            return self._Lock()

    class _WorkerProcess:
        alive = True
        exitcode: int | None = None

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            call_order.append("process:terminate")

        def kill(self) -> None:
            call_order.append("process:kill")
            self.alive = False
            self.exitcode = -9

        def join(self, timeout: float | None = None) -> None:
            call_order.append(f"process:join:{timeout}")

    class _FailureRegistry:
        def transition_active_job_state(self, **kwargs: object) -> AnalyticalTaskMutationResult:
            assert kwargs["to_state"] is AnalyticalTaskState.FAILED
            call_order.append("database:failed")
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED
            )

    class _TrackedHandles(dict[str, object]):
        def pop(self, key: str, default: object = None) -> object:
            call_order.append(f"handle:remove:{key}")
            return super().pop(key, default)

    class _RuntimeHarness:
        _registry_service = _FailureRegistry()
        _failed_jobs = 0
        _mark_job_failed = ExecutionRuntime._mark_job_failed
        _stop_failed_worker = ExecutionRuntime._stop_failed_worker
        _finalize_worker_handle = ExecutionRuntime._finalize_worker_handle
        _force_stop_worker_processes = ExecutionRuntime._force_stop_worker_processes

        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            call_order.append(f"event:{event_name}")

        def _finalize_control_requested_transition(self, **_kwargs: object) -> None:
            pytest.fail("the failure transition must be applied")

    process = _WorkerProcess()
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            task_id="task-worker-stop",
            job_id="job-worker-stop",
            worker_instance_id="worker-stop",
            task_record_version=1,
            job_record_version=1,
            config_hash="3" * 64,
            checkpoint=RuntimeStateCheckpoint.new(pipeline_version=DEFAULT_PIPELINE_VERSION),
            pipeline_definition=PipelineDefinition(
                name=DEFAULT_PIPELINE_NAME,
                version=DEFAULT_PIPELINE_VERSION,
                stages=(),
            ),
            process=process,
            runtime_shutdown_event=_StopEvent(),
            external_process_pid_state=_PidState(),
            job_dir=tmp_path / "job",
            current_stage=COMPARATIVE_ANALYSIS_STAGE_ID,
        ),
    )
    unrelated_handle = object()
    runtime = _RuntimeHarness()
    runtime._running_workers = _TrackedHandles(
        {
            handle.job_id: handle,
            "job-unrelated": unrelated_handle,
        }
    )

    def _commit_failure(**_kwargs: object) -> StageArtifactManifest:
        raise StageCommitError("Safe stage commit failure.", code="TEST_COMMIT_ERROR")

    def _cleanup(*, job_dir: Path, worker_instance_id: str) -> None:
        assert job_dir == handle.job_dir
        assert worker_instance_id == handle.worker_instance_id
        assert process.is_alive() is False
        assert runtime._running_workers[handle.job_id] is handle
        call_order.append("cleanup")

    monkeypatch.setattr(engine_module, "commit_stage_directory", _commit_failure)
    monkeypatch.setattr(engine_module, "cleanup_worker_staging", _cleanup)
    message = StageReadyToCommitMessage(
        task_id=handle.task_id,
        job_id=handle.job_id,
        worker_instance_id=handle.worker_instance_id,
        lease_token="lease-worker-stop",
        stage_id=COMPARATIVE_ANALYSIS_STAGE_ID,
        staging_directory=str(tmp_path / "staging"),
        manifest_path=str(tmp_path / "staging" / "stage_manifest.json"),
    )

    ExecutionRuntime._handle_stage_ready_to_commit(
        runtime,  # type: ignore[arg-type]
        handle=handle,
        message=message,
    )

    first_bounded_join = call_order.index("process:join:1.0")
    terminate = call_order.index("process:terminate")
    second_bounded_join = call_order.index("process:join:1.0", first_bounded_join + 1)
    kill = call_order.index("process:kill")
    final_blocking_join = call_order.index("process:join:None")
    cleanup = call_order.index("cleanup")
    removal = call_order.index(f"handle:remove:{handle.job_id}")
    assert call_order.index("database:failed") < call_order.index("stop:set")
    assert call_order.index("stop:set") < first_bounded_join
    assert first_bounded_join < terminate < second_bounded_join < kill
    assert kill < final_blocking_join < cleanup < removal
    assert handle.job_id not in runtime._running_workers
    assert runtime._running_workers == {"job-unrelated": unrelated_handle}

    ExecutionRuntime._handle_worker_message(
        runtime,  # type: ignore[arg-type]
        JobCompletedMessage(
            task_id=handle.task_id,
            job_id=handle.job_id,
            worker_instance_id=handle.worker_instance_id,
            lease_token="lease-worker-stop",
        ),
    )

    assert call_order[-1] == "event:stale_worker_message_rejected"


def test_force_stop_terminates_registered_external_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated_process_ids: list[int] = []

    class _PidState:
        value = 4321

        class _Lock:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_args: object) -> None:
                return None

        def get_lock(self) -> _Lock:
            return self._Lock()

    class _WorkerProcess:
        alive = True

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.alive = False

        def join(self, timeout: float) -> None:
            assert timeout == 1.0

    monkeypatch.setattr(
        engine_module,
        "terminate_process_tree_by_pid",
        lambda process_id: terminated_process_ids.append(process_id),
    )
    pid_state = _PidState()
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            external_process_pid_state=pid_state,
            process=_WorkerProcess(),
        ),
    )

    external_process_stopped = engine_module._force_stop_worker_processes(handle)

    assert external_process_stopped is True
    assert terminated_process_ids
    assert set(terminated_process_ids) == {4321}
    assert pid_state.value == 0
    assert handle.process.is_alive() is False


def test_forced_alignment_stop_emits_registered_shutdown_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, object] | None]] = []
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            task_id="task-alignment-runtime",
            job_id="job-alignment-runtime",
            current_stage=ALIGNMENT_STAGE_ID,
        ),
    )
    monkeypatch.setattr(
        engine_module,
        "_force_stop_worker_processes",
        lambda _handle: True,
    )

    class _RuntimeHarness:
        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            emitted.append((event_name, context))

    ExecutionRuntime._force_stop_worker_processes(
        _RuntimeHarness(),  # type: ignore[arg-type]
        handle,
    )

    assert emitted[0][0] == "ALIGNMENT_MAFFT_STOPPED_SHUTDOWN"
    assert emitted[0][1] is not None
    assert emitted[0][1]["forced"] is True
