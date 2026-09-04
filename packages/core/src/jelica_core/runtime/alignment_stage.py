from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final

from jelica_core.alignment import (
    ALIGNMENT_DIAGNOSTICS_RELATIVE_PATH,
    ALIGNMENT_FASTA_RELATIVE_PATH,
    ALIGNMENT_MANIFEST_RELATIVE_PATH,
    ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH,
    ALIGNMENT_STAGE_ID,
    AlignmentEngine,
    AlignmentEngineRequest,
    AlignmentExecutionPlanKind,
    AlignmentInputSequence,
    AlignmentManifest,
    AlignmentResultValidationError,
    AlignmentStageOutcome,
    MafftAlignmentEngine,
    MafftError,
    build_reference_coordinate_map,
    compute_input_set_hash,
    expand_logical_samples,
    parse_aligned_fasta,
    plan_alignment,
    read_single_fasta_record,
    validate_unique_alignment,
)
from jelica_core.alignment.operations import write_canonical_fasta_atomically
from jelica_core.config import AnalysisAlignmentEngine, ResolvedAnalysisConfig
from jelica_core.input_sources import SUPPORTED_FASTA_EXTENSIONS
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    INPUT_PROCESSING_STAGE_ID,
    InputProcessingLogicalSample,
    InputProcessingManifest,
)
from .pipeline import ProgressReporter, StageContext, StageRunResult

ALIGNMENT_STARTED_EVENT: Final = "ALIGNMENT_STARTED"
ALIGNMENT_SKIPPED_EVENT: Final = "ALIGNMENT_SKIPPED"
ALIGNMENT_PREALIGNED_VALIDATION_STARTED_EVENT: Final = (
    "ALIGNMENT_PREALIGNED_VALIDATION_STARTED"
)
ALIGNMENT_MAFFT_PROBED_EVENT: Final = "ALIGNMENT_MAFFT_PROBED"
ALIGNMENT_MAFFT_LAUNCHED_EVENT: Final = "ALIGNMENT_MAFFT_LAUNCHED"
ALIGNMENT_MAFFT_COMPLETED_EVENT: Final = "ALIGNMENT_MAFFT_COMPLETED"
ALIGNMENT_MAFFT_FAILED_EVENT: Final = "ALIGNMENT_MAFFT_FAILED"
ALIGNMENT_MAFFT_STOPPED_PAUSE_EVENT: Final = "ALIGNMENT_MAFFT_STOPPED_PAUSE"
ALIGNMENT_MAFFT_STOPPED_CANCEL_EVENT: Final = "ALIGNMENT_MAFFT_STOPPED_CANCEL"
ALIGNMENT_MAFFT_STOPPED_SHUTDOWN_EVENT: Final = "ALIGNMENT_MAFFT_STOPPED_SHUTDOWN"
ALIGNMENT_RESULT_INVALID_EVENT: Final = "ALIGNMENT_RESULT_INVALID"
ALIGNMENT_RESULT_PUBLISHED_EVENT: Final = "ALIGNMENT_RESULT_PUBLISHED"
ALIGNMENT_COMPLETED_EVENT: Final = "ALIGNMENT_COMPLETED"

_INTERNAL_TASK_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_directory_max_depth", "ncbi_max_retries"}
)


