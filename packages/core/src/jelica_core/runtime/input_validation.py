from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import NAMESPACE_URL, uuid5

from jelica_contracts import JSONObject, JSONValue
from jelica_core.config import AnalysisAlignmentMode
from jelica_core.sample_selection import (
    SampleSelectorCandidate,
    SampleSelectorResolutionError,
    SampleSelectorResolutionMethod,
    SampleSelectorResolutionReason,
    SampleSelectorResolver,
)

from .input_parsers import (
    PARSER_ISSUE_FASTA_MALFORMED,
    PARSER_ISSUE_FILE_NOT_FOUND,
    PARSER_ISSUE_FILE_UNREADABLE,
    PARSER_ISSUE_FORMAT_NOT_ALLOWED_FOR_ALIGNMENT_MODE,
    PARSER_ISSUE_GAP_NOT_ALLOWED_FOR_ALIGNMENT_MODE,
    PARSER_ISSUE_GENBANK_MALFORMED,
    PARSER_ISSUE_RECORD_DUPLICATE_ID,
    PARSER_ISSUE_RECORD_ID_MISSING,
    PARSER_ISSUE_RECORD_SEQUENCE_EMPTY,
    PARSER_ISSUE_SEQUENCE_ABSENT,
    PARSER_ISSUE_UNSUPPORTED_FORMAT,
    ParsedInputFileResult,
)
from .input_processing_models import (
    INPUT_PROCESSING_SEQUENCE_ARTIFACTS_DIR,
    InputProcessingAlignmentSummary,
    InputProcessingDatasetSummary,
    InputProcessingFileStatus,
    InputProcessingLogicalSample,
    InputProcessingProcessedFile,
    InputProcessingResolvedReference,
    InputProcessingUniqueSequence,
    InputProcessingValidationIssue,
    LogicalSampleProvenance,
    ParsedInputRecord,
    ReferenceResolutionMethod,
    SampleValidationStatus,
    SequenceFacts,
    ValidationIssueScope,
    ValidationIssueSeverity,
    sequence_id_digest,
)
from .sequence_inspector import SequenceInspectionResult

VALIDATION_ISSUE_MALFORMED_INPUT_FILE: Final = "malformed_input_file"
VALIDATION_ISSUE_UNSUPPORTED_FORMAT_FOR_ALIGNMENT_MODE: Final = (
    "unsupported_format_for_alignment_mode"
)
VALIDATION_ISSUE_MISSING_RECORD_ID: Final = "missing_record_id"
VALIDATION_ISSUE_DUPLICATE_RECORD_ID_IN_FILE: Final = "duplicate_record_id_in_file"
VALIDATION_ISSUE_EMPTY_SEQUENCE: Final = "empty_sequence"
VALIDATION_ISSUE_MISSING_SEQUENCE: Final = "missing_sequence"
VALIDATION_ISSUE_GAP_NOT_ALLOWED: Final = "gap_not_allowed"
VALIDATION_ISSUE_INVALID_NUCLEOTIDE_SYMBOL: Final = "invalid_nucleotide_symbol"

VALIDATION_ISSUE_NO_VALID_SAMPLES: Final = "no_valid_samples"
VALIDATION_ISSUE_PREALIGNED_LENGTH_MISMATCH: Final = "prealigned_length_mismatch"
VALIDATION_ISSUE_REFERENCE_REQUIRED: Final = "reference_required"
VALIDATION_ISSUE_REFERENCE_NOT_FOUND: Final = "reference_not_found"
VALIDATION_ISSUE_REFERENCE_AMBIGUOUS: Final = "reference_ambiguous"
VALIDATION_ISSUE_REFERENCE_INVALID: Final = "reference_invalid"

