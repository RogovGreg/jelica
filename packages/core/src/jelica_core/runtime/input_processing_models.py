from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jelica_contracts import JSONObject
from jelica_core.config import AnalysisAlignmentMode, AnalysisKmerStrand

INPUT_PROCESSING_STAGE_ID: Final = "input_processing"
INPUT_PROCESSING_MANIFEST_SCHEMA_VERSION: Final = 1
INPUT_PROCESSING_MANIFEST_RELATIVE_PATH: Final = "input_processing/input_processing_manifest.json"
INPUT_PROCESSING_SEQUENCE_ARTIFACTS_DIR: Final = "input_processing/sequences"
INPUT_PROCESSING_KMER_HITS_DIR: Final = "input_processing/kmer_hits"
INPUT_PROCESSING_KMER_HITS_SCHEMA_VERSION: Final = 1
SEQUENCE_ID_PREFIX: Final = "sha256:"
SEQUENCE_DIGEST_PATTERN: Final = r"^[0-9a-f]{64}$"
SEQUENCE_ID_PATTERN: Final = r"^(?:sha256:)?[0-9a-f]{64}$"


class InputProcessingState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class InputProcessingFileStatus(StrEnum):
    PROCESSED = "processed"
    SKIPPED = "skipped"
    FAILED = "failed"


class ValidationIssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationIssueScope(StrEnum):
    FILE = "file"
    RECORD = "record"
    SAMPLE = "sample"
    DATASET = "dataset"


class SampleValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    WARNING = "warning"


class DuplicateRelationType(StrEnum):
    DUPLICATE_OF = "duplicate_of"


class InputProcessingCoordinateSystem(StrEnum):
    ZERO_BASED_END_EXCLUSIVE = "zero_based_end_exclusive"


class SequenceStrand(StrEnum):
    PLUS = "+"
    MINUS = "-"


class KmerMatchKind(StrEnum):
    DEFINITE = "definite"
    POSSIBLE = "possible"


class ReferenceResolutionMethod(StrEnum):
    RECORD_ID = "record_id"
    FILE_PATH_AND_RECORD_ID = "file_path_and_record_id"


_BASE_COUNT_SYMBOLS: Final[tuple[str, ...]] = ("A", "C", "G", "T", "U")
# The existing IUPAC mask gives T and U the same ambiguity state. Direct counts
# remain separate; only ambiguous symbols can contribute to both potential values.
_AMBIGUOUS_BASES_BY_SYMBOL: Final[dict[str, tuple[str, ...]]] = {
    "R": ("A", "G"),
    "Y": ("C", "T", "U"),
    "S": ("C", "G"),
    "W": ("A", "T", "U"),
    "K": ("G", "T", "U"),
    "M": ("A", "C"),
    "B": ("C", "G", "T", "U"),
    "D": ("A", "G", "T", "U"),
    "H": ("A", "C", "T", "U"),
    "V": ("A", "C", "G"),
    "N": ("A", "C", "G", "T", "U"),
}


def _normalize_non_empty_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _validate_relative_artifact_path(value: str, *, field_name: str) -> str:
    normalized = _normalize_non_empty_text(value, field_name=field_name)
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if posix_path.is_absolute() or windows_path.is_absolute():
        raise ValueError(f"{field_name} must be a relative path")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise ValueError(f"{field_name} must not escape the workdir")
    return normalized


def _normalize_non_negative_counts(
    value: dict[str, int],
    *,
    field_name: str,
) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for key, count in value.items():
        normalized_key = _normalize_non_empty_text(str(key), field_name=f"{field_name} key")
        if count < 0:
            raise ValueError(f"{field_name} values must be >= 0")
        normalized[normalized_key] = count
    return normalized


def normalize_sequence_id(value: str) -> str:
    normalized = _normalize_non_empty_text(value, field_name="sequence_id").lower()
    digest = normalized
    if normalized.startswith(SEQUENCE_ID_PREFIX):
        digest = normalized[len(SEQUENCE_ID_PREFIX) :]
    if re.fullmatch(SEQUENCE_DIGEST_PATTERN, digest) is None:
        raise ValueError("sequence_id must be 'sha256:<64 lowercase hex>'")
    return f"{SEQUENCE_ID_PREFIX}{digest}"


