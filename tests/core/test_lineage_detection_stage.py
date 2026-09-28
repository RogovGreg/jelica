from __future__ import annotations

import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import jelica_core.runtime.from_phase as from_phase_module
import jelica_core.runtime.lineage_detection_stage as lineage_stage_module
from jelica_core.config import AnalysisConfigInput, ResolvedAnalysisConfig, resolve_analysis_config
from jelica_core.lineage_detection import (
    LINEAGE_ASSIGNMENTS_TSV_RELATIVE_PATH,
    LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH,
    LINEAGE_DETECTION_STAGE_ID,
    LINEAGE_GROUPS_JSON_RELATIVE_PATH,
    LineageAssignmentStatus,
    NextcladeAvailability,
    NextcladePangoRecord,
    NextcladeRunResult,
)
from jelica_core.reporting import build_result_overview
from jelica_core.result_package import (
    JELICA_PACKAGE_CONFIGURATION_PATH,
    JELICA_PACKAGE_INPUT_MANIFEST_PATH,
    JELICA_PACKAGE_MANIFEST_PATH,
    JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
    JELICA_PACKAGE_TASK_PATH,
    JelicaPackageManifest,
    ResultPackageArtifactInfo,
    ResultPackageProducerInfo,
    ResultPackageStageInfo,
    ResultPackageTaskInfo,
    ResultPackageTaskStatus,
    compute_content_id,
    infer_media_type,
    serialize_stable_json,
)
from jelica_core.runtime.artifacts import (
    StageArtifactManifest,
    StageSnapshotErrorCode,
    StageSnapshotValidationError,
    commit_stage_directory,
    validate_committed_stage_snapshot,
    write_stage_manifest,
)
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
from jelica_core.runtime.lineage_detection_stage import (
    LineageDetectionStage,
    LineageDetectionStageError,
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
        self.values: list[float] = []

    def update(
        self,
        *,
        description: str | None = None,
        progress: float | None = None,
    ) -> None:
        _ = description
        if progress is not None:
            self.values.append(progress)

    def __call__(self, value: float) -> None:
        self.values.append(value)


class _FakeNextcladeRunner:
    def __init__(self, *, output: str) -> None:
        self.output = output
        self.probe_calls = 0
        self.run_calls = 0
        self.submitted_fasta = ""

    def probe(self) -> NextcladeAvailability:
        self.probe_calls += 1
        return NextcladeAvailability(
            available=True,
            executable=Path("/test/nextclade"),
            version="nextclade 3.14.0",
            source="test",
        )

    def run(
        self,
        *,
        availability: NextcladeAvailability,
        dataset: Path,
        input_fasta: Path,
        output_tsv: Path,
        working_directory: Path,
        control_check: Any,
        process_started: Any,
        process_stopped: Any,
    ) -> NextcladeRunResult:
        _ = (
            availability,
            dataset,
            working_directory,
            control_check,
            process_started,
            process_stopped,
        )
        self.run_calls += 1
        self.submitted_fasta = input_fasta.read_text(encoding="utf-8")
        output_tsv.write_text(self.output, encoding="utf-8")
        return NextcladeRunResult(output_tsv_path=output_tsv, diagnostics="")


class _NeverCalledRunner:
    def probe(self) -> NextcladeAvailability:
        raise AssertionError("disabled lineage detection must not probe Nextclade")


def _sequence_id(sequence: str) -> str:
    return "sha256:" + hashlib.sha256(sequence.encode("utf-8")).hexdigest()


def _facts(sequence: str, *, sequence_id: str) -> SequenceFacts:
    symbols = Counter(sequence)
    length = len(sequence)
    gc_count = symbols.get("G", 0) + symbols.get("C", 0)
    return SequenceFacts(
        source_length=length,
        ungapped_length=length,
        recognized_nucleotide_count=length,
        symbol_counts=dict(symbols),
        canonical_count=length,
        ambiguous_count=0,
        gap_count=0,
        invalid_symbol_count=0,
        invalid_symbol_counts={},
        gc_count=gc_count,
        gc_content_total=gc_count / length,
        resolved_gc_content=gc_count / length,
        expected_gc_count=float(gc_count),
        expected_gc_content=gc_count / length,
        u_count=0,
        sequence_id=sequence_id,
    )


def _resolved_config(*, dataset: Path | None, enabled: bool) -> ResolvedAnalysisConfig:
    lineage: dict[str, object] = {"enabled": enabled}
    if dataset is not None:
        lineage["dataset"] = str(dataset)
    return resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": ["sample-a.fa", "sample-b.fa", "sample-c.fa"],
                "alignment": {"mode": "none"},
                "comparative_analysis": {"enabled": False},
                "distance_matrix": {"enabled": False},
                "phylogenetic_tree": {"enabled": False},
                "clade_detection": {"enabled": False},
                "lineage_detection": lineage,
            }
        )
    ).config


