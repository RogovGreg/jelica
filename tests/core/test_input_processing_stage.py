from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from jelica_core.config import (
    AnalysisAlignmentMode,
    AnalysisConfigInput,
    ResolvedAnalysisStatisticsConfig,
    resolve_analysis_config,
)
from jelica_core.events import run_initialize_analysis_task_from_inputs, run_start_analytical_task
from jelica_core.result_package import (
    JELICA_PACKAGE_CONFIGURATION_PATH,
    JELICA_PACKAGE_MANIFEST_PATH,
    JELICA_PACKAGE_TASK_PATH,
    RESULT_PACKAGE_LINK_FILENAME,
    JelicaPackageManifest,
    ResultPackageTaskStatus,
    load_result_package_link,
    validate_result_package_file,
)
from jelica_core.runtime.artifacts import (
    StageArtifactManifest,
    StageCommitError,
    commit_stage_directory,
    validate_committed_stage_snapshot,
    write_stage_manifest,
)
from jelica_core.runtime.input_parsers import (
    INPUT_MANIFEST_RELATIVE_PATH,
    InputRecordParser,
    MaterializedInputFile,
    ParsedInputFileResult,
)
from jelica_core.runtime.input_processing_models import (
    INPUT_PROCESSING_KMER_HITS_DIR,
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    InputProcessingManifest,
    KmerHitsSidecar,
    ParsedInputRecord,
)
from jelica_core.runtime.input_processing_stage import (
    INPUT_PROCESSING_COMPLETED_EVENT,
    INPUT_PROCESSING_DATASET_INVALID_REASON,
    INPUT_PROCESSING_FILE_PROCESSED_EVENT,
    INPUT_PROCESSING_STARTED_EVENT,
    INPUT_PROCESSING_VALIDATION_FAILED_EVENT,
    InputProcessingError,
    InputProcessingStage,
)
from jelica_core.runtime.models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RuntimeStateCheckpoint,
    WorkerLaunchSpec,
)
from jelica_core.runtime.pipeline import StageContext, StageEventReporter, StageRunResult
from jelica_core.runtime.progress import NullProgressReporter, ProgressReporter
from jelica_core.runtime.sequence_inspector import SequenceInspectionResult, SequenceInspector
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import AnalyticalTaskRegistryService, AnalyticalTaskState
from jelica_core.tasks.storage import compute_config_hash, write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now


@dataclass(frozen=True, slots=True)
class _InputFileFixture:
    source_reference: str
    materialized_relative_path: str
    format_hint: str
    payload: str
    source_type: str = "local_file"


class _RecordingProgressReporter:
    def __init__(self) -> None:
        self.starts: list[tuple[str, float | None]] = []
        self.updates: list[tuple[str | None, float | None]] = []
        self.completions: list[str | None] = []

    def start(self, *, description: str, total: float | None = None) -> None:
        self.starts.append((description, total))

    def update(
        self,
        *,
        description: str | None = None,
        progress: float | None = None,
    ) -> None:
        self.updates.append((description, progress))

    def complete(self, *, description: str | None = None) -> None:
        self.completions.append(description)

    def __call__(self, progress: float) -> None:
        self.update(progress=progress)


def _resolved_config_document(
    *,
    samples: tuple[str, ...] = ("sample.fasta",),
    alignment_mode: str = "compute",
    reference: str | None = None,
    kmers: tuple[str, ...] = (),
    kmer_strand: str = "forward",
) -> dict[str, object]:
    statistics: dict[str, object] | None = None
    if len(kmers) > 0:
        statistics = {"kmers": list(kmers), "kmer_strand": kmer_strand}
    config = resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": list(samples),
                "alignment": {"mode": alignment_mode},
                "reference": reference,
                "statistics": statistics,
            }
        )
    ).config
    return config.model_dump(mode="json")


