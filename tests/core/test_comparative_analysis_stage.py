from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from jelica_core.alignment import (
    ALIGNMENT_FASTA_RELATIVE_PATH,
    ALIGNMENT_MANIFEST_RELATIVE_PATH,
    ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH,
    AlignmentManifest,
    AlignmentStageOutcome,
    CanonicalAlignmentRow,
    build_reference_coordinate_map,
    write_canonical_fasta_atomically,
)
from jelica_core.comparative_analysis import (
    COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
    COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
    COMPARATIVE_ANALYSIS_STAGE_ID,
    DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
    PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH,
    PAIRWISE_DIFFERENCES_RELATIVE_PATH,
    REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH,
    REFERENCE_DIFFERENCES_RELATIVE_PATH,
    ComparativeAnalysisManifest,
    ComparativeAnalysisStatus,
)
from jelica_core.comparative_analysis.aligned_comparator import AlignedSequenceComparator
from jelica_core.config import AnalysisConfigInput, ResolvedAnalysisConfig, resolve_analysis_config
from jelica_core.runtime import comparative_analysis_stage as comparative_stage_module
from jelica_core.runtime.comparative_analysis_stage import ComparativeAnalysisStage
from jelica_core.runtime.input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    InputProcessingDatasetSummary,
    InputProcessingLogicalSample,
    InputProcessingManifest,
    InputProcessingResolvedReference,
    InputProcessingState,
    InputProcessingUniqueSequence,
    LogicalSampleProvenance,
    ReferenceResolutionMethod,
    SampleValidationStatus,
    SequenceFacts,
)
from jelica_core.runtime.models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RuntimeStateCheckpoint,
    WorkerLaunchSpec,
)
from jelica_core.runtime.pipeline import StageContext, build_pipeline_definition
from jelica_core.tasks.storage import compute_config_hash, write_text_atomically


class _ProgressRecorder:
    def __init__(self) -> None:
        self.updates: list[tuple[str | None, float | None]] = []

    def start(self, *, description: str, total: float | None = None) -> None:
        self.update(description=description, progress=0.0)

    def update(
        self,
        *,
        description: str | None = None,
        progress: float | None = None,
    ) -> None:
        self.updates.append((description, progress))

    def complete(self, *, description: str | None = None) -> None:
        self.update(description=description, progress=1.0)

    def __call__(self, progress: float) -> None:
        self.update(progress=progress)


def _sequence_id(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _facts(value: str, *, sequence_id: str) -> SequenceFacts:
    symbols = Counter(value)
    ungapped = value.replace("-", "")
    gc_count = symbols.get("G", 0) + symbols.get("C", 0)
    length = len(ungapped)
    return SequenceFacts(
        source_length=len(value),
        ungapped_length=length,
        recognized_nucleotide_count=length,
        symbol_counts=dict(symbols),
        canonical_count=length,
        ambiguous_count=0,
        gap_count=symbols.get("-", 0),
        invalid_symbol_count=0,
        invalid_symbol_counts={},
        gc_count=gc_count,
        gc_content_total=(gc_count / len(value) if value else None),
        resolved_gc_content=(gc_count / length if length else None),
        expected_gc_count=float(gc_count),
        expected_gc_content=(gc_count / length if length else None),
        u_count=symbols.get("U", 0),
        sequence_id=sequence_id,
    )


def _resolved_config(
    *,
    sample_ids: tuple[str, ...],
    alignment_mode: str,
    reference: str | None,
    statistics: bool,
    sequence_differences: bool,
    pairwise: bool,
    substitutions: bool = True,
    insertions: bool = True,
    deletions: bool = True,
) -> ResolvedAnalysisConfig:
    comparative: dict[str, Any] = {
        "enabled": True,
        "statistics": {"enabled": statistics},
        "sequence_differences": {
            "enabled": sequence_differences,
            "substitutions": substitutions,
            "insertions": insertions,
            "deletions": deletions,
        },
        "reference": {"mode": "enabled" if reference is not None else "disabled"},
        "pairwise": ({"enabled": True} if pairwise else {"enabled": False}),
    }
    return resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": [f"{sample_id}.fa" for sample_id in sample_ids],
                "reference": reference,
                "alignment": {"mode": alignment_mode},
                "comparative_analysis": comparative,
                "distance_matrix": {"enabled": False},
                "phylogenetic_tree": {"enabled": False},
            }
        )
    ).config


