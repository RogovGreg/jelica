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
    ALIGNMENT_STAGE_ID,
    AlignmentManifest,
    AlignmentStageOutcome,
    CanonicalAlignmentRow,
    write_canonical_fasta_atomically,
)
from jelica_core.config import AnalysisConfigInput, ResolvedAnalysisConfig, resolve_analysis_config
from jelica_core.distance_matrix import (
    DISTANCE_MATRIX_JSON_RELATIVE_PATH,
    DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
    DISTANCE_MATRIX_STAGE_ID,
    DISTANCE_MATRIX_TSV_RELATIVE_PATH,
    DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
    DistanceMatrixManifest,
    DistanceMatrixResult,
    DistanceMatrixStatus,
    DistancePairRecord,
)
from jelica_core.runtime.artifacts import (
    StageArtifactManifest,
    commit_stage_directory,
    validate_committed_stage_snapshot,
    write_stage_manifest,
)
from jelica_core.runtime.distance_matrix_stage import DistanceMatrixStage, DistanceMatrixStageError
from jelica_core.runtime.input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    INPUT_PROCESSING_STAGE_ID,
    InputProcessingDatasetSummary,
    InputProcessingLogicalSample,
    InputProcessingManifest,
    InputProcessingState,
    InputProcessingUniqueSequence,
    LogicalSampleProvenance,
    SampleValidationStatus,
    SequenceFacts,
    input_processing_artifact_paths,
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
    alignment_mode: str = "compute",
    distance_matrix_enabled: bool = True,
) -> ResolvedAnalysisConfig:
    return resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": ["sample-a.fa", "sample-b.fa", "sample-c.fa"],
                "alignment": {"mode": alignment_mode},
                "comparative_analysis": {"enabled": False},
                "distance_matrix": {"enabled": distance_matrix_enabled},
                "phylogenetic_tree": {"enabled": False},
            }
        )
    ).config


def _stage_context(
    tmp_path: Path,
    *,
    config: ResolvedAnalysisConfig,
    control_check: Any | None = None,
) -> StageContext:
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
        stage_index=5,
        stage_staging_directory=(
            job_dir / "staging" / DISTANCE_MATRIX_STAGE_ID / "worker-1"
        ),
        control_check=control_check,
    )


def _write_input_manifest(
    context: StageContext,
    *,
    rows: tuple[tuple[str, str], ...],
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
        ),
    )
    path = (
        context.launch_spec.job_dir
        / "stages"
        / INPUT_PROCESSING_STAGE_ID
        / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    )
    path.parent.mkdir(parents=True)
    write_text_atomically(
        path=path,
        payload=json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
    )
    return manifest


def _write_input_processing_sequence_artifacts(
    context: StageContext, manifest: InputProcessingManifest
) -> None:
    root = context.launch_spec.job_dir / "stages" / INPUT_PROCESSING_STAGE_ID
    for item in manifest.unique_sequences:
        artifact_path = root / item.sequence_artifact_path
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(">seq\nA\n", encoding="utf-8")


def _write_alignment(
    context: StageContext,
    *,
    config: ResolvedAnalysisConfig,
    input_manifest: InputProcessingManifest,
    aligned_rows: tuple[tuple[str, str], ...],
    override_result_sha256: str | None = None,
) -> AlignmentManifest:
    samples_by_id = {sample.sample_id: sample for sample in input_manifest.logical_samples}
    canonical_rows = tuple(
        CanonicalAlignmentRow(
            sample_id=sample_id,
            sequence_id=str(samples_by_id[sample_id].sequence_id),
            aligned_sequence=aligned_sequence,
        )
        for sample_id, aligned_sequence in aligned_rows
    )
    root = context.launch_spec.job_dir / "stages" / ALIGNMENT_STAGE_ID
    fasta_path = root / ALIGNMENT_FASTA_RELATIVE_PATH
    fasta_path.parent.mkdir(parents=True)
    result_hash = write_canonical_fasta_atomically(path=fasta_path, rows=canonical_rows)
    manifest = AlignmentManifest(
        task_id=context.launch_spec.task_id,
        job_id=context.launch_spec.job_id,
        config_hash=context.launch_spec.config_hash,
        mode=config.alignment.mode,
        logical_sample_count=len(canonical_rows),
        unique_sequence_count=len({row.sequence_id for row in canonical_rows}),
        alignment_length=len(canonical_rows[0].aligned_sequence),
        aligned_fasta_path=ALIGNMENT_FASTA_RELATIVE_PATH,
        input_set_sha256="0" * 64,
        result_sha256=override_result_sha256 or result_hash,
        started_at="2026-08-05T00:00:00Z",
        completed_at="2026-08-05T00:00:01Z",
        duration_seconds=1.0,
        outcome=AlignmentStageOutcome.COMPLETED,
    )
    write_text_atomically(
        path=root / ALIGNMENT_MANIFEST_RELATIVE_PATH,
        payload=json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
    )
    return manifest