def _build_stage_context(
    tmp_path: Path,
    *,
    config_document: dict[str, object],
    task_id: str = "task-1",
    job_id: str = "job-1",
    worker_instance_id: str = "worker-1",
    event_reporter: StageEventReporter | None = None,
    control_check: Callable[[], None] | None = None,
) -> StageContext:
    task_dir = tmp_path / "task"
    job_dir = task_dir / "jobs" / job_id
    config_revision_path = task_dir / "configs" / "000001.json"
    config_revision_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(
        path=config_revision_path,
        payload=json.dumps(config_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    launch_spec = WorkerLaunchSpec(
        task_id=task_id,
        job_id=job_id,
        worker_instance_id=worker_instance_id,
        lease_token="lease-1",
        database_path=tmp_path / "jelica.db",
        task_dir=task_dir,
        job_dir=job_dir,
        config_revision_path=config_revision_path,
        config_hash=compute_config_hash(config_document),
        runtime_state_json=RuntimeStateCheckpoint.new(
            pipeline_version=DEFAULT_PIPELINE_VERSION
        ).to_runtime_state_json(),
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )
    return StageContext(
        launch_spec=launch_spec,
        stage_index=2,
        stage_staging_directory=job_dir / "staging" / "input_processing" / worker_instance_id,
        event_reporter=event_reporter,
        control_check=control_check,
    )


def _prepare_acquisition_output(
    *,
    context: StageContext,
    input_files: tuple[_InputFileFixture, ...],
    acquisition_root: Path | None = None,
) -> None:
    target_root = acquisition_root or (
        context.launch_spec.job_dir / "stages" / "input_acquisition"
    )
    materialized_items: list[dict[str, object]] = []
    for input_file in input_files:
        target_path = target_root / input_file.materialized_relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(path=target_path, payload=input_file.payload)
        payload_bytes = input_file.payload.encode("utf-8")
        materialized_items.append(
            {
                "relative_path": input_file.materialized_relative_path,
                "source_type": input_file.source_type,
                "source_reference": input_file.source_reference,
                "format_hint": input_file.format_hint,
                "size_bytes": len(payload_bytes),
                "sha256": hashlib.sha256(payload_bytes).hexdigest(),
            }
        )
    manifest_payload = {
        "schema_version": 1,
        "task_id": context.launch_spec.task_id,
        "job_id": context.launch_spec.job_id,
        "config_revision_path": str(context.launch_spec.config_revision_path),
        "config_hash": context.launch_spec.config_hash,
        "generated_at": serialize_utc_datetime(utc_now()),
        "sources": [],
        "materialized_files": materialized_items,
        "skipped_duplicates": [],
        "source_errors": [],
    }
    manifest_path = target_root / INPUT_MANIFEST_RELATIVE_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(
        path=manifest_path,
        payload=json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _run_stage(
    *,
    context: StageContext,
    stage: InputProcessingStage | None = None,
    progress_reporter: ProgressReporter | None = None,
) -> tuple[InputProcessingStage, StageRunResult]:
    active_stage = stage or InputProcessingStage()
    reporter = progress_reporter or NullProgressReporter()
    reporter.start(description=active_stage.stage_id)
    active_stage.preflight(context)
    result = active_stage.run(context, reporter)
    reporter.complete(description=active_stage.stage_id)
    return active_stage, result


def _load_staging_manifest(context: StageContext) -> InputProcessingManifest:
    manifest_path = context.stage_staging_directory / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return InputProcessingManifest.model_validate(payload)


def _normalize_manifest_for_comparison(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    normalized.pop("generated_at", None)
    return normalized


def _initialize_core(jelica_home: Path) -> CoreConfigService:
    service = CoreConfigService(jelica_home=jelica_home)
    service.initialize_system_config(force=True)
    return service


def _write_sample(path: Path, *, sample_id: str, sequence: str = "ACGT") -> None:
    path.write_text(f">{sample_id}\n{sequence}\n", encoding="utf-8")


def _initialize_task_for_runtime(
    *,
    service: CoreConfigService,
    sample_paths: tuple[Path, ...],
    raw_overrides: tuple[str, ...] = (),
) -> str:
    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=raw_overrides,
        positional_sources=tuple(str(path) for path in sample_paths),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    return initialized.value.task_id


def test_stage_builds_manifest_and_artifacts_for_single_valid_sample(tmp_path: Path) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/single.fasta",
                materialized_relative_path="inputs/files/0001_single.fasta",
                format_hint=".fasta",
                payload=">single\nACGU\n",
            ),
        ),
    )
    _stage, result = _run_stage(context=context)
    assert result.failure is None

    manifest = _load_staging_manifest(context)
    assert manifest.dataset_issues == ()
    assert manifest.dataset_summary.valid_sample_count == 1
    assert manifest.dataset_summary.comparative_analysis_available is False
    assert len(manifest.logical_samples) == 1
    assert manifest.logical_samples[0].eligible_for_analysis is True
    assert len(manifest.unique_sequences) == 1

    unique = manifest.unique_sequences[0]
    sequence_path = context.stage_staging_directory / unique.sequence_artifact_path
    sequence_content = sequence_path.read_text(encoding="utf-8")
    assert sequence_content.startswith(f">{unique.sequence_id}\n")
    assert "\nACGU\n" in sequence_content
    assert sequence_content.endswith("\n")
    assert unique.kmer_hits_path is None


def test_stage_resolves_materialized_paths_after_acquisition_commit_move(tmp_path: Path) -> None:
    class _CommitAfterFirstFileParser:
        def __init__(self, *, worker_root: Path, committed_root: Path) -> None:
            self._delegate = InputRecordParser()
            self._calls = 0
            self._worker_root = worker_root
            self._committed_root = committed_root

        def parse_materialized_file(
            self,
            *,
            stage_staging_directory: Path,
            materialized_file: MaterializedInputFile,
            alignment_mode: AnalysisAlignmentMode | str,
        ) -> ParsedInputFileResult:
            result = self._delegate.parse_materialized_file(
                stage_staging_directory=stage_staging_directory,
                materialized_file=materialized_file,
                alignment_mode=alignment_mode,
            )
            self._calls += 1
            if self._calls == 1 and self._worker_root.exists():
                self._committed_root.parent.mkdir(parents=True, exist_ok=True)
                self._worker_root.replace(self._committed_root)
            return result

    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    worker_acquisition_root = (
        context.launch_spec.job_dir
        / "staging"
        / "input_acquisition"
        / context.launch_spec.worker_instance_id
    )
    committed_acquisition_root = context.launch_spec.job_dir / "stages" / "input_acquisition"
    _prepare_acquisition_output(
        context=context,
        acquisition_root=worker_acquisition_root,
        input_files=(
            _InputFileFixture(
                source_reference="NC_000913.3",
                materialized_relative_path="inputs/files/0001_nc_record.fasta",
                format_hint=".fasta",
                payload=">NC_000913.3\nACGTACGTAC\n",
                source_type="ncbi_nucleotide_record",
            ),
            _InputFileFixture(
                source_reference="inline(length=13,preview=ACCTCTGGG...)",
                materialized_relative_path="inputs/files/0002_inline_sequence.fasta",
                format_hint=".fasta",
                payload=">jelica_inline_sequence_0001\nACCTCTGGGCAAA\n",
                source_type="inline_sequence",
            ),
        ),
    )

    stage = InputProcessingStage(
        parser=_CommitAfterFirstFileParser(
            worker_root=worker_acquisition_root,
            committed_root=committed_acquisition_root,
        )
    )
    _stage, result = _run_stage(context=context, stage=stage)
    assert result.failure is None

    manifest = _load_staging_manifest(context)
    assert manifest.dataset_summary.discovered_record_count == 2
    assert manifest.dataset_summary.valid_sample_count == 2
    assert manifest.dataset_summary.invalid_sample_count == 0
    assert manifest.dataset_summary.unique_sequence_count == 2
    assert len(manifest.processed_files) == 2
    assert all(item.status.value == "processed" for item in manifest.processed_files)

    inline_sample = next(
        sample
        for sample in manifest.logical_samples
        if sample.original_record_id == "jelica_inline_sequence_0001"
    )
    assert inline_sample.validation_status.value == "valid"
    assert inline_sample.inspection_facts is None
    assert inline_sample.sequence_id is not None

    inline_unique_sequence = next(
        item for item in manifest.unique_sequences if item.sequence_id == inline_sample.sequence_id
    )
    assert inline_unique_sequence.facts.source_length == 13
    assert inline_unique_sequence.facts.ungapped_length == 13
    assert inline_unique_sequence.facts.canonical_count == 13
    assert inline_unique_sequence.facts.invalid_symbol_count == 0
    inline_sequence_artifact = (
        context.stage_staging_directory / inline_unique_sequence.sequence_artifact_path
    ).read_text(encoding="utf-8")
    assert inline_sequence_artifact.startswith(f">{inline_unique_sequence.sequence_id}\n")
    assert "\nACCTCTGGGCAAA\n" in inline_sequence_artifact


def test_stage_manifest_omits_normalized_sequence_payload(tmp_path: Path) -> None:
    class _CapturingInspector:
        def __init__(self) -> None:
            self._delegate = SequenceInspector()
            self.captured: list[SequenceInspectionResult] = []

        def inspect(
            self,
            parsed_record: ParsedInputRecord,
            *,
            statistics_config: ResolvedAnalysisStatisticsConfig,
            alignment_mode: AnalysisAlignmentMode | str,
            control_check: Callable[[], None] | None = None,
        ) -> SequenceInspectionResult:
            result = self._delegate.inspect(
                parsed_record,
                statistics_config=statistics_config,
                alignment_mode=alignment_mode,
                control_check=control_check,
            )
            self.captured.append(result)
            return result

    sequence = "ACCTCTGGGCAAA"
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="inline(length=13,preview=ACCTCTGGG...)",
                materialized_relative_path="inputs/files/0001_inline_sequence.fasta",
                format_hint=".fasta",
                payload=f">jelica_inline_sequence_0001\n{sequence}\n",
                source_type="inline_sequence",
            ),
        ),
    )
    inspector = _CapturingInspector()
    stage = InputProcessingStage(inspector=inspector)
    _stage, result = _run_stage(context=context, stage=stage)
    assert result.failure is None
    assert len(inspector.captured) == 1
    assert inspector.captured[0].normalized_sequence == sequence

    manifest_path = context.stage_staging_directory / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_payload = json.loads(manifest_text)
    unique_payload = manifest_payload["unique_sequences"][0]
    facts_payload = unique_payload["facts"]
    assert "normalized_sequence" not in facts_payload
    assert sequence not in manifest_text

    sequence_artifact = (
        context.stage_staging_directory / str(unique_payload["sequence_artifact_path"])
    ).read_text(encoding="utf-8")
    assert f"\n{sequence}\n" in sequence_artifact


