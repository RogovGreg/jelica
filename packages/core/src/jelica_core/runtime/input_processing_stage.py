from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Final, Protocol

from jelica_core.config import (
    AnalysisAlignmentMode,
    ResolvedAnalysisConfig,
    ResolvedAnalysisStatisticsConfig,
)
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .input_parsers import (
    INPUT_MANIFEST_RELATIVE_PATH,
    PARSER_ISSUE_FILE_NOT_FOUND,
    InputRecordParser,
    MaterializedInputFile,
    ParsedInputFileResult,
)
from .input_processing_models import (
    INPUT_PROCESSING_KMER_HITS_DIR,
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    INPUT_PROCESSING_SEQUENCE_ARTIFACTS_DIR,
    InputProcessingFileStatus,
    InputProcessingManifest,
    InputProcessingState,
    InputProcessingUniqueSequence,
    InputProcessingValidationIssue,
    KmerHitsSidecar,
    KmerQuerySummary,
    ParsedInputRecord,
    ValidationIssueScope,
    input_processing_artifact_paths,
    sequence_id_digest,
)
from .input_validation import (
    DatasetValidator,
    InputValidationResult,
    RecordValidator,
    ValidatedRecord,
)
from .pipeline import ProgressReporter, StageContext, StageFailure, StageRunResult
from .sequence_inspector import SequenceInspectionResult, SequenceInspector

INPUT_ACQUISITION_STAGE_ID: Final = "input_acquisition"
INPUT_PROCESSING_DATASET_INVALID_REASON: Final = "input_processing_dataset_invalid"
INPUT_PROCESSING_STARTED_EVENT: Final = "INPUT_PROCESSING_STARTED"
INPUT_PROCESSING_FILE_PROCESSED_EVENT: Final = "INPUT_PROCESSING_FILE_PROCESSED"
INPUT_PROCESSING_COMPLETED_EVENT: Final = "INPUT_PROCESSING_COMPLETED"
INPUT_PROCESSING_VALIDATION_FAILED_EVENT: Final = "INPUT_PROCESSING_VALIDATION_FAILED"
INPUT_PROCESSING_FAILED_EVENT: Final = "INPUT_PROCESSING_FAILED"
# Kept as import-compatible names; short-lived updates now use ProgressReporter.
INPUT_PROCESSING_PROGRESS_FILE_STARTED_EVENT: Final = "INPUT_PROCESSING_FILE_STARTED"
INPUT_PROCESSING_PROGRESS_RECORD_STARTED_EVENT: Final = "INPUT_PROCESSING_RECORD_STARTED"
INPUT_PROCESSING_PROGRESS_RECORD_PROCESSED_EVENT: Final = "INPUT_PROCESSING_RECORD_PROCESSED"
INPUT_PROCESSING_DATASET_INVALID_EVENT: Final = INPUT_PROCESSING_VALIDATION_FAILED_EVENT
_FASTA_LINE_WIDTH: Final = 80
_INTERNAL_TASK_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_directory_max_depth", "ncbi_max_retries"}
)


class InputProcessingError(RuntimeError):
    """Raised when runtime input processing cannot produce a publishable stage output."""


class _InputRecordParserProtocol(Protocol):
    def parse_materialized_file(
        self,
        *,
        stage_staging_directory: Path,
        materialized_file: MaterializedInputFile,
        alignment_mode: AnalysisAlignmentMode | str,
    ) -> ParsedInputFileResult: ...


class _SequenceInspectorProtocol(Protocol):
    def inspect(
        self,
        parsed_record: ParsedInputRecord,
        *,
        statistics_config: ResolvedAnalysisStatisticsConfig,
        alignment_mode: AnalysisAlignmentMode | str,
        control_check: Callable[[], None] | None = None,
    ) -> SequenceInspectionResult: ...


@dataclass(frozen=True, slots=True)
class InputProcessingStageResult:
    manifest_relative_path: str
    valid_sample_count: int
    comparative_analysis_available: bool
    dataset_valid: bool
    dataset_issue_codes: tuple[str, ...]
    artifacts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _AcquisitionArtifacts:
    committed_root_directory: Path
    worker_staging_root_directory: Path | None
    manifest_path: Path
    materialized_files: tuple[MaterializedInputFile, ...]


