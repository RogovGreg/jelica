from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from Bio import SeqIO
from Bio.SeqIO.FastaIO import SimpleFastaParser
from Bio.SeqRecord import SeqRecord
from pydantic import BaseModel, ConfigDict, Field, field_validator

from jelica_contracts import JSONObject, JSONValue
from jelica_core.config import AnalysisAlignmentMode
from jelica_core.input_sources import (
    SUPPORTED_FASTA_EXTENSIONS,
    SUPPORTED_GENBANK_EXTENSIONS,
    SUPPORTED_TEXT_EXTENSIONS,
)

from .input_processing_models import (
    InputProcessingValidationIssue,
    ParsedInputRecord,
    ValidationIssueScope,
    ValidationIssueSeverity,
)

INPUT_MANIFEST_RELATIVE_PATH: Final = "inputs/input_manifest.json"
_ALIGNED_FASTA_EXTENSIONS: Final[frozenset[str]] = frozenset({".afa", ".mfa"})

PARSER_ISSUE_FILE_NOT_FOUND: Final = "input_file_not_found"
PARSER_ISSUE_FILE_UNREADABLE: Final = "input_file_unreadable"
PARSER_ISSUE_UNSUPPORTED_FORMAT: Final = "input_format_unsupported"
PARSER_ISSUE_FORMAT_NOT_ALLOWED_FOR_ALIGNMENT_MODE: Final = (
    "input_format_not_allowed_for_alignment_mode"
)
PARSER_ISSUE_FASTA_MALFORMED: Final = "fasta_malformed"
PARSER_ISSUE_GENBANK_MALFORMED: Final = "genbank_malformed"
PARSER_ISSUE_RECORD_ID_MISSING: Final = "record_id_missing"
PARSER_ISSUE_RECORD_SEQUENCE_EMPTY: Final = "record_sequence_empty"
PARSER_ISSUE_RECORD_DUPLICATE_ID: Final = "record_duplicate_id_in_file"
PARSER_ISSUE_SEQUENCE_ABSENT: Final = "record_sequence_absent"
PARSER_ISSUE_GAP_NOT_ALLOWED_FOR_ALIGNMENT_MODE: Final = (
    "record_gap_not_allowed_for_alignment_mode"
)


class MaterializedInputFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    format_hint: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    source_path: str | None = None
    requested_accession: str | None = None
    resolved_accession: str | None = None
    inline_length: int | None = Field(default=None, ge=0)

    @field_validator("relative_path", "source_type", "source_reference")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("value must not be empty")
        return normalized

    @field_validator("format_hint")
    @classmethod
    def _normalize_format_hint(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized == "":
            raise ValueError("format_hint must not be empty")
        return normalized


class ParsedInputFileResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_manifest_relative_path: str = Field(min_length=1)
    materialized_file: MaterializedInputFile
    records: tuple[ParsedInputRecord, ...] = Field(default_factory=tuple)
    issues: tuple[InputProcessingValidationIssue, ...] = Field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class _NormalizedSequence:
    sequence: str
    dot_to_gap_replacements: int
    forbidden_gap_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InputRecordParser:
    input_manifest_relative_path: str = INPUT_MANIFEST_RELATIVE_PATH

    def parse_materialized_file(
        self,
        *,
        stage_staging_directory: Path,
        materialized_file: MaterializedInputFile,
        alignment_mode: AnalysisAlignmentMode | str,
    ) -> ParsedInputFileResult:
        resolved_alignment_mode = _normalize_alignment_mode(alignment_mode)
        format_hint = materialized_file.format_hint.lower()
        issues: list[InputProcessingValidationIssue] = []
        records: list[ParsedInputRecord] = []

        if (
            _is_alignment_only_extension(format_hint)
            and resolved_alignment_mode != AnalysisAlignmentMode.PREALIGNED
        ):
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_FORMAT_NOT_ALLOWED_FOR_ALIGNMENT_MODE,
                    message=(
                        f"Input format '{format_hint}' is not allowed when alignment.mode is "
                        f"'{resolved_alignment_mode.value}'."
                    ),
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                    context={
                        "alignment_mode": resolved_alignment_mode.value,
                        "format_hint": format_hint,
                    },
                )
            )
            return ParsedInputFileResult(
                input_manifest_relative_path=self.input_manifest_relative_path,
                materialized_file=materialized_file,
                records=tuple(records),
                issues=tuple(issues),
            )

        file_path = stage_staging_directory / materialized_file.relative_path
        if not file_path.exists():
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_FILE_NOT_FOUND,
                    message=f"Materialized file was not found: {materialized_file.relative_path}.",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                )
            )
            return ParsedInputFileResult(
                input_manifest_relative_path=self.input_manifest_relative_path,
                materialized_file=materialized_file,
                records=tuple(),
                issues=tuple(issues),
            )

        if format_hint in SUPPORTED_FASTA_EXTENSIONS:
            self._parse_fasta_file(
                file_path=file_path,
                materialized_file=materialized_file,
                alignment_mode=resolved_alignment_mode,
                records=records,
                issues=issues,
            )
        elif format_hint in SUPPORTED_GENBANK_EXTENSIONS:
            self._parse_genbank_file(
                file_path=file_path,
                materialized_file=materialized_file,
                alignment_mode=resolved_alignment_mode,
                records=records,
                issues=issues,
            )
        elif format_hint in SUPPORTED_TEXT_EXTENSIONS:
            self._parse_txt_file(
                file_path=file_path,
                materialized_file=materialized_file,
                alignment_mode=resolved_alignment_mode,
                records=records,
                issues=issues,
            )
        else:
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_UNSUPPORTED_FORMAT,
                    message=f"Input format '{format_hint}' is not supported by parser.",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                    context={"format_hint": format_hint},
                )
            )

        return ParsedInputFileResult(
            input_manifest_relative_path=self.input_manifest_relative_path,
            materialized_file=materialized_file,
            records=tuple(records),
            issues=tuple(issues),
        )

    def iter_records(
        self,
        *,
        stage_staging_directory: Path,
        materialized_file: MaterializedInputFile,
        alignment_mode: AnalysisAlignmentMode | str,
    ) -> tuple[ParsedInputRecord, ...]:
        result = self.parse_materialized_file(
            stage_staging_directory=stage_staging_directory,
            materialized_file=materialized_file,
            alignment_mode=alignment_mode,
        )
        return result.records

    def _parse_fasta_file(
        self,
        *,
        file_path: Path,
        materialized_file: MaterializedInputFile,
        alignment_mode: AnalysisAlignmentMode,
        records: list[ParsedInputRecord],
        issues: list[InputProcessingValidationIssue],
    ) -> None:
        seen_record_ids: dict[str, int] = {}
        parsed_any_record = False

        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                for record_index, (title, raw_sequence) in enumerate(SimpleFastaParser(handle)):
                    parsed_any_record = True
                    record_id, description = _parse_fasta_title(title)
                    if record_id is None:
                        issues.append(
                            _build_issue(
                                code=PARSER_ISSUE_RECORD_ID_MISSING,
                                message="FASTA record ID is missing.",
                                scope=ValidationIssueScope.RECORD,
                                materialized_file=materialized_file,
                                record_index=record_index,
                            )
                        )
                    elif record_id in seen_record_ids:
                        issues.append(
                            _build_issue(
                                code=PARSER_ISSUE_RECORD_DUPLICATE_ID,
                                message=(
                                    f"Duplicate FASTA record ID '{record_id}' in the same file."
                                ),
                                scope=ValidationIssueScope.RECORD,
                                materialized_file=materialized_file,
                                record_id=record_id,
                                record_index=record_index,
                                context={"first_record_index": seen_record_ids[record_id]},
                            )
                        )
                    else:
                        seen_record_ids[record_id] = record_index

                    normalized = _normalize_sequence_representation(
                        raw_sequence=raw_sequence,
                        alignment_mode=alignment_mode,
                    )
                    if normalized.sequence == "":
                        issues.append(
                            _build_issue(
                                code=PARSER_ISSUE_RECORD_SEQUENCE_EMPTY,
                                message="FASTA record sequence is empty.",
                                scope=ValidationIssueScope.RECORD,
                                materialized_file=materialized_file,
                                record_id=record_id,
                                record_index=record_index,
                            )
                        )
                        continue

                    if len(normalized.forbidden_gap_symbols) > 0:
                        issues.append(
                            _build_issue(
                                code=PARSER_ISSUE_GAP_NOT_ALLOWED_FOR_ALIGNMENT_MODE,
                                message=(
                                    "Gap symbols are not allowed when alignment.mode is 'compute'."
                                ),
                                scope=ValidationIssueScope.RECORD,
                                materialized_file=materialized_file,
                                record_id=record_id,
                                record_index=record_index,
                                context={
                                    "alignment_mode": alignment_mode.value,
                                    "symbols": list(normalized.forbidden_gap_symbols),
                                },
                            )
                        )

                    metadata = _normalization_metadata(
                        dot_to_gap_replacements=normalized.dot_to_gap_replacements
                    )
                    records.append(
                        ParsedInputRecord(
                            input_manifest_source_reference=materialized_file.source_reference,
                            input_manifest_relative_path=self.input_manifest_relative_path,
                            materialized_relative_path=materialized_file.relative_path,
                            format_hint=materialized_file.format_hint,
                            record_index=record_index,
                            record_id=record_id,
                            description=description,
                            metadata=metadata,
                            raw_sequence=normalized.sequence,
                        )
                    )
        except OSError as error:
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_FILE_UNREADABLE,
                    message=f"Input file is unreadable: {error}",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                )
            )
            return
        except ValueError as error:
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_FASTA_MALFORMED,
                    message=f"Malformed FASTA file: {error}",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                )
            )
            return

        if not parsed_any_record:
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_FASTA_MALFORMED,
                    message="Malformed FASTA file: no FASTA records were found.",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                )
            )

    def _parse_genbank_file(
        self,
        *,
        file_path: Path,
        materialized_file: MaterializedInputFile,
        alignment_mode: AnalysisAlignmentMode,
        records: list[ParsedInputRecord],
        issues: list[InputProcessingValidationIssue],
    ) -> None:
        parsed_any_record = False

        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                for record_index, seq_record in enumerate(SeqIO.parse(handle, "genbank")):
                    parsed_any_record = True
                    normalized = _normalize_sequence_representation(
                        raw_sequence=str(seq_record.seq),
                        alignment_mode=alignment_mode,
                    )
                    record_id = _normalize_optional_text(seq_record.id)
                    description = _normalize_optional_text(seq_record.description)
                    if normalized.sequence == "":
                        issues.append(
                            _build_issue(
                                code=PARSER_ISSUE_SEQUENCE_ABSENT,
                                message="GenBank record sequence is absent.",
                                scope=ValidationIssueScope.RECORD,
                                materialized_file=materialized_file,
                                record_id=record_id,
                                record_index=record_index,
                            )
                        )
                        continue

                    if len(normalized.forbidden_gap_symbols) > 0:
                        issues.append(
                            _build_issue(
                                code=PARSER_ISSUE_GAP_NOT_ALLOWED_FOR_ALIGNMENT_MODE,
                                message=(
                                    "Gap symbols are not allowed when alignment.mode is 'compute'."
                                ),
                                scope=ValidationIssueScope.RECORD,
                                materialized_file=materialized_file,
                                record_id=record_id,
                                record_index=record_index,
                                context={
                                    "alignment_mode": alignment_mode.value,
                                    "symbols": list(normalized.forbidden_gap_symbols),
                                },
                            )
                        )

                    metadata = _build_genbank_metadata(seq_record)
                    dot_replacements = normalized.dot_to_gap_replacements
                    if dot_replacements > 0:
                        metadata["normalization"] = {
                            "dot_to_gap_replacements": dot_replacements
                        }

                    records.append(
                        ParsedInputRecord(
                            input_manifest_source_reference=materialized_file.source_reference,
                            input_manifest_relative_path=self.input_manifest_relative_path,
                            materialized_relative_path=materialized_file.relative_path,
                            format_hint=materialized_file.format_hint,
                            record_index=record_index,
                            record_id=record_id,
                            description=description,
                            metadata=metadata,
                            raw_sequence=normalized.sequence,
                        )
                    )
        except OSError as error:
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_FILE_UNREADABLE,
                    message=f"Input file is unreadable: {error}",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                )
            )
            return
        except ValueError as error:
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_GENBANK_MALFORMED,
                    message=f"Malformed GenBank file: {error}",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                )
            )
            return

        if not parsed_any_record:
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_GENBANK_MALFORMED,
                    message="Malformed GenBank file: no records were found.",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                )
            )

    def _parse_txt_file(
        self,
        *,
        file_path: Path,
        materialized_file: MaterializedInputFile,
        alignment_mode: AnalysisAlignmentMode,
        records: list[ParsedInputRecord],
        issues: list[InputProcessingValidationIssue],
    ) -> None:
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                raw_payload = handle.read()
        except OSError as error:
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_FILE_UNREADABLE,
                    message=f"Input file is unreadable: {error}",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                )
            )
            return

        normalized = _normalize_sequence_representation(
            raw_sequence=raw_payload,
            alignment_mode=alignment_mode,
        )
        if normalized.sequence == "":
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_RECORD_SEQUENCE_EMPTY,
                    message="TXT sequence is empty after formatting whitespace removal.",
                    scope=ValidationIssueScope.FILE,
                    materialized_file=materialized_file,
                )
            )
            return

        if len(normalized.forbidden_gap_symbols) > 0:
            issues.append(
                _build_issue(
                    code=PARSER_ISSUE_GAP_NOT_ALLOWED_FOR_ALIGNMENT_MODE,
                    message="Gap symbols are not allowed when alignment.mode is 'compute'.",
                    scope=ValidationIssueScope.RECORD,
                    materialized_file=materialized_file,
                    record_index=0,
                    context={
                        "alignment_mode": alignment_mode.value,
                        "symbols": list(normalized.forbidden_gap_symbols),
                    },
                )
            )

        metadata = _normalization_metadata(
            dot_to_gap_replacements=normalized.dot_to_gap_replacements
        )
        records.append(
            ParsedInputRecord(
                input_manifest_source_reference=materialized_file.source_reference,
                input_manifest_relative_path=self.input_manifest_relative_path,
                materialized_relative_path=materialized_file.relative_path,
                format_hint=materialized_file.format_hint,
                record_index=0,
                record_id=None,
                description=None,
                metadata=metadata,
                raw_sequence=normalized.sequence,
            )
        )