_PARSER_ISSUE_CODE_MAPPING: Final[dict[str, str]] = {
    PARSER_ISSUE_FILE_NOT_FOUND: VALIDATION_ISSUE_MALFORMED_INPUT_FILE,
    PARSER_ISSUE_FILE_UNREADABLE: VALIDATION_ISSUE_MALFORMED_INPUT_FILE,
    PARSER_ISSUE_UNSUPPORTED_FORMAT: VALIDATION_ISSUE_MALFORMED_INPUT_FILE,
    PARSER_ISSUE_FASTA_MALFORMED: VALIDATION_ISSUE_MALFORMED_INPUT_FILE,
    PARSER_ISSUE_GENBANK_MALFORMED: VALIDATION_ISSUE_MALFORMED_INPUT_FILE,
    PARSER_ISSUE_FORMAT_NOT_ALLOWED_FOR_ALIGNMENT_MODE: (
        VALIDATION_ISSUE_UNSUPPORTED_FORMAT_FOR_ALIGNMENT_MODE
    ),
    PARSER_ISSUE_RECORD_ID_MISSING: VALIDATION_ISSUE_MISSING_RECORD_ID,
    PARSER_ISSUE_RECORD_DUPLICATE_ID: VALIDATION_ISSUE_DUPLICATE_RECORD_ID_IN_FILE,
    PARSER_ISSUE_RECORD_SEQUENCE_EMPTY: VALIDATION_ISSUE_EMPTY_SEQUENCE,
    PARSER_ISSUE_SEQUENCE_ABSENT: VALIDATION_ISSUE_MISSING_SEQUENCE,
    PARSER_ISSUE_GAP_NOT_ALLOWED_FOR_ALIGNMENT_MODE: VALIDATION_ISSUE_GAP_NOT_ALLOWED,
}

_SAMPLE_ID_NAMESPACE: Final = uuid5(NAMESPACE_URL, "jelica:input_processing:sample")


@dataclass(frozen=True, slots=True)
class ValidatedRecord:
    parsed_record: ParsedInputRecord
    logical_sample: InputProcessingLogicalSample
    facts: SequenceFacts
    inspection_result: SequenceInspectionResult

    @property
    def is_valid(self) -> bool:
        return self.logical_sample.eligible_for_analysis


@dataclass(frozen=True, slots=True)
class InputValidationResult:
    processed_files: tuple[InputProcessingProcessedFile, ...]
    logical_samples: tuple[InputProcessingLogicalSample, ...]
    unique_sequences: tuple[InputProcessingUniqueSequence, ...]
    dataset_issues: tuple[InputProcessingValidationIssue, ...]
    dataset_summary: InputProcessingDatasetSummary
    resolved_reference: InputProcessingResolvedReference | None


@dataclass(slots=True)
class _UniqueSequenceAccumulator:
    facts: SequenceFacts
    sample_ids: list[str]


@dataclass(slots=True)
class RecordValidator:
    def validate(
        self,
        *,
        parsed_record: ParsedInputRecord,
        parser_issues: tuple[InputProcessingValidationIssue, ...],
        inspection_result: SequenceInspectionResult,
    ) -> ValidatedRecord:
        record_issues = [_canonicalize_issue(issue) for issue in parser_issues]
        facts = inspection_result.facts
        if facts.invalid_symbol_count > 0:
            invalid_symbol_counts_context: dict[str, JSONValue] = {
                symbol: count for symbol, count in facts.invalid_symbol_counts.items()
            }
            invalid_positions_context: list[JSONValue] = [
                position for position in facts.invalid_positions
            ]
            issue_context: JSONObject = {
                "invalid_symbol_count": facts.invalid_symbol_count,
                "invalid_symbol_counts": invalid_symbol_counts_context,
                "invalid_positions": invalid_positions_context,
                "invalid_positions_truncated": facts.invalid_positions_truncated,
            }
            record_issues.append(
                InputProcessingValidationIssue(
                    code=VALIDATION_ISSUE_INVALID_NUCLEOTIDE_SYMBOL,
                    message="Sequence contains invalid nucleotide symbols.",
                    severity=ValidationIssueSeverity.ERROR,
                    scope=ValidationIssueScope.RECORD,
                    path=str(parsed_record.materialized_relative_path),
                    record_id=_optional_text(parsed_record.record_id),
                    record_index=parsed_record.record_index,
                    context=issue_context,
                )
            )

        normalized_issues = _deduplicate_issues(record_issues)
        sample_id = _build_sample_id(parsed_record=parsed_record)
        has_error = any(
            issue.severity == ValidationIssueSeverity.ERROR
            for issue in normalized_issues
        )
        validation_status = (
            SampleValidationStatus.VALID
            if not has_error
            else SampleValidationStatus.INVALID
        )
        logical_sample = InputProcessingLogicalSample(
            sample_id=sample_id,
            provenance=LogicalSampleProvenance(
                input_manifest_source_reference=str(parsed_record.input_manifest_source_reference),
                materialized_relative_path=str(parsed_record.materialized_relative_path),
                record_index=parsed_record.record_index,
                format_hint=str(parsed_record.format_hint),
            ),
            original_record_id=_optional_text(parsed_record.record_id),
            original_description=_optional_text(parsed_record.description),
            validation_status=validation_status,
            validation_issues=tuple(normalized_issues),
            sequence_id=facts.sequence_id,
            inspection_facts=facts if has_error else None,
            eligible_for_analysis=not has_error,
        )
        return ValidatedRecord(
            parsed_record=parsed_record,
            logical_sample=logical_sample,
            facts=facts,
            inspection_result=inspection_result,
        )


