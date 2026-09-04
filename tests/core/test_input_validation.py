from __future__ import annotations

from collections import defaultdict

import pytest

from jelica_core.config import AnalysisAlignmentMode, ResolvedAnalysisStatisticsConfig
from jelica_core.runtime.input_parsers import (
    PARSER_ISSUE_RECORD_SEQUENCE_EMPTY,
    MaterializedInputFile,
    ParsedInputFileResult,
)
from jelica_core.runtime.input_processing_models import (
    InputProcessingResolvedReference,
    InputProcessingValidationIssue,
    ParsedInputRecord,
    ValidationIssueScope,
    ValidationIssueSeverity,
)
from jelica_core.runtime.input_validation import (
    VALIDATION_ISSUE_EMPTY_SEQUENCE,
    VALIDATION_ISSUE_INVALID_NUCLEOTIDE_SYMBOL,
    VALIDATION_ISSUE_NO_VALID_SAMPLES,
    VALIDATION_ISSUE_PREALIGNED_LENGTH_MISMATCH,
    VALIDATION_ISSUE_REFERENCE_AMBIGUOUS,
    VALIDATION_ISSUE_REFERENCE_INVALID,
    VALIDATION_ISSUE_REFERENCE_NOT_FOUND,
    VALIDATION_ISSUE_REFERENCE_REQUIRED,
    DatasetValidator,
    InputValidationResult,
    RecordValidator,
    ValidatedRecord,
)
from jelica_core.runtime.sequence_inspector import SequenceInspectionResult, SequenceInspector


def _parsed_record(
    sequence: str,
    *,
    source_reference: str = "data/a.fasta",
    materialized_relative_path: str = "inputs/files/a.fasta",
    format_hint: str = ".fasta",
    record_index: int = 0,
    record_id: str | None = "record-1",
    description: str | None = None,
) -> ParsedInputRecord:
    return ParsedInputRecord(
        input_manifest_source_reference=source_reference,
        input_manifest_relative_path="inputs/input_manifest.json",
        materialized_relative_path=materialized_relative_path,
        format_hint=format_hint,
        record_index=record_index,
        record_id=record_id,
        description=description,
        metadata={},
        raw_sequence=sequence,
    )


def _statistics() -> ResolvedAnalysisStatisticsConfig:
    return ResolvedAnalysisStatisticsConfig()


def _inspect(
    parsed_record: ParsedInputRecord,
    *,
    alignment_mode: AnalysisAlignmentMode = AnalysisAlignmentMode.NONE,
) -> SequenceInspectionResult:
    return SequenceInspector().inspect(
        parsed_record,
        statistics_config=_statistics(),
        alignment_mode=alignment_mode,
    )


def _parser_issue(
    *,
    code: str,
    scope: ValidationIssueScope = ValidationIssueScope.RECORD,
    path: str = "inputs/files/a.fasta",
    record_id: str | None = None,
    record_index: int | None = None,
) -> InputProcessingValidationIssue:
    return InputProcessingValidationIssue(
        code=code,
        message=code,
        severity=ValidationIssueSeverity.ERROR,
        scope=scope,
        path=path,
        record_id=record_id,
        record_index=record_index,
        context={},
    )


def _validated_record(
    parsed_record: ParsedInputRecord,
    *,
    parser_issues: tuple[InputProcessingValidationIssue, ...] = (),
    alignment_mode: AnalysisAlignmentMode = AnalysisAlignmentMode.NONE,
) -> ValidatedRecord:
    return RecordValidator().validate(
        parsed_record=parsed_record,
        parser_issues=parser_issues,
        inspection_result=_inspect(parsed_record, alignment_mode=alignment_mode),
    )


def _parsed_file(
    *,
    source_reference: str,
    materialized_relative_path: str,
    format_hint: str,
    records: tuple[ParsedInputRecord, ...],
    issues: tuple[InputProcessingValidationIssue, ...] = (),
) -> ParsedInputFileResult:
    return ParsedInputFileResult(
        input_manifest_relative_path="inputs/input_manifest.json",
        materialized_file=MaterializedInputFile(
            relative_path=materialized_relative_path,
            source_type="local_file",
            source_reference=source_reference,
            format_hint=format_hint,
            size_bytes=100,
            sha256="0" * 64,
        ),
        records=records,
        issues=issues,
    )