def _stage_context(
    tmp_path: Path,
    *,
    config: ResolvedAnalysisConfig,
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
        stage_index=3,
        stage_staging_directory=job_dir / "staging" / LINEAGE_DETECTION_STAGE_ID / "worker-1",
    )


def _prepare_committed_input_processing_snapshot(context: StageContext) -> InputProcessingManifest:
    values = (("sample-a", "ACGT"), ("sample-b", "ACGT"), ("sample-c", "TTAA"))
    unique_sequences: dict[str, InputProcessingUniqueSequence] = {}
    samples: list[InputProcessingLogicalSample] = []
    for index, (sample_id, sequence) in enumerate(values):
        sequence_id = _sequence_id(sequence)
        samples.append(
            InputProcessingLogicalSample(
                sample_id=sample_id,
                provenance=LogicalSampleProvenance(
                    input_manifest_source_reference=f"source-{index}.fa",
                    materialized_relative_path=f"inputs/files/source-{index}.fa",
                    record_index=index,
                    format_hint=".fa",
                ),
                original_record_id=sample_id,
                validation_status=SampleValidationStatus.VALID,
                sequence_id=sequence_id,
                eligible_for_analysis=True,
            )
        )
        previous = unique_sequences.get(sequence_id)
        logical_sample_ids = (
            (*previous.logical_sample_ids, sample_id)
            if previous is not None
            else (sample_id,)
        )
        unique_sequences[sequence_id] = InputProcessingUniqueSequence(
            sequence_id=sequence_id,
            sequence_artifact_path=f"input_processing/sequences/{len(unique_sequences)}.fasta",
            ungapped_sequence_sha256=hashlib.sha256(sequence.encode("utf-8")).hexdigest(),
            facts=_facts(sequence, sequence_id=sequence_id),
            logical_sample_ids=logical_sample_ids,
        )
    manifest = InputProcessingManifest(
        task_id=context.launch_spec.task_id,
        job_id=context.launch_spec.job_id,
        config_revision_path="configs/000001.json",
        config_hash=context.launch_spec.config_hash,
        generated_at="2026-09-28T00:00:00Z",
        processing_state=InputProcessingState.COMPLETED,
        logical_samples=tuple(samples),
        unique_sequences=tuple(unique_sequences.values()),
        dataset_summary=InputProcessingDatasetSummary(
            discovered_record_count=3,
            valid_sample_count=3,
            invalid_sample_count=0,
            unique_sequence_count=2,
            duplicate_logical_sample_count=1,
            comparative_analysis_available=True,
        ),
    )
    stage_root = context.launch_spec.job_dir / "stages" / INPUT_PROCESSING_STAGE_ID
    manifest_path = stage_root / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(
        path=manifest_path,
        payload=json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
    )
    sequence_by_id = {
        _sequence_id(sequence): sequence
        for _, sequence in values
    }
    for item in manifest.unique_sequences:
        path = stage_root / item.sequence_artifact_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f">{item.sequence_id}\n{sequence_by_id[item.sequence_id]}\n",
            encoding="utf-8",
        )
    write_stage_manifest(
        directory=stage_root,
        manifest=StageArtifactManifest(
            stage_id=INPUT_PROCESSING_STAGE_ID,
            job_id=context.launch_spec.job_id,
            worker_instance_id=context.launch_spec.worker_instance_id,
            pipeline_version=context.launch_spec.pipeline_version,
            completed_at="2026-09-28T00:00:01Z",
            artifacts=input_processing_artifact_paths(manifest),
        ),
    )
    return manifest


