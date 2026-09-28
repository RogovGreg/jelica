from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator

from jelica_core.input_sources import resolve_submission_local_path

SEQUENCE_METADATA_FIELDS: Final[tuple[str, ...]] = (
    "collection_date",
    "geo_loc_name",
    "host",
    "isolation_source",
    "isolate",
    "strain",
    "lat_lon",
    "host_disease",
    "sex",
    "genotype",
    "serotype",
    "haplotype",
    "note",
    "collected_by",
    "lab_host",
)
_CSV_IDENTIFIER_FIELDS: Final[frozenset[str]] = frozenset({"record_id", "source"})
_CSV_ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    (*_CSV_IDENTIFIER_FIELDS, *SEQUENCE_METADATA_FIELDS)
)


class SampleMetadataError(ValueError):
    """Controlled error for user-supplied sequence metadata."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SequenceMetadata(BaseModel):
    """Uninterpreted, per-logical-sample biological metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collection_date: str | None = None
    geo_loc_name: str | None = None
    host: str | None = None
    isolation_source: str | None = None
    isolate: str | None = None
    strain: str | None = None
    lat_lon: str | None = None
    host_disease: str | None = None
    sex: str | None = None
    genotype: str | None = None
    serotype: str | None = None
    haplotype: str | None = None
    note: str | None = None
    collected_by: str | None = None
    lab_host: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field_name in SEQUENCE_METADATA_FIELDS:
            field_value = normalized.get(field_name)
            if field_value is None:
                continue
            if not isinstance(field_value, str):
                raise ValueError(f"{field_name} must be a string or null")
            stripped = field_value.strip()
            normalized[field_name] = stripped or None
        return normalized

    def has_values(self) -> bool:
        return any(getattr(self, field_name) is not None for field_name in SEQUENCE_METADATA_FIELDS)


class SampleMetadataSelector(BaseModel):
    """A normalized selector persisted in the immutable task configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str | None = None
    source_path: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field_name in ("record_id", "source_path"):
            field_value = normalized.get(field_name)
            if field_value is None:
                continue
            if not isinstance(field_value, str):
                raise ValueError(f"{field_name} must be a string or null")
            stripped = field_value.strip()
            normalized[field_name] = stripped or None
        return normalized

    @model_validator(mode="after")
    def _require_identifier(self) -> SampleMetadataSelector:
        if self.record_id is None and self.source_path is None:
            raise ValueError("metadata selector requires record_id and/or source_path")
        return self


class SampleMetadataOverride(BaseModel):
    """One effective metadata override stored with a task configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: SampleMetadataSelector
    metadata: SequenceMetadata

    @model_validator(mode="after")
    def _require_metadata_values(self) -> SampleMetadataOverride:
        if not self.metadata.has_values():
            raise ValueError("metadata override must contain at least one metadata value")
        return self


@dataclass(frozen=True, slots=True)
class SampleMetadataCandidate:
    candidate_id: str
    record_id: str | None
    source_path: str | None
    automatic_metadata: SequenceMetadata


class SampleMetadataMatchError(SampleMetadataError):
    """Controlled dataset-level error when an override cannot select one record."""


def load_sample_metadata_csv(
    *,
    metadata_csv: str,
    submission_base_dir: Path | None,
) -> tuple[SampleMetadataOverride, ...]:
    """Validate a CSV and convert it into durable, normalized overrides."""

    normalized_path = _bind_metadata_csv_path(
        metadata_csv=metadata_csv,
        submission_base_dir=submission_base_dir,
    )
    path = Path(normalized_path)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames
            _validate_csv_headers(headers)
            rows = tuple(reader)
    except SampleMetadataError:
        raise
    except (OSError, UnicodeError, csv.Error) as error:
        raise SampleMetadataError(
            code="metadata_csv_unreadable",
            message=f"Sample metadata CSV could not be read: {path}.",
        ) from error

    assert headers is not None
    overrides: list[SampleMetadataOverride] = []
    for row_number, row in enumerate(rows, start=2):
        if None in row:
            raise SampleMetadataError(
                code="metadata_csv_invalid",
                message=(
                    f"Sample metadata CSV row {row_number} has more values than its header."
                ),
            )
        if _is_blank_row(row=row, headers=headers):
            continue
        record_id = _optional_text(row.get("record_id"))
        raw_source = _optional_text(row.get("source"))
        if record_id is None and raw_source is None:
            raise SampleMetadataError(
                code="metadata_csv_missing_identifier",
                message=(
                    f"Sample metadata CSV row {row_number} requires record_id and/or source."
                ),
            )
        metadata = SequenceMetadata(
            **{
                field_name: row.get(field_name)
                for field_name in SEQUENCE_METADATA_FIELDS
                if field_name in headers
            }
        )
        if not metadata.has_values():
            raise SampleMetadataError(
                code="metadata_csv_no_metadata_values",
                message=(
                    f"Sample metadata CSV row {row_number} requires at least one metadata value."
                ),
            )
        source_path = (
            resolve_submission_local_path(source=raw_source, base_directory=path.parent)
            if raw_source is not None
            else None
        )
        overrides.append(
            SampleMetadataOverride(
                selector=SampleMetadataSelector(
                    record_id=record_id,
                    source_path=source_path,
                ),
                metadata=metadata,
            )
        )
    return tuple(overrides)