def _stage_context(tmp_path: Path, *, config: ResolvedAnalysisConfig) -> StageContext:
    task_dir = tmp_path / "task"
    job_dir = task_dir / "jobs" / "job-1"
    config_path = task_dir / "configs" / "000001.json"
    config_path.parent.mkdir(parents=True)
    document = config.model_dump(mode="json")
    write_text_atomically(
        path=config_path,
        payload=json.dumps(document, ensure_ascii=False, sort_keys=True),
    )
    launch_spec = WorkerLaunchSpec(
        task_id="task-1",
        job_id="job-1",
        worker_instance_id="worker-1",
        lease_token="lease-1",
        database_path=tmp_path / "registry.sqlite3",
        task_dir=task_dir,
        job_dir=job_dir,
        config_revision_path=config_path,
        config_hash=compute_config_hash(document),
        runtime_state_json=RuntimeStateCheckpoint.new(
            pipeline_version=DEFAULT_PIPELINE_VERSION
        ).to_runtime_state_json(),
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )
    return StageContext(
        launch_spec=launch_spec,
        stage_index=4,
        stage_staging_directory=(
            job_dir / "staging" / COMPARATIVE_ANALYSIS_STAGE_ID / "worker-1"
        ),
    )


def _write_input_manifest(
    context: StageContext,
    *,
    rows: tuple[tuple[str, str], ...],
    reference_sample_id: str | None,
) -> InputProcessingManifest:
    logical_samples: list[InputProcessingLogicalSample] = []
    unique_by_id: dict[str, InputProcessingUniqueSequence] = {}
    for index, (sample_id, value) in enumerate(rows):
        sequence_id = _sequence_id(value)
        logical_samples.append(
            InputProcessingLogicalSample(
                sample_id=sample_id,
                provenance=LogicalSampleProvenance(
                    input_manifest_source_reference=f"source-{index}",
                    materialized_relative_path=f"inputs/files/sample-{index}.fa",
                    record_index=index,
                    format_hint=".fa",
                ),
                original_record_id=sample_id,
                validation_status=SampleValidationStatus.VALID,
                sequence_id=sequence_id,
                eligible_for_analysis=True,
            )
        )
        previous = unique_by_id.get(sequence_id)
        logical_ids = (
            (*previous.logical_sample_ids, sample_id)
            if previous is not None
            else (sample_id,)
        )
        unique_by_id[sequence_id] = InputProcessingUniqueSequence(
            sequence_id=sequence_id,
            sequence_artifact_path=f"input_processing/sequences/{index}.fasta",
            ungapped_sequence_sha256=hashlib.sha256(
                value.replace("-", "").encode("utf-8")
            ).hexdigest(),
            facts=_facts(value, sequence_id=sequence_id),
            logical_sample_ids=logical_ids,
        )
    resolved_reference = None
    if reference_sample_id is not None:
        reference = next(
            sample for sample in logical_samples if sample.sample_id == reference_sample_id
        )
        assert reference.sequence_id is not None
        resolved_reference = InputProcessingResolvedReference(
            selector=reference_sample_id,
            sample_id=reference_sample_id,
            sequence_id=reference.sequence_id,
            source_relative_path="inputs/files/reference.fa",
            record_id=reference_sample_id,
            resolution_method=ReferenceResolutionMethod.RECORD_ID,
        )
    manifest = InputProcessingManifest(
        task_id=context.launch_spec.task_id,
        job_id=context.launch_spec.job_id,
        config_revision_path="configs/000001.json",
        config_hash=context.launch_spec.config_hash,
        generated_at="2026-08-05T00:00:00Z",
        processing_state=InputProcessingState.COMPLETED,
        logical_samples=tuple(logical_samples),
        unique_sequences=tuple(unique_by_id.values()),
        dataset_summary=InputProcessingDatasetSummary(
            discovered_record_count=len(logical_samples),
            valid_sample_count=len(logical_samples),
            invalid_sample_count=0,
            unique_sequence_count=len(unique_by_id),
            duplicate_logical_sample_count=len(logical_samples) - len(unique_by_id),
            comparative_analysis_available=len(logical_samples) > 1,
            reference_dependent_analysis_available=resolved_reference is not None,
        ),
        resolved_reference=resolved_reference,
    )
    path = (
        context.launch_spec.job_dir
        / "stages"
        / "input_processing"
        / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    )
    path.parent.mkdir(parents=True)
    write_text_atomically(
        path=path,
        payload=json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
    )
    return manifest