def _write_generic_stage_manifest(
    *,
    root: Path,
    stage_id: str,
    worker_instance_id: str,
    artifacts: tuple[str, ...],
) -> None:
    write_stage_manifest(
        directory=root,
        manifest=StageArtifactManifest(
            stage_id=stage_id,
            job_id="job-1",
            worker_instance_id=worker_instance_id,
            pipeline_version=DEFAULT_PIPELINE_VERSION,
            completed_at="2026-08-05T00:00:01Z",
            artifacts=artifacts,
        ),
    )


def _run_stage(
    context: StageContext,
) -> tuple[DistanceMatrixManifest, list[tuple[str, dict[str, object]]], Any]:
    events: list[tuple[str, dict[str, object]]] = []
    run_context = StageContext(
        launch_spec=context.launch_spec,
        stage_index=context.stage_index,
        stage_staging_directory=context.stage_staging_directory,
        event_reporter=lambda name, payload: events.append((name, payload)),
        control_check=context.control_check,
    )
    stage = DistanceMatrixStage()
    stage.preflight(run_context)
    result = stage.run(run_context, _ProgressRecorder())
    manifest = DistanceMatrixManifest.model_validate_json(
        (
            run_context.stage_staging_directory / DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH
        ).read_text(encoding="utf-8")
    )
    return manifest, events, result


def _load_distance_result(stage_root: Path) -> DistanceMatrixResult:
    return DistanceMatrixResult.model_validate_json(
        (stage_root / DISTANCE_MATRIX_JSON_RELATIVE_PATH).read_text(encoding="utf-8")
    )


def _load_pair_records(stage_root: Path) -> list[DistancePairRecord]:
    lines = (stage_root / DISTANCE_PAIRS_JSONL_RELATIVE_PATH).read_text(
        encoding="utf-8"
    ).splitlines()
    return [
        DistancePairRecord.model_validate(json.loads(line))
        for line in lines
        if line.strip() != ""
    ]


def _payload_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def test_default_pipeline_places_distance_matrix_after_comparative_analysis() -> None:
    pipeline = build_pipeline_definition(
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )

    assert [stage.stage_id for stage in pipeline.stages][-5:] == [
        "comparative_analysis",
        DISTANCE_MATRIX_STAGE_ID,
        "phylogenetic_tree",
        "clade_detection",
        "result_package",
    ]


def test_disabled_distance_stage_publishes_only_skipped_manifest(tmp_path: Path) -> None:
    config = _resolved_config(alignment_mode="none", distance_matrix_enabled=False)
    context = _stage_context(tmp_path, config=config)

    manifest, _events, result = _run_stage(context)

    assert manifest.enabled is False
    assert manifest.status is DistanceMatrixStatus.COMPLETED
    assert manifest.skipped_reason == "distance_matrix_disabled"
    assert result.artifacts == (DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,)
    assert not (context.stage_staging_directory / DISTANCE_MATRIX_JSON_RELATIVE_PATH).exists()