@dataclass(frozen=True, slots=True)
class InputProcessingStage:
    stage_id: str = "input_processing"
    weight: float = 1.0
    parser: _InputRecordParserProtocol | None = None
    inspector: _SequenceInspectorProtocol | None = None
    record_validator: RecordValidator | None = None
    dataset_validator: DatasetValidator | None = None

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        context.check_control()
        resolved_config = _load_resolved_config(context.launch_spec.config_revision_path)
        progress_reporter(0.05)

        context.check_control()
        acquisition = _load_acquisition_artifacts(context=context)
        progress_reporter(0.1)
        context.emit_event(
            INPUT_PROCESSING_STARTED_EVENT,
            {
                "input_file_count": len(acquisition.materialized_files),
                "alignment_mode": resolved_config.alignment.mode.value,
                "configured_kmer_count": len(resolved_config.statistics.kmers),
                "reference_configured": resolved_config.reference is not None,
                "detail": (
                    "Input processing started: "
                    f"{len(acquisition.materialized_files)} materialized input files."
                ),
            },
        )

        parser = self.parser or InputRecordParser()
        inspector = self.inspector or SequenceInspector()
        record_validator = self.record_validator or RecordValidator()
        dataset_validator = self.dataset_validator or DatasetValidator()

        parsed_files: list[ParsedInputFileResult] = []
        validated_records: list[ValidatedRecord] = []
        inspection_by_sequence_id: dict[str, SequenceInspectionResult] = {}
        total_files = max(len(acquisition.materialized_files), 1)
        for file_index, materialized_file in enumerate(acquisition.materialized_files, start=1):
            context.check_control()
            materialized_root_directory = _resolve_materialized_file_root_directory(
                acquisition=acquisition,
                relative_path=materialized_file.relative_path,
            )
            _update_progress_description(
                progress_reporter,
                description=(
                    f"Processing {materialized_file.relative_path} "
                    f"({file_index}/{total_files})"
                ),
            )
            parsed_file = _parse_materialized_file_with_root_fallback(
                parser=parser,
                acquisition=acquisition,
                preferred_root_directory=materialized_root_directory,
                materialized_file=materialized_file,
                alignment_mode=resolved_config.alignment.mode,
            )
            parsed_files.append(parsed_file)
            file_validated_records: list[ValidatedRecord] = []
            for parsed_record in parsed_file.records:
                context.check_control()
                _update_progress_description(
                    progress_reporter,
                    description=(
                        f"Processing {_record_label(parsed_record=parsed_record)} "
                        f"in {materialized_file.relative_path}"
                    ),
                )
                inspection_result = inspector.inspect(
                    parsed_record,
                    statistics_config=resolved_config.statistics,
                    alignment_mode=resolved_config.alignment.mode,
                    control_check=context.check_control,
                )
                validated_record = record_validator.validate(
                    parsed_record=parsed_record,
                    parser_issues=_record_issues_for_record(
                        parsed_file=parsed_file,
                        parsed_record=parsed_record,
                    ),
                    inspection_result=inspection_result,
                )
                validated_records.append(validated_record)
                file_validated_records.append(validated_record)
                _update_progress_description(
                    progress_reporter,
                    description=(
                        f"Processed {_record_label(parsed_record=parsed_record)}: "
                        f"{'valid' if validated_record.is_valid else 'invalid'}"
                    ),
                )
                if (
                    validated_record.is_valid
                    and validated_record.facts.sequence_id is not None
                    and validated_record.facts.sequence_id not in inspection_by_sequence_id
                ):
                    sequence_id = validated_record.facts.sequence_id
                    inspection_by_sequence_id[sequence_id] = inspection_result
            context.emit_event(
                INPUT_PROCESSING_FILE_PROCESSED_EVENT,
                _build_file_processed_event_context(
                    parsed_file=parsed_file,
                    validated_records=file_validated_records,
                    file_index=file_index,
                    total_file_count=total_files,
                ),
            )
            progress_reporter(0.1 + (0.5 * file_index / total_files))

        context.check_control()
        validation_result = dataset_validator.build_result(
            parsed_files=tuple(parsed_files),
            validated_records=tuple(validated_records),
            alignment_mode=resolved_config.alignment.mode,
            reference_selector=resolved_config.reference,
        )
        progress_reporter(0.7)

        stage_result = _write_stage_artifacts(
            context=context,
            resolved_config=resolved_config,
            validation_result=validation_result,
            inspection_by_sequence_id=inspection_by_sequence_id,
            input_manifest_relative_path=INPUT_MANIFEST_RELATIVE_PATH,
            control_check=context.check_control,
        )
        progress_reporter(0.95)

        if not stage_result.dataset_valid:
            validation_failure_context = _build_validation_failed_event_context(
                validation_result=validation_result,
                manifest_relative_path=stage_result.manifest_relative_path,
            )
            context.emit_event(INPUT_PROCESSING_VALIDATION_FAILED_EVENT, validation_failure_context)
            return StageRunResult(
                artifacts=stage_result.artifacts,
                failure=StageFailure(
                    reason=INPUT_PROCESSING_DATASET_INVALID_REASON,
                    detail=str(validation_failure_context["detail"]),
                    failure_event_name=INPUT_PROCESSING_DATASET_INVALID_EVENT,
                    failure_context=validation_failure_context,
                ),
                check_control_before_commit=True,
            )
        context.emit_event(
            INPUT_PROCESSING_COMPLETED_EVENT,
            _build_completed_event_context(
                validation_result=validation_result,
                manifest_relative_path=stage_result.manifest_relative_path,
                alignment_mode=resolved_config.alignment.mode.value,
            ),
        )
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=stage_result.artifacts,
            check_control_before_commit=True,
        )