def _write_alignment(
    context: StageContext,
    *,
    config: ResolvedAnalysisConfig,
    input_manifest: InputProcessingManifest,
    rows: tuple[tuple[str, str], ...],
    reference_sample_id: str,
) -> None:
    samples_by_id = {sample.sample_id: sample for sample in input_manifest.logical_samples}
    canonical_rows = tuple(
        CanonicalAlignmentRow(
            sample_id=sample_id,
            sequence_id=str(samples_by_id[sample_id].sequence_id),
            aligned_sequence=value,
        )
        for sample_id, value in rows
    )
    root = context.launch_spec.job_dir / "stages" / "alignment"
    fasta_path = root / ALIGNMENT_FASTA_RELATIVE_PATH
    fasta_path.parent.mkdir(parents=True)
    result_hash = write_canonical_fasta_atomically(path=fasta_path, rows=canonical_rows)
    coordinate_map = build_reference_coordinate_map(
        rows=canonical_rows,
        reference_sample_id=reference_sample_id,
    )
    map_path = root / ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH
    write_text_atomically(
        path=map_path,
        payload=json.dumps(coordinate_map.model_dump(mode="json"), sort_keys=True),
    )
    reference = samples_by_id[reference_sample_id]
    manifest = AlignmentManifest(
        task_id=context.launch_spec.task_id,
        job_id=context.launch_spec.job_id,
        config_hash=context.launch_spec.config_hash,
        mode=config.alignment.mode,
        logical_sample_count=len(canonical_rows),
        unique_sequence_count=len({row.sequence_id for row in canonical_rows}),
        alignment_length=len(canonical_rows[0].aligned_sequence),
        reference_sample_id=reference_sample_id,
        reference_sequence_id=reference.sequence_id,
        aligned_fasta_path=ALIGNMENT_FASTA_RELATIVE_PATH,
        reference_coordinate_map_path=ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH,
        input_set_sha256="0" * 64,
        result_sha256=result_hash,
        started_at="2026-08-05T00:00:00Z",
        completed_at="2026-08-05T00:00:01Z",
        duration_seconds=1.0,
        outcome=AlignmentStageOutcome.COMPLETED,
    )
    manifest_path = root / ALIGNMENT_MANIFEST_RELATIVE_PATH
    write_text_atomically(
        path=manifest_path,
        payload=json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
    )


def _move_alignment_to_worker_staging(context: StageContext) -> tuple[Path, Path]:
    committed_root = context.launch_spec.job_dir / "stages" / "alignment"
    staging_root = (
        context.launch_spec.job_dir
        / "staging"
        / "alignment"
        / context.launch_spec.worker_instance_id
    )
    staging_root.parent.mkdir(parents=True, exist_ok=True)
    committed_root.replace(staging_root)
    return staging_root, committed_root


def _run_stage(
    context: StageContext,
    *,
    stage: ComparativeAnalysisStage | None = None,
) -> tuple[ComparativeAnalysisManifest, list[tuple[str, dict[str, object]]], Any]:
    events: list[tuple[str, dict[str, object]]] = []
    context = StageContext(
        launch_spec=context.launch_spec,
        stage_index=context.stage_index,
        stage_staging_directory=context.stage_staging_directory,
        event_reporter=lambda name, payload: events.append((name, payload)),
        control_check=context.control_check,
    )
    selected_stage = stage or ComparativeAnalysisStage()
    selected_stage.preflight(context)
    result = selected_stage.run(context, _ProgressRecorder())
    manifest = ComparativeAnalysisManifest.model_validate_json(
        (context.stage_staging_directory / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH)
        .read_text(encoding="utf-8")
    )
    return manifest, events, result


