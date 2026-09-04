from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import jelica_core.runtime.phylogenetic_tree_stage as phylogenetic_tree_stage_module
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
)
from jelica_core.phylogenetic_tree import (
    PhylogeneticTreeConstructionMode,
    PhylogeneticTreeManifest,
    PhylogeneticTreeStatus,
)
from jelica_core.runtime.artifacts import (
    StageArtifactManifest,
    StageCommitError,
    StageSnapshotErrorCode,
    StageSnapshotValidationError,
    commit_stage_directory,
    validate_committed_stage_snapshot,
    write_stage_manifest,
)
from jelica_core.runtime.distance_matrix_stage import DistanceMatrixStage
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
from jelica_core.runtime.phylogenetic_tree_stage import (
    PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
    PHYLOGENETIC_TREE_PROGRESS_EVENT,
    PHYLOGENETIC_TREE_SKIPPED_EVENT,
    PHYLOGENETIC_TREE_STAGE_ID,
    PHYLOGENETIC_TREE_STARTED_EVENT,
    TREE_DIAGNOSTICS_RELATIVE_PATH,
    TREE_JSON_RELATIVE_PATH,
    TREE_ROOTED_NWK_RELATIVE_PATH,
    TREE_UNROOTED_NWK_RELATIVE_PATH,
    PhylogeneticTreeStage,
    PhylogeneticTreeStageError,
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
    phylogenetic_tree_enabled: bool = True,
) -> ResolvedAnalysisConfig:
    return resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": ["sample-a.fa", "sample-b.fa", "sample-c.fa", "sample-d.fa"],
                "alignment": {"mode": alignment_mode},
                "comparative_analysis": {"enabled": False},
                "distance_matrix": {"enabled": distance_matrix_enabled},
                "phylogenetic_tree": {"enabled": phylogenetic_tree_enabled},
            }
        )
    ).config


def _stage_context(
    tmp_path: Path,
    *,
    config: ResolvedAnalysisConfig,
    stage_id: str,
    stage_index: int,
    control_check: Any | None = None,
) -> StageContext:
    task_dir = tmp_path / "task"
    job_dir = task_dir / "jobs" / "job-1"
    config_path = task_dir / "configs" / "000001.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
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
        stage_index=stage_index,
        stage_staging_directory=job_dir / "staging" / stage_id / "worker-1",
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
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(
        path=path,
        payload=json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
    )
    return manifest