def _record_label(*, parsed_record: ParsedInputRecord) -> str:
    record_id = parsed_record.record_id.strip() if parsed_record.record_id is not None else ""
    if record_id != "":
        return record_id
    return f"record #{parsed_record.record_index + 1}"


def _update_progress_description(
    progress_reporter: ProgressReporter,
    *,
    description: str,
) -> None:
    update = getattr(progress_reporter, "update", None)
    if callable(update):
        update(description=description)


def _primary_issue_code(*, issues: tuple[InputProcessingValidationIssue, ...]) -> str | None:
    issue = _primary_issue(issues=issues)
    if issue is None:
        return None
    return issue.code


def _primary_issue(
    *,
    issues: tuple[InputProcessingValidationIssue, ...],
) -> InputProcessingValidationIssue | None:
    for issue in issues:
        if issue.severity.value == "error":
            return issue
    if len(issues) > 0:
        return issues[0]
    return None


def _build_file_processed_event_context(
    *,
    parsed_file: ParsedInputFileResult,
    validated_records: list[ValidatedRecord],
    file_index: int,
    total_file_count: int,
) -> dict[str, object]:
    parsed_record_count = len(parsed_file.records)
    valid_sample_count = sum(1 for item in validated_records if item.is_valid)
    invalid_sample_count = len(validated_records) - valid_sample_count
    all_issues = list(parsed_file.issues)
    for record in validated_records:
        all_issues.extend(record.logical_sample.validation_issues)
    deduplicated_issues = _deduplicate_issues(all_issues)
    issue_count_by_code = Counter(issue.code for issue in deduplicated_issues)
    issue_count_by_severity = Counter(issue.severity.value for issue in deduplicated_issues)
    issue_count = len(deduplicated_issues)
    primary_issue = _primary_issue(issues=tuple(deduplicated_issues))

    status = InputProcessingFileStatus.PROCESSED
    if parsed_record_count == 0 and issue_count > 0:
        status = InputProcessingFileStatus.FAILED
    elif parsed_record_count == 0:
        status = InputProcessingFileStatus.SKIPPED

    if status == InputProcessingFileStatus.FAILED:
        event_type = "error"
    elif issue_count > 0:
        event_type = "warning"
    else:
        event_type = "info"

    detail = (
        f"Input file [{file_index}/{total_file_count}] processed: "
        f"{parsed_record_count} records, "
        f"{valid_sample_count} valid, "
        f"{invalid_sample_count} invalid."
    )
    if issue_count > 0:
        detail = f"{detail} Issues: {issue_count}."

    return {
        "relative_path": parsed_file.materialized_file.relative_path,
        "format_hint": parsed_file.materialized_file.format_hint,
        "file_index": file_index,
        "total_file_count": total_file_count,
        "parsed_record_count": parsed_record_count,
        "valid_sample_count": valid_sample_count,
        "invalid_sample_count": invalid_sample_count,
        "issue_count": issue_count,
        "issue_count_by_code": dict(sorted(issue_count_by_code.items(), key=lambda item: item[0])),
        "issue_count_by_severity": dict(
            sorted(issue_count_by_severity.items(), key=lambda item: item[0])
        ),
        "primary_issue_code": None if primary_issue is None else primary_issue.code,
        "primary_issue_message": None if primary_issue is None else primary_issue.message,
        "processing_status": status.value,
        "detail": detail,
        "event_type": event_type,
    }


