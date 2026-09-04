from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from Bio.SeqIO.FastaIO import SimpleFastaParser

from jelica_core.config import (
    AnalysisAlignmentMode,
    AnalysisMafftDirectionAdjustment,
)

from .models import (
    AlignmentExecutionPlan,
    AlignmentExecutionPlanKind,
    AlignmentInputSequence,
    ReferenceCoordinateMap,
)

ALIGNED_NUCLEOTIDE_SYMBOLS: Final = frozenset("ACGTURYSWKMBDHVN-")
_COMPLEMENT = str.maketrans(
    {
        "A": "T",
        "C": "G",
        "G": "C",
        "T": "A",
        "U": "A",
        "R": "Y",
        "Y": "R",
        "S": "S",
        "W": "W",
        "K": "M",
        "M": "K",
        "B": "V",
        "D": "H",
        "H": "D",
        "V": "B",
        "N": "N",
    }
)


class AlignmentResultValidationError(RuntimeError):
    """Safe validation error that never embeds biological sequence content."""

    def __init__(
        self,
        *,
        code: str,
        detail: str,
        record_id: str | None = None,
        sequence_id: str | None = None,
        position: int | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.record_id = record_id
        self.sequence_id = sequence_id
        self.position = position
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ValidatedUniqueAlignment:
    aligned_by_sequence_id: dict[str, str]
    reoriented_sequence_ids: frozenset[str]
    alignment_length: int


@dataclass(frozen=True, slots=True)
class CanonicalAlignmentRow:
    sample_id: str
    sequence_id: str
    aligned_sequence: str


def plan_alignment(
    *,
    mode: AnalysisAlignmentMode,
    logical_sample_count: int,
    unique_sequence_count: int,
) -> AlignmentExecutionPlan:
    if logical_sample_count < 0 or unique_sequence_count < 0:
        raise ValueError("alignment counts must be non-negative")
    if unique_sequence_count > logical_sample_count:
        raise ValueError("unique_sequence_count cannot exceed logical_sample_count")
    if mode is AnalysisAlignmentMode.NONE:
        return AlignmentExecutionPlan(
            kind=AlignmentExecutionPlanKind.DISABLED,
            logical_sample_count=logical_sample_count,
            unique_sequence_count=unique_sequence_count,
            reason="alignment_mode_none",
        )
    if mode is AnalysisAlignmentMode.PREALIGNED:
        return AlignmentExecutionPlan(
            kind=AlignmentExecutionPlanKind.PREALIGNED,
            logical_sample_count=logical_sample_count,
            unique_sequence_count=unique_sequence_count,
            reason="prealigned_input",
        )
    if unique_sequence_count < 2:
        return AlignmentExecutionPlan(
            kind=AlignmentExecutionPlanKind.DIRECT,
            logical_sample_count=logical_sample_count,
            unique_sequence_count=unique_sequence_count,
            reason="fewer_than_two_unique_sequences",
        )
    return AlignmentExecutionPlan(
        kind=AlignmentExecutionPlanKind.ENGINE,
        logical_sample_count=logical_sample_count,
        unique_sequence_count=unique_sequence_count,
        reason="multiple_unique_sequences",
    )


def read_single_fasta_record(*, path: str) -> tuple[str, str]:
    try:
        with open(path, encoding="utf-8", errors="strict") as handle:
            records = list(SimpleFastaParser(handle))
    except (OSError, UnicodeError) as error:
        raise AlignmentResultValidationError(
            code="alignment_artifact_unreadable",
            detail="A sequence artifact could not be read.",
        ) from error
    if len(records) != 1:
        raise AlignmentResultValidationError(
            code="alignment_artifact_record_count",
            detail="A sequence artifact must contain exactly one FASTA record.",
        )
    title, sequence = records[0]
    record_id = title.split(maxsplit=1)[0].strip()
    if record_id == "":
        raise AlignmentResultValidationError(
            code="alignment_artifact_identifier_missing",
            detail="A sequence artifact contains a FASTA record without an identifier.",
        )
    return record_id, "".join(sequence.split()).upper()


def parse_aligned_fasta(*, path: str) -> tuple[tuple[str, str], ...]:
    try:
        with open(path, encoding="utf-8", errors="strict") as handle:
            records = tuple(
                (title.split(maxsplit=1)[0].strip(), "".join(sequence.split()).upper())
                for title, sequence in SimpleFastaParser(handle)
            )
    except (OSError, UnicodeError) as error:
        raise AlignmentResultValidationError(
            code="alignment_output_unreadable",
            detail="Alignment engine output could not be read.",
        ) from error
    if len(records) == 0:
        raise AlignmentResultValidationError(
            code="alignment_output_empty",
            detail="Alignment engine output is empty or is not aligned FASTA.",
        )
    for record_id, _sequence in records:
        if record_id == "":
            raise AlignmentResultValidationError(
                code="alignment_output_identifier_missing",
                detail="Alignment engine output contains a record without an identifier.",
            )
    return records


def validate_unique_alignment(
    *,
    records: Iterable[tuple[str, str]],
    expected_sequences: Mapping[str, str] | None = None,
    expected_ungapped_sha256: Mapping[str, str] | None = None,
    internal_record_ids: Mapping[str, str] | None = None,
    reverse_marked_record_ids: frozenset[str] = frozenset(),
    direction_adjustment: AnalysisMafftDirectionAdjustment = (
        AnalysisMafftDirectionAdjustment.NONE
    ),
) -> ValidatedUniqueAlignment:
    if (expected_sequences is None) == (expected_ungapped_sha256 is None):
        raise ValueError(
            "exactly one independent expected sequence representation must be provided"
        )
    if expected_sequences is not None:
        expected_ids = set(expected_sequences)
    else:
        assert expected_ungapped_sha256 is not None
        expected_ids = set(expected_ungapped_sha256)
    record_to_sequence_id = (
        {record_id: sequence_id for sequence_id, record_id in internal_record_ids.items()}
        if internal_record_ids is not None
        else {sequence_id: sequence_id for sequence_id in expected_ids}
    )
    aligned_by_sequence_id: dict[str, str] = {}
    reoriented: set[str] = set()
    alignment_length: int | None = None

    for raw_record_id, aligned_sequence in records:
        record_id, prefix_reversed = _normalize_engine_record_id(raw_record_id)
        sequence_id = record_to_sequence_id.get(record_id)
        if sequence_id is None:
            raise AlignmentResultValidationError(
                code="alignment_output_unknown_record",
                detail="Alignment engine output contains an unknown record identifier.",
                record_id=raw_record_id,
            )
        if sequence_id in aligned_by_sequence_id:
            raise AlignmentResultValidationError(
                code="alignment_output_duplicate_record",
                detail="Alignment engine output contains a duplicate expected record.",
                record_id=raw_record_id,
                sequence_id=sequence_id,
            )
        if aligned_sequence == "":
            raise AlignmentResultValidationError(
                code="alignment_output_empty_record",
                detail="Alignment engine output contains an empty aligned record.",
                record_id=raw_record_id,
                sequence_id=sequence_id,
            )
        _validate_aligned_symbols(
            aligned_sequence=aligned_sequence,
            record_id=raw_record_id,
            sequence_id=sequence_id,
        )
        if alignment_length is None:
            alignment_length = len(aligned_sequence)
        elif len(aligned_sequence) != alignment_length:
            raise AlignmentResultValidationError(
                code="alignment_output_length_mismatch",
                detail="Aligned FASTA records do not all have the same length.",
                record_id=raw_record_id,
                sequence_id=sequence_id,
            )

        ungapped = aligned_sequence.replace("-", "")
        if expected_sequences is not None:
            expected = expected_sequences[sequence_id].replace("-", "").upper()
            is_forward = ungapped == expected
            is_reverse = ungapped == reverse_complement(expected)
        else:
            assert expected_ungapped_sha256 is not None
            actual_digest = hashlib.sha256(ungapped.encode("utf-8")).hexdigest()
            is_forward = actual_digest == expected_ungapped_sha256[sequence_id]
            is_reverse = False
        marked_reversed = prefix_reversed or record_id in reverse_marked_record_ids
        if not is_forward:
            if direction_adjustment is AnalysisMafftDirectionAdjustment.NONE or not is_reverse:
                raise AlignmentResultValidationError(
                    code="alignment_output_sequence_mismatch",
                    detail=(
                        "An aligned record does not match its normalized input after gaps "
                        "are removed."
                    ),
                    record_id=raw_record_id,
                    sequence_id=sequence_id,
                )
            reoriented.add(sequence_id)
        elif marked_reversed:
            if direction_adjustment is AnalysisMafftDirectionAdjustment.NONE:
                raise AlignmentResultValidationError(
                    code="alignment_output_unexpected_orientation_marker",
                    detail="Alignment output contains an unexpected direction marker.",
                    record_id=raw_record_id,
                    sequence_id=sequence_id,
                )
            reoriented.add(sequence_id)
        aligned_by_sequence_id[sequence_id] = aligned_sequence

    missing = expected_ids.difference(aligned_by_sequence_id)
    if missing:
        missing_id = sorted(missing)[0]
        raise AlignmentResultValidationError(
            code="alignment_output_missing_record",
            detail="Alignment engine output is missing an expected unique sequence.",
            sequence_id=missing_id,
        )
    if len(aligned_by_sequence_id) != len(expected_ids):
        raise AlignmentResultValidationError(
            code="alignment_output_record_count_mismatch",
            detail="Alignment engine output record count does not match the expected set.",
        )
    return ValidatedUniqueAlignment(
        aligned_by_sequence_id=aligned_by_sequence_id,
        reoriented_sequence_ids=frozenset(reoriented),
        alignment_length=alignment_length or 0,
    )


def expand_logical_samples(
    *,
    aligned_by_sequence_id: Mapping[str, str],
    logical_samples: Iterable[tuple[str, str]],
) -> tuple[CanonicalAlignmentRow, ...]:
    rows: list[CanonicalAlignmentRow] = []
    seen_sample_ids: set[str] = set()
    alignment_length: int | None = None
    for sample_id, sequence_id in logical_samples:
        if sample_id in seen_sample_ids:
            raise AlignmentResultValidationError(
                code="alignment_duplicate_sample_id",
                detail="Logical sample identifiers must be unique in canonical alignment.",
                sequence_id=sequence_id,
            )
        aligned_sequence = aligned_by_sequence_id.get(sequence_id)
        if aligned_sequence is None:
            raise AlignmentResultValidationError(
                code="alignment_sample_sequence_missing",
                detail="A logical sample has no validated aligned sequence.",
                sequence_id=sequence_id,
            )
        if alignment_length is None:
            alignment_length = len(aligned_sequence)
        elif len(aligned_sequence) != alignment_length:
            raise AlignmentResultValidationError(
                code="alignment_expansion_length_mismatch",
                detail="Expanded logical sample rows do not share one alignment length.",
                sequence_id=sequence_id,
            )
        seen_sample_ids.add(sample_id)
        rows.append(
            CanonicalAlignmentRow(
                sample_id=sample_id,
                sequence_id=sequence_id,
                aligned_sequence=aligned_sequence,
            )
        )
    return tuple(rows)


def build_reference_coordinate_map(
    *,
    rows: Iterable[CanonicalAlignmentRow],
    reference_sample_id: str | None,
) -> ReferenceCoordinateMap:
    all_rows = tuple(rows)
    alignment_length = len(all_rows[0].aligned_sequence) if all_rows else 0
    if reference_sample_id is None:
        raise AlignmentResultValidationError(
            code="alignment_reference_unresolved",
            detail="A reference sample is required to build the coordinate map.",
        )
    matching = [row for row in all_rows if row.sample_id == reference_sample_id]
    if len(matching) == 0:
        raise AlignmentResultValidationError(
            code="alignment_reference_missing",
            detail="The resolved reference is absent from canonical alignment.",
        )
    if len(matching) > 1:
        raise AlignmentResultValidationError(
            code="alignment_reference_not_unique",
            detail="The resolved reference occurs more than once in canonical alignment.",
        )
    reference = matching[0]
    reference_position = 0
    positions: list[int | None] = []
    for symbol in reference.aligned_sequence:
        if symbol == "-":
            positions.append(None)
            continue
        reference_position += 1
        positions.append(reference_position)
    return ReferenceCoordinateMap(
        alignment_length=alignment_length,
        reference_sample_id=reference.sample_id,
        reference_sequence_id=reference.sequence_id,
        reference_positions=tuple(positions),
    )


def serialize_canonical_fasta(rows: Iterable[CanonicalAlignmentRow], *, width: int = 80) -> str:
    if width <= 0:
        raise ValueError("FASTA line width must be positive")
    lines: list[str] = []
    for row in rows:
        lines.append(f">{row.sample_id}")
        for start in range(0, len(row.aligned_sequence), width):
            lines.append(row.aligned_sequence[start : start + width])
    return "\n".join(lines) + ("\n" if lines else "")


def write_canonical_fasta_atomically(
    *,
    path: Path,
    rows: Iterable[CanonicalAlignmentRow],
    width: int = 80,
) -> str:
    """Stream canonical FASTA to an atomic temporary file and return its SHA-256."""
    if width <= 0:
        raise ValueError("FASTA line width must be positive")
    temp_path: Path | None = None
    digest = hashlib.sha256()
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            for row in rows:
                header = f">{row.sample_id}\n".encode("utf-8")
                handle.write(header)
                digest.update(header)
                for start in range(0, len(row.aligned_sequence), width):
                    chunk = (row.aligned_sequence[start : start + width] + "\n").encode(
                        "utf-8"
                    )
                    handle.write(chunk)
                    digest.update(chunk)
        os.replace(temp_path, path)
    except OSError:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def compute_input_set_hash(sequences: Iterable[AlignmentInputSequence]) -> str:
    payload = [
        {
            "sequence_id": item.sequence_id,
            "logical_sample_ids": list(item.logical_sample_ids),
            "length": len(item.sequence.replace("-", "")),
        }
        for item in sequences
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reverse_complement(sequence: str) -> str:
    return sequence.upper().translate(_COMPLEMENT)[::-1]


def _validate_aligned_symbols(
    *, aligned_sequence: str, record_id: str, sequence_id: str
) -> None:
    for index, symbol in enumerate(aligned_sequence, start=1):
        if symbol not in ALIGNED_NUCLEOTIDE_SYMBOLS:
            raise AlignmentResultValidationError(
                code="alignment_output_invalid_symbol",
                detail="An aligned record contains an unsupported nucleotide symbol.",
                record_id=record_id,
                sequence_id=sequence_id,
                position=index,
            )


def _normalize_engine_record_id(record_id: str) -> tuple[str, bool]:
    normalized = record_id.strip()
    reversed_marker = False
    while normalized.startswith("_R_"):
        normalized = normalized[3:]
        reversed_marker = True
    if normalized.startswith("_F_"):
        normalized = normalized[3:]
    return normalized, reversed_marker
