from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

_DEFINITE_SYMBOLS = frozenset("ACGTU")
_GAP_SYMBOL = "-"


@dataclass(frozen=True, slots=True)
class PairDistanceComputation:
    mismatch_count: int
    comparable_site_count: int
    excluded_gap_site_count: int
    excluded_ambiguous_site_count: int
    distance: float | None

    @property
    def classified_site_count(self) -> int:
        return (
            self.comparable_site_count
            + self.excluded_gap_site_count
            + self.excluded_ambiguous_site_count
        )


def expected_pair_count(sequence_count: int) -> int:
    if sequence_count < 0:
        raise ValueError("sequence_count must be >= 0")
    return (sequence_count * (sequence_count - 1)) // 2


def iter_unordered_index_pairs(sequence_count: int) -> Iterator[tuple[int, int]]:
    if sequence_count < 0:
        raise ValueError("sequence_count must be >= 0")
    for left_index in range(sequence_count):
        for right_index in range(left_index + 1, sequence_count):
            yield left_index, right_index


def initialize_distance_matrix(sequence_count: int) -> list[list[float | None]]:
    if sequence_count < 0:
        raise ValueError("sequence_count must be >= 0")
    matrix = [[None for _ in range(sequence_count)] for _ in range(sequence_count)]
    for index in range(sequence_count):
        matrix[index][index] = 0.0
    return matrix


def set_symmetric_distance(
    matrix: list[list[float | None]],
    *,
    left_index: int,
    right_index: int,
    distance: float | None,
) -> None:
    if left_index < 0 or right_index < 0:
        raise ValueError("matrix indexes must be >= 0")
    if left_index >= len(matrix) or right_index >= len(matrix):
        raise ValueError("matrix indexes must be within matrix dimensions")
    if left_index == right_index:
        raise ValueError("matrix indexes must describe an off-diagonal pair")
    matrix[left_index][right_index] = distance
    matrix[right_index][left_index] = distance


def compute_p_distance_pair(
    *,
    left_aligned_sequence: str,
    right_aligned_sequence: str,
    control_check: Callable[[], None] | None = None,
    control_check_interval: int = 1024,
    uracil_thymine_equivalent: bool = True,
) -> PairDistanceComputation:
    if len(left_aligned_sequence) != len(right_aligned_sequence):
        raise ValueError("aligned pair rows must have the same length")
    if control_check_interval <= 0:
        raise ValueError("control_check_interval must be > 0")

    mismatch_count = 0
    comparable_site_count = 0
    excluded_gap_site_count = 0
    excluded_ambiguous_site_count = 0

    for column_index, (left_symbol, right_symbol) in enumerate(
        zip(left_aligned_sequence, right_aligned_sequence, strict=True),
        start=1,
    ):
        if control_check is not None and column_index % control_check_interval == 0:
            control_check()

        if left_symbol == _GAP_SYMBOL or right_symbol == _GAP_SYMBOL:
            excluded_gap_site_count += 1
            continue

        if left_symbol not in _DEFINITE_SYMBOLS or right_symbol not in _DEFINITE_SYMBOLS:
            excluded_ambiguous_site_count += 1
            continue

        comparable_site_count += 1
        if not _definite_symbols_match(
            left_symbol=left_symbol,
            right_symbol=right_symbol,
            uracil_thymine_equivalent=uracil_thymine_equivalent,
        ):
            mismatch_count += 1

    classified_site_count = (
        comparable_site_count
        + excluded_gap_site_count
        + excluded_ambiguous_site_count
    )
    if classified_site_count != len(left_aligned_sequence):
        raise ValueError("pair classification counts must cover the entire alignment length")

    distance = (
        None
        if comparable_site_count == 0
        else mismatch_count / comparable_site_count
    )
    return PairDistanceComputation(
        mismatch_count=mismatch_count,
        comparable_site_count=comparable_site_count,
        excluded_gap_site_count=excluded_gap_site_count,
        excluded_ambiguous_site_count=excluded_ambiguous_site_count,
        distance=distance,
    )


def matrix_to_tuple(
    matrix: Sequence[Sequence[float | None]],
) -> tuple[tuple[float | None, ...], ...]:
    return tuple(tuple(row) for row in matrix)


def serialize_distance_matrix_tsv(
    *,
    sequence_ids: Sequence[str],
    matrix: Sequence[Sequence[float | None]],
    undefined_marker: str = "NA",
) -> str:
    size = len(sequence_ids)
    if len(matrix) != size:
        raise ValueError("matrix row count must match sequence_ids length")
    if undefined_marker.strip() == "":
        raise ValueError("undefined_marker must not be empty")

    lines = ["\t".join(("sequence_id", *sequence_ids))]
    for row_index, sequence_id in enumerate(sequence_ids):
        row = matrix[row_index]
        if len(row) != size:
            raise ValueError("matrix must be square")
        serialized_row = [sequence_id]
        for column_index, value in enumerate(row):
            if row_index == column_index:
                if value is None:
                    raise ValueError("matrix diagonal values must be defined")
                serialized_row.append("0")
                continue
            if value is None:
                serialized_row.append(undefined_marker)
                continue
            if value < 0.0 or value > 1.0:
                raise ValueError("distance values must be within [0, 1]")
            serialized_row.append(format(value, ".15g"))
        lines.append("\t".join(serialized_row))
    return "\n".join(lines) + "\n"


def _definite_symbols_match(
    *,
    left_symbol: str,
    right_symbol: str,
    uracil_thymine_equivalent: bool,
) -> bool:
    if left_symbol == right_symbol:
        return True
    if not uracil_thymine_equivalent:
        return False
    return {left_symbol, right_symbol} == {"T", "U"}


__all__ = [
    "PairDistanceComputation",
    "compute_p_distance_pair",
    "expected_pair_count",
    "initialize_distance_matrix",
    "iter_unordered_index_pairs",
    "matrix_to_tuple",
    "serialize_distance_matrix_tsv",
    "set_symmetric_distance",
]