def _deduplicate_issues(
    issues: list[InputProcessingValidationIssue] | tuple[InputProcessingValidationIssue, ...],
) -> list[InputProcessingValidationIssue]:
    deduplicated: list[InputProcessingValidationIssue] = []
    seen: set[
        tuple[str, str, str, str, str | None, str | None, int | None, str | None, str]
    ] = set()
    for issue in issues:
        context_key = json.dumps(issue.context, ensure_ascii=False, sort_keys=True)
        identity = (
            issue.code,
            issue.message,
            issue.severity.value,
            issue.scope.value,
            issue.path,
            issue.record_id,
            issue.record_index,
            issue.sample_id,
            context_key,
        )
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(issue)
    return deduplicated


def _build_completed_event_context(
    *,
    validation_result: InputValidationResult,
    manifest_relative_path: str,
    alignment_mode: str,
) -> dict[str, object]:
    summary = validation_result.dataset_summary
    processed_input_file_count = sum(
        1
        for item in validation_result.processed_files
        if item.status is InputProcessingFileStatus.PROCESSED
    )
    failed_processed_files = [
        item
        for item in validation_result.processed_files
        if item.status is InputProcessingFileStatus.FAILED
    ]
    failed_input_file_count = len(failed_processed_files)
    failed_input_files = [
        {
            "relative_path": item.relative_path,
            "code": _primary_issue_code(issues=item.validation_issues) or "unknown_issue",
        }
        for item in failed_processed_files
    ]
    valid_sample_count = summary.valid_sample_count
    if valid_sample_count == 1:
        detail = (
            "Input processing completed for 1 valid sample. "
            "Primary statistics are available. "
            "Comparative stages were skipped because at least 2 valid samples are required."
        )
    else:
        detail = (
            f"Input processing completed for {valid_sample_count} valid samples. "
            "Dataset is ready for comparative analysis, but comparative stages are "
            "not executed yet in the current version."
        )

    return {
        "processed_file_count": len(validation_result.processed_files),
        "parsed_record_count": summary.discovered_record_count,
        "valid_sample_count": summary.valid_sample_count,
        "invalid_sample_count": summary.invalid_sample_count,
        "unique_sequence_count": summary.unique_sequence_count,
        "duplicate_logical_sample_count": summary.duplicate_logical_sample_count,
        "processed_input_file_count": processed_input_file_count,
        "failed_input_file_count": failed_input_file_count,
        "failed_input_files": failed_input_files,
        "comparative_analysis_available": summary.comparative_analysis_available,
        "manifest_path": manifest_relative_path,
        "alignment_mode": alignment_mode,
        "resolved_reference_present": validation_result.resolved_reference is not None,
        "detail": detail,
    }


def _build_validation_failed_event_context(
    *,
    validation_result: InputValidationResult,
    manifest_relative_path: str,
) -> dict[str, object]:
    summary = validation_result.dataset_summary
    issue_codes = list(dict.fromkeys(issue.code for issue in validation_result.dataset_issues))
    detail = (
        "Input processing manifest was published at "
        f"'{manifest_relative_path}', but dataset validation failed: "
        + ", ".join(issue_codes)
    )
    return {
        "manifest_path": manifest_relative_path,
        "valid_sample_count": summary.valid_sample_count,
        "invalid_sample_count": summary.invalid_sample_count,
        "dataset_issue_codes": issue_codes,
        "comparative_analysis_available": False,
        "detail": detail,
    }


def _load_resolved_config(config_revision_path: Path) -> ResolvedAnalysisConfig:
    payload = _read_json_object(config_revision_path)
    filtered_payload = {
        key: value
        for key, value in payload.items()
        if key not in _INTERNAL_TASK_CONFIG_FIELDS
    }
    try:
        return ResolvedAnalysisConfig.model_validate(filtered_payload)
    except Exception as error:
        raise InputProcessingError(
            f"immutable config revision is invalid: '{config_revision_path}': {error}"
        ) from error