def test_stage_enables_control_check_before_commit(tmp_path: Path) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/control-check.fasta",
                materialized_relative_path="inputs/files/0001_control_check.fasta",
                format_hint=".fasta",
                payload=">sample\nACGT\n",
            ),
        ),
    )

    _stage, result = _run_stage(context=context)
    assert result.failure is None
    assert result.check_control_before_commit is True


def test_stage_keeps_invalid_logical_sample_with_inspection_facts(tmp_path: Path) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/multi.fasta",
                materialized_relative_path="inputs/files/0001_multi.fasta",
                format_hint=".fasta",
                payload=">valid\nACGT\n>invalid\nAXGT\n",
            ),
        ),
    )
    _stage, result = _run_stage(context=context)
    assert result.failure is None

    manifest = _load_staging_manifest(context)
    assert manifest.dataset_summary.valid_sample_count == 1
    assert manifest.dataset_summary.invalid_sample_count == 1
    assert len(manifest.logical_samples) == 2
    invalid = next(
        sample
        for sample in manifest.logical_samples
        if not sample.eligible_for_analysis
    )
    assert invalid.inspection_facts is not None
    assert invalid.inspection_facts.invalid_symbol_count == 1
    assert len(manifest.unique_sequences) == 1


def test_stage_deduplicates_and_writes_single_kmer_sidecar_for_unique_sequence(
    tmp_path: Path,
) -> None:
    emitted_events: list[tuple[str, dict[str, object]]] = []
    progress_reporter = _RecordingProgressReporter()

    def _event_reporter(event_name: str, context: dict[str, object]) -> None:
        emitted_events.append((event_name, context))

    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(kmers=("AT", "RY")),
        event_reporter=_event_reporter,
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/dups.fasta",
                materialized_relative_path="inputs/files/0001_dups.fasta",
                format_hint=".fasta",
                payload=">a\nATAT\n>b\nATAT\n",
            ),
        ),
    )
    _stage, result = _run_stage(
        context=context,
        progress_reporter=progress_reporter,
    )
    assert result.failure is None

    manifest = _load_staging_manifest(context)
    assert manifest.dataset_summary.valid_sample_count == 2
    assert manifest.dataset_summary.unique_sequence_count == 1
    assert manifest.dataset_summary.duplicate_logical_sample_count == 1
    assert len(manifest.unique_sequences) == 1

    unique = manifest.unique_sequences[0]
    assert unique.kmer_hits_path is not None
    sidecar_path = context.stage_staging_directory / unique.kmer_hits_path
    sidecar = KmerHitsSidecar.model_validate(json.loads(sidecar_path.read_text(encoding="utf-8")))
    assert sidecar.schema_version == 1
    assert sidecar.sequence_id == unique.sequence_id
    assert [item.query for item in sidecar.queries] == ["AT", "RY"]
    assert all(
        summary.hits_path == unique.kmer_hits_path
        for summary in unique.facts.kmer_summaries
    )

    generic_manifest = StageArtifactManifest(
        stage_id="input_processing",
        job_id=context.launch_spec.job_id,
        worker_instance_id=context.launch_spec.worker_instance_id,
        pipeline_version=context.launch_spec.pipeline_version,
        completed_at="2026-08-05T00:00:00Z",
        artifacts=result.artifacts,
    )
    runtime_surface_payload = json.dumps(
        {
            "generic_manifest": generic_manifest.model_dump(mode="json"),
            "events": emitted_events,
            "progress": {
                "starts": progress_reporter.starts,
                "updates": progress_reporter.updates,
                "completions": progress_reporter.completions,
            },
            "failure": result.failure,
        },
        ensure_ascii=False,
        default=str,
    )
    assert all(f'"{item.query}"' not in runtime_surface_payload for item in sidecar.queries)

    manifest_payload = json.loads(
        (context.stage_staging_directory / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )
    first_unique = manifest_payload["unique_sequences"][0]
    assert "hits" not in first_unique["facts"]


@pytest.mark.parametrize(
    "kmers",
    [("AT",), ("AT", "RY")],
    ids=["one-query", "multiple-queries"],
)
def test_stage_commits_kmer_sidecars_with_canonical_artifact_tuple(
    tmp_path: Path,
    kmers: tuple[str, ...],
) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(kmers=kmers),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/samples.fasta",
                materialized_relative_path="inputs/files/0001_samples.fasta",
                format_hint=".fasta",
                payload=">a\nATAT\n>b\nACGT\n",
            ),
        ),
    )

    _stage, result = _run_stage(context=context)
    assert result.failure is None
    domain_manifest = _load_staging_manifest(context)
    domain_artifacts = [INPUT_PROCESSING_MANIFEST_RELATIVE_PATH]
    for unique_sequence in domain_manifest.unique_sequences:
        domain_artifacts.append(unique_sequence.sequence_artifact_path)
        assert unique_sequence.kmer_hits_path is not None
        domain_artifacts.append(unique_sequence.kmer_hits_path)
    assert result.artifacts == tuple(domain_artifacts)

    generic_manifest = StageArtifactManifest(
        stage_id="input_processing",
        job_id=context.launch_spec.job_id,
        worker_instance_id=context.launch_spec.worker_instance_id,
        pipeline_version=context.launch_spec.pipeline_version,
        completed_at="2026-08-05T00:00:00Z",
        artifacts=result.artifacts,
    )
    if len(kmers) > 1:
        inconsistent_manifest_path = write_stage_manifest(
            directory=context.stage_staging_directory,
            manifest=generic_manifest.model_copy(
                update={"artifacts": generic_manifest.artifacts[:-1]}
            ),
        )
        with pytest.raises(
            StageCommitError,
            match="generic and input-processing artifact sets are inconsistent",
        ):
            commit_stage_directory(
                job_dir=context.launch_spec.job_dir,
                stage_id="input_processing",
                job_id=context.launch_spec.job_id,
                worker_instance_id=context.launch_spec.worker_instance_id,
                pipeline_version=context.launch_spec.pipeline_version,
                staging_directory=context.stage_staging_directory,
                manifest_path=inconsistent_manifest_path,
                task_id=context.launch_spec.task_id,
                config_hash=context.launch_spec.config_hash,
            )
        assert context.stage_staging_directory.is_dir()

    generic_manifest_path = write_stage_manifest(
        directory=context.stage_staging_directory,
        manifest=generic_manifest,
    )
    committed_manifest = commit_stage_directory(
        job_dir=context.launch_spec.job_dir,
        stage_id="input_processing",
        job_id=context.launch_spec.job_id,
        worker_instance_id=context.launch_spec.worker_instance_id,
        pipeline_version=context.launch_spec.pipeline_version,
        staging_directory=context.stage_staging_directory,
        manifest_path=generic_manifest_path,
        task_id=context.launch_spec.task_id,
        config_hash=context.launch_spec.config_hash,
    )
    assert committed_manifest.artifacts == tuple(domain_artifacts)
    assert context.stage_staging_directory.exists() is False

    snapshot = validate_committed_stage_snapshot(
        job_dir=context.launch_spec.job_dir,
        stage_id="input_processing",
        expected_job_id=context.launch_spec.job_id,
        expected_pipeline_version=context.launch_spec.pipeline_version,
        expected_task_id=context.launch_spec.task_id,
        expected_config_hash=context.launch_spec.config_hash,
    )
    assert snapshot.manifest.artifacts == tuple(domain_artifacts)
    fingerprints = {
        fingerprint.relative_path: fingerprint
        for fingerprint in snapshot.artifact_fingerprints
    }
    committed_root = context.launch_spec.job_dir / "stages" / "input_processing"
    for unique_sequence in domain_manifest.unique_sequences:
        assert unique_sequence.kmer_hits_path is not None
        sidecar_path = committed_root / unique_sequence.kmer_hits_path
        sidecar_bytes = sidecar_path.read_bytes()
        fingerprint = fingerprints[unique_sequence.kmer_hits_path]
        assert fingerprint.size_bytes == len(sidecar_bytes)
        assert fingerprint.sha256 == hashlib.sha256(sidecar_bytes).hexdigest()
        assert fingerprint.record_count is None
        sidecar_payload = json.loads(sidecar_bytes)
        queries = sidecar_payload["queries"]
        assert all("query" in query_result for query_result in queries)
        assert [query_result["query"] for query_result in queries] == list(kmers)

    assert all(".tmp" not in artifact for artifact in snapshot.manifest.artifacts)