@dataclass(slots=True)
class DatasetValidator:
    def build_result(
        self,
        *,
        parsed_files: tuple[ParsedInputFileResult, ...],
        validated_records: tuple[ValidatedRecord, ...],
        alignment_mode: AnalysisAlignmentMode | str,
        reference_selector: str | None,
    ) -> InputValidationResult:
        resolved_alignment_mode = _resolve_alignment_mode(alignment_mode)
        logical_samples = tuple(item.logical_sample for item in validated_records)
        valid_records = [item for item in validated_records if item.is_valid]
        valid_sample_count = len(valid_records)
        invalid_sample_count = len(validated_records) - valid_sample_count

        unique_sequences = _build_unique_sequences(valid_records=tuple(valid_records))
        unique_sequence_count = len(unique_sequences)
        duplicate_logical_sample_count = max(valid_sample_count - unique_sequence_count, 0)

        dataset_issues: list[InputProcessingValidationIssue] = []
        comparative_available = valid_sample_count >= 2
        alignment_length: int | None = None
        if valid_sample_count == 0:
            dataset_issues.append(
                _dataset_issue(
                    code=VALIDATION_ISSUE_NO_VALID_SAMPLES,
                    message="No valid logical samples were produced from parsed records.",
                )
            )
            comparative_available = False

        if resolved_alignment_mode == AnalysisAlignmentMode.PREALIGNED and valid_sample_count >= 2:
            aligned_lengths = {item.facts.source_length for item in valid_records}
            if len(aligned_lengths) == 1:
                alignment_length = next(iter(aligned_lengths))
            else:
                comparative_available = False
                aligned_lengths_context: list[JSONValue] = [
                    length for length in sorted(aligned_lengths)
                ]
                dataset_issues.append(
                    _dataset_issue(
                        code=VALIDATION_ISSUE_PREALIGNED_LENGTH_MISMATCH,
                        message=(
                            "Valid prealigned samples must have identical aligned/source length."
                        ),
                        context={"aligned_lengths": aligned_lengths_context},
                    )
                )
        elif (
            valid_sample_count >= 1
            and resolved_alignment_mode == AnalysisAlignmentMode.PREALIGNED
        ):
            alignment_length = valid_records[0].facts.source_length

        resolved_reference: InputProcessingResolvedReference | None = None
        reference_dependent_available = False
        if reference_selector is not None:
            resolved_reference, reference_issue = _resolve_reference_selector(
                selector=reference_selector,
                logical_samples=logical_samples,
            )
            if reference_issue is not None:
                dataset_issues.append(reference_issue)
            elif resolved_reference is not None:
                reference_dependent_available = comparative_available
            if (
                resolved_alignment_mode == AnalysisAlignmentMode.PREALIGNED
                and valid_sample_count >= 2
                and resolved_reference is None
            ):
                comparative_available = False
        elif (
            resolved_alignment_mode == AnalysisAlignmentMode.PREALIGNED
            and valid_sample_count >= 2
        ):
            comparative_available = False
            dataset_issues.append(
                _dataset_issue(
                    code=VALIDATION_ISSUE_REFERENCE_REQUIRED,
                    message=(
                        "Reference selector is required for prealigned datasets "
                        "with 2+ valid samples."
                    ),
                )
            )

        if not comparative_available:
            reference_dependent_available = False

        processed_files = _build_processed_file_summaries(
            parsed_files=parsed_files,
            logical_samples=logical_samples,
        )
        dataset_summary = InputProcessingDatasetSummary(
            discovered_record_count=len(validated_records),
            valid_sample_count=valid_sample_count,
            invalid_sample_count=invalid_sample_count,
            unique_sequence_count=unique_sequence_count,
            duplicate_logical_sample_count=duplicate_logical_sample_count,
            comparative_analysis_available=comparative_available,
            reference_dependent_analysis_available=reference_dependent_available,
            alignment_summary=InputProcessingAlignmentSummary(
                mode=resolved_alignment_mode,
                aligned_sample_count=valid_sample_count,
                alignment_length=alignment_length,
            ),
        )
        return InputValidationResult(
            processed_files=processed_files,
            logical_samples=logical_samples,
            unique_sequences=unique_sequences,
            dataset_issues=tuple(_deduplicate_issues(dataset_issues)),
            dataset_summary=dataset_summary,
            resolved_reference=resolved_reference,
        )