def sequence_id_digest(value: str) -> str:
    return normalize_sequence_id(value).split(":", maxsplit=1)[1]


def sequence_id_from_digest(digest: str) -> str:
    normalized_digest = _normalize_non_empty_text(digest, field_name="digest").lower()
    if re.fullmatch(SEQUENCE_DIGEST_PATTERN, normalized_digest) is None:
        raise ValueError("digest must be 64 lowercase hex symbols")
    return f"{SEQUENCE_ID_PREFIX}{normalized_digest}"


class ParsedInputRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_manifest_source_reference: str = Field(min_length=1)
    input_manifest_relative_path: str = Field(min_length=1)
    materialized_relative_path: str = Field(min_length=1)
    format_hint: str = Field(min_length=1)
    record_index: int = Field(ge=0)
    record_id: str | None = None
    description: str | None = None
    metadata: JSONObject = Field(default_factory=dict)
    raw_sequence: str | None = None
    raw_sequence_path: str | None = None

    @field_validator("materialized_relative_path")
    @classmethod
    def _validate_materialized_relative_path(cls, value: str) -> str:
        return _validate_relative_artifact_path(value, field_name="materialized_relative_path")

    @field_validator("raw_sequence")
    @classmethod
    def _normalize_raw_sequence(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_non_empty_text(value, field_name="raw_sequence")

    @field_validator("raw_sequence_path")
    @classmethod
    def _normalize_raw_sequence_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_relative_artifact_path(value, field_name="raw_sequence_path")

    @model_validator(mode="after")
    def _require_sequence_payload(self) -> ParsedInputRecord:
        if self.raw_sequence is None and self.raw_sequence_path is None:
            raise ValueError("either raw_sequence or raw_sequence_path must be provided")
        return self


class InputProcessingValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: ValidationIssueSeverity
    scope: ValidationIssueScope
    path: str | None = None
    record_id: str | None = None
    record_index: int | None = Field(default=None, ge=0)
    sample_id: str | None = None
    context: JSONObject = Field(default_factory=dict)

    @field_validator("code", "message")
    @classmethod
    def _normalize_text(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "field")
        return _normalize_non_empty_text(value, field_name=field_name)

    @field_validator("path")
    @classmethod
    def _normalize_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_non_empty_text(value, field_name="path")


class KmerCoordinateRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_range(self) -> KmerCoordinateRange:
        if self.end < self.start:
            raise ValueError("range end must be >= start")
        return self


class KmerHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    match_kind: KmerMatchKind
    strand: SequenceStrand
    sequence_range: KmerCoordinateRange
    alignment_range: KmerCoordinateRange | None = None
    coordinate_system: InputProcessingCoordinateSystem = (
        InputProcessingCoordinateSystem.ZERO_BASED_END_EXCLUSIVE
    )


class KmerQuerySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1)
    definite_match_count: int = Field(ge=0)
    possible_match_count: int = Field(ge=0)
    strand: AnalysisKmerStrand
    hits_path: str | None = None

    @field_validator("query")
    @classmethod
    def _normalize_query(cls, value: str) -> str:
        return _normalize_non_empty_text(value, field_name="query").upper()

    @field_validator("hits_path")
    @classmethod
    def _normalize_hits_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_relative_artifact_path(value, field_name="hits_path")


class KmerQueryHits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1)
    strand: AnalysisKmerStrand
    coordinate_system: InputProcessingCoordinateSystem = (
        InputProcessingCoordinateSystem.ZERO_BASED_END_EXCLUSIVE
    )
    hits: tuple[KmerHit, ...] = Field(default_factory=tuple)

    @field_validator("query")
    @classmethod
    def _normalize_query(cls, value: str) -> str:
        return _normalize_non_empty_text(value, field_name="query").upper()


