from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jelica_core.alignment import (
    ALIGNMENT_FASTA_RELATIVE_PATH,
    ALIGNMENT_MANIFEST_RELATIVE_PATH,
    ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH,
    AlignmentEngineResult,
    AlignmentExecutionPlanKind,
    AlignmentResultValidationError,
    AlignmentToolAvailability,
    CanonicalAlignmentRow,
    build_reference_coordinate_map,
    expand_logical_samples,
    plan_alignment,
    validate_unique_alignment,
)
from jelica_core.alignment.operations import write_canonical_fasta_atomically
from jelica_core.config import (
    AnalysisAlignmentMode,
    AnalysisConfigInput,
    resolve_analysis_config,
)
from jelica_core.runtime.alignment_stage import AlignmentStage, AlignmentStageError
from jelica_core.runtime.artifacts import (
    StageArtifactManifest,
    commit_stage_directory,
    write_stage_manifest,
)
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
from jelica_core.runtime.messages import WorkerStopReason
from jelica_core.runtime.models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RuntimeStateCheckpoint,
    WorkerLaunchSpec,
)
from jelica_core.runtime.pipeline import StageContext
from jelica_core.runtime.progress import NullProgressReporter
from jelica_core.tasks.storage import compute_config_hash, write_text_atomically


def _sequence_id(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ungapped_digest(value: str) -> str:
    return hashlib.sha256(value.replace("-", "").encode("utf-8")).hexdigest()


def _context(
    tmp_path: Path,
    *,
    reference: str,
    alignment_mode: str = "prealigned",
) -> StageContext:
    resolved = resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": ["first.afa", "second.afa"],
                "reference": reference,
                "alignment": {"mode": alignment_mode},
            }
        )
    ).config
    config_document = resolved.model_dump(mode="json")
    task_dir = tmp_path / "task"
    job_dir = task_dir / "jobs" / "job-1"
    config_path = task_dir / "configs" / "000001.json"
    config_path.parent.mkdir(parents=True)
    write_text_atomically(
        path=config_path,
        payload=json.dumps(config_document, ensure_ascii=False, sort_keys=True),
    )
    launch_spec = WorkerLaunchSpec(
        task_id="task-1",
        job_id="job-1",
        worker_instance_id="worker-1",
        lease_token="lease-1",
        database_path=tmp_path / "jelica.db",
        task_dir=task_dir,
        job_dir=job_dir,
        config_revision_path=config_path,
        config_hash=compute_config_hash(config_document),
        runtime_state_json=RuntimeStateCheckpoint.new(
            pipeline_version=DEFAULT_PIPELINE_VERSION
        ).to_runtime_state_json(),
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )
    return StageContext(
        launch_spec=launch_spec,
        stage_index=3,
        stage_staging_directory=job_dir / "staging" / "alignment" / "worker-1",
    )


def _facts(*, sequence_id: str, aligned: str) -> SequenceFacts:
    ungapped_length = len(aligned.replace("-", ""))
    return SequenceFacts(
        source_length=len(aligned),
        ungapped_length=ungapped_length,
        recognized_nucleotide_count=ungapped_length,
        symbol_counts={},
        canonical_count=ungapped_length,
        ambiguous_count=0,
        gap_count=aligned.count("-"),
        invalid_symbol_count=0,
        invalid_symbol_counts={},
        invalid_positions=(),
        invalid_positions_truncated=False,
        gc_count=0,
        gc_content_total=0.0,
        resolved_gc_content=0.0,
        expected_gc_count=0.0,
        expected_gc_content=0.0,
        u_count=0,
        sequence_id=sequence_id,
    )