def _dataset_result(
    validated_records: tuple[ValidatedRecord, ...],
    *,
    alignment_mode: AnalysisAlignmentMode = AnalysisAlignmentMode.NONE,
    reference_selector: str | None = None,
    parsed_files: tuple[ParsedInputFileResult, ...] | None = None,
) -> InputValidationResult:
    if parsed_files is None:
        grouped: dict[tuple[str, str, str], list[ParsedInputRecord]] = defaultdict(list)
        for validated in validated_records:
            key = (
                validated.parsed_record.input_manifest_source_reference,
                validated.parsed_record.materialized_relative_path,
                validated.parsed_record.format_hint,
            )
            grouped[key].append(validated.parsed_record)
        parsed_files = tuple(
            _parsed_file(
                source_reference=key[0],
                materialized_relative_path=key[1],
                format_hint=key[2],
                records=tuple(records),
            )
            for key, records in grouped.items()
        )
    return DatasetValidator().build_result(
        parsed_files=parsed_files,
        validated_records=validated_records,
        alignment_mode=alignment_mode,
        reference_selector=reference_selector,
    )


def _codes(issues: tuple[InputProcessingValidationIssue, ...]) -> set[str]:
    return {issue.code for issue in issues}


def test_record_validator_accepts_u_and_iupac_symbols() -> None:
    validated = _validated_record(_parsed_record("ACGTURYSWKMBDHVN"))

    assert validated.is_valid is True
    assert validated.logical_sample.eligible_for_analysis is True
    assert validated.logical_sample.validation_issues == ()


def test_record_validator_marks_invalid_symbol_from_sequence_facts() -> None:
    validated = _validated_record(_parsed_record("ACXGTX"))

    assert validated.is_valid is False
    assert validated.logical_sample.eligible_for_analysis is False
    invalid_issue = next(
        issue
        for issue in validated.logical_sample.validation_issues
        if issue.code == VALIDATION_ISSUE_INVALID_NUCLEOTIDE_SYMBOL
    )
    assert invalid_issue.context["invalid_symbol_count"] == 2
    assert invalid_issue.context["invalid_symbol_counts"] == {"X": 2}
    assert invalid_issue.context["invalid_positions"] == [2, 5]


def test_record_validator_maps_parser_issue_and_does_not_invalidate_neighbor_record() -> None:
    invalid_record = _parsed_record("ACGT", record_id="bad", record_index=0)
    valid_record = _parsed_record("ACGU", record_id="good", record_index=1)

    invalid_validated = _validated_record(
        invalid_record,
        parser_issues=(
            _parser_issue(
                code=PARSER_ISSUE_RECORD_SEQUENCE_EMPTY,
                record_id="bad",
                record_index=0,
            ),
        ),
    )
    valid_validated = _validated_record(valid_record)
    result = _dataset_result((invalid_validated, valid_validated))

    assert invalid_validated.logical_sample.eligible_for_analysis is False
    assert valid_validated.logical_sample.eligible_for_analysis is True
    assert VALIDATION_ISSUE_EMPTY_SEQUENCE in _codes(
        invalid_validated.logical_sample.validation_issues
    )
    assert result.dataset_summary.valid_sample_count == 1
    assert result.dataset_summary.invalid_sample_count == 1
    assert VALIDATION_ISSUE_NO_VALID_SAMPLES not in _codes(result.dataset_issues)


def test_record_validator_does_not_reinspect_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed_record = _parsed_record("ACGT")
    inspection_result = _inspect(parsed_record)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("SequenceInspector.inspect must not be called by RecordValidator")

    monkeypatch.setattr(SequenceInspector, "inspect", _boom)

    validated = RecordValidator().validate(
        parsed_record=parsed_record,
        parser_issues=(),
        inspection_result=inspection_result,
    )
    assert validated.is_valid is True


def test_sample_id_is_stable_and_distinguishes_record_indexes() -> None:
    base = _parsed_record("ACGT", record_id="same", record_index=0)
    same_again = _parsed_record("ACGT", record_id="same", record_index=0)
    other_index = _parsed_record("ACGT", record_id="same", record_index=1)
    same_sequence_different_file = _parsed_record(
        "ACGT",
        source_reference="data/other.fasta",
        materialized_relative_path="inputs/files/other.fasta",
        record_id="same",
        record_index=0,
    )

    base_validated = _validated_record(base)
    same_again_validated = _validated_record(same_again)
    other_index_validated = _validated_record(other_index)
    other_file_validated = _validated_record(same_sequence_different_file)

    assert base_validated.logical_sample.sample_id == same_again_validated.logical_sample.sample_id
    assert base_validated.logical_sample.sample_id != other_index_validated.logical_sample.sample_id
    assert base_validated.logical_sample.sample_id != other_file_validated.logical_sample.sample_id


