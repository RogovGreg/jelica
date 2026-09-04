from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jelica_core.comparative_analysis.aligned_comparator import (
    AlignedSequenceComparator,
    ComparisonIdentity,
    DifferenceEventType,
    DirectedAlignedComparison,
    DirectedComparisonProjector,
)
from jelica_core.comparative_analysis.errors import (
    AlignedComparisonError,
    ComparisonErrorCode,
)

_LEFT = ComparisonIdentity(sample_id="sample-left", sequence_id="sequence-left")
_RIGHT = ComparisonIdentity(sample_id="sample-right", sequence_id="sequence-right")


def _compare(
    left: str,
    right: str,
    *,
    equivalent: bool = False,
    same_sequence_id: bool = False,
) -> DirectedAlignedComparison:
    right_identity = (
        ComparisonIdentity(
            sample_id=_RIGHT.sample_id,
            sequence_id=_LEFT.sequence_id,
        )
        if same_sequence_id
        else _RIGHT
    )
    return AlignedSequenceComparator().compare(
        left_aligned_sequence=left,
        right_aligned_sequence=right,
        left_identity=_LEFT,
        right_identity=right_identity,
        uracil_thymine_equivalent=equivalent,
    )


def test_complete_match_has_unit_identity_and_no_events() -> None:
    result = _compare("ACGTU", "ACGTU")

    assert result.events == ()
    assert result.summary.comparable_base_count == 5
    assert result.summary.matching_base_count == 5
    assert result.summary.identity_on_comparable_bases == 1.0


def test_single_substitution_is_directed_event() -> None:
    result = _compare("AC", "AT")

    assert [event.type for event in result.events] == [
        DifferenceEventType.SUBSTITUTION
    ]
    assert result.events[0].msa_column_start == 2
    assert result.events[0].left_start == 2
    assert result.events[0].right_start == 2
    assert result.summary.substitution_event_count == 1
    assert result.summary.substituted_base_count == 1


def test_consecutive_substitutions_merge_into_one_event() -> None:
    result = _compare("AAA", "ACC")

    assert len(result.events) == 1
    assert result.events[0].type is DifferenceEventType.SUBSTITUTION
    assert result.events[0].msa_column_start == 2
    assert result.events[0].msa_column_end == 3
    assert result.events[0].length == 2
    assert result.summary.substitution_event_count == 1
    assert result.summary.substituted_base_count == 2


def test_single_base_insertion_is_relative_to_left() -> None:
    result = _compare("A-C", "ATC")

    event = result.events[0]
    assert event.type is DifferenceEventType.INSERTION
    assert event.left_start is None
    assert event.right_start == 2
    assert result.summary.inserted_base_count == 1


def test_multibase_insertion_merges_and_counts_affected_bases() -> None:
    result = _compare("A--C", "ATTC")

    assert result.summary.insertion_event_count == 1
    assert result.summary.inserted_base_count == 2
    assert result.events[0].length == 2


def test_single_base_deletion_is_relative_to_left() -> None:
    result = _compare("ATC", "A-C")

    event = result.events[0]
    assert event.type is DifferenceEventType.DELETION
    assert event.left_start == 2
    assert event.right_start is None
    assert result.summary.deleted_base_count == 1


def test_multibase_deletion_merges_and_counts_affected_bases() -> None:
    result = _compare("ATTC", "A--C")

    assert result.summary.deletion_event_count == 1
    assert result.summary.deleted_base_count == 2
    assert result.events[0].left_start == 2
    assert result.events[0].left_end == 3


def test_match_splits_events_of_the_same_type() -> None:
    result = _compare("ACA", "TCT")

    assert [event.type for event in result.events] == [
        DifferenceEventType.SUBSTITUTION,
        DifferenceEventType.SUBSTITUTION,
    ]
    assert [event.msa_column_start for event in result.events] == [1, 3]


def test_gap_gap_is_ignored_and_not_comparable() -> None:
    result = _compare("A-C", "A-C")

    assert result.events == ()
    assert result.summary.both_gap_column_count == 1
    assert result.summary.comparable_base_count == 2


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("N", "A"),
        ("R", "A"),
        ("R", "C"),
        ("-", "N"),
    ],
)
def test_any_ambiguous_column_is_uncertain(left: str, right: str) -> None:
    result = _compare(left, right)

    assert result.events[0].type is DifferenceEventType.UNCERTAIN
    assert result.summary.uncertain_event_count == 1
    assert result.summary.uncertain_column_count == 1
    assert result.summary.comparable_base_count == 0


def test_consecutive_uncertain_columns_merge() -> None:
    result = _compare("RN", "AY")

    assert len(result.events) == 1
    assert result.events[0].type is DifferenceEventType.UNCERTAIN
    assert result.events[0].length == 2
    assert result.summary.uncertain_column_count == 2