def test_distance_stage_completed_and_preserves_duplicate_logical_mapping(
    tmp_path: Path,
) -> None:
    config = _resolved_config()
    context = _stage_context(tmp_path, config=config)
    input_manifest = _write_input_manifest(
        context,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
        ),
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        aligned_rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
        ),
    )

    manifest, events, result = _run_stage(context)
    stage_root = context.stage_staging_directory
    distance_result = _load_distance_result(stage_root)
    pairs = _load_pair_records(stage_root)

    assert manifest.status is DistanceMatrixStatus.COMPLETED
    assert manifest.unique_sequence_count == 2
    assert manifest.expected_pair_count == 1
    assert manifest.defined_distance_count == 1
    assert manifest.undefined_distance_count == 0
    assert result.artifacts == (
        DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
        DISTANCE_MATRIX_JSON_RELATIVE_PATH,
        DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
        DISTANCE_MATRIX_TSV_RELATIVE_PATH,
    )
    assert distance_result.sequence_references[0].logical_sample_ids == (
        "sample-a",
        "sample-b",
    )
    assert len(pairs) == 1
    assert pairs[0].left_index == 0
    assert pairs[0].right_index == 1
    assert pairs[0].mismatch_count == 1
    assert pairs[0].comparable_site_count == 4
    assert pairs[0].distance == pytest.approx(0.25)
    assert distance_result.matrix == ((0.0, 0.25), (0.25, 0.0))
    assert any(
        name == "DISTANCE_MATRIX_PROGRESS" and payload.get("phase") == "ready_to_commit"
        for name, payload in events
    )
    assert not any(name == "DISTANCE_MATRIX_RESULT_PUBLISHED" for name, _payload in events)
    assert not any(name == "DISTANCE_MATRIX_COMPLETED" for name, _payload in events)
    for _event_name, payload in events:
        payload_text = _payload_text(payload)
        assert "sample-a" not in payload_text
        assert "AAAA" not in payload_text


def test_distance_stage_partial_success_when_pair_has_no_comparable_sites(
    tmp_path: Path,
) -> None:
    config = _resolved_config()
    context = _stage_context(tmp_path, config=config)
    input_manifest = _write_input_manifest(
        context,
        rows=(("sample-a", "AAAA"), ("sample-b", "TTTT")),
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        aligned_rows=(("sample-a", "N-"), ("sample-b", "-N")),
    )

    manifest, _events, _result = _run_stage(context)
    distance_result = _load_distance_result(context.stage_staging_directory)
    pairs = _load_pair_records(context.stage_staging_directory)

    assert manifest.status is DistanceMatrixStatus.PARTIAL_SUCCESS
    assert manifest.defined_distance_count == 0
    assert manifest.undefined_distance_count == 1
    assert distance_result.matrix == ((0.0, None), (None, 0.0))
    assert len(pairs) == 1
    assert pairs[0].distance is None
    assert pairs[0].state.value == "undefined_no_comparable_sites"


def test_distance_stage_builds_trivial_matrix_for_single_sequence(tmp_path: Path) -> None:
    config = _resolved_config()
    context = _stage_context(tmp_path, config=config)
    input_manifest = _write_input_manifest(context, rows=(("sample-a", "AAAA"),))
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        aligned_rows=(("sample-a", "AATT"),),
    )

    manifest, _events, _result = _run_stage(context)
    distance_result = _load_distance_result(context.stage_staging_directory)
    pairs = _load_pair_records(context.stage_staging_directory)

    assert manifest.status is DistanceMatrixStatus.COMPLETED
    assert manifest.unique_sequence_count == 1
    assert manifest.expected_pair_count == 0
    assert manifest.processed_pair_count == 0
    assert distance_result.matrix == ((0.0,),)
    assert pairs == []


def test_distance_stage_fails_on_alignment_hash_mismatch_without_publication(
    tmp_path: Path,
) -> None:
    config = _resolved_config()
    context = _stage_context(tmp_path, config=config)
    input_manifest = _write_input_manifest(
        context,
        rows=(("sample-a", "AAAA"), ("sample-b", "AAAT")),
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        aligned_rows=(("sample-a", "AAAA"), ("sample-b", "AAAT")),
        override_result_sha256="f" * 64,
    )
    stage = DistanceMatrixStage()
    stage.preflight(context)

    with pytest.raises(DistanceMatrixStageError) as error_info:
        stage.run(context, _ProgressRecorder())

    assert error_info.value.reason == "alignment_result_hash_mismatch"
    assert not (
        context.stage_staging_directory / DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH
    ).exists()
    assert not (context.stage_staging_directory / DISTANCE_PAIRS_JSONL_RELATIVE_PATH).exists()