def _write_input_processing_result(
    context: StageContext,
    *,
    aligned_by_id: dict[str, str],
    logical_samples: tuple[tuple[str, str], ...],
    reference_sample_id: str,
    identity_overrides: dict[str, str] | None = None,
) -> None:
    root = context.launch_spec.job_dir / "stages" / "input_processing"
    unique_sequences: list[InputProcessingUniqueSequence] = []
    for sequence_id, aligned in aligned_by_id.items():
        relative_path = f"input_processing/sequences/{sequence_id.split(':', 1)[1]}.fasta"
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(path=target, payload=f">{sequence_id}\n{aligned}\n")
        sample_ids = tuple(
            sample_id for sample_id, sample_sequence_id in logical_samples
            if sample_sequence_id == sequence_id
        )
        unique_sequences.append(
            InputProcessingUniqueSequence(
                sequence_id=sequence_id,
                sequence_artifact_path=relative_path,
                ungapped_sequence_sha256=(identity_overrides or {}).get(
                    sequence_id, _ungapped_digest(aligned)
                ),
                facts=_facts(sequence_id=sequence_id, aligned=aligned),
                logical_sample_ids=sample_ids,
            )
        )

    samples = tuple(
        InputProcessingLogicalSample(
            sample_id=sample_id,
            provenance=LogicalSampleProvenance(
                input_manifest_source_reference=f"{sample_id}.afa",
                materialized_relative_path=f"inputs/files/{sample_id}.afa",
                record_index=index,
                format_hint=".afa",
            ),
            original_record_id=sample_id,
            validation_status=SampleValidationStatus.VALID,
            sequence_id=sequence_id,
            eligible_for_analysis=True,
        )
        for index, (sample_id, sequence_id) in enumerate(logical_samples)
    )
    reference = next(sample for sample in samples if sample.sample_id == reference_sample_id)
    assert reference.sequence_id is not None
    manifest = InputProcessingManifest(
        task_id=context.launch_spec.task_id,
        job_id=context.launch_spec.job_id,
        config_revision_path="configs/000001.json",
        config_hash=context.launch_spec.config_hash,
        input_manifest_path="inputs/input_manifest.json",
        generated_at="2026-08-04T00:00:00Z",
        processing_state=InputProcessingState.COMPLETED,
        logical_samples=samples,
        unique_sequences=tuple(unique_sequences),
        dataset_summary=InputProcessingDatasetSummary(
            discovered_record_count=len(samples),
            valid_sample_count=len(samples),
            invalid_sample_count=0,
            unique_sequence_count=len(unique_sequences),
            duplicate_logical_sample_count=len(samples) - len(unique_sequences),
            comparative_analysis_available=len(samples) > 1,
            reference_dependent_analysis_available=True,
        ),
        resolved_reference=InputProcessingResolvedReference(
            selector=reference_sample_id,
            sample_id=reference_sample_id,
            sequence_id=reference.sequence_id,
            source_relative_path=f"inputs/files/{reference_sample_id}.afa",
            record_id=reference_sample_id,
            resolution_method=ReferenceResolutionMethod.RECORD_ID,
        ),
    )
    manifest_path = root / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(
        path=manifest_path,
        payload=json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
    )


def test_prealigned_stage_uses_independent_identity_and_expands_duplicates(
    tmp_path: Path,
) -> None:
    first = "AC-GT"
    second = "ACG-T"
    first_id = _sequence_id(first)
    second_id = _sequence_id(second)
    context = _context(tmp_path, reference="reference")
    _write_input_processing_result(
        context,
        aligned_by_id={first_id: first, second_id: second},
        logical_samples=(
            ("sample-a", first_id),
            ("reference", second_id),
            ("sample-c", first_id),
        ),
        reference_sample_id="reference",
    )

    events: list[tuple[str, dict[str, object]]] = []
    context = StageContext(
        launch_spec=context.launch_spec,
        stage_index=context.stage_index,
        stage_staging_directory=context.stage_staging_directory,
        event_reporter=lambda name, payload: events.append((name, payload)),
    )
    stage = AlignmentStage()
    stage.preflight(context)
    result = stage.run(context, NullProgressReporter())

    assert result.artifacts == (
        ALIGNMENT_MANIFEST_RELATIVE_PATH,
        ALIGNMENT_FASTA_RELATIVE_PATH,
        ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH,
    )
    canonical = (context.stage_staging_directory / ALIGNMENT_FASTA_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )
    assert canonical.count(">") == 3
    coordinate_payload = json.loads(
        (
            context.stage_staging_directory / ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH
        ).read_text(encoding="utf-8")
    )
    assert coordinate_payload["reference_sample_id"] == "reference"
    assert coordinate_payload["reference_positions"] == [1, 2, 3, None, 4]
    manifest_text = (
        context.stage_staging_directory / ALIGNMENT_MANIFEST_RELATIVE_PATH
    ).read_text(encoding="utf-8")
    serialized_events = json.dumps(events, sort_keys=True)
    assert first not in manifest_text
    assert second not in manifest_text
    assert first not in serialized_events
    assert second not in serialized_events

    stage_manifest = StageArtifactManifest(
        stage_id="alignment",
        job_id=context.launch_spec.job_id,
        worker_instance_id=context.launch_spec.worker_instance_id,
        pipeline_version=context.launch_spec.pipeline_version,
        completed_at="2026-01-01T00:00:00Z",
        artifacts=result.artifacts,
    )
    stage_manifest_path = write_stage_manifest(
        directory=context.stage_staging_directory,
        manifest=stage_manifest,
    )
    committed = commit_stage_directory(
        job_dir=context.launch_spec.job_dir,
        stage_id="alignment",
        job_id=context.launch_spec.job_id,
        worker_instance_id=context.launch_spec.worker_instance_id,
        pipeline_version=context.launch_spec.pipeline_version,
        staging_directory=context.stage_staging_directory,
        manifest_path=stage_manifest_path,
    )
    assert committed.artifacts == result.artifacts
    assert not context.stage_staging_directory.exists()
    committed_root = context.launch_spec.job_dir / "stages" / "alignment"
    assert all((committed_root / artifact).is_file() for artifact in result.artifacts)


