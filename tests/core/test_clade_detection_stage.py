from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import jelica_core.runtime.clade_detection_stage as clade_detection_stage_module
from jelica_core.alignment import (
    ALIGNMENT_FASTA_RELATIVE_PATH,
    ALIGNMENT_MANIFEST_RELATIVE_PATH,
    ALIGNMENT_STAGE_ID,
    AlignmentManifest,
    AlignmentStageOutcome,
    CanonicalAlignmentRow,
    write_canonical_fasta_atomically,
)
from jelica_core.clade_detection import (
    CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
    CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
    CLADE_DETECTION_STAGE_ID,
    CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
    INFERRED_CLADES_JSON_RELATIVE_PATH,
    CladeAssignmentRecord,
    CladeDetectionComputationError,
    CladeDetectionManifest,
    CladeDetectionStatus,
    detect_inferred_clades,
    parse_clade_assignments_tsv,
    serialize_clade_assignments_tsv,
)
from jelica_core.config import AnalysisConfigInput, ResolvedAnalysisConfig, resolve_analysis_config
from jelica_core.distance_matrix import (
    DISTANCE_MATRIX_JSON_RELATIVE_PATH,
    DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
    DISTANCE_MATRIX_STAGE_ID,
)
from jelica_core.phylogenetic_tree import (
    PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
    PHYLOGENETIC_TREE_STAGE_ID,
    TREE_JSON_RELATIVE_PATH,
)
from jelica_core.runtime.artifacts import (
    StageArtifactManifest,
    StageSnapshotErrorCode,
    StageSnapshotValidationError,
    commit_stage_directory,
    validate_committed_stage_snapshot,
    write_stage_manifest,
)
from jelica_core.runtime.clade_detection_stage import (
    CLADE_DETECTION_FAILED_EVENT,
    CLADE_DETECTION_PROGRESS_EVENT,
    CladeDetectionStage,
    CladeDetectionStageError,
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
from jelica_core.runtime.phylogenetic_tree_stage import PhylogeneticTreeStage
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
    clade_detection_enabled: bool = True,
    clade_threshold: float | None = 0.3,
) -> ResolvedAnalysisConfig:
    clade_detection: dict[str, object] = {"enabled": clade_detection_enabled}
    if clade_threshold is not None:
        clade_detection["max_within_clade_distance"] = clade_threshold
    return resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": ["sample-a.fa", "sample-b.fa", "sample-c.fa", "sample-d.fa"],
                "alignment": {"mode": alignment_mode},
                "comparative_analysis": {"enabled": False},
                "distance_matrix": {"enabled": distance_matrix_enabled},
                "phylogenetic_tree": {"enabled": phylogenetic_tree_enabled},
                "clade_detection": clade_detection,
            }
        )
    ).config