def _build_issue(
    *,
    code: str,
    message: str,
    scope: ValidationIssueScope,
    materialized_file: MaterializedInputFile,
    context: JSONObject | None = None,
    record_id: str | None = None,
    record_index: int | None = None,
) -> InputProcessingValidationIssue:
    return InputProcessingValidationIssue(
        code=code,
        message=message,
        severity=ValidationIssueSeverity.ERROR,
        scope=scope,
        path=materialized_file.relative_path,
        record_id=record_id,
        record_index=record_index,
        context=context or {},
    )


def _normalize_alignment_mode(alignment_mode: AnalysisAlignmentMode | str) -> AnalysisAlignmentMode:
    if isinstance(alignment_mode, AnalysisAlignmentMode):
        return alignment_mode
    return AnalysisAlignmentMode(alignment_mode.strip().lower())


def _is_alignment_only_extension(format_hint: str) -> bool:
    return format_hint in _ALIGNED_FASTA_EXTENSIONS


def _parse_fasta_title(title: str) -> tuple[str | None, str | None]:
    normalized_title = title.strip()
    if normalized_title == "":
        return None, None
    header_parts = normalized_title.split(maxsplit=1)
    record_id = header_parts[0].strip()
    if record_id == "":
        return None, None
    if len(header_parts) == 1:
        return record_id, None
    description = header_parts[1].strip()
    if description == "":
        return record_id, None
    return record_id, description