def _load_acquisition_artifacts(*, context: StageContext) -> _AcquisitionArtifacts:
    worker_staging_root = (
        context.launch_spec.job_dir
        / "staging"
        / INPUT_ACQUISITION_STAGE_ID
        / context.launch_spec.worker_instance_id
    )
    committed_root = context.launch_spec.job_dir / "stages" / INPUT_ACQUISITION_STAGE_ID
    candidate_roots = (committed_root, worker_staging_root)
    manifest_path: Path | None = None
    selected_root: Path | None = None
    for root in candidate_roots:
        candidate_manifest = root / INPUT_MANIFEST_RELATIVE_PATH
        if candidate_manifest.is_file():
            manifest_path = candidate_manifest
            selected_root = root
            break

    if manifest_path is None or selected_root is None:
        raise InputProcessingError(
            "published input_acquisition manifest is missing for input_processing stage"
        )

    manifest = _read_json_object(manifest_path)
    if manifest.get("task_id") != context.launch_spec.task_id:
        raise InputProcessingError("input manifest task_id does not match current task")
    if manifest.get("job_id") != context.launch_spec.job_id:
        raise InputProcessingError("input manifest job_id does not match current job")
    if manifest.get("config_hash") != context.launch_spec.config_hash:
        raise InputProcessingError("input manifest config_hash does not match immutable revision")

    raw_materialized_files = manifest.get("materialized_files")
    if not isinstance(raw_materialized_files, list):
        raise InputProcessingError("input manifest must contain 'materialized_files' list")

    materialized_files: list[MaterializedInputFile] = []
    for index, raw_file in enumerate(raw_materialized_files):
        if not isinstance(raw_file, dict):
            raise InputProcessingError(
                f"input manifest item materialized_files[{index}] must be an object"
            )
        try:
            materialized = MaterializedInputFile.model_validate(raw_file)
        except Exception as error:
            raise InputProcessingError(
                f"input manifest item materialized_files[{index}] is invalid: {error}"
            ) from error
        normalized_relative_path = _normalize_manifest_relative_path(materialized.relative_path)
        materialized_files.append(
            materialized.model_copy(update={"relative_path": normalized_relative_path})
        )

    return _AcquisitionArtifacts(
        committed_root_directory=committed_root,
        worker_staging_root_directory=(
            worker_staging_root if selected_root == worker_staging_root else None
        ),
        manifest_path=manifest_path,
        materialized_files=tuple(materialized_files),
    )


def _normalize_manifest_relative_path(relative_path: str) -> str:
    normalized = relative_path.strip().replace("\\", "/")
    if normalized == "":
        raise InputProcessingError("materialized file path must not be empty")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if posix_path.is_absolute() or windows_path.is_absolute():
        raise InputProcessingError(
            f"unsafe materialized file path is absolute: '{relative_path}'"
        )
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise InputProcessingError(
            f"unsafe materialized file path escapes workdir: '{relative_path}'"
        )
    if len(posix_path.parts) < 2 or posix_path.parts[0] != "inputs":
        raise InputProcessingError(
            f"materialized file path must stay inside inputs/: '{relative_path}'"
        )
    return posix_path.as_posix()


def _validate_safe_materialized_path(*, root_directory: Path, relative_path: str) -> Path:
    normalized_relative = _normalize_manifest_relative_path(relative_path)
    candidate_path = root_directory / Path(PurePosixPath(normalized_relative))
    resolved_root = root_directory.resolve(strict=False)
    resolved_candidate = candidate_path.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as error:
        raise InputProcessingError(
            f"unsafe materialized file path resolves outside acquisition root: '{relative_path}'"
        ) from error

    current = root_directory
    for part in PurePosixPath(normalized_relative).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise InputProcessingError(
                f"unsafe materialized file path traverses symlink: '{relative_path}'"
            )
    return candidate_path


def _resolve_materialized_file_root_directory(
    *,
    acquisition: _AcquisitionArtifacts,
    relative_path: str,
) -> Path:
    committed_candidate = _validate_safe_materialized_path(
        root_directory=acquisition.committed_root_directory,
        relative_path=relative_path,
    )
    if committed_candidate.is_file():
        return acquisition.committed_root_directory

    worker_staging_root = acquisition.worker_staging_root_directory
    if worker_staging_root is not None:
        worker_staging_candidate = _validate_safe_materialized_path(
            root_directory=worker_staging_root,
            relative_path=relative_path,
        )
        if worker_staging_candidate.is_file():
            return worker_staging_root

    return acquisition.committed_root_directory