def test_stage_does_not_write_kmer_sidecars_when_kmers_are_empty(tmp_path: Path) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/no-kmers.fasta",
                materialized_relative_path="inputs/files/0001_no_kmers.fasta",
                format_hint=".fasta",
                payload=">a\nACGT\n",
            ),
        ),
    )
    _stage, result = _run_stage(context=context)
    assert result.failure is None

    manifest = _load_staging_manifest(context)
    assert manifest.unique_sequences[0].kmer_hits_path is None
    sidecar_dir = context.stage_staging_directory / INPUT_PROCESSING_KMER_HITS_DIR
    assert sidecar_dir.exists() is False


def test_stage_publishes_manifest_and_returns_failure_when_no_valid_samples(tmp_path: Path) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/invalid.fasta",
                materialized_relative_path="inputs/files/0001_invalid.fasta",
                format_hint=".fasta",
                payload=">bad\nXXXX\n",
            ),
        ),
    )
    _stage, result = _run_stage(context=context)
    assert result.failure is not None
    assert result.failure.reason == INPUT_PROCESSING_DATASET_INVALID_REASON

    manifest = _load_staging_manifest(context)
    issue_codes = {issue.code for issue in manifest.dataset_issues}
    assert "no_valid_samples" in issue_codes
    assert manifest.dataset_summary.valid_sample_count == 0
    assert len(manifest.unique_sequences) == 0