def merge_sequence_metadata(
    *,
    automatic: SequenceMetadata,
    override: SequenceMetadata,
) -> SequenceMetadata:
    """Overlay explicitly supplied user values over automatic metadata."""

    return SequenceMetadata(
        **{
            field_name: (
                override_value
                if (override_value := getattr(override, field_name)) is not None
                else getattr(automatic, field_name)
            )
            for field_name in SEQUENCE_METADATA_FIELDS
        }
    )


def resolve_sample_metadata_overrides(
    *,
    candidates: tuple[SampleMetadataCandidate, ...],
    overrides: tuple[SampleMetadataOverride, ...],
) -> dict[str, SequenceMetadata]:
    """Resolve every override exactly once and return final metadata by candidate ID."""

    resolved: dict[str, SequenceMetadata] = {
        candidate.candidate_id: candidate.automatic_metadata for candidate in candidates
    }
    targeted_candidate_ids: set[str] = set()
    for override in overrides:
        matches = _matching_candidates(candidates=candidates, selector=override.selector)
        if len(matches) == 0:
            raise SampleMetadataMatchError(
                code="metadata_csv_target_not_found",
                message="Sample metadata selector did not match any parsed record.",
            )
        if len(matches) > 1:
            raise SampleMetadataMatchError(
                code="metadata_csv_target_ambiguous",
                message="Sample metadata selector matched multiple parsed records.",
            )
        candidate = matches[0]
        if candidate.candidate_id in targeted_candidate_ids:
            raise SampleMetadataMatchError(
                code="metadata_csv_duplicate_target",
                message="Multiple sample metadata rows target the same parsed record.",
            )
        targeted_candidate_ids.add(candidate.candidate_id)
        resolved[candidate.candidate_id] = merge_sequence_metadata(
            automatic=candidate.automatic_metadata,
            override=override.metadata,
        )
    return resolved


def _bind_metadata_csv_path(*, metadata_csv: str, submission_base_dir: Path | None) -> str:
    normalized = metadata_csv.strip()
    if normalized == "":
        raise SampleMetadataError(
            code="metadata_csv_invalid",
            message="Sample metadata CSV path must not be empty.",
        )
    path = Path(
        resolve_submission_local_path(
            source=normalized,
            base_directory=submission_base_dir or Path.cwd(),
        )
    )
    if path.is_symlink():
        raise SampleMetadataError(
            code="metadata_csv_invalid",
            message="Symbolic links are not supported for sample metadata CSV files.",
        )
    if not path.is_file():
        raise SampleMetadataError(
            code="metadata_csv_unreadable",
            message=f"Sample metadata CSV is not a regular file: {path}.",
        )
    return str(path)


def _validate_csv_headers(headers: list[str] | None) -> None:
    if headers is None or len(headers) == 0:
        raise SampleMetadataError(
            code="metadata_csv_invalid",
            message="Sample metadata CSV must contain a header row.",
        )
    if any(header is None or header == "" for header in headers):
        raise SampleMetadataError(
            code="metadata_csv_invalid",
            message="Sample metadata CSV header names must not be empty.",
        )
    if len(set(headers)) != len(headers):
        raise SampleMetadataError(
            code="metadata_csv_invalid",
            message="Sample metadata CSV must not contain duplicate header names.",
        )
    unknown = [header for header in headers if header not in _CSV_ALLOWED_FIELDS]
    if unknown:
        raise SampleMetadataError(
            code="metadata_csv_unknown_column",
            message=(
                "Sample metadata CSV contains unsupported column(s): "
                + ", ".join(unknown)
                + "."
            ),
        )
    if not any(header in _CSV_IDENTIFIER_FIELDS for header in headers):
        raise SampleMetadataError(
            code="metadata_csv_missing_identifier",
            message="Sample metadata CSV requires a record_id and/or source column.",
        )


def _is_blank_row(*, row: dict[str | None, str | list[str] | None], headers: list[str]) -> bool:
    return all(_optional_text(row.get(header)) is None for header in headers)


def _matching_candidates(
    *,
    candidates: tuple[SampleMetadataCandidate, ...],
    selector: SampleMetadataSelector,
) -> tuple[SampleMetadataCandidate, ...]:
    return tuple(
        candidate
        for candidate in candidates
        if (selector.record_id is None or candidate.record_id == selector.record_id)
        and (selector.source_path is None or candidate.source_path == selector.source_path)
    )


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


__all__ = [
    "SEQUENCE_METADATA_FIELDS",
    "SampleMetadataCandidate",
    "SampleMetadataError",
    "SampleMetadataMatchError",
    "SampleMetadataOverride",
    "SampleMetadataSelector",
    "SequenceMetadata",
    "load_sample_metadata_csv",
    "merge_sequence_metadata",
    "resolve_sample_metadata_overrides",
]