def test_uracil_and_thymine_are_substitution_by_default() -> None:
    result = _compare("U", "T")

    assert result.summary.substituted_base_count == 1
    assert result.summary.matching_base_count == 0


def test_uracil_and_thymine_can_be_equivalent_by_policy() -> None:
    result = _compare("U", "T", equivalent=True)

    assert result.events == ()
    assert result.summary.matching_base_count == 1
    assert result.summary.identity_on_comparable_bases == 1.0


def test_unequal_lengths_raise_safe_structured_error() -> None:
    left = "GG"
    right = "G"

    with pytest.raises(AlignedComparisonError) as error_info:
        _compare(left, right)

    assert error_info.value.code is ComparisonErrorCode.ALIGNED_LENGTH_MISMATCH
    assert left not in str(error_info.value)
    assert right not in str(error_info.value)
    assert error_info.value.safe_details == {"left_length": 2, "right_length": 1}


def test_invalid_symbol_error_never_contains_sequence_or_symbol() -> None:
    invalid_symbol = "X"
    left = f"A{invalid_symbol}"
    right = "AC"

    with pytest.raises(AlignedComparisonError) as error_info:
        _compare(left, right)

    assert error_info.value.code is ComparisonErrorCode.ALIGNED_SYMBOL_INVALID
    assert left not in str(error_info.value)
    assert invalid_symbol not in str(error_info.value)
    assert invalid_symbol not in repr(error_info.value.safe_details)
    assert error_info.value.safe_details["position"] == 2


def test_identity_uses_only_definite_comparable_bases() -> None:
    result = _compare("AC-N", "ATGN")

    assert result.summary.matching_base_count == 1
    assert result.summary.substituted_base_count == 1
    assert result.summary.comparable_base_count == 2
    assert result.summary.identity_on_comparable_bases == pytest.approx(0.5)
    assert result.summary.inserted_base_count == 1
    assert result.summary.uncertain_column_count == 1


def test_comparable_count_invariant_is_serialized() -> None:
    result = _compare("ACG", "ATG")
    summary = result.summary

    assert summary.comparable_base_count == (
        summary.matching_base_count + summary.substituted_base_count
    )
    payload = result.model_dump(mode="json")
    assert DirectedAlignedComparison.model_validate(payload) == result
    json.dumps(payload, allow_nan=False)


def test_identity_is_null_when_no_definite_comparable_bases() -> None:
    result = _compare("N-", "A-")

    assert result.summary.comparable_base_count == 0
    assert result.summary.identity_on_comparable_bases is None


def test_event_count_and_affected_base_count_are_distinct() -> None:
    result = _compare("A--C", "ATTC")

    assert result.summary.insertion_event_count == 1
    assert result.summary.inserted_base_count == 2


def test_insertion_at_start_uses_before_first_anchor() -> None:
    result = _compare("--A", "CCA")
    event = result.events[0]

    assert event.msa_column_start == 1
    assert event.msa_column_end == 2
    assert event.after_left_position is None
    assert event.before_left_position == 1
    assert event.right_start == 1
    assert event.right_end == 2


def test_insertion_in_middle_uses_two_sided_anchor() -> None:
    result = _compare("A--C", "ATTC")
    event = result.events[0]

    assert event.after_left_position == 1
    assert event.before_left_position == 2
    assert event.right_start == 2
    assert event.right_end == 3


def test_insertion_at_end_uses_after_last_anchor() -> None:
    result = _compare("A--", "ATT")
    event = result.events[0]

    assert event.after_left_position == 1
    assert event.before_left_position is None
    assert event.right_start == 2
    assert event.right_end == 3


def test_reverse_projection_flips_indel_and_coordinates() -> None:
    forward = _compare("A-", "AC")

    reverse = DirectedComparisonProjector().reverse(forward)

    assert reverse.left == forward.right
    assert reverse.right == forward.left
    assert reverse.events[0].type is DifferenceEventType.DELETION
    assert reverse.events[0].left_start == 2
    assert reverse.events[0].right_start is None
    assert reverse.events[0].after_right_position == 1
    assert reverse.events[0].before_right_position is None
    assert reverse.summary.deletion_event_count == 1
    assert reverse.summary.insertion_event_count == 0


def test_identical_sequence_id_shortcut_has_no_confirmed_differences() -> None:
    result = _compare("AN-", "AN-", same_sequence_id=True)

    assert result.identical_sequence_shortcut is True
    assert result.summary.substituted_base_count == 0
    assert result.summary.inserted_base_count == 0
    assert result.summary.deleted_base_count == 0
    assert result.summary.matching_base_count == 1
    assert result.summary.uncertain_column_count == 1
    assert result.summary.both_gap_column_count == 1


def test_result_contract_is_frozen() -> None:
    result = _compare("A", "A")

    with pytest.raises(ValidationError):
        result.events = ()  # type: ignore[misc]