def test_stage_fails_dataset_for_prealigned_length_mismatch_after_publication(
    tmp_path: Path,
) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(
            alignment_mode="prealigned",
            reference="r1",
        ),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/a.afa",
                materialized_relative_path="inputs/files/0001_a.afa",
                format_hint=".afa",
                payload=">r1\nA-CG\n",
            ),
            _InputFileFixture(
                source_reference="data/b.afa",
                materialized_relative_path="inputs/files/0002_b.afa",
                format_hint=".afa",
                payload=">r2\nACGTA-\n",
            ),
        ),
    )
    _stage, result = _run_stage(context=context)
    assert result.failure is not None

    manifest = _load_staging_manifest(context)
    issue_codes = {issue.code for issue in manifest.dataset_issues}
    assert "prealigned_length_mismatch" in issue_codes
    assert manifest.dataset_summary.comparative_analysis_available is False


def test_stage_marks_external_reference_selector_as_dataset_error(tmp_path: Path) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(reference="outside/ref.fasta::ref"),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/a.fasta",
                materialized_relative_path="inputs/files/0001_a.fasta",
                format_hint=".fasta",
                payload=">ref\nACGT\n",
            ),
            _InputFileFixture(
                source_reference="data/b.fasta",
                materialized_relative_path="inputs/files/0002_b.fasta",
                format_hint=".fasta",
                payload=">x\nTGCA\n",
            ),
        ),
    )
    _stage, result = _run_stage(context=context)
    assert result.failure is not None

    manifest = _load_staging_manifest(context)
    issue_codes = {issue.code for issue in manifest.dataset_issues}
    assert "reference_not_found" in issue_codes


def test_stage_rejects_unsafe_manifest_path_and_does_not_publish_manifest(tmp_path: Path) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    acquisition_root = context.launch_spec.job_dir / "stages" / "input_acquisition"
    outside_file = context.launch_spec.job_dir / "escape.fasta"
    outside_file.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(path=outside_file, payload=">a\nACGT\n")
    manifest_payload = {
        "schema_version": 1,
        "task_id": context.launch_spec.task_id,
        "job_id": context.launch_spec.job_id,
        "config_revision_path": str(context.launch_spec.config_revision_path),
        "config_hash": context.launch_spec.config_hash,
        "generated_at": "2026-08-02T00:00:00Z",
        "sources": [],
        "materialized_files": [
            {
                "relative_path": "../escape.fasta",
                "source_type": "local_file",
                "source_reference": "data/escape.fasta",
                "format_hint": ".fasta",
                "size_bytes": outside_file.stat().st_size,
                "sha256": hashlib.sha256(outside_file.read_bytes()).hexdigest(),
            }
        ],
        "skipped_duplicates": [],
        "source_errors": [],
    }
    manifest_path = acquisition_root / INPUT_MANIFEST_RELATIVE_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(
        path=manifest_path,
        payload=json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    stage = InputProcessingStage()
    stage.preflight(context)
    with pytest.raises(InputProcessingError):
        stage.run(context, NullProgressReporter())

    assert (
        context.stage_staging_directory / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    ).exists() is False


def test_stage_preserves_u_and_uses_digest_only_artifact_names(tmp_path: Path) -> None:
    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/u-vs-t.fasta",
                materialized_relative_path="inputs/files/0001_u_vs_t.fasta",
                format_hint=".fasta",
                payload=">u\nAUUG\n>t\nATTG\n",
            ),
        ),
    )
    _stage, result = _run_stage(context=context)
    assert result.failure is None

    manifest = _load_staging_manifest(context)
    assert len(manifest.unique_sequences) == 2
    artifacts = {item.sequence_artifact_path: item for item in manifest.unique_sequences}
    for artifact_path in artifacts:
        assert ":" not in Path(artifact_path).name
    contents = {
        artifact_path: (context.stage_staging_directory / artifact_path).read_text(encoding="utf-8")
        for artifact_path in artifacts
    }
    assert any("\nAUUG\n" in value for value in contents.values())
    assert any("\nATTG\n" in value for value in contents.values())


def test_stage_is_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    def _execute(run_root: Path) -> tuple[dict[str, object], dict[str, str]]:
        context = _build_stage_context(
            run_root,
            config_document=_resolved_config_document(kmers=("AT",)),
            task_id="task-det",
            job_id="job-det",
            worker_instance_id="worker-det",
        )
        _prepare_acquisition_output(
            context=context,
            input_files=(
                _InputFileFixture(
                    source_reference="data/det.fasta",
                    materialized_relative_path="inputs/files/0001_det.fasta",
                    format_hint=".fasta",
                    payload=">a\nATAT\n>b\nATAT\n",
                ),
            ),
        )
        _stage, result = _run_stage(context=context)
        assert result.failure is None
        manifest_payload = json.loads(
            (context.stage_staging_directory / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH).read_text(
                encoding="utf-8"
            )
        )
        artifact_contents: dict[str, str] = {}
        for unique in manifest_payload["unique_sequences"]:
            sequence_path = str(unique["sequence_artifact_path"])
            artifact_contents[sequence_path] = (
                context.stage_staging_directory / sequence_path
            ).read_text(encoding="utf-8")
            kmer_hits_path = unique.get("kmer_hits_path")
            if isinstance(kmer_hits_path, str):
                artifact_contents[kmer_hits_path] = (
                    context.stage_staging_directory / kmer_hits_path
                ).read_text(encoding="utf-8")
        return manifest_payload, artifact_contents

    first_manifest, first_artifacts = _execute(tmp_path / "run-1")
    second_manifest, second_artifacts = _execute(tmp_path / "run-2")

    assert _normalize_manifest_for_comparison(first_manifest) == _normalize_manifest_for_comparison(
        second_manifest
    )
    assert first_artifacts == second_artifacts