def _write_input_processing_sequence_artifacts(
    context: StageContext,
    manifest: InputProcessingManifest,
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
    fasta_path.parent.mkdir(parents=True, exist_ok=True)
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
        result_sha256=result_hash,
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


def _prepare_staged_distance_snapshot(
    *,
    distance_context: StageContext,
    config: ResolvedAnalysisConfig,
    rows: tuple[tuple[str, str], ...],
    aligned_rows: tuple[tuple[str, str], ...],
) -> Path:
    input_manifest = _write_input_manifest(distance_context, rows=rows)
    _write_input_processing_sequence_artifacts(distance_context, input_manifest)
    _write_alignment(
        distance_context,
        config=config,
        input_manifest=input_manifest,
        aligned_rows=aligned_rows,
    )
    _write_generic_stage_manifest(
        root=distance_context.launch_spec.job_dir / "stages" / INPUT_PROCESSING_STAGE_ID,
        stage_id=INPUT_PROCESSING_STAGE_ID,
        worker_instance_id=distance_context.launch_spec.worker_instance_id,
        artifacts=input_processing_artifact_paths(input_manifest),
    )
    _write_generic_stage_manifest(
        root=distance_context.launch_spec.job_dir / "stages" / ALIGNMENT_STAGE_ID,
        stage_id=ALIGNMENT_STAGE_ID,
        worker_instance_id=distance_context.launch_spec.worker_instance_id,
        artifacts=(ALIGNMENT_MANIFEST_RELATIVE_PATH, ALIGNMENT_FASTA_RELATIVE_PATH),
    )

    stage = DistanceMatrixStage()
    stage.preflight(distance_context)
    stage_result = stage.run(distance_context, _ProgressRecorder())
    stage_manifest_path = write_stage_manifest(
        directory=distance_context.stage_staging_directory,
        manifest=StageArtifactManifest(
            stage_id=DISTANCE_MATRIX_STAGE_ID,
            job_id=distance_context.launch_spec.job_id,
            worker_instance_id=distance_context.launch_spec.worker_instance_id,
            pipeline_version=distance_context.launch_spec.pipeline_version,
            completed_at="2026-08-05T00:00:01Z",
            artifacts=stage_result.artifacts,
        ),
    )
    return stage_manifest_path


def _prepare_committed_distance_snapshot(
    *,
    distance_context: StageContext,
    config: ResolvedAnalysisConfig,
    rows: tuple[tuple[str, str], ...],
    aligned_rows: tuple[tuple[str, str], ...],
) -> None:
    stage_manifest_path = _prepare_staged_distance_snapshot(
        distance_context=distance_context,
        config=config,
        rows=rows,
        aligned_rows=aligned_rows,
    )
    commit_stage_directory(
        job_dir=distance_context.launch_spec.job_dir,
        stage_id=DISTANCE_MATRIX_STAGE_ID,
        job_id=distance_context.launch_spec.job_id,
        worker_instance_id=distance_context.launch_spec.worker_instance_id,
        pipeline_version=distance_context.launch_spec.pipeline_version,
        staging_directory=distance_context.stage_staging_directory,
        manifest_path=stage_manifest_path,
        task_id=distance_context.launch_spec.task_id,
        config_hash=distance_context.launch_spec.config_hash,
    )


def _run_tree_stage(
    context: StageContext,
) -> tuple[PhylogeneticTreeManifest, list[tuple[str, dict[str, object]]], Any]:
    events: list[tuple[str, dict[str, object]]] = []
    run_context = StageContext(
        launch_spec=context.launch_spec,
        stage_index=context.stage_index,
        stage_staging_directory=context.stage_staging_directory,
        event_reporter=lambda name, payload: events.append((name, payload)),
        control_check=context.control_check,
    )
    stage = PhylogeneticTreeStage()
    stage.preflight(run_context)
    result = stage.run(run_context, _ProgressRecorder())
    manifest = PhylogeneticTreeManifest.model_validate_json(
        (run_context.stage_staging_directory / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH)
        .read_text(encoding="utf-8")
    )
    return manifest, events, result


def _payload_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def test_default_pipeline_places_phylogenetic_tree_after_distance_matrix() -> None:
    pipeline = build_pipeline_definition(
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )

    assert [stage.stage_id for stage in pipeline.stages][-5:] == [
        "comparative_analysis",
        DISTANCE_MATRIX_STAGE_ID,
        PHYLOGENETIC_TREE_STAGE_ID,
        "clade_detection",
        "result_package",
    ]


def test_disabled_phylogenetic_tree_stage_publishes_only_skipped_manifest(
    tmp_path: Path,
) -> None:
    config = _resolved_config(
        alignment_mode="none",
        distance_matrix_enabled=False,
        phylogenetic_tree_enabled=False,
    )
    context = _stage_context(
        tmp_path,
        config=config,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        stage_index=6,
    )

    manifest, events, result = _run_tree_stage(context)

    assert manifest.enabled is False
    assert manifest.status is PhylogeneticTreeStatus.COMPLETED
    assert manifest.skipped_reason == "phylogenetic_tree_disabled"
    assert result.artifacts == (PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,)
    assert any(name == PHYLOGENETIC_TREE_SKIPPED_EVENT for name, _payload in events)
    assert not (context.stage_staging_directory / TREE_JSON_RELATIVE_PATH).exists()


def test_phylogenetic_tree_stage_completes_and_snapshot_validates(
    tmp_path: Path,
) -> None:
    config = _resolved_config()
    distance_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=DISTANCE_MATRIX_STAGE_ID,
        stage_index=5,
    )
    tree_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        stage_index=6,
    )
    _prepare_committed_distance_snapshot(
        distance_context=distance_context,
        config=config,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
        aligned_rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
    )

    manifest, events, stage_result = _run_tree_stage(tree_context)
    stage_root = tree_context.stage_staging_directory
    tree_result = json.loads((stage_root / TREE_JSON_RELATIVE_PATH).read_text(encoding="utf-8"))

    assert manifest.status is PhylogeneticTreeStatus.COMPLETED
    assert manifest.leaf_count == 3
    assert manifest.construction_mode is PhylogeneticTreeConstructionMode.NEIGHBOR_JOINING
    assert stage_result.artifacts == (
        PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
        TREE_UNROOTED_NWK_RELATIVE_PATH,
        TREE_ROOTED_NWK_RELATIVE_PATH,
        TREE_JSON_RELATIVE_PATH,
        TREE_DIAGNOSTICS_RELATIVE_PATH,
    )
    assert len(tree_result["canonical_leaf_order"]) == 3
    assert any(
        len(mapping["logical_sample_ids"]) > 1 for mapping in tree_result["leaf_mappings"]
    )
    assert any(name == PHYLOGENETIC_TREE_STARTED_EVENT for name, _payload in events)
    assert any(
        name == PHYLOGENETIC_TREE_PROGRESS_EVENT and payload.get("phase") == "ready_to_commit"
        for name, payload in events
    )
    assert not any(name == "PHYLOGENETIC_TREE_RESULT_PUBLISHED" for name, _payload in events)
    assert not any(name == "PHYLOGENETIC_TREE_COMPLETED" for name, _payload in events)
    for _event_name, payload in events:
        payload_text = _payload_text(payload)
        assert "sample-a" not in payload_text
        assert "AAAA" not in payload_text

    stage_manifest_path = write_stage_manifest(
        directory=tree_context.stage_staging_directory,
        manifest=StageArtifactManifest(
            stage_id=PHYLOGENETIC_TREE_STAGE_ID,
            job_id=tree_context.launch_spec.job_id,
            worker_instance_id=tree_context.launch_spec.worker_instance_id,
            pipeline_version=tree_context.launch_spec.pipeline_version,
            completed_at="2026-08-05T00:00:02Z",
            artifacts=stage_result.artifacts,
        ),
    )
    commit_stage_directory(
        job_dir=tree_context.launch_spec.job_dir,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        job_id=tree_context.launch_spec.job_id,
        worker_instance_id=tree_context.launch_spec.worker_instance_id,
        pipeline_version=tree_context.launch_spec.pipeline_version,
        staging_directory=tree_context.stage_staging_directory,
        manifest_path=stage_manifest_path,
        task_id=tree_context.launch_spec.task_id,
        config_hash=tree_context.launch_spec.config_hash,
    )
    snapshot = validate_committed_stage_snapshot(
        job_dir=tree_context.launch_spec.job_dir,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        expected_job_id=tree_context.launch_spec.job_id,
        expected_pipeline_version=tree_context.launch_spec.pipeline_version,
        expected_task_id=tree_context.launch_spec.task_id,
        expected_config_hash=tree_context.launch_spec.config_hash,
    )

    assert snapshot.domain_status == "completed"
    assert snapshot.source_artifacts == (
        f"stages/{DISTANCE_MATRIX_STAGE_ID}/{DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH}",
        f"stages/{DISTANCE_MATRIX_STAGE_ID}/{DISTANCE_MATRIX_JSON_RELATIVE_PATH}",
    )


