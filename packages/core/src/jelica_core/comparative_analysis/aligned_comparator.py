from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jelica_core.alignment import ALIGNED_NUCLEOTIDE_SYMBOLS, CanonicalAlignmentRow

from .errors import AlignedComparisonError, ComparisonErrorCode

_DEFINITE_SYMBOLS = frozenset("ACGTU")
_GAP = "-"


class ComparisonCoordinateSystem(StrEnum):
    ONE_BASED_INCLUSIVE = "one_based_inclusive"


class DifferenceEventType(StrEnum):
    SUBSTITUTION = "substitution"
    INSERTION = "insertion"
    DELETION = "deletion"
    UNCERTAIN = "uncertain"


class ComparisonIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str = Field(min_length=1)
    sequence_id: str = Field(min_length=1)


class AlignedDifferenceEvent(BaseModel):
    """A normalized event with 1-based inclusive MSA and ungapped positions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: DifferenceEventType
    msa_column_start: int = Field(ge=1)
    msa_column_end: int = Field(ge=1)
    length: int = Field(ge=1)
    left_start: int | None = Field(default=None, ge=1)
    left_end: int | None = Field(default=None, ge=1)
    right_start: int | None = Field(default=None, ge=1)
    right_end: int | None = Field(default=None, ge=1)
    after_left_position: int | None = Field(default=None, ge=1)
    before_left_position: int | None = Field(default=None, ge=1)
    after_right_position: int | None = Field(default=None, ge=1)
    before_right_position: int | None = Field(default=None, ge=1)
    coordinate_system: ComparisonCoordinateSystem = (
        ComparisonCoordinateSystem.ONE_BASED_INCLUSIVE
    )

    @model_validator(mode="after")
    def _validate_coordinates(self) -> AlignedDifferenceEvent:
        if self.msa_column_end < self.msa_column_start:
            raise ValueError("MSA event end must not precede its start")
        if self.length != self.msa_column_end - self.msa_column_start + 1:
            raise ValueError("event length must equal its inclusive MSA span")
        _validate_optional_span(self.left_start, self.left_end, side="left")
        _validate_optional_span(self.right_start, self.right_end, side="right")
        if self.left_start is not None and (
            self.after_left_position is not None
            or self.before_left_position is not None
        ):
            raise ValueError("left anchors require an absent left span")
        if self.right_start is not None and (
            self.after_right_position is not None
            or self.before_right_position is not None
        ):
            raise ValueError("right anchors require an absent right span")
        if self.type is DifferenceEventType.INSERTION and (
            self.left_start is not None or self.right_start is None
        ):
            raise ValueError("insertion requires only a right affected span")
        if self.type is DifferenceEventType.DELETION and (
            self.left_start is None or self.right_start is not None
        ):
            raise ValueError("deletion requires only a left affected span")
        if self.type is DifferenceEventType.SUBSTITUTION and (
            self.left_start is None or self.right_start is None
        ):
            raise ValueError("substitution requires both affected spans")
        return self


class AlignedComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    msa_column_count: int = Field(ge=0)
    both_gap_column_count: int = Field(ge=0)
    comparable_base_count: int = Field(ge=0)
    matching_base_count: int = Field(ge=0)
    substitution_event_count: int = Field(ge=0)
    substituted_base_count: int = Field(ge=0)
    insertion_event_count: int = Field(ge=0)
    inserted_base_count: int = Field(ge=0)
    deletion_event_count: int = Field(ge=0)
    deleted_base_count: int = Field(ge=0)
    uncertain_event_count: int = Field(ge=0)
    uncertain_column_count: int = Field(ge=0)
    identity_on_comparable_bases: float | None = Field(
        default=None, ge=0.0, le=1.0
    )

    @model_validator(mode="after")
    def _validate_summary(self) -> AlignedComparisonSummary:
        if self.comparable_base_count != (
            self.matching_base_count + self.substituted_base_count
        ):
            raise ValueError("comparable count must equal matches plus substitutions")
        classified_columns = (
            self.both_gap_column_count
            + self.comparable_base_count
            + self.inserted_base_count
            + self.deleted_base_count
            + self.uncertain_column_count
        )
        if classified_columns != self.msa_column_count:
            raise ValueError("summary column classes must cover the MSA")
        expected_identity = (
            None
            if self.comparable_base_count == 0
            else self.matching_base_count / self.comparable_base_count
        )
        if expected_identity is None:
            if self.identity_on_comparable_bases is not None:
                raise ValueError("identity must be null without comparable bases")
        elif self.identity_on_comparable_bases is None or not math.isclose(
            self.identity_on_comparable_bases,
            expected_identity,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("identity is inconsistent with comparison counts")
        return self


class DirectedAlignedComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left: ComparisonIdentity
    right: ComparisonIdentity
    summary: AlignedComparisonSummary
    events: tuple[AlignedDifferenceEvent, ...] = Field(default_factory=tuple)
    uracil_thymine_equivalent: bool = False
    identical_sequence_shortcut: bool = False
    coordinate_system: ComparisonCoordinateSystem = (
        ComparisonCoordinateSystem.ONE_BASED_INCLUSIVE
    )


class _NeutralEventKind(StrEnum):
    SUBSTITUTION = "substitution"
    FIRST_GAP = "first_gap"
    SECOND_GAP = "second_gap"
    UNCERTAIN = "uncertain"


@dataclass(slots=True)
class _EventBlock:
    kind: _NeutralEventKind
    msa_start: int
    msa_end: int
    left_prefix_before: int
    right_prefix_before: int
    left_start: int | None = None
    left_end: int | None = None
    right_start: int | None = None
    right_end: int | None = None

    def extend(
        self,
        *,
        column: int,
        left_before: int,
        left_after: int,
        right_before: int,
        right_after: int,
    ) -> None:
        self.msa_end = column
        if left_after > left_before:
            if self.left_start is None:
                self.left_start = left_before + 1
            self.left_end = left_after
        if right_after > right_before:
            if self.right_start is None:
                self.right_start = right_before + 1
            self.right_end = right_after


@dataclass(frozen=True, slots=True)
class _ScanResult:
    msa_column_count: int
    both_gap_column_count: int
    matching_base_count: int
    substituted_base_count: int
    blocks: tuple[_EventBlock, ...]
    left_length: int
    right_length: int
    identical_sequence_shortcut: bool


class AlignedSequenceComparator:
    """Linearly compare two rows of an already canonicalized MSA."""

    def compare_rows(
        self,
        *,
        left: CanonicalAlignmentRow,
        right: CanonicalAlignmentRow,
        uracil_thymine_equivalent: bool = False,
    ) -> DirectedAlignedComparison:
        return self.compare(
            left_aligned_sequence=left.aligned_sequence,
            right_aligned_sequence=right.aligned_sequence,
            left_identity=ComparisonIdentity(
                sample_id=left.sample_id,
                sequence_id=left.sequence_id,
            ),
            right_identity=ComparisonIdentity(
                sample_id=right.sample_id,
                sequence_id=right.sequence_id,
            ),
            uracil_thymine_equivalent=uracil_thymine_equivalent,
        )

    def compare(
        self,
        *,
        left_aligned_sequence: str | None,
        right_aligned_sequence: str | None,
        left_identity: ComparisonIdentity,
        right_identity: ComparisonIdentity,
        uracil_thymine_equivalent: bool = False,
    ) -> DirectedAlignedComparison:
        _validate_aligned_inputs(
            left_aligned_sequence=left_aligned_sequence,
            right_aligned_sequence=right_aligned_sequence,
            left_identity=left_identity,
            right_identity=right_identity,
        )
        assert left_aligned_sequence is not None
        assert right_aligned_sequence is not None

        if left_identity.sequence_id == right_identity.sequence_id:
            if left_aligned_sequence != right_aligned_sequence:
                raise AlignedComparisonError(
                    code=ComparisonErrorCode.IDENTICAL_SEQUENCE_MISMATCH,
                    detail=(
                        "Rows with one sequence identifier have inconsistent aligned values."
                    ),
                    safe_details={
                        "left_sample_id": left_identity.sample_id,
                        "right_sample_id": right_identity.sample_id,
                    },
                )
            scan = _scan_identical_sequence(left_aligned_sequence, left_identity)
        else:
            scan = _scan_aligned_pair(
                left_aligned_sequence,
                right_aligned_sequence,
                left_identity=left_identity,
                right_identity=right_identity,
                uracil_thymine_equivalent=uracil_thymine_equivalent,
            )
        return _project_scan(
            scan,
            left_identity=left_identity,
            right_identity=right_identity,
            uracil_thymine_equivalent=uracil_thymine_equivalent,
        )


class DirectedComparisonProjector:
    """Project one directed scan to other logical identities or its reverse."""

    def project(
        self,
        comparison: DirectedAlignedComparison,
        *,
        left_identity: ComparisonIdentity,
        right_identity: ComparisonIdentity,
        reverse: bool = False,
    ) -> DirectedAlignedComparison:
        projected = self.reverse(comparison) if reverse else comparison
        return projected.model_copy(
            update={"left": left_identity, "right": right_identity}
        )

    def reverse(
        self,
        comparison: DirectedAlignedComparison,
    ) -> DirectedAlignedComparison:
        reversed_events = tuple(_reverse_event(event) for event in comparison.events)
        summary = comparison.summary
        reversed_summary = AlignedComparisonSummary(
            msa_column_count=summary.msa_column_count,
            both_gap_column_count=summary.both_gap_column_count,
            comparable_base_count=summary.comparable_base_count,
            matching_base_count=summary.matching_base_count,
            substitution_event_count=summary.substitution_event_count,
            substituted_base_count=summary.substituted_base_count,
            insertion_event_count=summary.deletion_event_count,
            inserted_base_count=summary.deleted_base_count,
            deletion_event_count=summary.insertion_event_count,
            deleted_base_count=summary.inserted_base_count,
            uncertain_event_count=summary.uncertain_event_count,
            uncertain_column_count=summary.uncertain_column_count,
            identity_on_comparable_bases=summary.identity_on_comparable_bases,
        )
        return DirectedAlignedComparison(
            left=comparison.right,
            right=comparison.left,
            summary=reversed_summary,
            events=reversed_events,
            uracil_thymine_equivalent=comparison.uracil_thymine_equivalent,
            identical_sequence_shortcut=comparison.identical_sequence_shortcut,
        )


def _validate_aligned_inputs(
    *,
    left_aligned_sequence: str | None,
    right_aligned_sequence: str | None,
    left_identity: ComparisonIdentity,
    right_identity: ComparisonIdentity,
) -> None:
    if left_aligned_sequence is None or left_aligned_sequence == "":
        raise AlignedComparisonError(
            code=ComparisonErrorCode.ALIGNED_SEQUENCE_MISSING,
            detail="Left aligned sequence is absent.",
            safe_details={"sample_id": left_identity.sample_id},
        )
    if right_aligned_sequence is None or right_aligned_sequence == "":
        raise AlignedComparisonError(
            code=ComparisonErrorCode.ALIGNED_SEQUENCE_MISSING,
            detail="Right aligned sequence is absent.",
            safe_details={"sample_id": right_identity.sample_id},
        )
    if len(left_aligned_sequence) != len(right_aligned_sequence):
        raise AlignedComparisonError(
            code=ComparisonErrorCode.ALIGNED_LENGTH_MISMATCH,
            detail="Aligned sequences must have equal MSA lengths.",
            safe_details={
                "left_length": len(left_aligned_sequence),
                "right_length": len(right_aligned_sequence),
            },
        )


def _scan_aligned_pair(
    left: str,
    right: str,
    *,
    left_identity: ComparisonIdentity,
    right_identity: ComparisonIdentity,
    uracil_thymine_equivalent: bool,
) -> _ScanResult:
    blocks: list[_EventBlock] = []
    current: _EventBlock | None = None
    left_position = 0
    right_position = 0
    both_gap_count = 0
    match_count = 0
    substitution_count = 0

    for column, (left_symbol, right_symbol) in enumerate(
        zip(left, right, strict=True), start=1
    ):
        _validate_symbol(left_symbol, side="left", position=column, identity=left_identity)
        _validate_symbol(
            right_symbol,
            side="right",
            position=column,
            identity=right_identity,
        )
        left_before = left_position
        right_before = right_position
        if left_symbol != _GAP:
            left_position += 1
        if right_symbol != _GAP:
            right_position += 1

        kind: _NeutralEventKind | None
        if left_symbol == _GAP and right_symbol == _GAP:
            both_gap_count += 1
            kind = None
        elif _is_ambiguous(left_symbol) or _is_ambiguous(right_symbol):
            kind = _NeutralEventKind.UNCERTAIN
        elif left_symbol == _GAP:
            kind = _NeutralEventKind.FIRST_GAP
        elif right_symbol == _GAP:
            kind = _NeutralEventKind.SECOND_GAP
        elif _definite_symbols_match(
            left_symbol,
            right_symbol,
            uracil_thymine_equivalent=uracil_thymine_equivalent,
        ):
            match_count += 1
            kind = None
        else:
            substitution_count += 1
            kind = _NeutralEventKind.SUBSTITUTION

        current = _append_or_flush_block(
            blocks=blocks,
            current=current,
            kind=kind,
            column=column,
            left_before=left_before,
            left_after=left_position,
            right_before=right_before,
            right_after=right_position,
        )

    if current is not None:
        blocks.append(current)
    return _ScanResult(
        msa_column_count=len(left),
        both_gap_column_count=both_gap_count,
        matching_base_count=match_count,
        substituted_base_count=substitution_count,
        blocks=tuple(blocks),
        left_length=left_position,
        right_length=right_position,
        identical_sequence_shortcut=False,
    )


def _scan_identical_sequence(
    aligned_sequence: str,
    identity: ComparisonIdentity,
) -> _ScanResult:
    blocks: list[_EventBlock] = []
    current: _EventBlock | None = None
    position = 0
    both_gap_count = 0
    match_count = 0
    for column, symbol in enumerate(aligned_sequence, start=1):
        _validate_symbol(symbol, side="left", position=column, identity=identity)
        before = position
        if symbol != _GAP:
            position += 1
        if symbol == _GAP:
            both_gap_count += 1
            kind = None
        elif _is_ambiguous(symbol):
            kind = _NeutralEventKind.UNCERTAIN
        else:
            match_count += 1
            kind = None
        current = _append_or_flush_block(
            blocks=blocks,
            current=current,
            kind=kind,
            column=column,
            left_before=before,
            left_after=position,
            right_before=before,
            right_after=position,
        )
    if current is not None:
        blocks.append(current)
    return _ScanResult(
        msa_column_count=len(aligned_sequence),
        both_gap_column_count=both_gap_count,
        matching_base_count=match_count,
        substituted_base_count=0,
        blocks=tuple(blocks),
        left_length=position,
        right_length=position,
        identical_sequence_shortcut=True,
    )


def _append_or_flush_block(
    *,
    blocks: list[_EventBlock],
    current: _EventBlock | None,
    kind: _NeutralEventKind | None,
    column: int,
    left_before: int,
    left_after: int,
    right_before: int,
    right_after: int,
) -> _EventBlock | None:
    if kind is None:
        if current is not None:
            blocks.append(current)
        return None
    if current is None or current.kind is not kind:
        if current is not None:
            blocks.append(current)
        current = _EventBlock(
            kind=kind,
            msa_start=column,
            msa_end=column,
            left_prefix_before=left_before,
            right_prefix_before=right_before,
        )
    current.extend(
        column=column,
        left_before=left_before,
        left_after=left_after,
        right_before=right_before,
        right_after=right_after,
    )
    return current


def _project_scan(
    scan: _ScanResult,
    *,
    left_identity: ComparisonIdentity,
    right_identity: ComparisonIdentity,
    uracil_thymine_equivalent: bool,
) -> DirectedAlignedComparison:
    events = tuple(
        _project_block(
            block,
            left_length=scan.left_length,
            right_length=scan.right_length,
        )
        for block in scan.blocks
    )
    inserted_count = sum(
        event.length for event in events if event.type is DifferenceEventType.INSERTION
    )
    deleted_count = sum(
        event.length for event in events if event.type is DifferenceEventType.DELETION
    )
    uncertain_count = sum(
        event.length for event in events if event.type is DifferenceEventType.UNCERTAIN
    )
    comparable_count = scan.matching_base_count + scan.substituted_base_count
    summary = AlignedComparisonSummary(
        msa_column_count=scan.msa_column_count,
        both_gap_column_count=scan.both_gap_column_count,
        comparable_base_count=comparable_count,
        matching_base_count=scan.matching_base_count,
        substitution_event_count=_event_count(events, DifferenceEventType.SUBSTITUTION),
        substituted_base_count=scan.substituted_base_count,
        insertion_event_count=_event_count(events, DifferenceEventType.INSERTION),
        inserted_base_count=inserted_count,
        deletion_event_count=_event_count(events, DifferenceEventType.DELETION),
        deleted_base_count=deleted_count,
        uncertain_event_count=_event_count(events, DifferenceEventType.UNCERTAIN),
        uncertain_column_count=uncertain_count,
        identity_on_comparable_bases=(
            None
            if comparable_count == 0
            else scan.matching_base_count / comparable_count
        ),
    )
    return DirectedAlignedComparison(
        left=left_identity,
        right=right_identity,
        summary=summary,
        events=events,
        uracil_thymine_equivalent=uracil_thymine_equivalent,
        identical_sequence_shortcut=scan.identical_sequence_shortcut,
    )


def _project_block(
    block: _EventBlock,
    *,
    left_length: int,
    right_length: int,
) -> AlignedDifferenceEvent:
    event_type = {
        _NeutralEventKind.SUBSTITUTION: DifferenceEventType.SUBSTITUTION,
        _NeutralEventKind.FIRST_GAP: DifferenceEventType.INSERTION,
        _NeutralEventKind.SECOND_GAP: DifferenceEventType.DELETION,
        _NeutralEventKind.UNCERTAIN: DifferenceEventType.UNCERTAIN,
    }[block.kind]
    after_left, before_left = _anchors_for_absent_span(
        start=block.left_start,
        prefix_before=block.left_prefix_before,
        total_length=left_length,
    )
    after_right, before_right = _anchors_for_absent_span(
        start=block.right_start,
        prefix_before=block.right_prefix_before,
        total_length=right_length,
    )
    return AlignedDifferenceEvent(
        type=event_type,
        msa_column_start=block.msa_start,
        msa_column_end=block.msa_end,
        length=block.msa_end - block.msa_start + 1,
        left_start=block.left_start,
        left_end=block.left_end,
        right_start=block.right_start,
        right_end=block.right_end,
        after_left_position=after_left,
        before_left_position=before_left,
        after_right_position=after_right,
        before_right_position=before_right,
    )


def _anchors_for_absent_span(
    *,
    start: int | None,
    prefix_before: int,
    total_length: int,
) -> tuple[int | None, int | None]:
    if start is not None:
        return None, None
    after = prefix_before if prefix_before > 0 else None
    before = prefix_before + 1 if prefix_before < total_length else None
    return after, before


def _reverse_event(event: AlignedDifferenceEvent) -> AlignedDifferenceEvent:
    event_type = event.type
    if event_type is DifferenceEventType.INSERTION:
        event_type = DifferenceEventType.DELETION
    elif event_type is DifferenceEventType.DELETION:
        event_type = DifferenceEventType.INSERTION
    return AlignedDifferenceEvent(
        type=event_type,
        msa_column_start=event.msa_column_start,
        msa_column_end=event.msa_column_end,
        length=event.length,
        left_start=event.right_start,
        left_end=event.right_end,
        right_start=event.left_start,
        right_end=event.left_end,
        after_left_position=event.after_right_position,
        before_left_position=event.before_right_position,
        after_right_position=event.after_left_position,
        before_right_position=event.before_left_position,
    )


def _event_count(
    events: tuple[AlignedDifferenceEvent, ...],
    event_type: DifferenceEventType,
) -> int:
    return sum(1 for event in events if event.type is event_type)


def _validate_symbol(
    symbol: str,
    *,
    side: str,
    position: int,
    identity: ComparisonIdentity,
) -> None:
    if symbol in ALIGNED_NUCLEOTIDE_SYMBOLS:
        return
    raise AlignedComparisonError(
        code=ComparisonErrorCode.ALIGNED_SYMBOL_INVALID,
        detail="Aligned sequence contains an unsupported symbol.",
        safe_details={
            "side": side,
            "position": position,
            "sample_id": identity.sample_id,
        },
    )


def _is_ambiguous(symbol: str) -> bool:
    return symbol != _GAP and symbol not in _DEFINITE_SYMBOLS


def _definite_symbols_match(
    left: str,
    right: str,
    *,
    uracil_thymine_equivalent: bool,
) -> bool:
    if left == right:
        return True
    return uracil_thymine_equivalent and {left, right} == {"T", "U"}


def _validate_optional_span(
    start: int | None,
    end: int | None,
    *,
    side: str,
) -> None:
    if (start is None) != (end is None):
        raise ValueError(f"{side} span must provide both boundaries or neither")
    if start is not None and end is not None and end < start:
        raise ValueError(f"{side} span end must not precede its start")