def _build_processed_file_summaries(
    *,
    parsed_files: tuple[ParsedInputFileResult, ...],
    logical_samples: tuple[InputProcessingLogicalSample, ...],
) -> tuple[InputProcessingProcessedFile, ...]:
    samples_by_path: dict[str, list[InputProcessingLogicalSample]] = {}
    for sample in logical_samples:
        path = sample.provenance.materialized_relative_path
        samples_by_path.setdefault(path, []).append(sample)

    processed_files: list[InputProcessingProcessedFile] = []
    for parsed_file in parsed_files:
        relative_path = parsed_file.materialized_file.relative_path
        file_samples = samples_by_path.get(relative_path, [])
        valid_count = sum(1 for sample in file_samples if sample.eligible_for_analysis)
        invalid_count = len(file_samples) - valid_count
        file_issues: list[InputProcessingValidationIssue] = [
            _canonicalize_issue(issue) for issue in parsed_file.issues
        ]
        for sample in file_samples:
            file_issues.extend(sample.validation_issues)
        normalized_issues = tuple(_deduplicate_issues(file_issues))

        status = InputProcessingFileStatus.PROCESSED
        if len(parsed_file.records) == 0 and len(normalized_issues) > 0:
            status = InputProcessingFileStatus.FAILED
        elif len(parsed_file.records) == 0 and len(normalized_issues) == 0:
            status = InputProcessingFileStatus.SKIPPED

        processed_files.append(
            InputProcessingProcessedFile(
                input_manifest_source_reference=parsed_file.materialized_file.source_reference,
                relative_path=relative_path,
                format_hint=parsed_file.materialized_file.format_hint,
                status=status,
                record_count=len(parsed_file.records),
                valid_sample_count=valid_count,
                invalid_sample_count=invalid_count,
                validation_issues=normalized_issues,
            )
        )
    return tuple(processed_files)


def _build_unique_sequences(
    *,
    valid_records: tuple[ValidatedRecord, ...],
) -> tuple[InputProcessingUniqueSequence, ...]:
    grouped: dict[str, _UniqueSequenceAccumulator] = {}
    for record in valid_records:
        sequence_id = record.facts.sequence_id
        if sequence_id is None:
            continue
        if sequence_id not in grouped:
            grouped[sequence_id] = _UniqueSequenceAccumulator(
                facts=record.facts,
                sample_ids=[],
            )
        grouped[sequence_id].sample_ids.append(record.logical_sample.sample_id)

    unique_entries: list[InputProcessingUniqueSequence] = []
    for sequence_id, grouped_entry in grouped.items():
        unique_entries.append(
            InputProcessingUniqueSequence(
                sequence_id=sequence_id,
                sequence_artifact_path=(
                    f"{INPUT_PROCESSING_SEQUENCE_ARTIFACTS_DIR}/"
                    f"{sequence_id_digest(sequence_id)}.fasta"
                ),
                facts=grouped_entry.facts,
                logical_sample_ids=tuple(grouped_entry.sample_ids),
            )
        )
    return tuple(unique_entries)