def test_default_pipeline_places_comparative_analysis_after_alignment() -> None:
    pipeline = build_pipeline_definition(
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )

    stage_ids = [stage.stage_id for stage in pipeline.stages]
    assert stage_ids[-6:] == [
        "alignment",
        COMPARATIVE_ANALYSIS_STAGE_ID,
        "distance_matrix",
        "phylogenetic_tree",
        "clade_detection",
        "result_package",
    ]


def test_disabled_comparative_analysis_publishes_only_skipped_manifest(
    tmp_path: Path,
) -> None:
    config = resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": ["sample-a.fa"],
                "alignment": {"mode": "none"},
                "comparative_analysis": {"enabled": False},
                "distance_matrix": {"enabled": False},
                "phylogenetic_tree": {"enabled": False},
            }
        )
    ).config
    context = _stage_context(tmp_path, config=config)

    manifest, events, result = _run_stage(context)

    assert manifest.enabled is False
    assert manifest.status is ComparativeAnalysisStatus.COMPLETED
    assert result.artifacts == (COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,)
    assert any(name == "COMPARATIVE_ANALYSIS_SKIPPED" for name, _payload in events)


def test_statistics_only_succeeds_without_alignment_artifacts(tmp_path: Path) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="none",
        reference=None,
        statistics=True,
        sequence_differences=False,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    _write_input_manifest(
        context,
        rows=(("sample-a", "AC"), ("sample-b", "AG")),
        reference_sample_id=None,
    )

    manifest, events, result = _run_stage(context)

    assert manifest.status is ComparativeAnalysisStatus.COMPLETED
    assert manifest.alignment_mode == "none"
    assert DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH in result.artifacts
    assert manifest.category_execution["statistics"].successful > 0
    metric_events = [
        payload
        for name, payload in events
        if name == "COMPARATIVE_ANALYSIS_PROGRESS"
        and payload.get("operation_kind") == "statistics_metric"
    ]
    assert metric_events[-1]["completed"] == metric_events[-1]["total"]


def test_missing_alignment_fails_only_sequence_part_when_statistics_succeed(
    tmp_path: Path,
) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="prealigned",
        reference=None,
        statistics=True,
        sequence_differences=True,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    _write_input_manifest(
        context,
        rows=(("sample-a", "AC"), ("sample-b", "AG")),
        reference_sample_id=None,
    )

    manifest, _events, result = _run_stage(context)

    assert manifest.status is ComparativeAnalysisStatus.PARTIAL_SUCCESS
    assert result.failure is None
    assert manifest.category_execution["statistics"].successful > 0
    sequence_category = manifest.category_execution["pairwise_sequence_differences"]
    assert sequence_category.successful == 0
    assert sequence_category.failed == sequence_category.total


def test_alignment_snapshot_read_retries_after_staging_commit_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="prealigned",
        reference=None,
        statistics=False,
        sequence_differences=True,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (("sample-a", "AC"), ("sample-b", "AG"))
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id=None,
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="sample-a",
    )
    staging_root, committed_root = _move_alignment_to_worker_staging(context)
    original_sha256_file = comparative_stage_module._sha256_file
    moved = False

    def move_before_hash(path: Path) -> str:
        nonlocal moved
        if not moved and path == staging_root / ALIGNMENT_FASTA_RELATIVE_PATH:
            staging_root.replace(committed_root)
            moved = True
        return original_sha256_file(path)

    monkeypatch.setattr(comparative_stage_module, "_sha256_file", move_before_hash)

    alignment = comparative_stage_module._load_alignment_artifacts(
        context=context,
        input_manifest=input_manifest,
        expected_mode=config.alignment.mode,
    )

    assert moved is True
    assert alignment.selected_root == committed_root
    assert set(alignment.rows_by_sample_id) == {"sample-a", "sample-b"}