def _parse_materialized_file_with_root_fallback(
    *,
    parser: _InputRecordParserProtocol,
    acquisition: _AcquisitionArtifacts,
    preferred_root_directory: Path,
    materialized_file: MaterializedInputFile,
    alignment_mode: AnalysisAlignmentMode | str,
) -> ParsedInputFileResult:
    parsed_file = parser.parse_materialized_file(
        stage_staging_directory=preferred_root_directory,
        materialized_file=materialized_file,
        alignment_mode=alignment_mode,
    )
    worker_staging_root = acquisition.worker_staging_root_directory
    if worker_staging_root is None:
        return parsed_file
    if not _has_file_not_found_issue(parsed_file):
        return parsed_file

    retry_root_directory = (
        worker_staging_root
        if preferred_root_directory == acquisition.committed_root_directory
        else acquisition.committed_root_directory
    )
    _validate_safe_materialized_path(
        root_directory=retry_root_directory,
        relative_path=materialized_file.relative_path,
    )
    retry_result = parser.parse_materialized_file(
        stage_staging_directory=retry_root_directory,
        materialized_file=materialized_file,
        alignment_mode=alignment_mode,
    )
    if _has_file_not_found_issue(retry_result):
        return parsed_file
    return retry_result


def _has_file_not_found_issue(parsed_file: ParsedInputFileResult) -> bool:
    for issue in parsed_file.issues:
        if issue.scope != ValidationIssueScope.FILE:
            continue
        if issue.code == PARSER_ISSUE_FILE_NOT_FOUND:
            return True
    return False


def _record_issues_for_record(
    *,
    parsed_file: ParsedInputFileResult,
    parsed_record: ParsedInputRecord,
) -> tuple[InputProcessingValidationIssue, ...]:
    matched: list[InputProcessingValidationIssue] = []
    for issue in parsed_file.issues:
        if issue.scope == ValidationIssueScope.FILE:
            continue
        if issue.record_index is not None and issue.record_index == parsed_record.record_index:
            matched.append(issue)
            continue
        if (
            issue.record_index is None
            and issue.record_id is not None
            and parsed_record.record_id is not None
            and issue.record_id.strip() == parsed_record.record_id.strip()
        ):
            matched.append(issue)
    return tuple(matched)