class AlignmentStageError(RuntimeError):
    """Safe stage error with a structured event contract."""

    def __init__(
        self,
        *,
        reason: str,
        detail: str,
        event_name: str,
        context: dict[str, object] | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.event_name = event_name
        self.context = context or {}
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _InputProcessingArtifacts:
    candidate_roots: tuple[Path, ...]
    selected_root: Path
    manifest: InputProcessingManifest


@dataclass(frozen=True, slots=True)
class AlignmentStage:
    stage_id: str = ALIGNMENT_STAGE_ID
    weight: float = 1.0
    engine: AlignmentEngine | None = None

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)
        (context.stage_staging_directory / "alignment").mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        started_at_value = utc_now()
        started_monotonic = time.monotonic()
        context.check_control()
        config = _load_resolved_config(context.launch_spec.config_revision_path)
        source = _load_input_processing_artifacts(context=context)
        valid_samples = tuple(
            sample for sample in source.manifest.logical_samples if sample.eligible_for_analysis
        )
        plan = plan_alignment(
            mode=config.alignment.mode,
            logical_sample_count=len(valid_samples),
            unique_sequence_count=len(source.manifest.unique_sequences),
        )
        context.emit_event(
            ALIGNMENT_STARTED_EVENT,
            {
                "mode": config.alignment.mode.value,
                "logical_sample_count": plan.logical_sample_count,
                "unique_sequence_count": plan.unique_sequence_count,
                "construction": (
                    config.alignment.construction.value
                    if config.alignment.construction is not None
                    else None
                ),
                "detail": "Alignment stage started.",
            },
        )
        progress_reporter(0.05)

        if plan.kind is AlignmentExecutionPlanKind.DISABLED:
            manifest = _build_disabled_manifest(
                context=context,
                config=config,
                source_manifest=source.manifest,
                started_at=serialize_utc_datetime(started_at_value),
                started_monotonic=started_monotonic,
            )
            _write_alignment_manifest(context=context, manifest=manifest)
            context.emit_event(
                ALIGNMENT_SKIPPED_EVENT,
                {
                    "reason": plan.reason,
                    "logical_sample_count": plan.logical_sample_count,
                    "unique_sequence_count": plan.unique_sequence_count,
                    "detail": "Alignment was skipped because alignment.mode is 'none'.",
                },
            )
            progress_reporter(1.0)
            return StageRunResult(
                artifacts=(ALIGNMENT_MANIFEST_RELATIVE_PATH,),
                check_control_before_commit=True,
            )

        context.check_control()
        input_sequences = _load_input_sequences(source=source)
        progress_reporter(0.15)
        expected_sequences = {item.sequence_id: item.sequence for item in input_sequences}
        reference_sample_id, reference_sequence_id = _resolve_effective_reference(
            source_manifest=source.manifest,
            valid_samples=valid_samples,
        )

        engine_version: str | None = None
        effective_arguments: tuple[str, ...] = tuple()
        diagnostics_path: str | None = None
        reoriented_sequence_ids: frozenset[str] = frozenset()
        try:
            if plan.kind is AlignmentExecutionPlanKind.PREALIGNED:
                _validate_prealigned_sources(valid_samples=valid_samples)
                expected_ungapped_sha256 = _load_prealigned_identities(source=source)
                context.emit_event(
                    ALIGNMENT_PREALIGNED_VALIDATION_STARTED_EVENT,
                    {
                        "logical_sample_count": len(valid_samples),
                        "unique_sequence_count": len(input_sequences),
                        "detail": "Prealigned FASTA input was accepted for validation.",
                    },
                )
                records = tuple((item.sequence_id, item.sequence) for item in input_sequences)
                validated = validate_unique_alignment(
                    records=records,
                    expected_ungapped_sha256=expected_ungapped_sha256,
                )
            elif plan.kind is AlignmentExecutionPlanKind.DIRECT:
                context.emit_event(
                    ALIGNMENT_SKIPPED_EVENT,
                    {
                        "reason": plan.reason,
                        "logical_sample_count": plan.logical_sample_count,
                        "unique_sequence_count": plan.unique_sequence_count,
                        "detail": (
                            "MAFFT was not required because fewer than two unique "
                            "sequences remain after deduplication."
                        ),
                    },
                )
                records = tuple((item.sequence_id, item.sequence) for item in input_sequences)
                validated = validate_unique_alignment(
                    records=records,
                    expected_sequences=expected_sequences,
                )
            else:
                if config.alignment.mafft is None or config.alignment.construction is None:
                    raise AlignmentStageError(
                        reason="alignment_config_invalid",
                        detail="Resolved compute alignment settings are incomplete.",
                        event_name=ALIGNMENT_MAFFT_FAILED_EVENT,
                    )
                selected_engine = self.engine or MafftAlignmentEngine()
                availability = selected_engine.probe(
                    explicit_executable=context.launch_spec.mafft_executable
                )
                if not availability.available:
                    availability_error = (
                        availability.error_code or "alignment_engine_unavailable"
                    )
                    raise AlignmentStageError(
                        reason=availability_error,
                        detail=availability.reason or "MAFFT is unavailable.",
                        event_name=ALIGNMENT_MAFFT_FAILED_EVENT,
                        context={"error_type": availability_error},
                    )
                engine_version = availability.version
                context.emit_event(
                    ALIGNMENT_MAFFT_PROBED_EVENT,
                    {
                        "engine": selected_engine.name,
                        "version": engine_version,
                        "executable_source": availability.source,
                        "detail": f"MAFFT {engine_version} was found and verified.",
                    },
                )
                request = AlignmentEngineRequest(
                    sequences=input_sequences,
                    construction=config.alignment.construction,
                    reference_sequence_id=reference_sequence_id,
                    mafft_config=config.alignment.mafft,
                    working_directory=(
                        context.stage_staging_directory / "alignment" / ".mafft-work"
                    ),
                    control_check=context.check_control,
                    process_started=context.register_external_process,
                    process_stopped=context.unregister_external_process,
                )
                context.emit_event(
                    ALIGNMENT_MAFFT_LAUNCHED_EVENT,
                    {
                        "engine": selected_engine.name,
                        "version": engine_version,
                        "construction": config.alignment.construction.value,
                        "unique_sequence_count": len(input_sequences),
                        "detail": "MAFFT alignment process was started.",
                    },
                )
                try:
                    engine_result = selected_engine.align(
                        availability=availability,
                        request=request,
                    )
                except BaseException as error:
                    control_event = _control_stop_event(error)
                    if control_event is not None:
                        context.emit_event(
                            control_event,
                            {
                                "restart_on_resume": True,
                                "detail": (
                                    "MAFFT was stopped by a runtime control request; the "
                                    "alignment stage will restart from the beginning."
                                ),
                            },
                        )
                    raise
                effective_arguments = engine_result.effective_arguments
                if engine_result.diagnostics_path is not None:
                    diagnostics_path = ALIGNMENT_DIAGNOSTICS_RELATIVE_PATH
                context.emit_event(
                    ALIGNMENT_MAFFT_COMPLETED_EVENT,
                    {
                        "engine": selected_engine.name,
                        "version": engine_result.version,
                        "unique_sequence_count": len(input_sequences),
                        "detail": "MAFFT process completed successfully.",
                    },
                )
                records = parse_aligned_fasta(path=str(engine_result.output_path))
                validated = validate_unique_alignment(
                    records=records,
                    expected_sequences=expected_sequences,
                    internal_record_ids=engine_result.internal_record_ids,
                    reverse_marked_record_ids=engine_result.reverse_marked_record_ids,
                    direction_adjustment=config.alignment.mafft.direction_adjustment,
                )
                reoriented_sequence_ids = validated.reoriented_sequence_ids
        except MafftError as error:
            raise AlignmentStageError(
                reason=error.code,
                detail=error.detail,
                event_name=ALIGNMENT_MAFFT_FAILED_EVENT,
                context={
                    "error_type": error.code,
                    "exit_code": error.exit_code,
                },
            ) from error
        except AlignmentResultValidationError as error:
            safe_context: dict[str, object] = {"error_type": error.code}
            if error.record_id is not None:
                safe_context["record_id"] = error.record_id
            if error.sequence_id is not None:
                safe_context["sequence_id"] = error.sequence_id
            if error.position is not None:
                safe_context["position"] = error.position
            raise AlignmentStageError(
                reason=(
                    "prealigned_input_invalid"
                    if plan.kind is AlignmentExecutionPlanKind.PREALIGNED
                    else "alignment_result_invalid"
                ),
                detail=error.detail,
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
                context=safe_context,
            ) from error

        progress_reporter(0.7)
        logical_sample_pairs = tuple(
            (sample.sample_id, _require_sample_sequence_id(sample)) for sample in valid_samples
        )
        try:
            rows = expand_logical_samples(
                aligned_by_sequence_id=validated.aligned_by_sequence_id,
                logical_samples=logical_sample_pairs,
            )
            coordinate_map = build_reference_coordinate_map(
                rows=rows,
                reference_sample_id=reference_sample_id,
            )
        except AlignmentResultValidationError as error:
            raise AlignmentStageError(
                reason="alignment_result_invalid",
                detail=error.detail,
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
                context={"error_type": error.code},
            ) from error
        context.check_control()

        aligned_fasta_path = context.stage_staging_directory / ALIGNMENT_FASTA_RELATIVE_PATH
        result_hash = write_canonical_fasta_atomically(path=aligned_fasta_path, rows=rows)
        coordinate_map_path = (
            context.stage_staging_directory / ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH
        )
        write_text_atomically(
            path=coordinate_map_path,
            payload=json.dumps(
                coordinate_map.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        reoriented_sample_ids = tuple(
            row.sample_id for row in rows if row.sequence_id in reoriented_sequence_ids
        )
        completed_at_value = utc_now()
        outcome = (
            AlignmentStageOutcome.SKIPPED_NOT_REQUIRED
            if plan.kind is AlignmentExecutionPlanKind.DIRECT
            else AlignmentStageOutcome.COMPLETED
        )
        manifest = AlignmentManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            mode=config.alignment.mode,
            construction=config.alignment.construction,
            requested_engine=config.alignment.engine,
            resolved_engine=(
                AnalysisAlignmentEngine.MAFFT
                if plan.kind is AlignmentExecutionPlanKind.ENGINE
                else config.alignment.engine
            ),
            engine_version=engine_version,
            requested_strategy=(
                config.alignment.mafft.strategy
                if config.alignment.mafft is not None
                else None
            ),
            effective_arguments=effective_arguments,
            mafft_settings=(
                config.alignment.mafft.model_dump(mode="json")
                if config.alignment.mafft is not None
                else None
            ),
            logical_sample_count=len(rows),
            unique_sequence_count=len(input_sequences),
            alignment_length=validated.alignment_length,
            reference_sample_id=coordinate_map.reference_sample_id,
            reference_sequence_id=coordinate_map.reference_sequence_id,
            reoriented_sequence_ids=tuple(sorted(reoriented_sequence_ids)),
            reoriented_sample_ids=reoriented_sample_ids,
            aligned_fasta_path=ALIGNMENT_FASTA_RELATIVE_PATH,
            reference_coordinate_map_path=ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH,
            diagnostics_path=diagnostics_path,
            input_set_sha256=compute_input_set_hash(input_sequences),
            result_sha256=result_hash,
            started_at=serialize_utc_datetime(started_at_value),
            completed_at=serialize_utc_datetime(completed_at_value),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            outcome=outcome,
        )
        _write_alignment_manifest(context=context, manifest=manifest)
        shutil.rmtree(
            context.stage_staging_directory / "alignment" / ".mafft-work",
            ignore_errors=True,
        )
        artifacts = [
            ALIGNMENT_MANIFEST_RELATIVE_PATH,
            ALIGNMENT_FASTA_RELATIVE_PATH,
            ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH,
        ]
        if diagnostics_path is not None:
            artifacts.append(diagnostics_path)
        _validate_artifacts(
            stage_directory=context.stage_staging_directory,
            artifacts=tuple(artifacts),
        )
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=tuple(artifacts),
            check_control_before_commit=True,
        )