def test_compute_stage_skips_engine_for_one_unique_sequence(tmp_path: Path) -> None:
    value = "NN-N"
    sequence_id = _sequence_id(value)
    context = _context(
        tmp_path,
        reference="sample-only",
        alignment_mode="compute",
    )
    _write_input_processing_result(
        context,
        aligned_by_id={sequence_id: value},
        logical_samples=(
            ("sample-only", sequence_id),
            ("sample-duplicate", sequence_id),
        ),
        reference_sample_id="sample-only",
    )
    events: list[str] = []
    context = StageContext(
        launch_spec=context.launch_spec,
        stage_index=context.stage_index,
        stage_staging_directory=context.stage_staging_directory,
        event_reporter=lambda name, _payload: events.append(name),
    )

    class _EngineMustNotRun:
        @property
        def name(self) -> str:
            return "unexpected"

        def probe(self, **_kwargs: object) -> object:
            pytest.fail("the engine must not be probed for one unique sequence")

        def align(self, **_kwargs: object) -> object:
            pytest.fail("the engine must not run for one unique sequence")

    stage = AlignmentStage(engine=_EngineMustNotRun())  # type: ignore[arg-type]
    stage.preflight(context)
    result = stage.run(context, NullProgressReporter())

    assert result.check_control_before_commit is True
    assert "ALIGNMENT_SKIPPED" in events
    canonical = (context.stage_staging_directory / ALIGNMENT_FASTA_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )
    assert canonical.count(">") == 2


def test_compute_stage_sends_unique_ids_and_restores_logical_samples(tmp_path: Path) -> None:
    first = "NNN"
    second = "NRY"
    first_id = _sequence_id(first)
    second_id = _sequence_id(second)
    context = _context(
        tmp_path,
        reference="reference",
        alignment_mode="compute",
    )
    _write_input_processing_result(
        context,
        aligned_by_id={first_id: first, second_id: second},
        logical_samples=(
            ("reference", first_id),
            ("sample-b", second_id),
            ("sample-c", first_id),
        ),
        reference_sample_id="reference",
    )

    class _CapturingEngine:
        request_sequence_ids: tuple[str, ...] = ()

        @property
        def name(self) -> str:
            return "mafft"

        def probe(self, **_kwargs: object) -> AlignmentToolAvailability:
            return AlignmentToolAvailability(
                available=True,
                executable=tmp_path / "fake-mafft",
                version="7.526",
                version_parts=(7, 526),
                source="test",
            )

        def align(self, *, availability: object, request: object) -> AlignmentEngineResult:
            from jelica_core.alignment import AlignmentEngineRequest

            assert isinstance(request, AlignmentEngineRequest)
            self.request_sequence_ids = tuple(item.sequence_id for item in request.sequences)
            internal_ids = {
                item.sequence_id: f"record-{index}"
                for index, item in enumerate(request.sequences, start=1)
            }
            output_path = request.working_directory / "fake-output.fasta"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            payload = "".join(
                f">{internal_ids[item.sequence_id]}\n{item.sequence}\n"
                for item in request.sequences
            )
            write_text_atomically(path=output_path, payload=payload)
            return AlignmentEngineResult(
                output_path=output_path,
                diagnostics_path=None,
                version="7.526",
                effective_arguments=("--test",),
                internal_record_ids=internal_ids,
                reverse_marked_record_ids=frozenset(),
            )

    engine = _CapturingEngine()
    stage = AlignmentStage(engine=engine)  # type: ignore[arg-type]
    stage.preflight(context)
    result = stage.run(context, NullProgressReporter())

    assert set(engine.request_sequence_ids) == {first_id, second_id}
    canonical = (context.stage_staging_directory / ALIGNMENT_FASTA_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )
    assert canonical.count(">") == 3
    assert result.check_control_before_commit is True