def test_stage_uses_parser_once_per_file_and_inspector_once_per_record(tmp_path: Path) -> None:
    class CountingParser:
        def __init__(self) -> None:
            self.calls = 0
            self._delegate = InputRecordParser()

        def parse_materialized_file(
            self,
            *,
            stage_staging_directory: Path,
            materialized_file: MaterializedInputFile,
            alignment_mode: AnalysisAlignmentMode | str,
        ) -> ParsedInputFileResult:
            self.calls += 1
            return self._delegate.parse_materialized_file(
                stage_staging_directory=stage_staging_directory,
                materialized_file=materialized_file,
                alignment_mode=alignment_mode,
            )

    class CountingInspector:
        def __init__(self) -> None:
            self.calls = 0
            self._delegate = SequenceInspector()

        def inspect(
            self,
            parsed_record: ParsedInputRecord,
            *,
            statistics_config: ResolvedAnalysisStatisticsConfig,
            alignment_mode: AnalysisAlignmentMode | str,
            control_check: Callable[[], None] | None = None,
        ) -> SequenceInspectionResult:
            self.calls += 1
            return self._delegate.inspect(
                parsed_record,
                statistics_config=statistics_config,
                alignment_mode=alignment_mode,
                control_check=control_check,
            )

    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/a.fasta",
                materialized_relative_path="inputs/files/0001_a.fasta",
                format_hint=".fasta",
                payload=">a\nACGT\n",
            ),
            _InputFileFixture(
                source_reference="data/b.fasta",
                materialized_relative_path="inputs/files/0002_b.fasta",
                format_hint=".fasta",
                payload=">b\nTGCA\n",
            ),
        ),
    )
    parser = CountingParser()
    inspector = CountingInspector()
    stage = InputProcessingStage(parser=parser, inspector=inspector)
    _stage, result = _run_stage(context=context, stage=stage)

    assert result.failure is None
    assert parser.calls == 2
    assert inspector.calls == 2


def test_stage_emits_live_progress_and_single_file_event_for_multi_record_file(
    tmp_path: Path,
) -> None:
    emitted_events: list[tuple[str, dict[str, object]]] = []
    progress_reporter = _RecordingProgressReporter()

    def _event_reporter(event_name: str, context: dict[str, object]) -> None:
        emitted_events.append((event_name, context))

    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
        event_reporter=_event_reporter,
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/multi.fasta",
                materialized_relative_path="inputs/files/0001_multi.fasta",
                format_hint=".fasta",
                payload=">valid\nACGT\n>broken\nAXGT\n",
            ),
        ),
    )

    _stage, result = _run_stage(context=context, progress_reporter=progress_reporter)
    assert result.failure is None

    names = [name for name, _ in emitted_events]
    assert names.count(INPUT_PROCESSING_STARTED_EVENT) == 1
    assert names.count(INPUT_PROCESSING_FILE_PROCESSED_EVENT) == 1
    assert names.count(INPUT_PROCESSING_COMPLETED_EVENT) == 1
    assert "INPUT_PROCESSING_FILE_STARTED" not in names
    assert "INPUT_PROCESSING_RECORD_STARTED" not in names
    assert "INPUT_PROCESSING_RECORD_PROCESSED" not in names

    assert progress_reporter.starts == [("input_processing", None)]
    assert progress_reporter.completions == ["input_processing"]
    assert progress_reporter.updates
    assert any(description is not None for description, _ in progress_reporter.updates)
    assert any(progress is not None for _, progress in progress_reporter.updates)

    file_event_context = next(
        context for name, context in emitted_events if name == INPUT_PROCESSING_FILE_PROCESSED_EVENT
    )
    assert file_event_context["parsed_record_count"] == 2
    assert file_event_context["valid_sample_count"] == 1
    assert file_event_context["invalid_sample_count"] == 1
    assert file_event_context["processing_status"] == "processed"
    assert file_event_context["event_type"] == "warning"
    assert "issue_count_by_code" in file_event_context
    assert "issue_count_by_severity" in file_event_context


def test_stage_emits_validation_failed_event_without_completed_success_event(
    tmp_path: Path,
) -> None:
    emitted_events: list[tuple[str, dict[str, object]]] = []

    def _event_reporter(event_name: str, context: dict[str, object]) -> None:
        emitted_events.append((event_name, context))

    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
        event_reporter=_event_reporter,
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/invalid.fasta",
                materialized_relative_path="inputs/files/0001_invalid.fasta",
                format_hint=".fasta",
                payload=">bad\nXXXX\n",
            ),
        ),
    )

    _stage, result = _run_stage(context=context)
    assert result.failure is not None
    assert result.failure.reason == INPUT_PROCESSING_DATASET_INVALID_REASON

    names = [name for name, _ in emitted_events]
    assert INPUT_PROCESSING_VALIDATION_FAILED_EVENT in names
    assert INPUT_PROCESSING_COMPLETED_EVENT not in names
    validation_context = next(
        context
        for name, context in emitted_events
        if name == INPUT_PROCESSING_VALIDATION_FAILED_EVENT
    )
    assert validation_context["manifest_path"] == INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    issue_codes = validation_context["dataset_issue_codes"]
    assert isinstance(issue_codes, list)
    assert "no_valid_samples" in issue_codes
    assert (
        context.stage_staging_directory / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    ).is_file()


def test_stage_control_check_can_interrupt_during_sequence_inspection(tmp_path: Path) -> None:
    class _StopRequested(RuntimeError):
        pass

    control_checks = 0

    def _control_check() -> None:
        nonlocal control_checks
        control_checks += 1
        if control_checks == 5:
            raise _StopRequested("cancelled")

    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
        control_check=_control_check,
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/large.fasta",
                materialized_relative_path="inputs/files/0001_large.fasta",
                format_hint=".fasta",
                payload=f">large\n{'A' * 20000}\n",
            ),
        ),
    )
    stage = InputProcessingStage()
    stage.preflight(context)
    with pytest.raises(_StopRequested):
        stage.run(context, NullProgressReporter())
    assert control_checks >= 5
    assert (
        context.stage_staging_directory / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    ).exists() is False