class KmerHitsSidecar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = INPUT_PROCESSING_KMER_HITS_SCHEMA_VERSION
    sequence_id: str = Field(pattern=SEQUENCE_ID_PATTERN)
    alignment_mode: AnalysisAlignmentMode | None = None
    coordinate_system: InputProcessingCoordinateSystem = (
        InputProcessingCoordinateSystem.ZERO_BASED_END_EXCLUSIVE
    )
    query_summaries: tuple[KmerQuerySummary, ...] = Field(default_factory=tuple)
    queries: tuple[KmerQueryHits, ...] = Field(default_factory=tuple)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != INPUT_PROCESSING_KMER_HITS_SCHEMA_VERSION:
            raise ValueError(
                "unsupported schema_version "
                f"{value}; expected {INPUT_PROCESSING_KMER_HITS_SCHEMA_VERSION}"
            )
        return value

    @field_validator("sequence_id")
    @classmethod
    def _normalize_sequence_id(cls, value: str) -> str:
        return normalize_sequence_id(value)


class CanonicalBaseCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    A: int = Field(ge=0)
    C: int = Field(ge=0)
    G: int = Field(ge=0)
    T: int = Field(ge=0)
    U: int = Field(ge=0)


class SequenceBaseCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definite: CanonicalBaseCounts
    potential: CanonicalBaseCounts

    @classmethod
    def from_symbol_counts(cls, symbol_counts: Mapping[str, int]) -> SequenceBaseCounts:
        definite_values = {
            symbol: symbol_counts.get(symbol, 0) for symbol in _BASE_COUNT_SYMBOLS
        }
        potential_values = dict(definite_values)
        for ambiguous_symbol, possible_bases in _AMBIGUOUS_BASES_BY_SYMBOL.items():
            occurrence_count = symbol_counts.get(ambiguous_symbol, 0)
            for base in possible_bases:
                potential_values[base] += occurrence_count

        return cls(
            definite=CanonicalBaseCounts(
                A=definite_values["A"],
                C=definite_values["C"],
                G=definite_values["G"],
                T=definite_values["T"],
                U=definite_values["U"],
            ),
            potential=CanonicalBaseCounts(
                A=potential_values["A"],
                C=potential_values["C"],
                G=potential_values["G"],
                T=potential_values["T"],
                U=potential_values["U"],
            ),
        )

    @model_validator(mode="after")
    def _potential_counts_include_definite_counts(self) -> SequenceBaseCounts:
        for symbol in _BASE_COUNT_SYMBOLS:
            if getattr(self.potential, symbol) < getattr(self.definite, symbol):
                raise ValueError(
                    f"potential count for {symbol} must be >= its definite count"
                )
        return self


class SequenceFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_length: int = Field(ge=0)
    ungapped_length: int = Field(ge=0)
    recognized_nucleotide_count: int = Field(ge=0)
    symbol_counts: dict[str, int] = Field(default_factory=dict)
    base_counts: SequenceBaseCounts
    canonical_count: int = Field(ge=0)
    ambiguous_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    invalid_symbol_count: int = Field(ge=0)
    invalid_symbol_counts: dict[str, int] = Field(default_factory=dict)
    invalid_positions: tuple[int, ...] = Field(default_factory=tuple)
    invalid_positions_truncated: bool = False
    gc_count: int = Field(ge=0)
    gc_content_total: float | None = Field(default=None, ge=0.0, le=1.0)
    resolved_gc_content: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_gc_count: float | None = Field(default=None, ge=0.0)
    expected_gc_content: float | None = Field(default=None, ge=0.0, le=1.0)
    u_count: int = Field(ge=0)
    sequence_id: str | None = Field(default=None, pattern=SEQUENCE_ID_PATTERN)
    kmer_summaries: tuple[KmerQuerySummary, ...] = Field(default_factory=tuple)

    @model_validator(mode="before")
    @classmethod
    def _derive_legacy_base_counts(cls, value: object) -> object:
        if not isinstance(value, Mapping) or "base_counts" in value:
            return value
        raw_symbol_counts = value.get("symbol_counts", {})
        if not isinstance(raw_symbol_counts, dict):
            return value
        try:
            symbol_counts = _normalize_non_negative_counts(
                raw_symbol_counts,
                field_name="symbol_counts",
            )
            base_counts = SequenceBaseCounts.from_symbol_counts(symbol_counts)
        except (TypeError, ValueError):
            return value
        normalized = dict(value)
        normalized["base_counts"] = base_counts
        return normalized

    @model_validator(mode="after")
    def _base_counts_match_symbol_counts(self) -> SequenceFacts:
        expected = SequenceBaseCounts.from_symbol_counts(self.symbol_counts)
        if self.base_counts != expected:
            raise ValueError("base_counts must match symbol_counts")
        return self

    @field_validator("symbol_counts")
    @classmethod
    def _normalize_symbol_counts(cls, value: dict[str, int]) -> dict[str, int]:
        return _normalize_non_negative_counts(value, field_name="symbol_counts")

    @field_validator("invalid_symbol_counts")
    @classmethod
    def _normalize_invalid_symbol_counts(cls, value: dict[str, int]) -> dict[str, int]:
        return _normalize_non_negative_counts(value, field_name="invalid_symbol_counts")

    @field_validator("invalid_positions")
    @classmethod
    def _normalize_invalid_positions(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        for position in value:
            if position < 0:
                raise ValueError("invalid_positions must be >= 0")
        return value

    @field_validator("sequence_id")
    @classmethod
    def _normalize_sequence_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_sequence_id(value)


class LogicalSampleProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_manifest_source_reference: str = Field(min_length=1)
    materialized_relative_path: str = Field(min_length=1)
    record_index: int = Field(ge=0)
    format_hint: str = Field(min_length=1)

    @field_validator("materialized_relative_path")
    @classmethod
    def _validate_materialized_path(cls, value: str) -> str:
        return _validate_relative_artifact_path(value, field_name="materialized_relative_path")


class LogicalSampleDuplicateRelation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relation_type: DuplicateRelationType = DuplicateRelationType.DUPLICATE_OF
    canonical_sample_id: str = Field(min_length=1)


class InputProcessingLogicalSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str = Field(min_length=1)
    provenance: LogicalSampleProvenance
    original_record_id: str | None = None
    original_description: str | None = None
    validation_status: SampleValidationStatus
    validation_issues: tuple[InputProcessingValidationIssue, ...] = Field(default_factory=tuple)
    sequence_id: str | None = Field(default=None, pattern=SEQUENCE_ID_PATTERN)
    inspection_facts: SequenceFacts | None = None
    duplicate_relation: LogicalSampleDuplicateRelation | None = None
    eligible_for_analysis: bool

    @field_validator("sequence_id")
    @classmethod
    def _normalize_sequence_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_sequence_id(value)

    @model_validator(mode="after")
    def _sample_and_sequence_ids_are_distinct(self) -> InputProcessingLogicalSample:
        if self.sequence_id is not None and self.sample_id == self.sequence_id:
            raise ValueError("sample_id must be distinct from sequence_id")
        return self


class InputProcessingUniqueSequence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence_id: str = Field(pattern=SEQUENCE_ID_PATTERN)
    sequence_artifact_path: str = Field(min_length=1)
    ungapped_sequence_sha256: str | None = Field(
        default=None,
        pattern=SEQUENCE_DIGEST_PATTERN,
    )
    facts: SequenceFacts
    logical_sample_ids: tuple[str, ...] = Field(default_factory=tuple)
    kmer_hits_path: str | None = None

    @field_validator("sequence_artifact_path")
    @classmethod
    def _normalize_sequence_artifact_path(cls, value: str) -> str:
        return _validate_relative_artifact_path(value, field_name="sequence_artifact_path")

    @field_validator("sequence_id")
    @classmethod
    def _normalize_sequence_id(cls, value: str) -> str:
        return normalize_sequence_id(value)

    @field_validator("kmer_hits_path")
    @classmethod
    def _normalize_kmer_hits_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_relative_artifact_path(value, field_name="kmer_hits_path")

    @model_validator(mode="after")
    def _ensure_facts_sequence_id_matches(self) -> InputProcessingUniqueSequence:
        if self.facts.sequence_id is None:
            return self
        if self.facts.sequence_id != self.sequence_id:
            raise ValueError("facts.sequence_id must match unique sequence_id")
        return self


class InputProcessingProcessedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_manifest_source_reference: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    format_hint: str = Field(min_length=1)
    status: InputProcessingFileStatus
    record_count: int = Field(ge=0)
    valid_sample_count: int = Field(ge=0)
    invalid_sample_count: int = Field(ge=0)
    validation_issues: tuple[InputProcessingValidationIssue, ...] = Field(default_factory=tuple)

    @field_validator("relative_path")
    @classmethod
    def _normalize_relative_path(cls, value: str) -> str:
        return _validate_relative_artifact_path(value, field_name="relative_path")


class InputProcessingAlignmentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: AnalysisAlignmentMode
    aligned_sample_count: int = Field(ge=0)
    alignment_length: int | None = Field(default=None, ge=0)


class InputProcessingResolvedReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    sequence_id: str = Field(pattern=SEQUENCE_ID_PATTERN)
    source_relative_path: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    resolution_method: ReferenceResolutionMethod | None = None

    @field_validator("selector", "sample_id", "record_id")
    @classmethod
    def _normalize_text(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "field")
        return _normalize_non_empty_text(value, field_name=field_name)

    @field_validator("sequence_id")
    @classmethod
    def _normalize_sequence_id(cls, value: str) -> str:
        return normalize_sequence_id(value)

    @field_validator("source_relative_path")
    @classmethod
    def _normalize_source_relative_path(cls, value: str) -> str:
        return _validate_relative_artifact_path(value, field_name="source_relative_path")


class InputProcessingDatasetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    discovered_record_count: int = Field(ge=0)
    valid_sample_count: int = Field(ge=0)
    invalid_sample_count: int = Field(ge=0)
    unique_sequence_count: int = Field(ge=0)
    duplicate_logical_sample_count: int = Field(ge=0)
    comparative_analysis_available: bool
    reference_dependent_analysis_available: bool = False
    alignment_summary: InputProcessingAlignmentSummary | None = None


class InputProcessingManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = INPUT_PROCESSING_MANIFEST_SCHEMA_VERSION
    stage_id: str = INPUT_PROCESSING_STAGE_ID
    task_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    config_revision_path: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    input_manifest_path: str = Field(default="inputs/input_manifest.json", min_length=1)
    generated_at: str = Field(min_length=1)
    processing_state: InputProcessingState
    processed_files: tuple[InputProcessingProcessedFile, ...] = Field(default_factory=tuple)
    logical_samples: tuple[InputProcessingLogicalSample, ...] = Field(default_factory=tuple)
    unique_sequences: tuple[InputProcessingUniqueSequence, ...] = Field(default_factory=tuple)
    dataset_issues: tuple[InputProcessingValidationIssue, ...] = Field(default_factory=tuple)
    dataset_summary: InputProcessingDatasetSummary
    resolved_reference: InputProcessingResolvedReference | None = None

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != INPUT_PROCESSING_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                "unsupported schema_version "
                f"{value}; expected {INPUT_PROCESSING_MANIFEST_SCHEMA_VERSION}"
            )
        return value

    @field_validator("input_manifest_path")
    @classmethod
    def _normalize_input_manifest_path(cls, value: str) -> str:
        return _validate_relative_artifact_path(value, field_name="input_manifest_path")


def input_processing_artifact_paths(
    manifest: InputProcessingManifest,
) -> tuple[str, ...]:
    """Return the canonical ordered artifact paths declared by the domain manifest."""

    artifacts = [INPUT_PROCESSING_MANIFEST_RELATIVE_PATH]
    for sequence in manifest.unique_sequences:
        artifacts.append(sequence.sequence_artifact_path)
        if sequence.kmer_hits_path is not None:
            artifacts.append(sequence.kmer_hits_path)
    return tuple(artifacts)