def test_phylogenetic_tree_stage_waits_for_pending_distance_matrix_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _resolved_config()
    distance_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=DISTANCE_MATRIX_STAGE_ID,
        stage_index=5,
    )
    tree_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        stage_index=6,
    )
    stage_manifest_path = _prepare_staged_distance_snapshot(
        distance_context=distance_context,
        config=config,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
        aligned_rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
    )
    committed_root = (
        distance_context.launch_spec.job_dir / "stages" / DISTANCE_MATRIX_STAGE_ID
    )
    assert not committed_root.exists()

    original_validate = phylogenetic_tree_stage_module.validate_committed_stage_snapshot
    attempts = 0
    committed = False

    def _validate_with_delayed_commit(**kwargs: Any):
        nonlocal attempts, committed
        attempts += 1
        if attempts == 1:
            raise StageCommitError(
                "simulated pending commit",
                code=StageSnapshotErrorCode.INVALID.value,
                stage_id=DISTANCE_MATRIX_STAGE_ID,
            )
        if not committed:
            commit_stage_directory(
                job_dir=distance_context.launch_spec.job_dir,
                stage_id=DISTANCE_MATRIX_STAGE_ID,
                job_id=distance_context.launch_spec.job_id,
                worker_instance_id=distance_context.launch_spec.worker_instance_id,
                pipeline_version=distance_context.launch_spec.pipeline_version,
                staging_directory=distance_context.stage_staging_directory,
                manifest_path=stage_manifest_path,
                task_id=distance_context.launch_spec.task_id,
                config_hash=distance_context.launch_spec.config_hash,
            )
            committed = True
        return original_validate(**kwargs)

    monkeypatch.setattr(
        phylogenetic_tree_stage_module,
        "validate_committed_stage_snapshot",
        _validate_with_delayed_commit,
    )

    manifest, events, _stage_result = _run_tree_stage(tree_context)

    assert attempts >= 2
    assert committed
    assert manifest.status is PhylogeneticTreeStatus.COMPLETED
    assert any(
        name == PHYLOGENETIC_TREE_PROGRESS_EVENT and payload.get("phase") == "ready_to_commit"
        for name, payload in events
    )
    assert not any(name == "PHYLOGENETIC_TREE_RESULT_PUBLISHED" for name, _payload in events)
    assert not any(name == "PHYLOGENETIC_TREE_COMPLETED" for name, _payload in events)