def test_invalid_logical_sample_is_preserved_with_sequence_id() -> None:
    validated = _validated_record(_parsed_record("AXGT"))

    assert validated.logical_sample.validation_status.value == "invalid"
    assert validated.logical_sample.sequence_id == validated.facts.sequence_id
    assert validated.logical_sample.sequence_id is not None


def test_dataset_deduplicates_valid_sequences_but_preserves_logical_samples() -> None:
    first = _validated_record(_parsed_record("ACGT", record_id="rec-1", record_index=0))
    second = _validated_record(_parsed_record("ACGT", record_id="rec-2", record_index=1))
    result = _dataset_result((first, second), alignment_mode=AnalysisAlignmentMode.COMPUTE)

    assert len(result.logical_samples) == 2
    assert len(result.unique_sequences) == 1
    assert result.dataset_summary.valid_sample_count == 2
    assert result.dataset_summary.unique_sequence_count == 1
    assert result.dataset_summary.duplicate_logical_sample_count == 1
    assert set(result.unique_sequences[0].logical_sample_ids) == {
        first.logical_sample.sample_id,
        second.logical_sample.sample_id,
    }
    assert result.dataset_issues == ()


def test_dataset_does_not_merge_auug_and_attg_sequences() -> None:
    auug = _validated_record(_parsed_record("AUUG", record_id="auug"))
    attg = _validated_record(_parsed_record("ATTG", record_id="attg", record_index=1))
    result = _dataset_result((auug, attg))

    assert result.dataset_summary.valid_sample_count == 2
    assert result.dataset_summary.unique_sequence_count == 2
    assert result.dataset_summary.duplicate_logical_sample_count == 0


def test_deduplication_ignores_record_id_collisions_between_files() -> None:
    first = _validated_record(
        _parsed_record(
            "ACGT",
            source_reference="data/a.fasta",
            materialized_relative_path="inputs/files/a.fasta",
            record_id="dup",
        )
    )
    second = _validated_record(
        _parsed_record(
            "TTTT",
            source_reference="data/b.fasta",
            materialized_relative_path="inputs/files/b.fasta",
            record_id="dup",
            record_index=0,
        )
    )
    result = _dataset_result((first, second))

    assert result.dataset_summary.valid_sample_count == 2
    assert result.dataset_summary.unique_sequence_count == 2


def test_dataset_no_valid_samples_sets_dataset_error() -> None:
    invalid = _validated_record(_parsed_record("ACXG"))
    result = _dataset_result((invalid,))

    assert result.dataset_summary.valid_sample_count == 0
    assert result.dataset_summary.comparative_analysis_available is False
    assert VALIDATION_ISSUE_NO_VALID_SAMPLES in _codes(result.dataset_issues)


def test_dataset_single_valid_sample_is_allowed_without_dataset_error() -> None:
    valid = _validated_record(_parsed_record("ACGT"))
    result = _dataset_result((valid,))

    assert result.dataset_summary.valid_sample_count == 1
    assert result.dataset_summary.comparative_analysis_available is False
    assert result.dataset_issues == ()


def test_prealigned_mismatch_is_dataset_error_and_keeps_record_validity() -> None:
    first = _validated_record(
        _parsed_record(
            "A--CG",
            source_reference="data/a.afa",
            materialized_relative_path="inputs/files/a.afa",
            format_hint=".afa",
            record_id="a",
        ),
        alignment_mode=AnalysisAlignmentMode.PREALIGNED,
    )
    second = _validated_record(
        _parsed_record(
            "ACGT",
            source_reference="data/b.afa",
            materialized_relative_path="inputs/files/b.afa",
            format_hint=".afa",
            record_id="b",
        ),
        alignment_mode=AnalysisAlignmentMode.PREALIGNED,
    )
    result = _dataset_result((first, second), alignment_mode=AnalysisAlignmentMode.PREALIGNED)

    assert first.logical_sample.eligible_for_analysis is True
    assert second.logical_sample.eligible_for_analysis is True
    assert VALIDATION_ISSUE_PREALIGNED_LENGTH_MISMATCH in _codes(result.dataset_issues)
    assert result.dataset_summary.comparative_analysis_available is False