def test_stage_control_check_interrupts_before_manifest_publication(tmp_path: Path) -> None:
    class _StopRequested(RuntimeError):
        pass

    control_checks = 0

    def _control_check() -> None:
        nonlocal control_checks
        control_checks += 1
        if control_checks == 7:
            raise _StopRequested("pause requested")

    context = _build_stage_context(
        tmp_path,
        config_document=_resolved_config_document(),
        control_check=_control_check,
    )
    _prepare_acquisition_output(
        context=context,
        input_files=(
            _InputFileFixture(
                source_reference="data/a.fasta",
                materialized_relative_path="inputs/files/0001_a.fasta",
                format_hint=".fasta",
                payload=">a\nACGT\n",
            ),
        ),
    )
    stage = InputProcessingStage()
    stage.preflight(context)
    with pytest.raises(_StopRequested):
        stage.run(context, NullProgressReporter())
    assert (
        context.stage_staging_directory / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    ).exists() is False


def test_stage_artifacts_are_committed_atomically_and_old_version_is_preserved_on_failure(
    tmp_path: Path,
) -> None:
    first_context = _build_stage_context(
        tmp_path / "first",
        config_document=_resolved_config_document(),
        task_id="task-atomic",
        job_id="job-atomic",
        worker_instance_id="worker-a",
    )
    _prepare_acquisition_output(
        context=first_context,
        input_files=(
            _InputFileFixture(
                source_reference="data/a.fasta",
                materialized_relative_path="inputs/files/0001_a.fasta",
                format_hint=".fasta",
                payload=">a\nACGT\n",
            ),
        ),
    )
    stage = InputProcessingStage()
    stage.preflight(first_context)
    first_result = stage.run(first_context, NullProgressReporter())
    assert first_result.failure is None
    assert (first_context.launch_spec.job_dir / "stages" / "input_processing").exists() is False

    first_manifest = StageArtifactManifest(
        stage_id="input_processing",
        job_id=first_context.launch_spec.job_id,
        worker_instance_id=first_context.launch_spec.worker_instance_id,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        completed_at="2026-08-02T00:00:00Z",
        artifacts=first_result.artifacts,
    )
    first_manifest_path = write_stage_manifest(
        directory=first_context.stage_staging_directory,
        manifest=first_manifest,
    )
    committed = commit_stage_directory(
        job_dir=first_context.launch_spec.job_dir,
        stage_id="input_processing",
        job_id=first_context.launch_spec.job_id,
        worker_instance_id=first_context.launch_spec.worker_instance_id,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        staging_directory=first_context.stage_staging_directory,
        manifest_path=first_manifest_path,
    )
    assert committed.artifacts == first_result.artifacts

    second_context = _build_stage_context(
        tmp_path / "first",
        config_document=_resolved_config_document(kmers=("AT",)),
        task_id="task-atomic",
        job_id="job-atomic",
        worker_instance_id="worker-b",
    )
    _prepare_acquisition_output(
        context=second_context,
        input_files=(
            _InputFileFixture(
                source_reference="data/a.fasta",
                materialized_relative_path="inputs/files/0001_a.fasta",
                format_hint=".fasta",
                payload=">a\nACGT\n",
            ),
        ),
    )
    stage.preflight(second_context)
    second_result = stage.run(second_context, NullProgressReporter())
    assert second_result.failure is None

    second_manifest = StageArtifactManifest(
        stage_id="input_processing",
        job_id=second_context.launch_spec.job_id,
        worker_instance_id=second_context.launch_spec.worker_instance_id,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        completed_at="2026-08-02T00:00:01Z",
        artifacts=second_result.artifacts,
    )
    second_manifest_path = write_stage_manifest(
        directory=second_context.stage_staging_directory,
        manifest=second_manifest,
    )
    with pytest.raises(StageCommitError):
        commit_stage_directory(
            job_dir=second_context.launch_spec.job_dir,
            stage_id="input_processing",
            job_id=second_context.launch_spec.job_id,
            worker_instance_id=second_context.launch_spec.worker_instance_id,
            pipeline_version=DEFAULT_PIPELINE_VERSION,
            staging_directory=second_context.stage_staging_directory,
            manifest_path=second_manifest_path,
        )

    committed_manifest_path = (
        second_context.launch_spec.job_dir
        / "stages"
        / "input_processing"
        / "stage_manifest.json"
    )
    committed_manifest_payload = json.loads(committed_manifest_path.read_text(encoding="utf-8"))
    assert committed_manifest_payload["artifacts"] == list(first_result.artifacts)


def test_runtime_marks_task_failed_for_dataset_invalid_after_publication(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    invalid_sample = tmp_path / "invalid.fasta"
    _write_sample(invalid_sample, sample_id="bad", sequence="XXXX")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(invalid_sample,))

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is False

    resolved = service.load_resolved_config()
    snapshot = AnalyticalTaskRegistryService(
        database_path=resolved.database_path
    ).get_task_snapshot(task_id=task_id)
    assert snapshot.task.state is AnalyticalTaskState.FAILED
    assert snapshot.active_or_latest_job is not None
    assert snapshot.active_or_latest_job.state is AnalyticalTaskState.FAILED
    manifest_path = (
        resolved.tasks_dir
        / task_id
        / "jobs"
        / snapshot.active_or_latest_job.job_id
        / "stages"
        / "input_processing"
        / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    )
    assert manifest_path.is_file()


def test_runtime_completes_task_for_single_valid_sample(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="ok", sequence="ACGT")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is True
    assert started.value is not None

    resolved = service.load_resolved_config()
    snapshot = AnalyticalTaskRegistryService(
        database_path=resolved.database_path
    ).get_task_snapshot(task_id=task_id)
    assert snapshot.task.state is AnalyticalTaskState.COMPLETED
    assert snapshot.active_or_latest_job is not None
    assert snapshot.active_or_latest_job.state is AnalyticalTaskState.COMPLETED
    manifest_path = (
        resolved.tasks_dir
        / task_id
        / "jobs"
        / snapshot.active_or_latest_job.job_id
        / "stages"
        / "input_processing"
        / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    )
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = InputProcessingManifest.model_validate(manifest_payload)
    assert manifest.dataset_summary.valid_sample_count == 1
    assert manifest.dataset_summary.comparative_analysis_available is False