def _resolve_reference_selector(
    *,
    selector: str,
    logical_samples: tuple[InputProcessingLogicalSample, ...],
) -> tuple[InputProcessingResolvedReference | None, InputProcessingValidationIssue | None]:
    resolver = SampleSelectorResolver(
        tuple(
            SampleSelectorCandidate(
                sample_id=sample.sample_id,
                sequence_id=sample.sequence_id,
                record_id=sample.original_record_id,
                source_reference=sample.provenance.input_manifest_source_reference,
                materialized_relative_path=sample.provenance.materialized_relative_path,
                eligible_for_analysis=sample.eligible_for_analysis,
                input_order=input_order,
            )
            for input_order, sample in enumerate(logical_samples)
        )
    )
    try:
        resolved = resolver.resolve(selector)
    except SampleSelectorResolutionError as error:
        code = VALIDATION_ISSUE_REFERENCE_INVALID
        if error.reason is SampleSelectorResolutionReason.NOT_FOUND:
            code = VALIDATION_ISSUE_REFERENCE_NOT_FOUND
        elif error.reason is SampleSelectorResolutionReason.AMBIGUOUS:
            code = VALIDATION_ISSUE_REFERENCE_AMBIGUOUS
        context: JSONObject = {}
        if error.selector is not None:
            context["selector"] = error.selector
        if error.matched_sample_ids:
            context["matched_sample_ids"] = list(error.matched_sample_ids)
        return None, _dataset_issue(code=code, message=error.detail, context=context)

    resolution_method = ReferenceResolutionMethod.RECORD_ID
    if (
        resolved.resolution_method
        is SampleSelectorResolutionMethod.FILE_PATH_AND_RECORD_ID
    ):
        resolution_method = ReferenceResolutionMethod.FILE_PATH_AND_RECORD_ID
    return InputProcessingResolvedReference(
        selector=resolved.original_selector,
        sample_id=resolved.sample_id,
        sequence_id=resolved.sequence_id,
        source_relative_path=resolved.materialized_relative_path,
        record_id=resolved.record_id,
        resolution_method=resolution_method,
    ), None


def _dataset_issue(
    *,
    code: str,
    message: str,
    context: JSONObject | None = None,
) -> InputProcessingValidationIssue:
    return InputProcessingValidationIssue(
        code=code,
        message=message,
        severity=ValidationIssueSeverity.ERROR,
        scope=ValidationIssueScope.DATASET,
        context=context or {},
    )


def _canonicalize_issue(issue: InputProcessingValidationIssue) -> InputProcessingValidationIssue:
    normalized_code = _PARSER_ISSUE_CODE_MAPPING.get(issue.code, issue.code)
    if normalized_code == issue.code:
        return issue
    return InputProcessingValidationIssue(
        code=normalized_code,
        message=issue.message,
        severity=issue.severity,
        scope=issue.scope,
        path=issue.path,
        record_id=issue.record_id,
        record_index=issue.record_index,
        sample_id=issue.sample_id,
        context=issue.context,
    )


def _deduplicate_issues(
    issues: list[InputProcessingValidationIssue],
) -> list[InputProcessingValidationIssue]:
    deduplicated: list[InputProcessingValidationIssue] = []
    seen: set[tuple[object, ...]] = set()
    for issue in issues:
        key = (
            issue.code,
            issue.scope.value,
            issue.path,
            issue.record_id,
            issue.record_index,
            issue.sample_id,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(issue)
    return deduplicated


def _build_sample_id(*, parsed_record: ParsedInputRecord) -> str:
    record_id = _optional_text(parsed_record.record_id) or ""
    key = "||".join(
        [
            str(parsed_record.input_manifest_source_reference),
            str(parsed_record.materialized_relative_path),
            str(parsed_record.format_hint),
            str(parsed_record.record_index),
            record_id,
        ]
    )
    return f"sample_{uuid5(_SAMPLE_ID_NAMESPACE, key).hex}"


def _resolve_alignment_mode(alignment_mode: AnalysisAlignmentMode | str) -> AnalysisAlignmentMode:
    if isinstance(alignment_mode, AnalysisAlignmentMode):
        return alignment_mode
    return AnalysisAlignmentMode(alignment_mode.strip().lower())


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    return normalized