def _normalize_sequence_representation(
    *,
    raw_sequence: str,
    alignment_mode: AnalysisAlignmentMode,
) -> _NormalizedSequence:
    compact = "".join(raw_sequence.split())
    upper_sequence = compact.upper()

    if alignment_mode is AnalysisAlignmentMode.COMPUTE:
        forbidden_symbols: list[str] = []
        if "-" in upper_sequence:
            forbidden_symbols.append("-")
        if "." in upper_sequence:
            forbidden_symbols.append(".")
        return _NormalizedSequence(
            sequence=upper_sequence,
            dot_to_gap_replacements=0,
            forbidden_gap_symbols=tuple(forbidden_symbols),
        )

    dot_to_gap_replacements = upper_sequence.count(".")
    normalized_sequence = upper_sequence.replace(".", "-")
    return _NormalizedSequence(
        sequence=normalized_sequence,
        dot_to_gap_replacements=dot_to_gap_replacements,
        forbidden_gap_symbols=tuple(),
    )


def _normalization_metadata(*, dot_to_gap_replacements: int) -> JSONObject:
    if dot_to_gap_replacements <= 0:
        return {}
    return {"normalization": {"dot_to_gap_replacements": dot_to_gap_replacements}}


def _build_genbank_metadata(record: SeqRecord) -> JSONObject:
    metadata: dict[str, JSONValue] = {}
    if len(record.annotations) > 0:
        metadata["annotations"] = _to_json_value(record.annotations)
    if len(record.dbxrefs) > 0:
        metadata["dbxrefs"] = [str(value) for value in record.dbxrefs]
    if record.name.strip() != "" and record.name != record.id:
        metadata["name"] = record.name
    return cast(JSONObject, metadata)


def _to_json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return cast(JSONValue, value)
    if isinstance(value, dict):
        normalized: dict[str, JSONValue] = {}
        for key, nested_value in value.items():
            normalized[str(key)] = _to_json_value(nested_value)
        return normalized
    if isinstance(value, (list, tuple, set)):
        return [_to_json_value(item) for item in value]
    return str(value)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    return normalized