def _stage_context(
    tmp_path: Path,
    *,
    config: ResolvedAnalysisConfig,
    stage_id: str,
    stage_index: int,
    event_reporter: Any | None = None,
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
        event_reporter=event_reporter,
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


def _commit_staged_snapshot(
    context: StageContext,
    *,
    stage_id: str,
    artifacts: tuple[str, ...],
) -> None:
    stage_manifest_path = write_stage_manifest(
        directory=context.stage_staging_directory,
        manifest=StageArtifactManifest(
            stage_id=stage_id,
            job_id=context.launch_spec.job_id,
            worker_instance_id=context.launch_spec.worker_instance_id,
            pipeline_version=context.launch_spec.pipeline_version,
            completed_at="2026-08-05T00:00:02Z",
            artifacts=artifacts,
        ),
    )
    commit_stage_directory(
        job_dir=context.launch_spec.job_dir,
        stage_id=stage_id,
        job_id=context.launch_spec.job_id,
        worker_instance_id=context.launch_spec.worker_instance_id,
        pipeline_version=context.launch_spec.pipeline_version,
        staging_directory=context.stage_staging_directory,
        manifest_path=stage_manifest_path,
        task_id=context.launch_spec.task_id,
        config_hash=context.launch_spec.config_hash,
    )


def _prepare_committed_distance_snapshot(
    *,
    distance_context: StageContext,
    config: ResolvedAnalysisConfig,
    rows: tuple[tuple[str, str], ...],
    aligned_rows: tuple[tuple[str, str], ...],
) -> None:
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
    _commit_staged_snapshot(
        distance_context,
        stage_id=DISTANCE_MATRIX_STAGE_ID,
        artifacts=stage_result.artifacts,
    )


def _prepare_committed_tree_snapshot(*, tree_context: StageContext) -> None:
    stage = PhylogeneticTreeStage()
    stage.preflight(tree_context)
    stage_result = stage.run(tree_context, _ProgressRecorder())
    _commit_staged_snapshot(
        tree_context,
        stage_id=PHYLOGENETIC_TREE_STAGE_ID,
        artifacts=stage_result.artifacts,
    )


def _clade_stage_root(context: StageContext) -> Path:
    return context.launch_spec.job_dir / "stages" / CLADE_DETECTION_STAGE_ID


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _refresh_clade_manifest_metadata(*, stage_root: Path, relative_path: str) -> None:
    manifest_path = stage_root / CLADE_DETECTION_MANIFEST_RELATIVE_PATH
    manifest = CladeDetectionManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    target = stage_root / relative_path
    updated = []
    for metadata in manifest.artifacts:
        if metadata.relative_path != relative_path:
            updated.append(metadata)
            continue
        updated.append(
            metadata.model_copy(
                update={
                    "size_bytes": target.stat().st_size,
                    "sha256": _sha256_path(target),
                }
            )
        )
    write_text_atomically(
        path=manifest_path,
        payload=json.dumps(
            manifest.model_copy(update={"artifacts": tuple(updated)}).model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _prepare_clade_stage_context(tmp_path: Path) -> StageContext:
    config = _resolved_config(clade_detection_enabled=True, clade_threshold=0.3)
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
    clade_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=CLADE_DETECTION_STAGE_ID,
        stage_index=7,
    )
    _prepare_committed_distance_snapshot(
        distance_context=distance_context,
        config=config,
        rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAT"),
            ("sample-c", "TTTT"),
            ("sample-d", "TTTA"),
        ),
        aligned_rows=(
            ("sample-a", "AAAA"),
            ("sample-b", "AAAT"),
            ("sample-c", "TTTT"),
            ("sample-d", "TTTA"),
        ),
    )
    _prepare_committed_tree_snapshot(tree_context=tree_context)
    return clade_context


def _commit_clade_snapshot(tmp_path: Path) -> StageContext:
    clade_context = _prepare_clade_stage_context(tmp_path)
    stage = CladeDetectionStage()
    stage.preflight(clade_context)
    stage_result = stage.run(clade_context, _ProgressRecorder())
    _commit_staged_snapshot(
        clade_context,
        stage_id=CLADE_DETECTION_STAGE_ID,
        artifacts=stage_result.artifacts,
    )
    return clade_context


def test_clade_assignments_tsv_round_trip_canonical_records() -> None:
    records = (
        CladeAssignmentRecord(
            clade_ordinal=1,
            clade_id="item_0",
            leaf_label="group_0",
            sequence_index=0,
            sequence_id="source_0",
            logical_sample_ids=("sample_0",),
            clade_leaf_count=1,
            clade_max_pairwise_distance=0.0,
            max_within_clade_distance=0.0,
        ),
        CladeAssignmentRecord(
            clade_ordinal=2,
            clade_id="item_1",
            leaf_label="group_1",
            sequence_index=1,
            sequence_id="source_1",
            logical_sample_ids=("sample_0", "sample_1"),
            clade_leaf_count=2,
            clade_max_pairwise_distance=0.12345678901234566,
            max_within_clade_distance=0.3,
        ),
    )

    serialized = serialize_clade_assignments_tsv(records)
    parsed = parse_clade_assignments_tsv(serialized)

    assert parsed == records
    assert serialized.endswith("\n")


def test_clade_assignments_tsv_round_trip_runtime_shaped_records(tmp_path: Path) -> None:
    context = _prepare_clade_stage_context(tmp_path)
    inputs = clade_detection_stage_module._load_upstream_inputs(context=context)
    config = clade_detection_stage_module._load_resolved_config(
        context.launch_spec.config_revision_path
    )
    threshold = config.clade_detection.max_within_clade_distance
    assert threshold is not None

    computation = detect_inferred_clades(
        phylogenetic_tree_result=inputs.tree_result,
        distance_matrix_result=inputs.matrix_result,
        method=config.clade_detection.method,
        max_within_clade_distance=threshold,
        tree_snapshot_manifest_sha256=inputs.tree_snapshot_manifest_sha256,
        matrix_snapshot_manifest_sha256=inputs.matrix_snapshot_manifest_sha256,
    )

    serialized = serialize_clade_assignments_tsv(computation.assignment_records)
    parsed = parse_clade_assignments_tsv(serialized)

    assert parsed == computation.assignment_records


def test_clade_assignments_tsv_round_trip_preserves_escaped_strings() -> None:
    records = (
        CladeAssignmentRecord(
            clade_ordinal=1,
            clade_id="value\tone",
            leaf_label="value\ntwo",
            sequence_index=0,
            sequence_id='value "three" Unicode_čćž',
            logical_sample_ids=(
                "value\tone",
                "value\ntwo",
                'value "three"',
                "Unicode_čćž",
            ),
            clade_leaf_count=1,
            clade_max_pairwise_distance=0.0,
            max_within_clade_distance=0.0,
        ),
    )

    serialized = serialize_clade_assignments_tsv(records)
    parsed = parse_clade_assignments_tsv(serialized)

    assert parsed == records


def test_parse_clade_assignments_tsv_rejects_unexpected_header() -> None:
    payload = (
        "ordinal\tclade_id\tleaf_label\tsequence_index\tsequence_id\tlogical_sample_ids\t"
        "clade_leaf_count\tclade_max_pairwise_distance\tmax_within_clade_distance\n"
        "1\titem_0\tgroup_0\t0\tsource_0\t[]\t1\t0.0\t0.0\n"
    )

    with pytest.raises(ValueError, match="unexpected header"):
        parse_clade_assignments_tsv(payload)


def test_parse_clade_assignments_tsv_rejects_extra_columns() -> None:
    records = (
        CladeAssignmentRecord(
            clade_ordinal=1,
            clade_id="item_0",
            leaf_label="group_0",
            sequence_index=0,
            sequence_id="source_0",
            logical_sample_ids=("sample_0",),
            clade_leaf_count=1,
            clade_max_pairwise_distance=0.0,
            max_within_clade_distance=0.0,
        ),
    )
    serialized = serialize_clade_assignments_tsv(records)
    lines = serialized.splitlines()
    lines[1] = lines[1] + "\textra_column"
    payload = "\n".join(lines) + "\n"

    with pytest.raises(ValueError, match="extra columns"):
        parse_clade_assignments_tsv(payload)


def test_clade_stage_accepts_fractional_assignment_tsv_round_trip(
    tmp_path: Path,
) -> None:
    config = _resolved_config(clade_detection_enabled=True, clade_threshold=0.2)
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
    clade_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=CLADE_DETECTION_STAGE_ID,
        stage_index=7,
    )
    _prepare_committed_distance_snapshot(
        distance_context=distance_context,
        config=config,
        rows=(
            ("sample-a", "AAAAAAA"),
            ("sample-b", "AAAAAAT"),
            ("sample-c", "TTTTTTT"),
            ("sample-d", "TTTTTTA"),
        ),
        aligned_rows=(
            ("sample-a", "AAAAAAA"),
            ("sample-b", "AAAAAAT"),
            ("sample-c", "TTTTTTT"),
            ("sample-d", "TTTTTTA"),
        ),
    )
    _prepare_committed_tree_snapshot(tree_context=tree_context)
    stage = CladeDetectionStage()
    stage.preflight(clade_context)

    stage_result = stage.run(clade_context, _ProgressRecorder())

    assignments_path = clade_context.stage_staging_directory / CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH
    parsed = parse_clade_assignments_tsv(assignments_path.read_text(encoding="utf-8"))
    assert len(parsed) > 0
    assert CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH in stage_result.artifacts