def test_phylogenetic_tree_stage_fails_for_partial_distance_matrix(
    tmp_path: Path,
) -> None:
    config = _resolved_config()
    distance_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=DISTANCE_MATRIX_STAGE_ID,
        stage_index=5,
    )
    tree_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        stage_index=6,
    )
    _prepare_committed_distance_snapshot(
        distance_context=distance_context,
        config=config,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "TTTT"),
        ),
        aligned_rows=(
            ("sample-a", "N-"),
            ("sample-b", "-N"),
        ),
    )
    stage = PhylogeneticTreeStage()
    stage.preflight(tree_context)

    with pytest.raises(PhylogeneticTreeStageError) as error_info:
        stage.run(tree_context, _ProgressRecorder())

    assert error_info.value.reason == "distance_matrix_incomplete"
    assert not (
        tree_context.stage_staging_directory / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH
    ).exists()


def _commit_tree_snapshot(
    tmp_path: Path,
    *,
    rows: tuple[tuple[str, str], ...],
    aligned_rows: tuple[tuple[str, str], ...],
) -> StageContext:
    config = _resolved_config()
    distance_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=DISTANCE_MATRIX_STAGE_ID,
        stage_index=5,
    )
    tree_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        stage_index=6,
    )
    _prepare_committed_distance_snapshot(
        distance_context=distance_context,
        config=config,
        rows=rows,
        aligned_rows=aligned_rows,
    )
    _manifest, _events, stage_result = _run_tree_stage(tree_context)
    stage_manifest_path = write_stage_manifest(
        directory=tree_context.stage_staging_directory,
        manifest=StageArtifactManifest(
            stage_id=PHYLOGENETIC_TREE_STAGE_ID,
            job_id=tree_context.launch_spec.job_id,
            worker_instance_id=tree_context.launch_spec.worker_instance_id,
            pipeline_version=tree_context.launch_spec.pipeline_version,
            completed_at="2026-08-05T00:00:03Z",
            artifacts=stage_result.artifacts,
        ),
    )
    commit_stage_directory(
        job_dir=tree_context.launch_spec.job_dir,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        job_id=tree_context.launch_spec.job_id,
        worker_instance_id=tree_context.launch_spec.worker_instance_id,
        pipeline_version=tree_context.launch_spec.pipeline_version,
        staging_directory=tree_context.stage_staging_directory,
        manifest_path=stage_manifest_path,
        task_id=tree_context.launch_spec.task_id,
        config_hash=tree_context.launch_spec.config_hash,
    )
    return tree_context