def test_compute_stage_rejects_invalid_engine_output_as_result_error(tmp_path: Path) -> None:
    first = "NNN"
    second = "NRY"
    first_id = _sequence_id(first)
    second_id = _sequence_id(second)
    context = _context(tmp_path, reference="reference", alignment_mode="compute")
    _write_input_processing_result(
        context,
        aligned_by_id={first_id: first, second_id: second},
        logical_samples=(("reference", first_id), ("sample-b", second_id)),
        reference_sample_id="reference",
    )

    class _InvalidOutputEngine:
        @property
        def name(self) -> str:
            return "mafft"

        def probe(self, **_kwargs: object) -> AlignmentToolAvailability:
            return AlignmentToolAvailability(
                available=True,
                executable=tmp_path / "fake-mafft",
                version="7.526",
                version_parts=(7, 526),
                source="test",
            )

        def align(self, *, availability: object, request: object) -> AlignmentEngineResult:
            from jelica_core.alignment import AlignmentEngineRequest

            assert isinstance(request, AlignmentEngineRequest)
            internal_ids = {
                item.sequence_id: f"record-{index}"
                for index, item in enumerate(request.sequences, start=1)
            }
            output_path = request.working_directory / "invalid-output.fasta"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomically(path=output_path, payload=">unknown-record\nNNN\n")
            return AlignmentEngineResult(
                output_path=output_path,
                diagnostics_path=None,
                version="7.526",
                effective_arguments=("--test",),
                internal_record_ids=internal_ids,
                reverse_marked_record_ids=frozenset(),
            )

    stage = AlignmentStage(engine=_InvalidOutputEngine())  # type: ignore[arg-type]
    stage.preflight(context)

    with pytest.raises(AlignmentStageError) as captured:
        stage.run(context, NullProgressReporter())

    assert captured.value.reason == "alignment_result_invalid"
    assert captured.value.event_name == "ALIGNMENT_RESULT_INVALID"
    assert not (
        context.stage_staging_directory / ALIGNMENT_FASTA_RELATIVE_PATH
    ).exists()


@pytest.mark.parametrize(
    ("reason", "expected_event"),
    [
        (WorkerStopReason.PAUSE_REQUESTED, "ALIGNMENT_MAFFT_STOPPED_PAUSE"),
        (WorkerStopReason.CANCEL_REQUESTED, "ALIGNMENT_MAFFT_STOPPED_CANCEL"),
        (WorkerStopReason.RUNTIME_SHUTDOWN, "ALIGNMENT_MAFFT_STOPPED_SHUTDOWN"),
    ],
)
def test_alignment_stage_emits_control_stop_event_without_partial_result(
    tmp_path: Path,
    reason: WorkerStopReason,
    expected_event: str,
) -> None:
    first = "NNN"
    second = "NRY"
    first_id = _sequence_id(first)
    second_id = _sequence_id(second)
    context = _context(tmp_path, reference="reference", alignment_mode="compute")
    _write_input_processing_result(
        context,
        aligned_by_id={first_id: first, second_id: second},
        logical_samples=(("reference", first_id), ("sample-b", second_id)),
        reference_sample_id="reference",
    )
    events: list[str] = []
    context = StageContext(
        launch_spec=context.launch_spec,
        stage_index=context.stage_index,
        stage_staging_directory=context.stage_staging_directory,
        event_reporter=lambda name, _payload: events.append(name),
    )

    class _ControlRequested(RuntimeError):
        def __init__(self) -> None:
            self.reason = reason

    class _StoppingEngine:
        @property
        def name(self) -> str:
            return "mafft"

        def probe(self, **_kwargs: object) -> AlignmentToolAvailability:
            return AlignmentToolAvailability(
                available=True,
                executable=tmp_path / "fake-mafft",
                version="7.526",
                version_parts=(7, 526),
                source="test",
            )

        def align(self, *, availability: object, request: object) -> AlignmentEngineResult:
            raise _ControlRequested

    stage = AlignmentStage(engine=_StoppingEngine())  # type: ignore[arg-type]
    stage.preflight(context)

    with pytest.raises(_ControlRequested):
        stage.run(context, NullProgressReporter())

    assert expected_event in events
    assert not (
        context.stage_staging_directory / ALIGNMENT_FASTA_RELATIVE_PATH
    ).exists()