@pytest.mark.parametrize(
    ("sequences", "raw_overrides", "expected_package_status", "partial_stage_id"),
    (
        (
            ("ACGT",),
            (
                "--alignment.mode=none",
                "--comparative_analysis.enabled=false",
                "--distance_matrix.enabled=false",
                "--phylogenetic_tree.enabled=false",
                "--clade_detection.enabled=false",
            ),
            ResultPackageTaskStatus.COMPLETED,
            None,
        ),
        (
            ("N-", "-N"),
            (
                "--alignment.mode=prealigned",
                "--reference=sample-1",
                "--comparative_analysis.enabled=false",
                "--comparative_analysis.reference.mode=disabled",
                "--phylogenetic_tree.enabled=false",
                "--clade_detection.enabled=false",
            ),
            ResultPackageTaskStatus.COMPLETED_WITH_WARNINGS,
            "distance_matrix",
        ),
    ),
)
def test_successful_runtime_publishes_package_for_disabled_or_partial_optional_phases(
    tmp_path: Path,
    sequences: tuple[str, ...],
    raw_overrides: tuple[str, ...],
    expected_package_status: ResultPackageTaskStatus,
    partial_stage_id: str | None,
) -> None:
    service = _initialize_core(tmp_path / "home")
    sample_paths: list[Path] = []
    for index, sequence in enumerate(sequences, start=1):
        sample_path = tmp_path / f"sample-{index}.fasta"
        _write_sample(sample_path, sample_id=f"sample-{index}", sequence=sequence)
        sample_paths.append(sample_path)
    task_id = _initialize_task_for_runtime(
        service=service,
        sample_paths=tuple(sample_paths),
        raw_overrides=raw_overrides,
    )

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)

    assert started.ok is True
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    snapshot = registry.get_task_snapshot(task_id=task_id)
    trace_id = registry.get_task_trace_id(task_id=task_id)
    assert snapshot.task.state is AnalyticalTaskState.COMPLETED
    assert trace_id is not None
    task_dir = resolved.tasks_dir / task_id
    link = load_result_package_link(path=task_dir / RESULT_PACKAGE_LINK_FILENAME)
    package_path = (task_dir / link.path).resolve(strict=True)
    validated = validate_result_package_file(
        path=package_path,
        expected_content_id=link.content_id,
        require_notes_absent=True,
    )
    assert validated.content_id == link.content_id

    with zipfile.ZipFile(package_path, mode="r") as archive:
        package_manifest = JelicaPackageManifest.model_validate_json(
            archive.read(JELICA_PACKAGE_MANIFEST_PATH)
        )
        task_metadata = json.loads(archive.read(JELICA_PACKAGE_TASK_PATH))
        configuration = json.loads(archive.read(JELICA_PACKAGE_CONFIGURATION_PATH))
        assert package_manifest.task.trace_id == trace_id
        assert task_metadata["trace_id"] == str(trace_id)
        assert configuration["trace_id"] == str(trace_id)
        assert package_manifest.task.status is expected_package_status
        stage_by_id = {stage.name: stage for stage in package_manifest.stages}
        if partial_stage_id is not None:
            assert stage_by_id[partial_stage_id].status == "partial_success"
            return

        for stage_id in (
            "comparative_analysis",
            "distance_matrix",
            "phylogenetic_tree",
            "clade_detection",
        ):
            stage = stage_by_id[stage_id]
            assert stage.status == "completed"
            assert len(stage.artifacts) == 1
            disabled_manifest = json.loads(archive.read(stage.artifacts[0]))
            assert disabled_manifest["enabled"] is False
        comparative_manifest = json.loads(
            archive.read(stage_by_id["comparative_analysis"].artifacts[0])
        )
        assert set(comparative_manifest["category_execution"]) == {
            "statistics",
            "reference_sequence_differences",
            "pairwise_sequence_differences",
        }
        assert all(
            category["total"] == 0
            for category in comparative_manifest["category_execution"].values()
        )


def test_runtime_uses_immutable_config_revision_for_alignment_reference_and_kmers(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    first = tmp_path / "first.fasta"
    second = tmp_path / "second.fasta"
    _write_sample(first, sample_id="r1", sequence="ACGT")
    _write_sample(second, sample_id="r2", sequence="ACGA")
    task_id = _initialize_task_for_runtime(
        service=service,
        sample_paths=(first, second),
        raw_overrides=(
            "--alignment.mode=prealigned",
            "--reference=r1",
            '--statistics.kmers=["AC","CG"]',
            "--statistics.kmer_strand=both",
        ),
    )
    service.set_parameter(parameter="default_alignment_mode", value="none")

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is True
    assert started.value is not None

    resolved = service.load_resolved_config()
    snapshot = AnalyticalTaskRegistryService(
        database_path=resolved.database_path
    ).get_task_snapshot(task_id=task_id)
    assert snapshot.active_or_latest_job is not None
    manifest_path = (
        resolved.tasks_dir
        / task_id
        / "jobs"
        / snapshot.active_or_latest_job.job_id
        / "stages"
        / "input_processing"
        / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    )
    manifest = InputProcessingManifest.model_validate(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest.dataset_summary.alignment_summary is not None
    assert manifest.dataset_summary.alignment_summary.mode.value == "prealigned"
    assert manifest.resolved_reference is not None
    assert manifest.resolved_reference.selector == "r1"
    assert len(manifest.unique_sequences) == 2
    first_unique = manifest.unique_sequences[0]
    assert [item.query for item in first_unique.facts.kmer_summaries] == ["AC", "CG"]
    assert all(item.strand.value == "both" for item in first_unique.facts.kmer_summaries)
    assert first_unique.kmer_hits_path is not None
    stage_root = manifest_path.parents[1]
    sidecar = KmerHitsSidecar.model_validate(
        json.loads(
            (stage_root / first_unique.kmer_hits_path).read_text(encoding="utf-8")
        )
    )
    assert sidecar.alignment_mode is not None
    assert sidecar.alignment_mode.value == "prealigned"
    assert [item.query for item in sidecar.query_summaries] == ["AC", "CG"]
    assert all(item.strand.value == "both" for item in sidecar.query_summaries)