def _load_resolved_config(config_revision_path: Path) -> ResolvedAnalysisConfig:
    try:
        payload = json.loads(config_revision_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AlignmentStageError(
            reason="alignment_config_unreadable",
            detail="Immutable analysis configuration could not be read.",
            event_name=ALIGNMENT_RESULT_INVALID_EVENT,
        ) from error
    if not isinstance(payload, dict):
        raise AlignmentStageError(
            reason="alignment_config_invalid",
            detail="Immutable analysis configuration must be a JSON object.",
            event_name=ALIGNMENT_RESULT_INVALID_EVENT,
        )
    filtered = {
        str(key): value
        for key, value in payload.items()
        if str(key) not in _INTERNAL_TASK_CONFIG_FIELDS
    }
    try:
        return ResolvedAnalysisConfig.model_validate(filtered)
    except Exception as error:
        raise AlignmentStageError(
            reason="alignment_config_invalid",
            detail="Immutable analysis configuration is not valid for alignment.",
            event_name=ALIGNMENT_RESULT_INVALID_EVENT,
        ) from error


def _load_input_processing_artifacts(*, context: StageContext) -> _InputProcessingArtifacts:
    committed_root = context.launch_spec.job_dir / "stages" / INPUT_PROCESSING_STAGE_ID
    staging_root = (
        context.launch_spec.job_dir
        / "staging"
        / INPUT_PROCESSING_STAGE_ID
        / context.launch_spec.worker_instance_id
    )
    candidate_roots = (committed_root, staging_root)
    for root in candidate_roots:
        manifest_path = root / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
        if not manifest_path.is_file():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = InputProcessingManifest.model_validate(payload)
        except Exception as error:
            raise AlignmentStageError(
                reason="input_processing_manifest_invalid",
                detail="Published input-processing manifest is invalid.",
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
            ) from error
        if manifest.task_id != context.launch_spec.task_id:
            raise AlignmentStageError(
                reason="input_processing_manifest_identity_mismatch",
                detail="Input-processing manifest task identity does not match this task.",
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
            )
        if manifest.job_id != context.launch_spec.job_id:
            raise AlignmentStageError(
                reason="input_processing_manifest_identity_mismatch",
                detail="Input-processing manifest job identity does not match this job.",
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
            )
        if manifest.config_hash != context.launch_spec.config_hash:
            raise AlignmentStageError(
                reason="input_processing_manifest_config_mismatch",
                detail="Input-processing manifest configuration hash does not match this job.",
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
            )
        return _InputProcessingArtifacts(
            candidate_roots=candidate_roots,
            selected_root=root,
            manifest=manifest,
        )
    raise AlignmentStageError(
        reason="input_processing_manifest_missing",
        detail="Published input-processing manifest is missing for alignment.",
        event_name=ALIGNMENT_RESULT_INVALID_EVENT,
    )


def _load_input_sequences(
    *, source: _InputProcessingArtifacts
) -> tuple[AlignmentInputSequence, ...]:
    sequences: list[AlignmentInputSequence] = []
    for item in source.manifest.unique_sequences:
        relative_path = _validate_relative_artifact_path(item.sequence_artifact_path)
        artifact_path = _find_artifact_path(source=source, relative_path=relative_path)
        record_id, sequence = read_single_fasta_record(path=str(artifact_path))
        if record_id != item.sequence_id:
            raise AlignmentStageError(
                reason="input_sequence_identifier_mismatch",
                detail="A sequence artifact identifier does not match its manifest entry.",
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
                context={"sequence_id": item.sequence_id},
            )
        sequences.append(
            AlignmentInputSequence(
                sequence_id=item.sequence_id,
                sequence=sequence,
                logical_sample_ids=item.logical_sample_ids,
            )
        )
    return tuple(sequences)


def _load_prealigned_identities(*, source: _InputProcessingArtifacts) -> dict[str, str]:
    identities: dict[str, str] = {}
    for item in source.manifest.unique_sequences:
        if item.ungapped_sequence_sha256 is None:
            raise AlignmentStageError(
                reason="prealigned_identity_missing",
                detail=(
                    "Input processing did not publish an independent normalized identity "
                    "for a prealigned record."
                ),
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
                context={"sequence_id": item.sequence_id},
            )
        identities[item.sequence_id] = item.ungapped_sequence_sha256
    return identities


def _find_artifact_path(*, source: _InputProcessingArtifacts, relative_path: str) -> Path:
    roots = (
        source.selected_root,
        *(root for root in source.candidate_roots if root != source.selected_root),
    )
    for root in roots:
        candidate = root / Path(PurePosixPath(relative_path))
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError:
            continue
        if resolved_candidate.is_file():
            return resolved_candidate
    raise AlignmentStageError(
        reason="input_sequence_artifact_missing",
        detail="A sequence artifact referenced by input processing is missing.",
        event_name=ALIGNMENT_RESULT_INVALID_EVENT,
        context={"relative_path": relative_path},
    )


def _validate_relative_artifact_path(relative_path: str) -> str:
    normalized = relative_path.strip().replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if normalized == "" or posix.is_absolute() or windows.is_absolute():
        raise AlignmentStageError(
            reason="input_sequence_artifact_path_invalid",
            detail="A sequence artifact path is not a safe relative path.",
            event_name=ALIGNMENT_RESULT_INVALID_EVENT,
        )
    if ".." in posix.parts or ".." in windows.parts:
        raise AlignmentStageError(
            reason="input_sequence_artifact_path_invalid",
            detail="A sequence artifact path escapes the input-processing stage.",
            event_name=ALIGNMENT_RESULT_INVALID_EVENT,
        )
    return posix.as_posix()


def _validate_prealigned_sources(
    *, valid_samples: tuple[InputProcessingLogicalSample, ...]
) -> None:
    allowed = frozenset(SUPPORTED_FASTA_EXTENSIONS)
    for sample in valid_samples:
        if sample.provenance.format_hint.lower() not in allowed:
            raise AlignmentStageError(
                reason="prealigned_format_invalid",
                detail="Prealigned mode accepts aligned FASTA input only.",
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
                context={
                    "sample_id": sample.sample_id,
                    "format_hint": sample.provenance.format_hint,
                },
            )


def _resolve_effective_reference(
    *,
    source_manifest: InputProcessingManifest,
    valid_samples: tuple[InputProcessingLogicalSample, ...],
) -> tuple[str | None, str | None]:
    resolved = source_manifest.resolved_reference
    if resolved is not None:
        matches = [sample for sample in valid_samples if sample.sample_id == resolved.sample_id]
        if len(matches) != 1 or matches[0].sequence_id != resolved.sequence_id:
            raise AlignmentStageError(
                reason="alignment_reference_invalid",
                detail="Resolved reference is inconsistent with valid logical samples.",
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
                context={"sample_id": resolved.sample_id, "sequence_id": resolved.sequence_id},
            )
        return resolved.sample_id, resolved.sequence_id
    if len(valid_samples) > 0:
        sequence_id = _require_sample_sequence_id(valid_samples[0])
        return valid_samples[0].sample_id, sequence_id
    raise AlignmentStageError(
        reason="alignment_reference_unresolved",
        detail="Alignment requires an unambiguously resolved reference sample.",
        event_name=ALIGNMENT_RESULT_INVALID_EVENT,
    )


def _require_sample_sequence_id(sample: InputProcessingLogicalSample) -> str:
    if sample.sequence_id is None:
        raise AlignmentStageError(
            reason="alignment_sample_sequence_id_missing",
            detail="An eligible logical sample has no sequence identifier.",
            event_name=ALIGNMENT_RESULT_INVALID_EVENT,
            context={"sample_id": sample.sample_id},
        )
    return sample.sequence_id


def _build_disabled_manifest(
    *,
    context: StageContext,
    config: ResolvedAnalysisConfig,
    source_manifest: InputProcessingManifest,
    started_at: str,
    started_monotonic: float,
) -> AlignmentManifest:
    input_metadata = [
        {
            "sequence_id": item.sequence_id,
            "logical_sample_ids": list(item.logical_sample_ids),
            "length": item.facts.ungapped_length,
        }
        for item in source_manifest.unique_sequences
    ]
    canonical = json.dumps(
        input_metadata,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    completed_at = utc_now()
    return AlignmentManifest(
        task_id=context.launch_spec.task_id,
        job_id=context.launch_spec.job_id,
        config_hash=context.launch_spec.config_hash,
        mode=config.alignment.mode,
        logical_sample_count=source_manifest.dataset_summary.valid_sample_count,
        unique_sequence_count=source_manifest.dataset_summary.unique_sequence_count,
        input_set_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        started_at=started_at,
        completed_at=serialize_utc_datetime(completed_at),
        duration_seconds=max(0.0, time.monotonic() - started_monotonic),
        outcome=AlignmentStageOutcome.SKIPPED_DISABLED,
    )


def _write_alignment_manifest(*, context: StageContext, manifest: AlignmentManifest) -> None:
    payload = manifest.model_dump(mode="json")
    AlignmentManifest.model_validate(payload)
    write_text_atomically(
        path=context.stage_staging_directory / ALIGNMENT_MANIFEST_RELATIVE_PATH,
        payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _validate_artifacts(*, stage_directory: Path, artifacts: tuple[str, ...]) -> None:
    for relative_path in artifacts:
        if not (stage_directory / relative_path).is_file():
            raise AlignmentStageError(
                reason="alignment_artifact_missing",
                detail="A validated alignment artifact is missing before publication.",
                event_name=ALIGNMENT_RESULT_INVALID_EVENT,
                context={"relative_path": relative_path},
            )


def _control_stop_event(error: BaseException) -> str | None:
    reason = getattr(error, "reason", None)
    reason_value = getattr(reason, "value", None)
    if reason_value == "pause_requested":
        return ALIGNMENT_MAFFT_STOPPED_PAUSE_EVENT
    if reason_value == "cancel_requested":
        return ALIGNMENT_MAFFT_STOPPED_CANCEL_EVENT
    if reason_value in {"runtime_shutdown", "preemption_requested", "deletion_requested"}:
        return ALIGNMENT_MAFFT_STOPPED_SHUTDOWN_EVENT
    return None