def test_reference_map_read_reloads_same_alignment_after_staging_commit_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _resolved_config(
        sample_ids=("reference", "sample-b"),
        alignment_mode="prealigned",
        reference="reference",
        statistics=False,
        sequence_differences=True,
        pairwise=False,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (("reference", "AC"), ("sample-b", "AG"))
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id="reference",
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="reference",
    )
    staging_root, committed_root = _move_alignment_to_worker_staging(context)
    alignment = comparative_stage_module._load_alignment_artifacts(
        context=context,
        input_manifest=input_manifest,
        expected_mode=config.alignment.mode,
    )
    original_read_text = Path.read_text
    moved = False

    def move_before_read(path: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal moved
        if not moved and path == staging_root / ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH:
            staging_root.replace(committed_root)
            moved = True
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", move_before_read)

    reloaded_alignment, coordinate_lookup = (
        comparative_stage_module._load_reference_coordinate_map(
            context=context,
            alignment=alignment,
            input_manifest=input_manifest,
            expected_mode=config.alignment.mode,
        )
    )

    assert moved is True
    assert reloaded_alignment.selected_root == committed_root
    assert coordinate_lookup.coordinate_map.reference_sample_id == "reference"


def test_alignment_snapshot_retry_rejects_changed_raw_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="prealigned",
        reference=None,
        statistics=False,
        sequence_differences=True,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (("sample-a", "AC"), ("sample-b", "AG"))
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id=None,
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="sample-a",
    )
    staging_root, committed_root = _move_alignment_to_worker_staging(context)
    original_sha256_file = comparative_stage_module._sha256_file
    moved = False

    def replace_with_changed_manifest(path: Path) -> str:
        nonlocal moved
        if not moved and path == staging_root / ALIGNMENT_FASTA_RELATIVE_PATH:
            staging_root.replace(committed_root)
            manifest_path = committed_root / ALIGNMENT_MANIFEST_RELATIVE_PATH
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["duration_seconds"] = 2.0
            write_text_atomically(path=manifest_path, payload=json.dumps(payload, sort_keys=True))
            moved = True
        return original_sha256_file(path)

    monkeypatch.setattr(
        comparative_stage_module,
        "_sha256_file",
        replace_with_changed_manifest,
    )

    with pytest.raises(
        comparative_stage_module.ComparativeAnalysisStageError
    ) as error_info:
        comparative_stage_module._load_alignment_artifacts(
            context=context,
            input_manifest=input_manifest,
            expected_mode=config.alignment.mode,
        )

    assert moved is True
    assert error_info.value.reason == "alignment_snapshot_manifest_changed"