def _commit_lineage_stage(context: StageContext, artifacts: tuple[str, ...]) -> None:
    manifest_path = write_stage_manifest(
        directory=context.stage_staging_directory,
        manifest=StageArtifactManifest(
            stage_id=LINEAGE_DETECTION_STAGE_ID,
            job_id=context.launch_spec.job_id,
            worker_instance_id=context.launch_spec.worker_instance_id,
            pipeline_version=context.launch_spec.pipeline_version,
            completed_at="2026-09-28T00:00:02Z",
            artifacts=artifacts,
        ),
    )
    commit_stage_directory(
        job_dir=context.launch_spec.job_dir,
        stage_id=LINEAGE_DETECTION_STAGE_ID,
        job_id=context.launch_spec.job_id,
        worker_instance_id=context.launch_spec.worker_instance_id,
        pipeline_version=context.launch_spec.pipeline_version,
        staging_directory=context.stage_staging_directory,
        manifest_path=manifest_path,
        task_id=context.launch_spec.task_id,
        config_hash=context.launch_spec.config_hash,
    )


def test_enabled_lineage_stage_runs_once_per_unique_sequence_and_commits(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "pathogen.json").write_text(
        json.dumps({"name": "nextstrain/sars-cov-2", "version": {"tag": "2026-09-01"}}),
        encoding="utf-8",
    )
    config = _resolved_config(dataset=dataset, enabled=True)
    context = _stage_context(tmp_path, config=config)
    input_manifest = _prepare_committed_input_processing_snapshot(context)
    first_id, second_id = (item.sequence_id for item in input_manifest.unique_sequences)
    runner = _FakeNextcladeRunner(
        output=(
            "seqName\tNextclade_pango\twarnings\terrors\n"
            f"{first_id}\tB.1.351\t\t\n"
            f"{second_id}\t\tQC warning\t\n"
        )
    )

    stage = LineageDetectionStage(runner=runner)  # type: ignore[arg-type]
    stage.preflight(context)
    result = stage.run(context, _ProgressRecorder())
    _commit_lineage_stage(context, result.artifacts)

    snapshot = validate_committed_stage_snapshot(
        job_dir=context.launch_spec.job_dir,
        stage_id=LINEAGE_DETECTION_STAGE_ID,
        expected_job_id=context.launch_spec.job_id,
        expected_pipeline_version=context.launch_spec.pipeline_version,
        expected_task_id=context.launch_spec.task_id,
        expected_config_hash=context.launch_spec.config_hash,
    )
    assert snapshot.domain_status == "completed"
    assert runner.probe_calls == 1
    assert runner.run_calls == 1
    assert runner.submitted_fasta.count(">sha256:") == 2
    committed_root = context.launch_spec.job_dir / "stages" / LINEAGE_DETECTION_STAGE_ID
    manifest = lineage_stage_module.LineageDetectionManifest.model_validate_json(
        (committed_root / LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert [(item.sample_id, item.lineage, item.status) for item in manifest.assignments] == [
        ("sample-a", "B.1.351", LineageAssignmentStatus.ASSIGNED),
        ("sample-b", "B.1.351", LineageAssignmentStatus.ASSIGNED),
        ("sample-c", None, LineageAssignmentStatus.UNASSIGNED),
    ]
    assert [(group.lineage, group.sample_ids) for group in manifest.groups] == [
        ("B.1.351", ("sample-a", "sample-b"))
    ]
    assert (committed_root / LINEAGE_ASSIGNMENTS_TSV_RELATIVE_PATH).is_file()
    assert (committed_root / LINEAGE_GROUPS_JSON_RELATIVE_PATH).is_file()


def test_disabled_lineage_stage_skips_nextclade_and_commits(tmp_path: Path) -> None:
    context = _stage_context(tmp_path, config=_resolved_config(dataset=None, enabled=False))
    stage = LineageDetectionStage(runner=_NeverCalledRunner())  # type: ignore[arg-type]
    stage.preflight(context)
    result = stage.run(context, _ProgressRecorder())
    _commit_lineage_stage(context, result.artifacts)

    snapshot = validate_committed_stage_snapshot(
        job_dir=context.launch_spec.job_dir,
        stage_id=LINEAGE_DETECTION_STAGE_ID,
        expected_job_id=context.launch_spec.job_id,
        expected_pipeline_version=context.launch_spec.pipeline_version,
        expected_task_id=context.launch_spec.task_id,
        expected_config_hash=context.launch_spec.config_hash,
    )
    assert snapshot.domain_status == "completed"
    assert result.artifacts == (LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH,)


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        (
            (
                NextcladePangoRecord(
                    sequence_name="sha256:" + "0" * 64,
                    lineage="XBB.1.5",
                ),
            ),
            "nextclade_output_unknown_sequence",
        ),
        (tuple(), "nextclade_output_missing_sequence"),
    ],
)
def test_nextclade_output_mapping_rejects_unknown_or_missing_records(
    tmp_path: Path,
    records: tuple[NextcladePangoRecord, ...],
    reason: str,
) -> None:
    context = _stage_context(tmp_path, config=_resolved_config(dataset=None, enabled=False))
    manifest = _prepare_committed_input_processing_snapshot(context)

    with pytest.raises(LineageDetectionStageError) as error_info:
        lineage_stage_module._expand_nextclade_records(records=records, manifest=manifest)

    assert error_info.value.reason == reason


