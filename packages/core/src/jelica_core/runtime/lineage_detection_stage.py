from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from jelica_core.config import ResolvedAnalysisConfig
from jelica_core.lineage_detection import (
    LINEAGE_ASSIGNMENTS_TSV_RELATIVE_PATH,
    LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH,
    LINEAGE_DETECTION_STAGE_ID,
    LINEAGE_GROUPS_JSON_RELATIVE_PATH,
    LineageAssignment,
    LineageAssignmentStatus,
    LineageDetectionManifest,
    LineageDetectionStatus,
    LineageGroupsResult,
    NextcladeError,
    NextcladePangoRecord,
    NextcladeRunner,
    artifact_metadata,
    build_lineage_groups,
    lineage_detection_artifact_paths,
    parse_lineage_assignments_tsv,
    parse_nextclade_pango_tsv,
    serialize_lineage_assignments_tsv,
)
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .artifacts import StageCommitError, validate_committed_stage_snapshot
from .input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    INPUT_PROCESSING_STAGE_ID,
    InputProcessingManifest,
    InputProcessingState,
)
from .pipeline import ProgressReporter, StageContext, StageRunResult

LINEAGE_DETECTION_STARTED_EVENT: Final = "LINEAGE_DETECTION_STARTED"
LINEAGE_DETECTION_SKIPPED_EVENT: Final = "LINEAGE_DETECTION_SKIPPED"
LINEAGE_DETECTION_PROGRESS_EVENT: Final = "LINEAGE_DETECTION_PROGRESS"
LINEAGE_DETECTION_RESULT_PUBLISHED_EVENT: Final = "LINEAGE_DETECTION_RESULT_PUBLISHED"
LINEAGE_DETECTION_COMPLETED_EVENT: Final = "LINEAGE_DETECTION_COMPLETED"
LINEAGE_DETECTION_FAILED_EVENT: Final = "LINEAGE_DETECTION_FAILED"

_INTERNAL_TASK_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_directory_max_depth", "ncbi_max_retries"}
)