def _tree_stage_root(context: StageContext) -> Path:
    return context.launch_spec.job_dir / "stages" / PHYLOGENETIC_TREE_STAGE_ID


def _validate_tree_snapshot(context: StageContext):
    return validate_committed_stage_snapshot(
        job_dir=context.launch_spec.job_dir,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        expected_job_id=context.launch_spec.job_id,
        expected_pipeline_version=context.launch_spec.pipeline_version,
        expected_task_id=context.launch_spec.task_id,
        expected_config_hash=context.launch_spec.config_hash,
    )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _refresh_tree_manifest_metadata(*, stage_root: Path, relative_path: str) -> None:
    manifest_path = stage_root / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH
    manifest = PhylogeneticTreeManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    target_path = stage_root / relative_path
    updated_artifacts = []
    for metadata in manifest.artifacts:
        if metadata.relative_path != relative_path:
            updated_artifacts.append(metadata)
            continue
        updated_artifacts.append(
            metadata.model_copy(
                update={
                    "size_bytes": target_path.stat().st_size,
                    "sha256": _sha256_path(target_path),
                }
            )
        )
    updated_manifest = manifest.model_copy(update={"artifacts": tuple(updated_artifacts)})
    write_text_atomically(
        path=manifest_path,
        payload=json.dumps(updated_manifest.model_dump(mode="json"), sort_keys=True),
    )


def _write_artifact_and_refresh_metadata(
    *,
    stage_root: Path,
    relative_path: str,
    payload: str,
) -> None:
    write_text_atomically(path=stage_root / relative_path, payload=payload)
    _refresh_tree_manifest_metadata(stage_root=stage_root, relative_path=relative_path)


def _load_tree_result_payload(stage_root: Path) -> dict[str, object]:
    return json.loads((stage_root / TREE_JSON_RELATIVE_PATH).read_text(encoding="utf-8"))