def _write_stage_artifacts(
    *,
    context: StageContext,
    resolved_config: ResolvedAnalysisConfig,
    validation_result: InputValidationResult,
    inspection_by_sequence_id: dict[str, SequenceInspectionResult],
    input_manifest_relative_path: str,
    control_check: Callable[[], None] | None = None,
) -> InputProcessingStageResult:
    sequences_dir = context.stage_staging_directory / INPUT_PROCESSING_SEQUENCE_ARTIFACTS_DIR
    sequences_dir.mkdir(parents=True, exist_ok=True)
    sidecars_dir = context.stage_staging_directory / INPUT_PROCESSING_KMER_HITS_DIR

    write_sidecars = len(resolved_config.statistics.kmers) > 0
    if write_sidecars:
        sidecars_dir.mkdir(parents=True, exist_ok=True)

    finalized_unique_sequences: list[InputProcessingUniqueSequence] = []

    for unique_sequence in validation_result.unique_sequences:
        if control_check is not None:
            control_check()
        inspection_result = inspection_by_sequence_id.get(unique_sequence.sequence_id)
        if inspection_result is None:
            raise InputProcessingError(
                f"missing inspection result for sequence '{unique_sequence.sequence_id}'"
            )
        normalized_sequence = inspection_result.normalized_sequence
        digest = sequence_id_digest(unique_sequence.sequence_id)
        sequence_relative_path = f"{INPUT_PROCESSING_SEQUENCE_ARTIFACTS_DIR}/{digest}.fasta"
        sequence_target_path = context.stage_staging_directory / sequence_relative_path
        write_text_atomically(
            path=sequence_target_path,
            payload=_serialize_fasta(
                sequence_id=unique_sequence.sequence_id,
                sequence=normalized_sequence,
            ),
        )
        sidecar_relative_path: str | None = None
        updated_facts = unique_sequence.facts
        if write_sidecars:
            sidecar_relative_path = f"{INPUT_PROCESSING_KMER_HITS_DIR}/{digest}.json"
            sidecar_target_path = context.stage_staging_directory / sidecar_relative_path
            sidecar = KmerHitsSidecar(
                sequence_id=unique_sequence.sequence_id,
                alignment_mode=resolved_config.alignment.mode,
                query_summaries=tuple(
                    KmerQuerySummary(
                        query=summary.query,
                        definite_match_count=summary.definite_match_count,
                        possible_match_count=summary.possible_match_count,
                        strand=summary.strand,
                    )
                    for summary in unique_sequence.facts.kmer_summaries
                ),
                queries=inspection_result.kmer_hits,
            )
            write_text_atomically(
                path=sidecar_target_path,
                payload=json.dumps(
                    sidecar.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            updated_facts = updated_facts.model_copy(
                update={
                    "kmer_summaries": tuple(
                        summary.model_copy(update={"hits_path": sidecar_relative_path})
                        for summary in updated_facts.kmer_summaries
                    )
                }
            )

        finalized_unique_sequences.append(
            unique_sequence.model_copy(
                update={
                    "sequence_artifact_path": sequence_relative_path,
                    "ungapped_sequence_sha256": hashlib.sha256(
                        normalized_sequence.replace("-", "").encode("utf-8")
                    ).hexdigest(),
                    "facts": updated_facts,
                    "kmer_hits_path": sidecar_relative_path,
                }
            )
        )

    config_revision_path = context.launch_spec.config_revision_path
    if control_check is not None:
        control_check()
    try:
        config_revision_relative_path = config_revision_path.relative_to(
            context.launch_spec.task_dir
        ).as_posix()
    except ValueError as error:
        raise InputProcessingError(
            "immutable config revision must be inside task workspace directory"
        ) from error

    processing_state = InputProcessingState.COMPLETED
    dataset_issue_codes = tuple(issue.code for issue in validation_result.dataset_issues)
    dataset_valid = len(dataset_issue_codes) == 0
    manifest = InputProcessingManifest(
        task_id=context.launch_spec.task_id,
        job_id=context.launch_spec.job_id,
        config_revision_path=config_revision_relative_path,
        config_hash=context.launch_spec.config_hash,
        input_manifest_path=input_manifest_relative_path,
        generated_at=serialize_utc_datetime(utc_now()),
        processing_state=processing_state,
        processed_files=validation_result.processed_files,
        logical_samples=validation_result.logical_samples,
        unique_sequences=tuple(finalized_unique_sequences),
        dataset_issues=validation_result.dataset_issues,
        dataset_summary=validation_result.dataset_summary,
        resolved_reference=validation_result.resolved_reference,
    )
    payload = manifest.model_dump(mode="json")
    _ = InputProcessingManifest.model_validate(payload)
    manifest_path = context.stage_staging_directory / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    write_text_atomically(
        path=manifest_path,
        payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    artifacts = input_processing_artifact_paths(manifest)
    _validate_stage_artifacts_exist(
        stage_staging_directory=context.stage_staging_directory,
        artifacts=artifacts,
    )
    return InputProcessingStageResult(
        manifest_relative_path=INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
        valid_sample_count=validation_result.dataset_summary.valid_sample_count,
        comparative_analysis_available=(
            validation_result.dataset_summary.comparative_analysis_available
        ),
        dataset_valid=dataset_valid,
        dataset_issue_codes=dataset_issue_codes,
        artifacts=artifacts,
    )


def _serialize_fasta(*, sequence_id: str, sequence: str) -> str:
    lines = [f">{sequence_id}"]
    for start in range(0, len(sequence), _FASTA_LINE_WIDTH):
        lines.append(sequence[start : start + _FASTA_LINE_WIDTH])
    return "\n".join(lines) + "\n"


def _validate_stage_artifacts_exist(
    *,
    stage_staging_directory: Path,
    artifacts: tuple[str, ...],
) -> None:
    for relative_artifact in artifacts:
        artifact_path = stage_staging_directory / relative_artifact
        if not artifact_path.is_file():
            raise InputProcessingError(f"referenced artifact is missing: '{relative_artifact}'")


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InputProcessingError(f"cannot read JSON object from '{path}': {error}") from error
    if not isinstance(loaded, dict):
        raise InputProcessingError(f"JSON document '{path}' must be an object")
    return {str(key): value for key, value in loaded.items()}