class LineageDetectionStageError(RuntimeError):
    """Safe failure raised for a lineage-detection stage boundary."""

    def __init__(
        self,
        *,
        reason: str,
        detail: str,
        context: dict[str, object] | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.event_name = LINEAGE_DETECTION_FAILED_EVENT
        self.context = context or {}
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _InputProcessingInputs:
    stage_root: Path
    manifest: InputProcessingManifest
    source_artifacts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DatasetMetadata:
    dataset_name: str | None
    dataset_version: str | None


@dataclass(frozen=True, slots=True)
class LineageDetectionStage:
    stage_id: str = LINEAGE_DETECTION_STAGE_ID
    weight: float = 1.0
    runner: NextcladeRunner | None = None

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)
        (context.stage_staging_directory / "lineage_detection").mkdir(
            parents=True,
            exist_ok=True,
        )

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        started_at = utc_now()
        started_monotonic = time.monotonic()
        context.check_control()
        config = _load_resolved_config(context.launch_spec.config_revision_path)
        lineage_config = config.lineage_detection
        context.emit_event(
            LINEAGE_DETECTION_STARTED_EVENT,
            {
                "enabled": lineage_config.enabled,
                "method": lineage_config.method.value,
                "detail": "Lineage-detection stage started.",
            },
        )
        if not lineage_config.enabled:
            return self._run_disabled(
                context=context,
                progress_reporter=progress_reporter,
                config=config,
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        if lineage_config.dataset is None:
            raise LineageDetectionStageError(
                reason="lineage_detection_dataset_missing",
                detail="Lineage detection requires a configured local Nextclade dataset.",
            )
        dataset = Path(lineage_config.dataset)
        dataset_metadata = _load_dataset_metadata(dataset=dataset)
        source = _load_input_processing_inputs(context=context)
        _validate_input_processing_mapping(manifest=source.manifest)
        _update_progress_description(
            progress_reporter,
            description="Lineage detection: validating input-processing sequences.",
        )
        progress_reporter(0.15)
        context.emit_event(
            LINEAGE_DETECTION_PROGRESS_EVENT,
            {
                "phase": "validate_inputs",
                "unique_sequence_count": len(source.manifest.unique_sequences),
                "detail": "Lineage detection: validated committed input-processing artifacts.",
            },
        )

        selected_runner = self.runner or NextcladeRunner()
        availability = selected_runner.probe()
        if not availability.available or availability.version is None:
            raise LineageDetectionStageError(
                reason=availability.error_code or "nextclade_unavailable",
                detail=availability.reason or "Nextclade is unavailable.",
            )
        context.check_control()
        root = context.stage_staging_directory
        with tempfile.TemporaryDirectory(
            prefix="lineage-nextclade-",
            dir=root,
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            input_fasta = temporary_root / "normalized_unique_sequences.fasta"
            _build_nextclade_input_fasta(
                output_path=input_fasta,
                stage_root=source.stage_root,
                manifest=source.manifest,
            )
            output_tsv = temporary_root / "nextclade.tsv"
            _update_progress_description(
                progress_reporter,
                description="Lineage detection: running Nextclade Pango assignment.",
            )
            context.emit_event(
                LINEAGE_DETECTION_PROGRESS_EVENT,
                {
                    "phase": "run_nextclade",
                    "tool_version": availability.version,
                    "unique_sequence_count": len(source.manifest.unique_sequences),
                    "detail": "Lineage detection: Nextclade Pango assignment is running.",
                },
            )
            progress_reporter(0.35)
            try:
                result = selected_runner.run(
                    availability=availability,
                    dataset=dataset,
                    input_fasta=input_fasta,
                    output_tsv=output_tsv,
                    working_directory=temporary_root / "work",
                    control_check=context.check_control,
                    process_started=context.register_external_process,
                    process_stopped=context.unregister_external_process,
                )
                records = parse_nextclade_pango_tsv(
                    result.output_tsv_path.read_text(encoding="utf-8")
                )
            except NextcladeError as error:
                raise LineageDetectionStageError(
                    reason=error.code,
                    detail=error.detail,
                    context={"exit_code": error.exit_code} if error.exit_code is not None else {},
                ) from error
            except (OSError, UnicodeError) as error:
                raise LineageDetectionStageError(
                    reason="nextclade_output_unreadable",
                    detail="Nextclade TSV output could not be read.",
                ) from error

        assignments = _expand_nextclade_records(
            records=records,
            manifest=source.manifest,
        )
        groups = build_lineage_groups(assignments)
        assigned_sample_count = sum(
            item.status is LineageAssignmentStatus.ASSIGNED for item in assignments
        )
        unassigned_sample_count = len(assignments) - assigned_sample_count
        _update_progress_description(
            progress_reporter,
            description="Lineage detection: validating and serializing assignments.",
        )
        progress_reporter(0.75)
        assignments_path = root / LINEAGE_ASSIGNMENTS_TSV_RELATIVE_PATH
        assignments_payload = serialize_lineage_assignments_tsv(assignments)
        write_text_atomically(path=assignments_path, payload=assignments_payload)
        parsed_assignments = parse_lineage_assignments_tsv(assignments_payload)
        if parsed_assignments != assignments:
            raise LineageDetectionStageError(
                reason="lineage_assignments_serialization_invalid",
                detail="lineage_assignments.tsv did not round-trip into canonical assignments.",
            )
        groups_path = root / LINEAGE_GROUPS_JSON_RELATIVE_PATH
        groups_result = LineageGroupsResult(
            groups=groups,
            assigned_sample_count=assigned_sample_count,
            unassigned_sample_count=unassigned_sample_count,
        )
        _write_json_model(path=groups_path, model=groups_result)
        assignments_metadata = artifact_metadata(
            assignments_path,
            relative_path=LINEAGE_ASSIGNMENTS_TSV_RELATIVE_PATH,
        )
        groups_metadata = artifact_metadata(
            groups_path,
            relative_path=LINEAGE_GROUPS_JSON_RELATIVE_PATH,
        )
        completed_at = utc_now()
        manifest = LineageDetectionManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=True,
            normalized_settings=lineage_config,
            status=LineageDetectionStatus.COMPLETED,
            method=lineage_config.method,
            tool_version=availability.version,
            dataset_path=lineage_config.dataset,
            dataset_name=dataset_metadata.dataset_name,
            dataset_version=dataset_metadata.dataset_version,
            assignments=assignments,
            groups=groups,
            assigned_sample_count=assigned_sample_count,
            unassigned_sample_count=unassigned_sample_count,
            started_at=serialize_utc_datetime(started_at),
            completed_at=serialize_utc_datetime(completed_at),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            source_artifacts=source.source_artifacts,
            artifacts=(assignments_metadata, groups_metadata),
        )
        _write_json_model(
            path=root / LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH,
            model=manifest,
        )
        _validate_staged_artifacts(root=root, manifest=manifest)
        context.check_control()
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=lineage_detection_artifact_paths(manifest),
            check_control_before_commit=True,
        )

    def _run_disabled(
        self,
        *,
        context: StageContext,
        progress_reporter: ProgressReporter,
        config: ResolvedAnalysisConfig,
        started_at: datetime,
        started_monotonic: float,
    ) -> StageRunResult:
        context.emit_event(
            LINEAGE_DETECTION_SKIPPED_EVENT,
            {
                "reason": "lineage_detection_disabled",
                "detail": "Lineage detection was skipped because it is disabled.",
            },
        )
        progress_reporter(0.5)
        manifest = LineageDetectionManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=False,
            normalized_settings=config.lineage_detection,
            skipped_reason="lineage_detection_disabled",
            status=LineageDetectionStatus.COMPLETED,
            method=config.lineage_detection.method,
            assigned_sample_count=0,
            unassigned_sample_count=0,
            started_at=serialize_utc_datetime(started_at),
            completed_at=serialize_utc_datetime(utc_now()),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
        )
        _write_json_model(
            path=context.stage_staging_directory / LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH,
            model=manifest,
        )
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=(LINEAGE_DETECTION_MANIFEST_RELATIVE_PATH,),
            check_control_before_commit=True,
        )