def test_prealigned_stage_rejects_identity_mismatch_without_exposing_content(
    tmp_path: Path,
) -> None:
    aligned = "AC-GT"
    sequence_id = _sequence_id(aligned)
    context = _context(tmp_path, reference="reference")
    _write_input_processing_result(
        context,
        aligned_by_id={sequence_id: aligned},
        logical_samples=(("reference", sequence_id),),
        reference_sample_id="reference",
        identity_overrides={sequence_id: _ungapped_digest("AC-GA")},
    )
    stage = AlignmentStage()
    stage.preflight(context)

    with pytest.raises(AlignmentStageError) as error_info:
        stage.run(context, NullProgressReporter())

    assert error_info.value.reason == "prealigned_input_invalid"
    assert aligned not in str(error_info.value)
    assert aligned not in json.dumps(error_info.value.context, sort_keys=True)
    assert not (
        context.stage_staging_directory / ALIGNMENT_FASTA_RELATIVE_PATH
    ).exists()


@pytest.mark.parametrize(
    ("records", "expected_code"),
    [
        ((("known", "AC-GT"),), "alignment_output_missing_record"),
        (
            (("known", "AC-GT"), ("unknown", "ACG-T")),
            "alignment_output_unknown_record",
        ),
        (
            (("known", "AC-GT"), ("other", "ACG-TT")),
            "alignment_output_length_mismatch",
        ),
    ],
)
def test_prealigned_validator_rejects_missing_unknown_and_unequal_lengths(
    records: tuple[tuple[str, str], ...],
    expected_code: str,
) -> None:
    expected_hashes = {
        "known": _ungapped_digest("AC-GT"),
        "other": _ungapped_digest("ACG-T"),
    }
    with pytest.raises(AlignmentResultValidationError) as error_info:
        validate_unique_alignment(
            records=records,
            expected_ungapped_sha256=expected_hashes,
        )
    assert error_info.value.code == expected_code


def test_reference_map_requires_explicit_present_reference_and_preserves_insertions() -> None:
    rows = expand_logical_samples(
        aligned_by_sequence_id={"shared": "AC-GT", "other": "ACG-T"},
        logical_samples=(
            ("sample-a", "shared"),
            ("reference", "shared"),
            ("sample-c", "other"),
        ),
    )
    coordinate_map = build_reference_coordinate_map(
        rows=rows,
        reference_sample_id="reference",
    )
    assert coordinate_map.reference_sequence_id == "shared"
    assert coordinate_map.reference_positions == (1, 2, None, 3, 4)

    with pytest.raises(AlignmentResultValidationError) as unresolved:
        build_reference_coordinate_map(rows=rows, reference_sample_id=None)
    assert unresolved.value.code == "alignment_reference_unresolved"

    with pytest.raises(AlignmentResultValidationError) as absent:
        build_reference_coordinate_map(rows=rows, reference_sample_id="absent")
    assert absent.value.code == "alignment_reference_missing"


def test_streaming_canonical_writer_hashes_written_file(tmp_path: Path) -> None:
    target = tmp_path / "canonical.fasta"
    rows = (
        CanonicalAlignmentRow(
            sample_id="sample-a",
            sequence_id="sequence-a",
            aligned_sequence="AC-GT",
        ),
        CanonicalAlignmentRow(
            sample_id="sample-b",
            sequence_id="sequence-b",
            aligned_sequence="ACG-T",
        ),
    )

    digest = write_canonical_fasta_atomically(path=target, rows=rows, width=3)

    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    assert target.read_text(encoding="utf-8").count(">") == 2


@pytest.mark.parametrize(
    ("mode", "logical_count", "unique_count", "expected_kind"),
    [
        (AnalysisAlignmentMode.COMPUTE, 0, 0, AlignmentExecutionPlanKind.DIRECT),
        (AnalysisAlignmentMode.COMPUTE, 1, 1, AlignmentExecutionPlanKind.DIRECT),
        (AnalysisAlignmentMode.COMPUTE, 3, 1, AlignmentExecutionPlanKind.DIRECT),
        (AnalysisAlignmentMode.COMPUTE, 3, 2, AlignmentExecutionPlanKind.ENGINE),
        (AnalysisAlignmentMode.PREALIGNED, 2, 2, AlignmentExecutionPlanKind.PREALIGNED),
    ],
)
def test_alignment_planning_covers_deduplication_thresholds(
    mode: AnalysisAlignmentMode,
    logical_count: int,
    unique_count: int,
    expected_kind: AlignmentExecutionPlanKind,
) -> None:
    plan = plan_alignment(
        mode=mode,
        logical_sample_count=logical_count,
        unique_sequence_count=unique_count,
    )

    assert plan.kind is expected_kind