def test_distance_stage_does_not_publish_partial_outputs_before_atomic_commit(
    tmp_path: Path,
) -> None:
    check_count = {"value": 0}

    def _control_check() -> None:
        check_count["value"] += 1
        if check_count["value"] >= 3:
            raise RuntimeError("stop-requested")

    config = _resolved_config()
    context = _stage_context(tmp_path, config=config, control_check=_control_check)
    input_manifest = _write_input_manifest(
        context,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAT"),
            ("sample-c", "AATT"),
        ),
    )
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        aligned_rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAT"),
            ("sample-c", "AATT"),
        ),
    )
    stage = DistanceMatrixStage()
    stage.preflight(context)

    with pytest.raises(RuntimeError, match="stop-requested"):
        stage.run(context, _ProgressRecorder())

    assert not (
        context.stage_staging_directory / DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH
    ).exists()
    assert not (context.stage_staging_directory / DISTANCE_MATRIX_JSON_RELATIVE_PATH).exists()
    assert not (context.stage_staging_directory / DISTANCE_PAIRS_JSONL_RELATIVE_PATH).exists()
    assert not (context.stage_staging_directory / DISTANCE_MATRIX_TSV_RELATIVE_PATH).exists()


def test_committed_distance_snapshot_passes_runtime_snapshot_validation(
    tmp_path: Path,
) -> None:
    config = _resolved_config()
    context = _stage_context(tmp_path, config=config)
    input_manifest = _write_input_manifest(
        context,
        rows=(("sample-a", "AAAA"), ("sample-b", "AAAT")),
    )
    _write_input_processing_sequence_artifacts(context, input_manifest)
    _write_alignment(
        context,
        config=config,
        input_manifest=input_manifest,
        aligned_rows=(("sample-a", "AAAA"), ("sample-b", "AAAT")),
    )
    _write_generic_stage_manifest(
        root=context.launch_spec.job_dir / "stages" / INPUT_PROCESSING_STAGE_ID,
        stage_id=INPUT_PROCESSING_STAGE_ID,
        worker_instance_id=context.launch_spec.worker_instance_id,
        artifacts=input_processing_artifact_paths(input_manifest),
    )
    _write_generic_stage_manifest(
        root=context.launch_spec.job_dir / "stages" / ALIGNMENT_STAGE_ID,
        stage_id=ALIGNMENT_STAGE_ID,
        worker_instance_id=context.launch_spec.worker_instance_id,
        artifacts=(ALIGNMENT_MANIFEST_RELATIVE_PATH, ALIGNMENT_FASTA_RELATIVE_PATH),
    )

    stage = DistanceMatrixStage()
    stage.preflight(context)
    stage_result = stage.run(context, _ProgressRecorder())
    stage_manifest_path = write_stage_manifest(
        directory=context.stage_staging_directory,
        manifest=StageArtifactManifest(
            stage_id=DISTANCE_MATRIX_STAGE_ID,
            job_id=context.launch_spec.job_id,
            worker_instance_id=context.launch_spec.worker_instance_id,
            pipeline_version=context.launch_spec.pipeline_version,
            completed_at="2026-08-05T00:00:01Z",
            artifacts=stage_result.artifacts,
        ),
    )
    commit_stage_directory(
        job_dir=context.launch_spec.job_dir,
        stage_id=DISTANCE_MATRIX_STAGE_ID,
        job_id=context.launch_spec.job_id,
        worker_instance_id=context.launch_spec.worker_instance_id,
        pipeline_version=context.launch_spec.pipeline_version,
        staging_directory=context.stage_staging_directory,
        manifest_path=stage_manifest_path,
        task_id=context.launch_spec.task_id,
        config_hash=context.launch_spec.config_hash,
    )

    snapshot = validate_committed_stage_snapshot(
        job_dir=context.launch_spec.job_dir,
        stage_id=DISTANCE_MATRIX_STAGE_ID,
        expected_job_id=context.launch_spec.job_id,
        expected_pipeline_version=context.launch_spec.pipeline_version,
        expected_task_id=context.launch_spec.task_id,
        expected_config_hash=context.launch_spec.config_hash,
    )

    assert snapshot.domain_status == "completed"
    assert snapshot.source_artifacts == (
        f"stages/{INPUT_PROCESSING_STAGE_ID}/{INPUT_PROCESSING_MANIFEST_RELATIVE_PATH}",
        f"stages/{ALIGNMENT_STAGE_ID}/{ALIGNMENT_MANIFEST_RELATIVE_PATH}",
        f"stages/{ALIGNMENT_STAGE_ID}/{ALIGNMENT_FASTA_RELATIVE_PATH}",
    )