def test_alignment_hash_mismatch_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="prealigned",
        reference=None,
        statistics=False,
        sequence_differences=True,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (("sample-a", "AC"), ("sample-b", "AG"))
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id=None,
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="sample-a",
    )
    manifest_path = (
        context.launch_spec.job_dir
        / "stages"
        / "alignment"
        / ALIGNMENT_MANIFEST_RELATIVE_PATH
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["result_sha256"] = "f" * 64
    write_text_atomically(path=manifest_path, payload=json.dumps(payload, sort_keys=True))
    original_resolve = comparative_stage_module._resolve_alignment_snapshot
    resolve_calls = 0

    def count_resolve(**kwargs: Any) -> Any:
        nonlocal resolve_calls
        resolve_calls += 1
        return original_resolve(**kwargs)

    monkeypatch.setattr(
        comparative_stage_module,
        "_resolve_alignment_snapshot",
        count_resolve,
    )

    with pytest.raises(
        comparative_stage_module.ComparativeAnalysisStageError
    ) as error_info:
        comparative_stage_module._load_alignment_artifacts(
            context=context,
            input_manifest=input_manifest,
            expected_mode=config.alignment.mode,
        )

    assert error_info.value.reason == "alignment_result_hash_mismatch"
    assert resolve_calls == 1


class _CountingAlignedComparator:
    def __init__(self) -> None:
        self.delegate = AlignedSequenceComparator()
        self.calls = 0

    def compare(self, **kwargs: Any) -> Any:
        self.calls += 1
        return self.delegate.compare(**kwargs)


def test_reverse_and_identical_projections_use_bounded_physical_work(
    tmp_path: Path,
) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="prealigned",
        reference=None,
        statistics=False,
        sequence_differences=True,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (("sample-a", "AC"), ("sample-b", "AG"))
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id=None,
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="sample-a",
    )
    counting = _CountingAlignedComparator()

    manifest, events, _result = _run_stage(
        context,
        stage=ComparativeAnalysisStage(aligned_comparator=counting),  # type: ignore[arg-type]
    )

    execution = manifest.plan_execution_counts
    assert manifest.plan_counts.scan_computation_count == 1
    assert execution.attempted_physical_scan_count == 1
    assert execution.successful_physical_scan_count == 1
    assert execution.executed_reused_projection_count == 1
    assert counting.calls == 1
    pairwise = manifest.category_execution["pairwise_sequence_differences"]
    assert pairwise.successful == 2
    progress = [
        payload
        for name, payload in events
        if name == "COMPARATIVE_ANALYSIS_PROGRESS"
        and payload.get("operation_kind") == "pairwise_comparison"
    ]
    assert progress[-1]["completed"] == 2
    assert progress[-1]["total"] == 2

    identical_context = _stage_context(tmp_path / "identical", config=config)
    identical_rows = (("sample-a", "AC"), ("sample-b", "AC"))
    identical_input = _write_input_manifest(
        identical_context,
        rows=identical_rows,
        reference_sample_id=None,
    )
    _write_alignment(
        identical_context,
        config=config,
        input_manifest=identical_input,
        rows=identical_rows,
        reference_sample_id="sample-a",
    )
    identical_counting = _CountingAlignedComparator()

    identical_manifest, _events, _result = _run_stage(
        identical_context,
        stage=ComparativeAnalysisStage(
            aligned_comparator=identical_counting  # type: ignore[arg-type]
        ),
    )

    identical_execution = identical_manifest.plan_execution_counts
    assert identical_manifest.plan_counts.scan_computation_count == 0
    assert identical_execution.attempted_physical_scan_count == 0
    assert identical_execution.attempted_identical_profile_count == 1
    assert identical_execution.identical_projection_count == 2
    assert identical_counting.calls == 1