def _write_tree_result_payload(stage_root: Path, payload: dict[str, object]) -> None:
    _write_artifact_and_refresh_metadata(
        stage_root=stage_root,
        relative_path=TREE_JSON_RELATIVE_PATH,
        payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _load_tree_diagnostics_payload(stage_root: Path) -> dict[str, object]:
    return json.loads((stage_root / TREE_DIAGNOSTICS_RELATIVE_PATH).read_text(encoding="utf-8"))


def _write_tree_diagnostics_payload(stage_root: Path, payload: dict[str, object]) -> None:
    _write_artifact_and_refresh_metadata(
        stage_root=stage_root,
        relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _load_tree_manifest(stage_root: Path) -> PhylogeneticTreeManifest:
    return PhylogeneticTreeManifest.model_validate_json(
        (stage_root / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8")
    )


def _write_tree_manifest(stage_root: Path, manifest: PhylogeneticTreeManifest) -> None:
    write_text_atomically(
        path=stage_root / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
        payload=json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
    )


def _sync_leaf_nodes_with_mappings(result_payload: dict[str, object]) -> None:
    leaf_mappings = {
        item["leaf_label"]: item for item in result_payload["leaf_mappings"]  # type: ignore[index]
    }
    for key in ("rooted", "unrooted"):
        representation = result_payload[key]  # type: ignore[index]
        for node in representation["nodes"]:  # type: ignore[index]
            if node.get("kind") != "leaf":
                continue
            mapping = leaf_mappings[node["leaf_label"]]
            node["sequence_index"] = mapping["sequence_index"]
            node["sequence_id"] = mapping["sequence_id"]
            node["logical_sample_ids"] = mapping["logical_sample_ids"]


@pytest.mark.parametrize(
    ("rows", "aligned_rows"),
    (
        (
            (("sample-a", "AAAA"),),
            (("sample-a", "AAAA"),),
        ),
        (
            (
                ("sample-a", "AAAA"),
                ("sample-b", "AAAT"),
            ),
            (
                ("sample-a", "AAAA"),
                ("sample-b", "AAAT"),
            ),
        ),
        (
            (
                ("sample-a", "AAAA"),
                ("sample-b", "AAAA"),
                ("sample-c", "AAAT"),
                ("sample-d", "AATT"),
            ),
            (
                ("sample-a", "AAAA"),
                ("sample-b", "AAAA"),
                ("sample-c", "AAAT"),
                ("sample-d", "AATT"),
            ),
        ),
    ),
    ids=("singleton", "pair", "neighbor-joining"),
)
def test_tree_snapshot_validator_accepts_valid_singleton_pair_and_general_cases(
    tmp_path: Path,
    rows: tuple[tuple[str, str], ...],
    aligned_rows: tuple[tuple[str, str], ...],
) -> None:
    context = _commit_tree_snapshot(
        tmp_path,
        rows=rows,
        aligned_rows=aligned_rows,
    )

    snapshot = _validate_tree_snapshot(context)

    assert snapshot.domain_status == "completed"


@pytest.mark.parametrize(
    "corruption",
    (
        "tree-json-malformed",
        "tree-json-schema",
        "diagnostics-malformed",
        "diagnostics-schema",
    ),
)
def test_tree_snapshot_validator_rejects_invalid_typed_tree_artifacts(
    tmp_path: Path,
    corruption: str,
) -> None:
    context = _commit_tree_snapshot(
        tmp_path,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
        aligned_rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
    )
    stage_root = _tree_stage_root(context)

    if corruption == "tree-json-malformed":
        _write_artifact_and_refresh_metadata(
            stage_root=stage_root,
            relative_path=TREE_JSON_RELATIVE_PATH,
            payload="{\n",
        )
    elif corruption == "tree-json-schema":
        result_payload = _load_tree_result_payload(stage_root)
        result_payload["schema_version"] = 999
        _write_tree_result_payload(stage_root, result_payload)
    elif corruption == "diagnostics-malformed":
        _write_artifact_and_refresh_metadata(
            stage_root=stage_root,
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
            payload="{\n",
        )
    elif corruption == "diagnostics-schema":
        diagnostics_payload = _load_tree_diagnostics_payload(stage_root)
        diagnostics_payload["schema_version"] = 999
        _write_tree_diagnostics_payload(stage_root, diagnostics_payload)
    else:
        raise AssertionError(f"unsupported corruption '{corruption}'")

    with pytest.raises(StageSnapshotValidationError) as captured:
        _validate_tree_snapshot(context)

    expected_code = (
        StageSnapshotErrorCode.ARTIFACT_UNREADABLE.value
        if corruption in {"tree-json-malformed", "diagnostics-malformed"}
        else StageSnapshotErrorCode.INVALID.value
    )
    assert captured.value.code == expected_code


@pytest.mark.parametrize(
    "corruption",
    (
        "duplicate-node-id",
        "unknown-edge-endpoint",
        "self-loop",
        "disconnected-cycle",
        "edge-count-mismatch",
        "duplicate-leaf-label",
        "missing-canonical-leaf",
        "unknown-leaf-label",
        "rooted-missing-root",
        "rooted-two-parents",
        "rooted-negative-branch",
    ),
)
def test_tree_snapshot_validator_rejects_tree_topology_invariant_violations(
    tmp_path: Path,
    corruption: str,
) -> None:
    context = _commit_tree_snapshot(
        tmp_path,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
        aligned_rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
    )
    stage_root = _tree_stage_root(context)
    result_payload = _load_tree_result_payload(stage_root)
    rooted = result_payload["rooted"]  # type: ignore[index]
    unrooted = result_payload["unrooted"]  # type: ignore[index]

    if corruption == "duplicate-node-id":
        rooted["nodes"][1]["node_id"] = rooted["nodes"][0]["node_id"]
    elif corruption == "unknown-edge-endpoint":
        rooted["edges"][0]["child_id"] = "missing_node"
    elif corruption == "self-loop":
        rooted["edges"][0]["child_id"] = rooted["edges"][0]["parent_id"]
    elif corruption == "disconnected-cycle":
        root_node_id = rooted["root_id"]
        leaf_node_ids = [
            item["node_id"]
            for item in rooted["nodes"]
            if item.get("kind") == "leaf"
        ]
        internal_node_ids = [
            item["node_id"]
            for item in rooted["nodes"]
            if item.get("kind") == "internal"
        ]
        assert len(leaf_node_ids) >= 3
        assert len(internal_node_ids) >= 1
        rooted["edges"] = [
            {"parent_id": root_node_id, "child_id": leaf_node_ids[0], "branch_length": 0.1},
            {"parent_id": root_node_id, "child_id": leaf_node_ids[1], "branch_length": 0.1},
            {
                "parent_id": internal_node_ids[0],
                "child_id": leaf_node_ids[2],
                "branch_length": 0.2,
            },
            {
                "parent_id": leaf_node_ids[2],
                "child_id": internal_node_ids[0],
                "branch_length": 0.2,
            },
        ]
        rooted["edge_count"] = len(rooted["edges"])
        result_payload["edge_count"] = len(rooted["edges"])
    elif corruption == "edge-count-mismatch":
        rooted["edge_count"] = rooted["edge_count"] + 1
        result_payload["edge_count"] = rooted["edge_count"]
    elif corruption == "duplicate-leaf-label":
        first_label = result_payload["canonical_leaf_order"][0]
        second_label = result_payload["canonical_leaf_order"][1]
        for node in rooted["nodes"]:
            if node.get("kind") == "leaf" and node["leaf_label"] == first_label:
                node["leaf_label"] = second_label
                break
    elif corruption == "missing-canonical-leaf":
        first_label = result_payload["canonical_leaf_order"][0]
        result_payload["canonical_leaf_order"] = result_payload["canonical_leaf_order"][1:]
        result_payload["leaf_mappings"] = [
            item for item in result_payload["leaf_mappings"] if item["leaf_label"] != first_label
        ]
    elif corruption == "unknown-leaf-label":
        for node in rooted["nodes"]:
            if node.get("kind") == "leaf":
                node["leaf_label"] = "leaf_999999"
                break
    elif corruption == "rooted-missing-root":
        rooted["root_id"] = None
    elif corruption == "rooted-two-parents":
        rooted["edges"][1]["child_id"] = rooted["edges"][0]["child_id"]
    elif corruption == "rooted-negative-branch":
        rooted["edges"][0]["branch_length"] = -0.25
    else:
        raise AssertionError(f"unsupported corruption '{corruption}'")

    result_payload["rooted"] = rooted
    result_payload["unrooted"] = unrooted
    _write_tree_result_payload(stage_root, result_payload)

    with pytest.raises(StageSnapshotValidationError) as captured:
        _validate_tree_snapshot(context)

    assert captured.value.code == StageSnapshotErrorCode.INVALID.value


@pytest.mark.parametrize(
    "corruption",
    (
        "manifest-count-mismatch",
        "diagnostics-count-mismatch",
        "input-digest-mismatch",
        "newick-leaf-set-mismatch",
        "newick-topology-mismatch",
        "newick-branch-length-mismatch",
        "rooted-newick-negative-branch",
        "applied-rooting-zero-diameter-mismatch",
    ),
)
def test_tree_snapshot_validator_rejects_cross_file_inconsistencies(
    tmp_path: Path,
    corruption: str,
) -> None:
    context = _commit_tree_snapshot(
        tmp_path,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
        aligned_rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
    )
    stage_root = _tree_stage_root(context)

    if corruption == "manifest-count-mismatch":
        manifest = _load_tree_manifest(stage_root)
        _write_tree_manifest(
            stage_root,
            manifest.model_copy(update={"leaf_count": manifest.leaf_count + 1}),
        )
    elif corruption == "diagnostics-count-mismatch":
        diagnostics_payload = _load_tree_diagnostics_payload(stage_root)
        diagnostics_payload["leaf_count"] = diagnostics_payload["leaf_count"] + 1
        _write_tree_diagnostics_payload(stage_root, diagnostics_payload)
    elif corruption == "input-digest-mismatch":
        result_payload = _load_tree_result_payload(stage_root)
        result_payload["input_snapshot_manifest_sha256"] = "f" * 64
        _write_tree_result_payload(stage_root, result_payload)
    elif corruption == "newick-leaf-set-mismatch":
        newick_payload = (
            (stage_root / TREE_ROOTED_NWK_RELATIVE_PATH).read_text(encoding="utf-8")
            .replace("leaf_000001", "leaf_999999", 1)
        )
        _write_artifact_and_refresh_metadata(
            stage_root=stage_root,
            relative_path=TREE_ROOTED_NWK_RELATIVE_PATH,
            payload=newick_payload,
        )
    elif corruption == "newick-topology-mismatch":
        result_payload = _load_tree_result_payload(stage_root)
        leaves = result_payload["canonical_leaf_order"]
        newick_payload = "(" + ",".join(f"{leaf}:0.1" for leaf in leaves) + ");\n"
        _write_artifact_and_refresh_metadata(
            stage_root=stage_root,
            relative_path=TREE_ROOTED_NWK_RELATIVE_PATH,
            payload=newick_payload,
        )
    elif corruption == "newick-branch-length-mismatch":
        newick_payload = (stage_root / TREE_ROOTED_NWK_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
        updated_payload = re.sub(r":[0-9]+(?:\.[0-9]+)?", ":9.9", newick_payload, count=1)
        _write_artifact_and_refresh_metadata(
            stage_root=stage_root,
            relative_path=TREE_ROOTED_NWK_RELATIVE_PATH,
            payload=updated_payload,
        )
    elif corruption == "rooted-newick-negative-branch":
        newick_payload = (stage_root / TREE_ROOTED_NWK_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
        updated_payload = re.sub(r":[0-9]+(?:\.[0-9]+)?", ":-0.1", newick_payload, count=1)
        _write_artifact_and_refresh_metadata(
            stage_root=stage_root,
            relative_path=TREE_ROOTED_NWK_RELATIVE_PATH,
            payload=updated_payload,
        )
    elif corruption == "applied-rooting-zero-diameter-mismatch":
        result_payload = _load_tree_result_payload(stage_root)
        result_payload["zero_diameter"] = False
        result_payload["applied_rooting"] = "deterministic_zero_diameter_fallback"
        _write_tree_result_payload(stage_root, result_payload)
    else:
        raise AssertionError(f"unsupported corruption '{corruption}'")

    with pytest.raises(StageSnapshotValidationError) as captured:
        _validate_tree_snapshot(context)

    assert captured.value.code in {
        StageSnapshotErrorCode.INVALID.value,
        StageSnapshotErrorCode.UPSTREAM_INVALID.value,
    }


def test_tree_snapshot_validator_keeps_failure_payload_privacy(
    tmp_path: Path,
) -> None:
    marker = "TEST_ONLY_USER_MARKER"
    context = _commit_tree_snapshot(
        tmp_path,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
        aligned_rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAA"),
            ("sample-c", "AAAT"),
            ("sample-d", "AATT"),
        ),
    )
    stage_root = _tree_stage_root(context)
    result_payload = _load_tree_result_payload(stage_root)

    result_payload["leaf_mappings"][0]["logical_sample_ids"] = [marker]
    result_payload["leaf_mappings"][1]["logical_sample_ids"] = [marker]
    _sync_leaf_nodes_with_mappings(result_payload)
    _write_tree_result_payload(stage_root, result_payload)

    with pytest.raises(StageSnapshotValidationError) as captured:
        _validate_tree_snapshot(context)

    rendered = str(captured.value)
    assert captured.value.code == StageSnapshotErrorCode.INVALID.value
    assert marker not in rendered