def test_prealigned_requires_reference_for_two_or_more_valid_samples() -> None:
    first = _validated_record(
        _parsed_record("ACGT", format_hint=".afa", record_id="a"),
        alignment_mode=AnalysisAlignmentMode.PREALIGNED,
    )
    second = _validated_record(
        _parsed_record("TGCA", format_hint=".afa", record_id="b", record_index=1),
        alignment_mode=AnalysisAlignmentMode.PREALIGNED,
    )
    result = _dataset_result((first, second), alignment_mode=AnalysisAlignmentMode.PREALIGNED)

    assert VALIDATION_ISSUE_REFERENCE_REQUIRED in _codes(result.dataset_issues)
    assert result.dataset_summary.comparative_analysis_available is False


def test_prealigned_single_valid_sample_does_not_require_reference() -> None:
    only = _validated_record(
        _parsed_record("ACGT", format_hint=".afa", record_id="only"),
        alignment_mode=AnalysisAlignmentMode.PREALIGNED,
    )
    result = _dataset_result((only,), alignment_mode=AnalysisAlignmentMode.PREALIGNED)

    assert VALIDATION_ISSUE_REFERENCE_REQUIRED not in _codes(result.dataset_issues)
    assert result.dataset_summary.comparative_analysis_available is False


def test_unqualified_reference_resolves_single_valid_record() -> None:
    first = _validated_record(_parsed_record("ACGT", record_id="r1", record_index=0))
    second = _validated_record(_parsed_record("TGCA", record_id="r2", record_index=1))
    result = _dataset_result((first, second), reference_selector="r2")

    assert result.resolved_reference is not None
    assert result.resolved_reference.sample_id == second.logical_sample.sample_id
    assert result.resolved_reference.sequence_id == second.logical_sample.sequence_id
    assert result.dataset_summary.reference_dependent_analysis_available is True


def test_unqualified_reference_reports_ambiguous_not_found_and_invalid() -> None:
    valid_one = _validated_record(
        _parsed_record(
            "ACGT",
            source_reference="data/a.fasta",
            materialized_relative_path="inputs/files/a.fasta",
            record_id="dup",
        )
    )
    valid_two = _validated_record(
        _parsed_record(
            "TGCA",
            source_reference="data/b.fasta",
            materialized_relative_path="inputs/files/b.fasta",
            record_id="dup",
            record_index=0,
        )
    )
    invalid = _validated_record(_parsed_record("AXGT", record_id="invalid-only", record_index=1))

    ambiguous = _dataset_result((valid_one, valid_two), reference_selector="dup")
    not_found = _dataset_result((valid_one, valid_two), reference_selector="missing")
    invalid_only = _dataset_result((valid_one, invalid), reference_selector="invalid-only")

    assert VALIDATION_ISSUE_REFERENCE_AMBIGUOUS in _codes(ambiguous.dataset_issues)
    assert VALIDATION_ISSUE_REFERENCE_NOT_FOUND in _codes(not_found.dataset_issues)
    assert VALIDATION_ISSUE_REFERENCE_INVALID in _codes(invalid_only.dataset_issues)


def test_unqualified_reference_cannot_target_txt_record_without_record_id() -> None:
    txt_record = _validated_record(
        _parsed_record(
            "ACGT",
            source_reference="data/plain.txt",
            materialized_relative_path="inputs/files/plain.txt",
            format_hint=".txt",
            record_id=None,
        )
    )
    result = _dataset_result((txt_record,), reference_selector="plain")

    assert VALIDATION_ISSUE_REFERENCE_NOT_FOUND in _codes(result.dataset_issues)


def test_qualified_reference_resolves_and_disambiguates_same_record_ids() -> None:
    first = _validated_record(
        _parsed_record(
            "ACGT",
            source_reference="data/a.fasta",
            materialized_relative_path="inputs/files/a.fasta",
            record_id="same",
        )
    )
    second = _validated_record(
        _parsed_record(
            "TGCA",
            source_reference="data/b.fasta",
            materialized_relative_path="inputs/files/b.fasta",
            record_id="same",
            record_index=0,
        )
    )
    result = _dataset_result((first, second), reference_selector="data/b.fasta::same")

    assert result.resolved_reference is not None
    assert result.resolved_reference.sample_id == second.logical_sample.sample_id
    assert result.resolved_reference.record_id == "same"