def test_nextclade_output_mapping_rejects_duplicate_records(tmp_path: Path) -> None:
    context = _stage_context(tmp_path, config=_resolved_config(dataset=None, enabled=False))
    manifest = _prepare_committed_input_processing_snapshot(context)
    sequence_id = manifest.unique_sequences[0].sequence_id

    with pytest.raises(LineageDetectionStageError) as error_info:
        lineage_stage_module._expand_nextclade_records(
            records=(
                NextcladePangoRecord(sequence_name=sequence_id, lineage="B.1.351"),
                NextcladePangoRecord(sequence_name=sequence_id, lineage="B.1.351"),
            ),
            manifest=manifest,
        )

    assert error_info.value.reason == "nextclade_output_duplicate_sequence"


def test_lineage_pipeline_is_after_input_processing_and_targetable() -> None:
    pipeline = build_pipeline_definition(
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )
    stage_ids = tuple(stage.stage_id for stage in pipeline.stages)
    assert stage_ids.index("input_processing") + 1 == stage_ids.index(LINEAGE_DETECTION_STAGE_ID)
    assert stage_ids.index(LINEAGE_DETECTION_STAGE_ID) < stage_ids.index("alignment")


def test_from_phase_rewrites_lineage_manifest_identity(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "pathogen.json").write_text("{}", encoding="utf-8")
    context = _stage_context(tmp_path, config=_resolved_config(dataset=dataset, enabled=True))
    input_manifest = _prepare_committed_input_processing_snapshot(context)
    runner = _FakeNextcladeRunner(
        output=(
            "seqName\tNextclade_pango\n"
            + "\n".join(
                f"{item.sequence_id}\tB.1.351" for item in input_manifest.unique_sequences
            )
            + "\n"
        )
    )
    stage = LineageDetectionStage(runner=runner)  # type: ignore[arg-type]
    stage.preflight(context)
    result = stage.run(context, _ProgressRecorder())
    _commit_lineage_stage(context, result.artifacts)

    stage_root = context.launch_spec.job_dir / "stages" / LINEAGE_DETECTION_STAGE_ID
    from_phase_module._rewrite_stage_domain_manifest(
        stage_root=stage_root,
        stage_id=LINEAGE_DETECTION_STAGE_ID,
        task_id="task-2",
        job_id="job-2",
        config_hash="a" * 64,
        config_relative_path="configs/000002.json",
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        upstream_manifest_hashes={},
    )

    rewritten = lineage_stage_module.LineageDetectionManifest.model_validate_json(
        (stage_root / LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert rewritten.task_id == "task-2"
    assert rewritten.job_id == "job-2"
    assert rewritten.config_hash == "a" * 64


def test_lineage_snapshot_rejects_tampered_assignment_artifact(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "pathogen.json").write_text("{}", encoding="utf-8")
    context = _stage_context(tmp_path, config=_resolved_config(dataset=dataset, enabled=True))
    input_manifest = _prepare_committed_input_processing_snapshot(context)
    runner = _FakeNextcladeRunner(
        output=(
            "seqName\tNextclade_pango\n"
            + "\n".join(
                f"{item.sequence_id}\tB.1.351" for item in input_manifest.unique_sequences
            )
            + "\n"
        )
    )
    stage = LineageDetectionStage(runner=runner)  # type: ignore[arg-type]
    stage.preflight(context)
    result = stage.run(context, _ProgressRecorder())
    _commit_lineage_stage(context, result.artifacts)
    assignments_path = (
        context.launch_spec.job_dir
        / "stages"
        / LINEAGE_DETECTION_STAGE_ID
        / LINEAGE_ASSIGNMENTS_TSV_RELATIVE_PATH
    )
    assignments_path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(StageSnapshotValidationError) as error_info:
        validate_committed_stage_snapshot(
            job_dir=context.launch_spec.job_dir,
            stage_id=LINEAGE_DETECTION_STAGE_ID,
            expected_job_id=context.launch_spec.job_id,
            expected_pipeline_version=context.launch_spec.pipeline_version,
            expected_task_id=context.launch_spec.task_id,
            expected_config_hash=context.launch_spec.config_hash,
        )

    assert error_info.value.code in {
        StageSnapshotErrorCode.SIZE_MISMATCH.value,
        StageSnapshotErrorCode.HASH_MISMATCH.value,
    }


def test_lineage_artifacts_are_packaged_and_exposed_in_result_overview(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "pathogen.json").write_text(
        json.dumps({"name": "nextstrain/sars-cov-2", "version": {"tag": "2026-09-01"}}),
        encoding="utf-8",
    )
    config = _resolved_config(dataset=dataset, enabled=True)
    context = _stage_context(tmp_path, config=config)
    input_manifest = _prepare_committed_input_processing_snapshot(context)
    runner = _FakeNextcladeRunner(
        output=(
            "seqName\tNextclade_pango\n"
            f"{input_manifest.unique_sequences[0].sequence_id}\tB.1.351\n"
            f"{input_manifest.unique_sequences[1].sequence_id}\t\n"
        )
    )
    stage = LineageDetectionStage(runner=runner)  # type: ignore[arg-type]
    stage.preflight(context)
    result = stage.run(context, _ProgressRecorder())
    _commit_lineage_stage(context, result.artifacts)

    input_root = context.launch_spec.job_dir / "stages" / INPUT_PROCESSING_STAGE_ID
    lineage_root = context.launch_spec.job_dir / "stages" / LINEAGE_DETECTION_STAGE_ID
    package_input_manifest_path = (
        "results/input_processing/" + INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    )
    package_lineage_paths = tuple(
        "results/lineage_detection/" + relative_path for relative_path in result.artifacts
    )
    normalized_fasta = b"".join(
        (input_root / sequence.sequence_artifact_path).read_bytes()
        for sequence in input_manifest.unique_sequences
    )
    payloads: dict[str, bytes] = {
        JELICA_PACKAGE_TASK_PATH: (
            b'{"task_id":"task-1","status":"completed",'
            b'"created_at":"2026-09-28T00:00:00Z",'
            b'"completed_at":"2026-09-28T00:00:02Z"}\n'
        ),
        JELICA_PACKAGE_CONFIGURATION_PATH: (
            serialize_stable_json(config.model_dump(mode="json")).encode("utf-8")
        ),
        JELICA_PACKAGE_INPUT_MANIFEST_PATH: b"{}\n",
        JELICA_PACKAGE_NORMALIZED_FASTA_PATH: normalized_fasta,
        package_input_manifest_path: (
            input_root / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
        ).read_bytes(),
        **{
            package_path: (lineage_root / relative_path).read_bytes()
            for package_path, relative_path in zip(
                package_lineage_paths,
                result.artifacts,
                strict=True,
            )
        },
    }
    stage_by_path: dict[str, str | None] = {
        JELICA_PACKAGE_TASK_PATH: None,
        JELICA_PACKAGE_CONFIGURATION_PATH: None,
        JELICA_PACKAGE_INPUT_MANIFEST_PATH: "input_acquisition",
        JELICA_PACKAGE_NORMALIZED_FASTA_PATH: "input_processing",
        package_input_manifest_path: "input_processing",
        **{path: "lineage_detection" for path in package_lineage_paths},
    }
    artifacts = tuple(
        ResultPackageArtifactInfo(
            path=path,
            stage=stage_by_path[path],
            media_type=infer_media_type(path),
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        for path, payload in sorted(payloads.items())
    )
    package_manifest = JelicaPackageManifest(
        content_id=compute_content_id(artifacts=artifacts),
        producer=ResultPackageProducerInfo(version="1.0.0-test"),
        package_created_at="2026-09-28T00:00:03Z",
        task=ResultPackageTaskInfo(
            task_id="task-1",
            status=ResultPackageTaskStatus.COMPLETED,
            created_at="2026-09-28T00:00:00Z",
            completed_at="2026-09-28T00:00:02Z",
        ),
        stages=(
            ResultPackageStageInfo(
                name="input_acquisition",
                status="completed",
                artifacts=(JELICA_PACKAGE_INPUT_MANIFEST_PATH,),
            ),
            ResultPackageStageInfo(
                name="input_processing",
                status="completed",
                artifacts=tuple(
                    sorted((JELICA_PACKAGE_NORMALIZED_FASTA_PATH, package_input_manifest_path))
                ),
            ),
            ResultPackageStageInfo(
                name="lineage_detection",
                status="completed",
                artifacts=tuple(sorted(package_lineage_paths)),
            ),
        ),
        artifacts=artifacts,
    )
    package_path = tmp_path / "result.jelica"
    with zipfile.ZipFile(package_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, payload in sorted(payloads.items()):
            archive.writestr(path, payload)
        archive.writestr(
            JELICA_PACKAGE_MANIFEST_PATH,
            serialize_stable_json(package_manifest.model_dump(mode="json")).encode("utf-8"),
        )

    overview = build_result_overview(package_path=package_path)

    assert overview.lineage_detection is not None
    assert overview.lineage_detection.dataset_name == "nextstrain/sars-cov-2"
    assert overview.lineage_detection.assigned_sample_count == 2
    assert overview.lineage_detection.unassigned_sample_count == 1
    assert [(item.sample_id, item.lineage) for item in overview.input_processing.samples] == [
        ("sample-a", "B.1.351"),
        ("sample-b", "B.1.351"),
        ("sample-c", None),
    ]