def test_clade_stage_emits_ready_to_commit_without_publication_events(
    tmp_path: Path,
) -> None:
    config = _resolved_config(clade_detection_enabled=True, clade_threshold=0.2)
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
    events: list[tuple[str, dict[str, object]]] = []
    clade_context = _stage_context(
        tmp_path,
        config=config,
        stage_id=CLADE_DETECTION_STAGE_ID,
        stage_index=7,
        event_reporter=lambda name, payload: events.append((name, payload)),
    )
    _prepare_committed_distance_snapshot(
        distance_context=distance_context,
        config=config,
        rows=(
            ("sample-a", "AAAAAAA"),
            ("sample-b", "AAAAAAT"),
            ("sample-c", "TTTTTTT"),
            ("sample-d", "TTTTTTA"),
        ),
        aligned_rows=(
            ("sample-a", "AAAAAAA"),
            ("sample-b", "AAAAAAT"),
            ("sample-c", "TTTTTTT"),
            ("sample-d", "TTTTTTA"),
        ),
    )
    _prepare_committed_tree_snapshot(tree_context=tree_context)
    stage = CladeDetectionStage()
    stage.preflight(clade_context)

    stage.run(clade_context, _ProgressRecorder())

    assert any(
        name == CLADE_DETECTION_PROGRESS_EVENT and payload.get("phase") == "ready_to_commit"
        for name, payload in events
    )
    assert not any(name == "CLADE_DETECTION_RESULT_PUBLISHED" for name, _payload in events)
    assert not any(name == "CLADE_DETECTION_COMPLETED" for name, _payload in events)