@pytest.mark.parametrize(
    "selector",
    (
        "outside/sample.fasta::rec-1",
        "sample.fasta::rec-1",
    ),
)
def test_qualified_reference_rejects_external_and_basename_only_selector(selector: str) -> None:
    sample = _validated_record(
        _parsed_record(
            "ACGT",
            source_reference="data/dir/sample.fasta",
            materialized_relative_path="inputs/files/sample.fasta",
            record_id="rec-1",
        )
    )
    result = _dataset_result((sample,), reference_selector=selector)

    assert VALIDATION_ISSUE_REFERENCE_NOT_FOUND in _codes(result.dataset_issues)


def test_qualified_reference_supports_windows_drive_path() -> None:
    windows_source = r"C:\data\sample.fasta"
    sample = _validated_record(
        _parsed_record(
            "ACGT",
            source_reference=windows_source,
            materialized_relative_path="inputs/files/windows_sample.fasta",
            record_id="ref-1",
        )
    )
    result = _dataset_result((sample,), reference_selector=rf"{windows_source}::ref-1")

    assert result.resolved_reference is not None
    assert result.resolved_reference.sample_id == sample.logical_sample.sample_id


def test_qualified_reference_invalid_when_only_invalid_sample_matches() -> None:
    invalid_sample = _validated_record(
        _parsed_record(
            "AXGT",
            source_reference="data/a.fasta",
            materialized_relative_path="inputs/files/a.fasta",
            record_id="ref",
        )
    )
    result = _dataset_result((invalid_sample,), reference_selector="data/a.fasta::ref")

    assert VALIDATION_ISSUE_REFERENCE_INVALID in _codes(result.dataset_issues)


def test_compute_and_none_modes_do_not_require_reference() -> None:
    first = _validated_record(_parsed_record("ACGT", record_id="r1"))
    second = _validated_record(_parsed_record("TGCA", record_id="r2", record_index=1))

    compute = _dataset_result((first, second), alignment_mode=AnalysisAlignmentMode.COMPUTE)
    none = _dataset_result((first, second), alignment_mode=AnalysisAlignmentMode.NONE)

    assert VALIDATION_ISSUE_REFERENCE_REQUIRED not in _codes(compute.dataset_issues)
    assert VALIDATION_ISSUE_REFERENCE_REQUIRED not in _codes(none.dataset_issues)
    assert compute.dataset_summary.comparative_analysis_available is True
    assert none.dataset_summary.comparative_analysis_available is True


def test_invalid_reference_in_compute_mode_blocks_only_reference_dependent_branch() -> None:
    first = _validated_record(_parsed_record("ACGT", record_id="r1"))
    second = _validated_record(_parsed_record("TGCA", record_id="r2", record_index=1))
    result = _dataset_result(
        (first, second),
        alignment_mode=AnalysisAlignmentMode.COMPUTE,
        reference_selector="missing",
    )

    assert result.dataset_summary.comparative_analysis_available is True
    assert result.dataset_summary.reference_dependent_analysis_available is False
    assert VALIDATION_ISSUE_REFERENCE_NOT_FOUND in _codes(result.dataset_issues)


def test_resolved_reference_round_trip() -> None:
    first = _validated_record(_parsed_record("ACGT", record_id="r1"))
    second = _validated_record(_parsed_record("TGCA", record_id="r2", record_index=1))
    result = _dataset_result((first, second), reference_selector="r1")

    assert result.resolved_reference is not None
    payload = result.resolved_reference.model_dump(mode="json")
    restored = InputProcessingResolvedReference.model_validate(payload)
    assert restored == result.resolved_reference


def test_reference_resolution_does_not_open_files(monkeypatch: pytest.MonkeyPatch) -> None:
    sample = _validated_record(_parsed_record("ACGT", record_id="ref"))

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("reference resolution must not open files")

    monkeypatch.setattr("builtins.open", _boom)
    result = _dataset_result((sample,), reference_selector="ref")
    assert result.resolved_reference is not None