def test_sequence_category_filtering_is_applied_to_published_artifacts(
    tmp_path: Path,
) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="prealigned",
        reference=None,
        statistics=False,
        sequence_differences=True,
        pairwise=True,
        substitutions=False,
        insertions=True,
        deletions=False,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (("sample-a", "A-CGN"), ("sample-b", "GTC-N"))
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id=None,
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="sample-a",
    )

    manifest, _events, _result = _run_stage(context)

    differences = [
        json.loads(line)
        for line in (
            context.stage_staging_directory / PAIRWISE_DIFFERENCES_RELATIVE_PATH
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert {record["type"] for record in differences} == {"insertion", "uncertain"}
    summaries = [
        json.loads(line)
        for line in (
            context.stage_staging_directory
            / PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert summaries
    assert all(item["summary"]["substitutions"] == {"requested": False} for item in summaries)
    assert all(item["summary"]["deletions"] == {"requested": False} for item in summaries)
    assert all(item["summary"]["insertions"]["requested"] is True for item in summaries)
    assert manifest.requested_difference_categories == ("insertion",)


def test_reference_coordinate_map_is_used_for_edge_and_internal_anchors(
    tmp_path: Path,
) -> None:
    config = _resolved_config(
        sample_ids=("reference", "sample-b"),
        alignment_mode="prealigned",
        reference="reference",
        statistics=False,
        sequence_differences=True,
        pairwise=False,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (("reference", "-AC-G-"), ("sample-b", "TATCGT"))
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id="reference",
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="reference",
    )

    manifest, _events, _result = _run_stage(context)

    records = [
        json.loads(line)
        for line in (
            context.stage_staging_directory / REFERENCE_DIFFERENCES_RELATIVE_PATH
        ).read_text(encoding="utf-8").splitlines()
    ]
    insertions = [record for record in records if record["type"] == "insertion"]
    assert [
        (item["after_reference_position"], item["before_reference_position"])
        for item in insertions
    ] == [(None, 1), (2, 3), (3, None)]
    assert manifest.status is ComparativeAnalysisStatus.COMPLETED


def test_one_statistical_metric_failure_does_not_cancel_other_metrics(
    tmp_path: Path,
) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="none",
        reference=None,
        statistics=True,
        sequence_differences=False,
        pairwise=False,
    )
    context = _stage_context(tmp_path, config=config)
    _write_input_manifest(
        context,
        rows=(("sample-a", "AC"), ("sample-b", "AG")),
        reference_sample_id=None,
    )

    def _metric_guard(metric_id: str) -> None:
        if metric_id == "metric-002":
            raise ValueError("safe injected metric failure")

    manifest, _events, result = _run_stage(
        context,
        stage=ComparativeAnalysisStage(statistics_metric_guard=_metric_guard),
    )

    statistics = manifest.category_execution["statistics"]
    assert manifest.status is ComparativeAnalysisStatus.PARTIAL_SUCCESS
    assert result.failure is None
    assert statistics.failed == 1
    assert statistics.successful == statistics.total - 1


class _AlwaysFailStatisticalComparison:
    def compare(self, _left: Any, _right: Any) -> Any:
        raise ValueError("safe injected statistical comparison failure")


def test_incomplete_statistical_deltas_mark_each_metric_failed(tmp_path: Path) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="none",
        reference=None,
        statistics=True,
        sequence_differences=False,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    _write_input_manifest(
        context,
        rows=(("sample-a", "AC"), ("sample-b", "AG")),
        reference_sample_id=None,
    )

    manifest, _events, result = _run_stage(
        context,
        stage=ComparativeAnalysisStage(
            statistical_comparator=_AlwaysFailStatisticalComparison()  # type: ignore[arg-type]
        ),
    )

    statistics = manifest.category_execution["statistics"]
    assert manifest.status is ComparativeAnalysisStatus.FAILED
    assert result.failure is not None
    assert statistics.successful == 0
    assert statistics.failed == statistics.total


class _FailFirstPhysicalComparison:
    def __init__(self) -> None:
        self.delegate = AlignedSequenceComparator()
        self.calls = 0

    def compare(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise ValueError("safe injected comparison failure")
        return self.delegate.compare(**kwargs)


def test_one_physical_failure_yields_partial_success_and_other_results(
    tmp_path: Path,
) -> None:
    config = _resolved_config(
        sample_ids=("reference", "sample-b", "sample-c"),
        alignment_mode="prealigned",
        reference="reference",
        statistics=False,
        sequence_differences=True,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (
        ("reference", "AC-G"),
        ("sample-b", "AT-G"),
        ("sample-c", "ACGG"),
    )
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id="reference",
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="reference",
    )
    failing = _FailFirstPhysicalComparison()

    manifest, _events, result = _run_stage(
        context,
        stage=ComparativeAnalysisStage(aligned_comparator=failing),  # type: ignore[arg-type]
    )

    assert manifest.status is ComparativeAnalysisStatus.PARTIAL_SUCCESS
    assert result.failure is None
    assert manifest.successful_result_count > 0
    assert manifest.failed_result_count > 0
    assert failing.calls == manifest.plan_counts.scan_computation_count
    failure_records = [
        json.loads(line)
        for line in (
        context.stage_staging_directory / COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert len(failure_records) == 1
    pairwise_summaries = [
        json.loads(line)
        for line in (
            context.stage_staging_directory / PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH
        ).read_text(encoding="utf-8").splitlines()
    ]
    reference_summaries = [
        json.loads(line)
        for line in (
            context.stage_staging_directory
            / REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH
        ).read_text(encoding="utf-8").splitlines()
    ]
    summaries = [*reference_summaries, *pairwise_summaries]
    failed_summaries = [item for item in summaries if item["status"] == "failed"]
    assert {item["status"] for item in pairwise_summaries} == {"completed", "failed"}
    assert {item["failure_id"] for item in failed_summaries} == {
        failure_records[0]["failure_id"]
    }
    assert failure_records[0]["affected_logical_result_count"] == len(
        failed_summaries
    )


class _AlwaysFailPhysicalComparison:
    def __init__(self) -> None:
        self.calls = 0

    def compare(self, **_kwargs: Any) -> Any:
        self.calls += 1
        raise ValueError("safe injected comparison failure")


def test_all_requested_sequence_results_failed_marks_stage_failed(tmp_path: Path) -> None:
    config = _resolved_config(
        sample_ids=("reference", "sample-b"),
        alignment_mode="prealigned",
        reference="reference",
        statistics=False,
        sequence_differences=True,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (("reference", "AC"), ("sample-b", "AG"))
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id="reference",
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="reference",
    )
    failing = _AlwaysFailPhysicalComparison()

    manifest, _events, result = _run_stage(
        context,
        stage=ComparativeAnalysisStage(aligned_comparator=failing),  # type: ignore[arg-type]
    )

    assert manifest.status is ComparativeAnalysisStatus.FAILED
    assert result.failure is not None
    assert manifest.successful_result_count == 0
    assert failing.calls == 1


def test_invalid_reference_map_is_local_and_pairwise_statistics_continue(
    tmp_path: Path,
) -> None:
    config = _resolved_config(
        sample_ids=("reference", "sample-b", "sample-c"),
        alignment_mode="prealigned",
        reference="reference",
        statistics=True,
        sequence_differences=True,
        pairwise=True,
    )
    context = _stage_context(tmp_path, config=config)
    rows = (
        ("reference", "AC-G"),
        ("sample-b", "AT-G"),
        ("sample-c", "ACGG"),
    )
    input_manifest = _write_input_manifest(
        context,
        rows=rows,
        reference_sample_id="reference",
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        rows=rows,
        reference_sample_id="reference",
    )
    map_path = (
        context.launch_spec.job_dir
        / "stages"
        / "alignment"
        / ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH
    )
    coordinate_payload = json.loads(map_path.read_text(encoding="utf-8"))
    coordinate_payload["reference_sample_id"] = "inconsistent-reference"
    write_text_atomically(path=map_path, payload=json.dumps(coordinate_payload))

    manifest, events, result = _run_stage(context)

    assert result.failure is None
    assert manifest.status is ComparativeAnalysisStatus.PARTIAL_SUCCESS
    assert manifest.category_execution["statistics"].successful > 0
    assert (
        manifest.category_execution["reference_sequence_differences"].status.value
        == "failed"
    )
    assert (
        manifest.category_execution["pairwise_sequence_differences"].successful > 0
    )
    protected = json.dumps(
        {
            "manifest": manifest.model_dump(mode="json"),
            "events": events,
            "failures": (
                context.stage_staging_directory
                / COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH
            ).read_text(encoding="utf-8"),
        },
        sort_keys=True,
    )
    assert all(value not in protected for _sample_id, value in rows)


def test_control_interrupt_before_publication_leaves_no_domain_manifest(
    tmp_path: Path,
) -> None:
    config = _resolved_config(
        sample_ids=("sample-a", "sample-b"),
        alignment_mode="none",
        reference=None,
        statistics=True,
        sequence_differences=False,
        pairwise=False,
    )
    original = _stage_context(tmp_path, config=config)
    _write_input_manifest(
        original,
        rows=(("sample-a", "AC"), ("sample-b", "AG")),
        reference_sample_id=None,
    )
    calls = 0

    def _stop() -> None:
        nonlocal calls
        calls += 1
        if calls >= 5:
            raise RuntimeError("control requested")

    context = StageContext(
        launch_spec=original.launch_spec,
        stage_index=original.stage_index,
        stage_staging_directory=original.stage_staging_directory,
        control_check=_stop,
    )
    stage = ComparativeAnalysisStage()
    stage.preflight(context)

    with pytest.raises(RuntimeError, match="control requested"):
        stage.run(context, _ProgressRecorder())

    assert not (
        context.stage_staging_directory / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH
    ).exists()