def _load_resolved_config(path: Path) -> ResolvedAnalysisConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LineageDetectionStageError(
            reason="lineage_detection_config_unreadable",
            detail="Immutable analysis configuration could not be read.",
        ) from error
    if not isinstance(payload, dict):
        raise LineageDetectionStageError(
            reason="lineage_detection_config_invalid",
            detail="Immutable analysis configuration must be a JSON object.",
        )
    filtered = {
        str(key): value
        for key, value in payload.items()
        if str(key) not in _INTERNAL_TASK_CONFIG_FIELDS
    }
    try:
        return ResolvedAnalysisConfig.model_validate(filtered)
    except Exception as error:
        raise LineageDetectionStageError(
            reason="lineage_detection_config_invalid",
            detail="Immutable analysis configuration is invalid for lineage detection.",
        ) from error


def _load_dataset_metadata(*, dataset: Path) -> _DatasetMetadata:
    if not dataset.is_absolute():
        raise LineageDetectionStageError(
            reason="lineage_detection_dataset_path_invalid",
            detail="Configured Nextclade dataset path must be absolute at execution time.",
        )
    if not dataset.is_dir() or dataset.is_symlink():
        raise LineageDetectionStageError(
            reason="lineage_detection_dataset_unreadable",
            detail="Configured Nextclade dataset directory is missing or invalid.",
        )
    pathogen_path = dataset / "pathogen.json"
    if not pathogen_path.is_file() or pathogen_path.is_symlink():
        raise LineageDetectionStageError(
            reason="lineage_detection_dataset_metadata_missing",
            detail="Configured Nextclade dataset does not contain a readable pathogen.json.",
        )
    try:
        payload = json.loads(pathogen_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LineageDetectionStageError(
            reason="lineage_detection_dataset_metadata_invalid",
            detail="Configured Nextclade dataset pathogen.json is invalid.",
        ) from error
    if not isinstance(payload, dict):
        raise LineageDetectionStageError(
            reason="lineage_detection_dataset_metadata_invalid",
            detail="Configured Nextclade dataset pathogen.json must be a JSON object.",
        )
    dataset_name = _optional_text(payload.get("name")) or _optional_text(payload.get("path"))
    version_value = payload.get("version")
    dataset_version = _optional_text(version_value)
    if isinstance(version_value, dict):
        dataset_version = _optional_text(version_value.get("tag"))
    return _DatasetMetadata(dataset_name=dataset_name, dataset_version=dataset_version)


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _load_input_processing_inputs(*, context: StageContext) -> _InputProcessingInputs:
    try:
        snapshot = validate_committed_stage_snapshot(
            job_dir=context.launch_spec.job_dir,
            stage_id=INPUT_PROCESSING_STAGE_ID,
            expected_job_id=context.launch_spec.job_id,
            expected_pipeline_version=context.launch_spec.pipeline_version,
            expected_task_id=context.launch_spec.task_id,
            expected_config_hash=context.launch_spec.config_hash,
        )
    except StageCommitError as error:
        raise LineageDetectionStageError(
            reason="input_processing_snapshot_invalid",
            detail="Published input-processing snapshot is missing or invalid.",
        ) from error
    stage_root = context.launch_spec.job_dir / "stages" / INPUT_PROCESSING_STAGE_ID
    try:
        manifest = InputProcessingManifest.model_validate_json(
            (stage_root / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise LineageDetectionStageError(
            reason="input_processing_manifest_invalid",
            detail="Published input-processing manifest is invalid for lineage detection.",
        ) from error
    if manifest.processing_state is not InputProcessingState.COMPLETED:
        raise LineageDetectionStageError(
            reason="input_processing_incomplete",
            detail="Lineage detection requires a completed input-processing stage.",
        )
    expected_artifacts = {
        INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
        *(item.sequence_artifact_path for item in manifest.unique_sequences),
    }
    if not expected_artifacts.issubset(set(snapshot.manifest.artifacts)):
        raise LineageDetectionStageError(
            reason="input_processing_snapshot_invalid",
            detail="Published input-processing snapshot lacks required sequence artifacts.",
        )
    source_artifacts = tuple(
        f"stages/{INPUT_PROCESSING_STAGE_ID}/{relative_path}"
        for relative_path in sorted(expected_artifacts)
    )
    return _InputProcessingInputs(
        stage_root=stage_root,
        manifest=manifest,
        source_artifacts=source_artifacts,
    )


def _validate_input_processing_mapping(*, manifest: InputProcessingManifest) -> None:
    eligible_samples = {
        sample.sample_id: sample.sequence_id
        for sample in manifest.logical_samples
        if sample.eligible_for_analysis and sample.sequence_id is not None
    }
    seen_sample_ids: set[str] = set()
    seen_sequence_ids: set[str] = set()
    for unique_sequence in manifest.unique_sequences:
        if unique_sequence.sequence_id in seen_sequence_ids:
            raise LineageDetectionStageError(
                reason="input_processing_mapping_invalid",
                detail="Input-processing unique sequence identifiers are not unique.",
            )
        seen_sequence_ids.add(unique_sequence.sequence_id)
        for sample_id in unique_sequence.logical_sample_ids:
            if (
                sample_id in seen_sample_ids
                or eligible_samples.get(sample_id) != unique_sequence.sequence_id
            ):
                raise LineageDetectionStageError(
                    reason="input_processing_mapping_invalid",
                    detail="Input-processing sample-to-sequence mapping is inconsistent.",
                )
            seen_sample_ids.add(sample_id)
    if set(eligible_samples) != seen_sample_ids:
        raise LineageDetectionStageError(
            reason="input_processing_mapping_invalid",
            detail="Input-processing eligible samples do not map to unique sequence artifacts.",
        )


def _build_nextclade_input_fasta(
    *,
    output_path: Path,
    stage_root: Path,
    manifest: InputProcessingManifest,
) -> None:
    with output_path.open("wb") as output_handle:
        for unique_sequence in manifest.unique_sequences:
            sequence_path = stage_root / unique_sequence.sequence_artifact_path
            if not sequence_path.is_file() or sequence_path.is_symlink():
                raise LineageDetectionStageError(
                    reason="input_processing_sequence_artifact_missing",
                    detail="A normalized input sequence artifact is missing or invalid.",
                )
            with sequence_path.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, output_handle, length=1024 * 1024)


def _expand_nextclade_records(
    *,
    records: tuple[NextcladePangoRecord, ...],
    manifest: InputProcessingManifest,
) -> tuple[LineageAssignment, ...]:
    expected_sequence_ids = {item.sequence_id for item in manifest.unique_sequences}
    records_by_sequence_id: dict[str, NextcladePangoRecord] = {}
    for record in records:
        if record.sequence_name not in expected_sequence_ids:
            raise LineageDetectionStageError(
                reason="nextclade_output_unknown_sequence",
                detail="Nextclade TSV contains a sequence that was not submitted by JELICA.",
            )
        if record.sequence_name in records_by_sequence_id:
            raise LineageDetectionStageError(
                reason="nextclade_output_duplicate_sequence",
                detail="Nextclade TSV contains duplicate results for a submitted sequence.",
            )
        records_by_sequence_id[record.sequence_name] = record
    missing = expected_sequence_ids.difference(records_by_sequence_id)
    if missing:
        raise LineageDetectionStageError(
            reason="nextclade_output_missing_sequence",
            detail="Nextclade TSV is missing a result for a submitted sequence.",
        )
    assignments: list[LineageAssignment] = []
    for unique_sequence in manifest.unique_sequences:
        record = records_by_sequence_id[unique_sequence.sequence_id]
        status = (
            LineageAssignmentStatus.ASSIGNED
            if record.lineage is not None
            else LineageAssignmentStatus.UNASSIGNED
        )
        assignments.extend(
            LineageAssignment(
                sample_id=sample_id,
                sequence_id=unique_sequence.sequence_id,
                lineage=record.lineage,
                status=status,
                diagnostics=record.diagnostics,
            )
            for sample_id in unique_sequence.logical_sample_ids
        )
    return tuple(sorted(assignments, key=lambda item: item.sample_id))


def _write_json_model(*, path: Path, model: BaseModel) -> None:
    payload = model.model_dump(mode="json")
    type(model).model_validate(payload)
    write_text_atomically(
        path=path,
        payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _validate_staged_artifacts(*, root: Path, manifest: LineageDetectionManifest) -> None:
    assignments_path = root / LINEAGE_ASSIGNMENTS_TSV_RELATIVE_PATH
    groups_path = root / LINEAGE_GROUPS_JSON_RELATIVE_PATH
    try:
        assignments = parse_lineage_assignments_tsv(assignments_path.read_text(encoding="utf-8"))
        groups = LineageGroupsResult.model_validate_json(groups_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise LineageDetectionStageError(
            reason="lineage_detection_artifact_invalid",
            detail="A lineage-detection artifact is invalid before publication.",
        ) from error
    if assignments != manifest.assignments or groups.groups != manifest.groups:
        raise LineageDetectionStageError(
            reason="lineage_detection_artifact_inconsistent",
            detail="Lineage-detection artifacts do not match the canonical manifest.",
        )
    for metadata in manifest.artifacts:
        path = root / metadata.relative_path
        if not path.is_file() or path.is_symlink():
            raise LineageDetectionStageError(
                reason="lineage_detection_artifact_missing",
                detail="A lineage-detection artifact is missing before publication.",
            )
        if path.stat().st_size != metadata.size_bytes or _sha256_file(path) != metadata.sha256:
            raise LineageDetectionStageError(
                reason="lineage_detection_artifact_integrity_failed",
                detail="A lineage-detection artifact failed integrity validation.",
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _update_progress_description(
    progress_reporter: ProgressReporter,
    *,
    description: str,
) -> None:
    update = getattr(progress_reporter, "update", None)
    if callable(update):
        update(description=description)


__all__ = [
    "LINEAGE_DETECTION_COMPLETED_EVENT",
    "LINEAGE_DETECTION_FAILED_EVENT",
    "LINEAGE_DETECTION_PROGRESS_EVENT",
    "LINEAGE_DETECTION_RESULT_PUBLISHED_EVENT",
    "LINEAGE_DETECTION_SKIPPED_EVENT",
    "LINEAGE_DETECTION_STARTED_EVENT",
    "LineageDetectionStage",
    "LineageDetectionStageError",
]
