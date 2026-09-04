from __future__ import annotations

import pytest

from jelica_core.distance_matrix import (
    compute_p_distance_pair,
    expected_pair_count,
    initialize_distance_matrix,
    iter_unordered_index_pairs,
    serialize_distance_matrix_tsv,
    set_symmetric_distance,
)


def test_identical_rows_have_zero_distance() -> None:
    result = compute_p_distance_pair(
        left_aligned_sequence="ACGT",
        right_aligned_sequence="ACGT",
    )

    assert result.mismatch_count == 0
    assert result.comparable_site_count == 4
    assert result.excluded_gap_site_count == 0
    assert result.excluded_ambiguous_site_count == 0
    assert result.distance == 0.0


def test_single_substitution_computes_expected_ratio() -> None:
    result = compute_p_distance_pair(
        left_aligned_sequence="ACGT",
        right_aligned_sequence="ATGT",
    )

    assert result.mismatch_count == 1
    assert result.comparable_site_count == 4
    assert result.distance == pytest.approx(0.25)


def test_t_and_u_are_treated_as_equivalent_on_comparable_sites() -> None:
    result = compute_p_distance_pair(
        left_aligned_sequence="TTUU",
        right_aligned_sequence="UUTT",
    )

    assert result.mismatch_count == 0
    assert result.comparable_site_count == 4
    assert result.distance == 0.0


def test_gap_has_priority_and_is_excluded_from_comparable_sites() -> None:
    result = compute_p_distance_pair(
        left_aligned_sequence="-N",
        right_aligned_sequence="AN",
    )

    assert result.excluded_gap_site_count == 1
    assert result.excluded_ambiguous_site_count == 1
    assert result.comparable_site_count == 0
    assert result.distance is None


def test_ambiguous_iupac_and_n_symbols_are_excluded() -> None:
    result = compute_p_distance_pair(
        left_aligned_sequence="ARN",
        right_aligned_sequence="ACY",
    )

    assert result.comparable_site_count == 1
    assert result.mismatch_count == 0
    assert result.excluded_ambiguous_site_count == 2
    assert result.distance == 0.0


def test_no_comparable_sites_returns_null_distance() -> None:
    result = compute_p_distance_pair(
        left_aligned_sequence="N--R",
        right_aligned_sequence="Y--N",
    )

    assert result.comparable_site_count == 0
    assert result.distance is None


def test_pair_classification_counts_cover_entire_alignment() -> None:
    left = "ACG-TN"
    right = "ATG-UN"
    result = compute_p_distance_pair(left_aligned_sequence=left, right_aligned_sequence=right)

    assert result.classified_site_count == len(left) == len(right)


def test_unordered_pair_iteration_is_stable_and_one_direction_only() -> None:
    pairs = list(iter_unordered_index_pairs(4))

    assert pairs == [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
    assert len(pairs) == expected_pair_count(4)


def test_matrix_helpers_keep_diagonal_zero_and_symmetric_distances() -> None:
    matrix = initialize_distance_matrix(3)
    set_symmetric_distance(matrix, left_index=0, right_index=1, distance=0.5)
    set_symmetric_distance(matrix, left_index=0, right_index=2, distance=None)

    assert matrix[0][0] == 0.0
    assert matrix[1][1] == 0.0
    assert matrix[2][2] == 0.0
    assert matrix[0][1] == matrix[1][0] == 0.5
    assert matrix[0][2] == matrix[2][0] is None


def test_tsv_serialization_is_deterministic_and_marks_undefined_distance() -> None:
    matrix = initialize_distance_matrix(2)
    set_symmetric_distance(matrix, left_index=0, right_index=1, distance=None)

    payload = serialize_distance_matrix_tsv(
        sequence_ids=("seq-a", "seq-b"),
        matrix=matrix,
    )

    assert payload == (
        "sequence_id\tseq-a\tseq-b\n"
        "seq-a\t0\tNA\n"
        "seq-b\tNA\t0\n"
    )