def test_default_pipeline_places_clade_detection_after_phylogenetic_tree() -> None:
    pipeline = build_pipeline_definition(
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )
    assert [stage.stage_id for stage in pipeline.stages][-5:] == [
        "comparative_analysis",
        "distance_matrix",
        "phylogenetic_tree",
        CLADE_DETECTION_STAGE_ID,
        "result_package",
    ]


def test_disabled_clade_stage_publishes_only_skipped_manifest(tmp_path: Path) -> None:
    config = _resolved_config(
        alignment_mode="none",
        distance_matrix_enabled=False,
        phylogenetic_tree_enabled=False,
        clade_detection_enabled=False,
        clade_threshold=None,
    )
    context = _stage_context(
        tmp_path,
        config=config,
        stage_id=CLADE_DETECTION_STAGE_ID,
        stage_index=7,
    )
    stage = CladeDetectionStage()
    stage.preflight(context)
    result = stage.run(context, _ProgressRecorder())
    manifest = CladeDetectionManifest.model_validate_json(
        (context.stage_staging_directory / CLADE_DETECTION_MANIFEST_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )

    assert manifest.enabled is False
    assert manifest.status is CladeDetectionStatus.COMPLETED
    assert result.artifacts == (CLADE_DETECTION_MANIFEST_RELATIVE_PATH,)
    assert not (context.stage_staging_directory / INFERRED_CLADES_JSON_RELATIVE_PATH).exists()


def test_clade_stage_committed_snapshot_passes_validation(tmp_path: Path) -> None:
    context = _commit_clade_snapshot(tmp_path)

    snapshot = validate_committed_stage_snapshot(
        job_dir=context.launch_spec.job_dir,
        stage_id=CLADE_DETECTION_STAGE_ID,
        expected_job_id=context.launch_spec.job_id,
        expected_pipeline_version=context.launch_spec.pipeline_version,
        expected_task_id=context.launch_spec.task_id,
        expected_config_hash=context.launch_spec.config_hash,
    )

    assert snapshot.domain_status == "completed"
    assert snapshot.source_artifacts == (
        f"stages/{PHYLOGENETIC_TREE_STAGE_ID}/{PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH}",
        f"stages/{PHYLOGENETIC_TREE_STAGE_ID}/{TREE_JSON_RELATIVE_PATH}",
        f"stages/{DISTANCE_MATRIX_STAGE_ID}/{DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH}",
        f"stages/{DISTANCE_MATRIX_STAGE_ID}/{DISTANCE_MATRIX_JSON_RELATIVE_PATH}",
    )


def test_clade_snapshot_semantic_validation_rejects_tampered_assignments(
    tmp_path: Path,
) -> None:
    context = _commit_clade_snapshot(tmp_path)
    stage_root = _clade_stage_root(context)
    assignments_path = stage_root / CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH
    rows = parse_clade_assignments_tsv(assignments_path.read_text(encoding="utf-8"))
    tampered_rows = (
        rows[0].model_copy(update={"clade_id": "clade_tampered"}),
        *rows[1:],
    )
    write_text_atomically(
        path=assignments_path,
        payload=serialize_clade_assignments_tsv(tampered_rows),
    )
    _refresh_clade_manifest_metadata(
        stage_root=stage_root,
        relative_path=CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
    )

    with pytest.raises(StageSnapshotValidationError) as error_info:
        validate_committed_stage_snapshot(
            job_dir=context.launch_spec.job_dir,
            stage_id=CLADE_DETECTION_STAGE_ID,
            expected_job_id=context.launch_spec.job_id,
            expected_pipeline_version=context.launch_spec.pipeline_version,
            expected_task_id=context.launch_spec.task_id,
            expected_config_hash=context.launch_spec.config_hash,
        )

    assert error_info.value.code == StageSnapshotErrorCode.INVALID.value


def test_clade_stage_fails_without_committed_tree_snapshot(tmp_path: Path) -> None:
    config = _resolved_config(clade_detection_enabled=True, clade_threshold=0.3)
    context = _stage_context(
        tmp_path,
        config=config,
        stage_id=CLADE_DETECTION_STAGE_ID,
        stage_index=7,
    )
    stage = CladeDetectionStage()
    stage.preflight(context)

    with pytest.raises(CladeDetectionStageError) as error_info:
        stage.run(context, _ProgressRecorder())

    assert error_info.value.reason == "phylogenetic_tree_snapshot_invalid"


def test_clade_stage_wraps_detect_computation_errors_with_stage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _prepare_clade_stage_context(tmp_path)
    stage = CladeDetectionStage()
    stage.preflight(context)

    def _raise_detect_error(*args: Any, **kwargs: Any) -> Any:
        raise CladeDetectionComputationError(
            reason="leaf_count_mismatch",
            detail="PRIVATE_CLADE_MEMBER_MARKER detect failure detail",
        )

    monkeypatch.setattr(clade_detection_stage_module, "detect_inferred_clades", _raise_detect_error)

    with pytest.raises(CladeDetectionStageError) as error_info:
        stage.run(context, _ProgressRecorder())

    error = error_info.value
    assert error.reason == "clade_detection_leaf_count_mismatch"
    assert error.event_name == CLADE_DETECTION_FAILED_EVENT
    assert error.context == {"phase": "compute_node_metrics"}
    assert isinstance(error.__cause__, CladeDetectionComputationError)
    assert not (
        context.stage_staging_directory / CLADE_DETECTION_MANIFEST_RELATIVE_PATH
    ).exists()
    assert not (context.stage_staging_directory / INFERRED_CLADES_JSON_RELATIVE_PATH).exists()
    assert not (context.stage_staging_directory / CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH).exists()
    assert not (context.stage_staging_directory / CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH).exists()


def test_clade_stage_privacy_filters_computation_detail_from_public_failure_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _prepare_clade_stage_context(tmp_path)
    stage = CladeDetectionStage()
    stage.preflight(context)
    private_marker = "PRIVATE_CLADE_MEMBER_MARKER"

    def _raise_validation_error(*args: Any, **kwargs: Any) -> None:
        raise CladeDetectionComputationError(
            reason="clade_partition_overlap",
            detail=f"{private_marker} validation detail",
        )

    monkeypatch.setattr(
        clade_detection_stage_module,
        "validate_published_inferred_clades",
        _raise_validation_error,
    )

    with pytest.raises(CladeDetectionStageError) as error_info:
        stage.run(context, _ProgressRecorder())

    error = error_info.value
    assert error.reason == "clade_detection_clade_partition_overlap"
    assert error.event_name == CLADE_DETECTION_FAILED_EVENT
    assert error.context == {"phase": "validate_partition"}
    assert isinstance(error.__cause__, CladeDetectionComputationError)
    assert private_marker in error.__cause__.detail
    public_payload = json.dumps(
        {"reason": error.reason, "detail": error.detail, "context": error.context},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert private_marker not in public_payload
