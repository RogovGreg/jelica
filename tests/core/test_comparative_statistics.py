from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jelica_core.comparative_analysis.errors import ComparisonErrorCode
from jelica_core.comparative_analysis.statistics import (
    ComparativeStatisticsError,
    ComparisonAvailability,
    DatasetStatisticalSummarizer,
    RelativeDeltaAvailability,
    SequenceFactsComparison,
    SequenceFactsDatasetSummary,
    StatisticalComparator,
    SummaryAvailability,
)
from jelica_core.config import AnalysisKmerStrand
from jelica_core.runtime.input_processing_models import (
    KmerQuerySummary,
    SequenceBaseCounts,
    SequenceFacts,
)

_AMBIGUOUS_SYMBOLS = frozenset({"R", "Y", "S", "W", "K", "M", "B", "D", "H", "V", "N"})
_CANONICAL_SYMBOLS = frozenset({"A", "C", "G", "T", "U"})


def _kmer(
    query: str,
    *,
    definite: int,
    possible: int,
    strand: AnalysisKmerStrand = AnalysisKmerStrand.FORWARD,
    hits_path: str | None = None,
) -> KmerQuerySummary:
    return KmerQuerySummary(
        query=query,
        definite_match_count=definite,
        possible_match_count=possible,
        strand=strand,
        hits_path=hits_path,
    )


def _facts(
    symbol_counts: dict[str, int],
    *,
    source_length: int | None = None,
    gap_count: int = 0,
    invalid_symbol_counts: dict[str, int] | None = None,
    invalid_positions_truncated: bool = False,
    gc_content_total: float | None = None,
    resolved_gc_content: float | None = None,
    expected_gc_count: float | None = None,
    expected_gc_content: float | None = None,
    kmers: tuple[KmerQuerySummary, ...] = (),
    sequence_id: str | None = None,
) -> SequenceFacts:
    invalid_counts = invalid_symbol_counts or {}
    canonical_count = sum(symbol_counts.get(symbol, 0) for symbol in _CANONICAL_SYMBOLS)
    ambiguous_count = sum(symbol_counts.get(symbol, 0) for symbol in _AMBIGUOUS_SYMBOLS)
    recognized_count = canonical_count + ambiguous_count
    invalid_count = sum(invalid_counts.values())
    resolved_source_length = (
        source_length
        if source_length is not None
        else recognized_count + gap_count + invalid_count
    )
    gc_count = symbol_counts.get("G", 0) + symbol_counts.get("C", 0)
    resolved_gc_total = gc_content_total
    if resolved_gc_total is None and resolved_source_length > 0:
        resolved_gc_total = gc_count / resolved_source_length
    resolved_gc = resolved_gc_content
    if resolved_gc is None and canonical_count > 0:
        resolved_gc = gc_count / canonical_count
    resolved_expected_count = (
        float(gc_count) if expected_gc_count is None else expected_gc_count
    )
    resolved_expected_content = expected_gc_content
    if resolved_expected_content is None and recognized_count > 0:
        resolved_expected_content = resolved_expected_count / recognized_count

    return SequenceFacts(
        source_length=resolved_source_length,
        ungapped_length=resolved_source_length - gap_count,
        recognized_nucleotide_count=recognized_count,
        symbol_counts=symbol_counts,
        base_counts=SequenceBaseCounts.from_symbol_counts(symbol_counts),
        canonical_count=canonical_count,
        ambiguous_count=ambiguous_count,
        gap_count=gap_count,
        invalid_symbol_count=invalid_count,
        invalid_symbol_counts=invalid_counts,
        invalid_positions=(),
        invalid_positions_truncated=invalid_positions_truncated,
        gc_count=gc_count,
        gc_content_total=resolved_gc_total,
        resolved_gc_content=resolved_gc,
        expected_gc_count=resolved_expected_count,
        expected_gc_content=resolved_expected_content,
        u_count=symbol_counts.get("U", 0),
        sequence_id=sequence_id,
        kmer_summaries=kmers,
    )


def _comparison_component(distribution: object, component: str) -> object:
    components = getattr(distribution, "components")
    return next(item for item in components if item.component == component)


def _summary_component(distribution: object, component: str) -> object:
    components = getattr(distribution, "components")
    return next(item for item in components if item.component == component)


def test_count_delta_is_right_minus_left_and_zero_is_not_missing() -> None:
    left = _facts({}, source_length=0, expected_gc_count=0.0)
    right = _facts({"A": 4}, source_length=4)

    result = StatisticalComparator().compare(left, right)

    source_length = result.counts.source_length
    assert source_length.status is ComparisonAvailability.AVAILABLE
    assert source_length.left == 0
    assert source_length.right == 4
    assert source_length.delta == 4
    assert source_length.relative_delta is None
    assert source_length.relative_delta_status is RelativeDeltaAvailability.LEFT_ZERO

    missing_left = StatisticalComparator().compare(None, left).counts.source_length
    assert missing_left.status is ComparisonAvailability.LEFT_MISSING
    assert missing_left.left is None
    assert missing_left.right == 0
    assert missing_left.delta is None


def test_count_relative_delta_uses_left_value_as_denominator() -> None:
    left = _facts({"A": 2})
    right = _facts({"A": 3})

    comparison = StatisticalComparator().compare(left, right).counts.canonical_count

    assert comparison.delta == 1
    assert comparison.relative_delta == pytest.approx(0.5)
    assert comparison.relative_delta_status is RelativeDeltaAvailability.AVAILABLE


def test_gc_proportion_comparison_includes_percentage_points() -> None:
    left = _facts(
        {"A": 3, "G": 1},
        gc_content_total=0.25,
        resolved_gc_content=0.25,
        expected_gc_content=0.25,
    )
    right = _facts(
        {"A": 2, "G": 2},
        gc_content_total=0.5,
        resolved_gc_content=0.5,
        expected_gc_content=0.5,
    )

    comparison = StatisticalComparator().compare(left, right).proportions.gc_content_total

    assert comparison.status is ComparisonAvailability.AVAILABLE
    assert comparison.delta == pytest.approx(0.25)
    assert comparison.percentage_point_delta == pytest.approx(25.0)


def test_symbol_and_base_distributions_compare_components_without_rescanning() -> None:
    left = _facts({"A": 1, "R": 1})
    right = _facts({"A": 1, "G": 1})

    result = StatisticalComparator().compare(left, right)

    ambiguous = _comparison_component(result.symbol_counts, "R")
    assert ambiguous.comparison.left == 1
    assert ambiguous.comparison.right == 0
    assert ambiguous.comparison.delta == -1
    assert ambiguous.comparison.relative_delta == pytest.approx(-1.0)

    definite_a = _comparison_component(result.base_counts.definite, "A")
    potential_a = _comparison_component(result.base_counts.potential, "A")
    assert definite_a.comparison.delta == 0
    assert potential_a.comparison.left == 2
    assert potential_a.comparison.right == 1
    assert potential_a.comparison.delta == -1


def test_kmer_and_quality_comparisons_are_typed_and_omit_technical_fields() -> None:
    left = _facts(
        {"A": 2},
        invalid_positions_truncated=False,
        kmers=(
            _kmer(
                "AA",
                definite=1,
                possible=0,
                hits_path="input_processing/kmer_hits/left.json",
            ),
        ),
        sequence_id="sha256:" + ("a" * 64),
    )
    right = _facts(
        {"A": 2},
        invalid_positions_truncated=True,
        kmers=(
            _kmer(
                "AA",
                definite=2,
                possible=1,
                strand=AnalysisKmerStrand.BOTH,
                hits_path="input_processing/kmer_hits/right.json",
            ),
            _kmer("AR", definite=0, possible=0),
        ),
        sequence_id="sha256:" + ("b" * 64),
    )

    result = StatisticalComparator().compare(left, right)

    assert result.invalid_positions_truncated.left is False
    assert result.invalid_positions_truncated.right is True
    assert result.invalid_positions_truncated.equal is False
    assert result.kmer_summaries[0].query == "AA"
    assert result.kmer_summaries[0].strand.left == "forward"
    assert result.kmer_summaries[0].strand.right == "both"
    assert result.kmer_summaries[0].strand.equal is False
    assert result.kmer_summaries[1].status is ComparisonAvailability.LEFT_MISSING
    assert result.kmer_summaries[1].definite_match_count.right == 0

    equal = StatisticalComparator().compare(left, left)
    assert equal.kmer_summaries[0].strand.equal is True

    payload = result.model_dump(mode="json")
    encoded = json.dumps(payload, allow_nan=False)
    assert "sequence_id" not in encoded
    assert "hits_path" not in encoded
    assert "invalid_positions" not in payload
    assert "invalid_positions_truncated" in payload


def test_kmer_validation_error_never_contains_query_literal() -> None:
    query = "PRIVATE_QUERY_LITERAL"
    facts = _facts(
        {"A": 1},
        kmers=(
            _kmer(query, definite=0, possible=0),
            _kmer(query, definite=0, possible=0),
        ),
    )

    with pytest.raises(ComparativeStatisticsError) as error_info:
        StatisticalComparator().compare(facts, None)

    assert error_info.value.code is ComparisonErrorCode.STATISTICS_KMER_DUPLICATE
    assert query not in str(error_info.value)


def test_missing_statistics_are_distinct_on_each_side_and_when_both_missing() -> None:
    facts = _facts({"A": 1})

    missing_left = StatisticalComparator().compare(None, facts)
    missing_right = StatisticalComparator().compare(facts, None)
    missing_both = StatisticalComparator().compare(None, None)

    assert missing_left.status is ComparisonAvailability.LEFT_MISSING
    assert missing_right.status is ComparisonAvailability.RIGHT_MISSING
    assert missing_both.status is ComparisonAvailability.BOTH_MISSING
    assert missing_right.counts.source_length.left == 1
    assert missing_right.counts.source_length.right is None
    assert missing_both.counts.source_length.delta is None
    missing_a = _comparison_component(missing_both.base_counts.definite, "A")
    assert missing_a.comparison.status is ComparisonAvailability.BOTH_MISSING


def test_dataset_numeric_summary_handles_zero_and_one_available_values() -> None:
    empty = DatasetStatisticalSummarizer().summarize(())
    empty_length = empty.numeric.source_length
    assert empty.sample_count == 0
    assert empty_length.status is SummaryAvailability.MISSING
    assert empty_length.available_count == 0
    assert empty_length.missing_count == 0
    assert empty_length.minimum is None
    assert empty_length.population_standard_deviation is None

    single = DatasetStatisticalSummarizer().summarize((_facts({"A": 3}),))
    single_length = single.numeric.source_length
    assert single_length.minimum == 3.0
    assert single_length.maximum == 3.0
    assert single_length.mean == 3.0
    assert single_length.median == 3.0
    assert single_length.population_standard_deviation == 0.0


def test_dataset_summary_counts_duplicate_logical_occurrences_and_missing_facts() -> None:
    duplicate = _facts({"A": 2}, invalid_positions_truncated=False)
    other = _facts({"G": 4}, invalid_positions_truncated=True)

    result = DatasetStatisticalSummarizer().summarize(
        (duplicate, duplicate, other, None)
    )

    source_length = result.numeric.source_length
    assert result.sample_count == 4
    assert result.facts_available_count == 3
    assert result.facts_missing_count == 1
    assert source_length.available_count == 3
    assert source_length.missing_count == 1
    assert source_length.minimum == 2.0
    assert source_length.maximum == 4.0
    assert source_length.mean == pytest.approx(8.0 / 3.0)
    assert source_length.median == 2.0
    assert source_length.population_standard_deviation == pytest.approx(
        (8.0 / 9.0) ** 0.5
    )

    a_counts = _summary_component(result.symbol_counts, "A").summary
    assert a_counts.available_count == 3
    assert a_counts.missing_count == 1
    assert a_counts.mean == pytest.approx(4.0 / 3.0)

    flag = result.invalid_positions_truncated
    assert flag.false_count == 2
    assert flag.true_count == 1
    assert flag.missing_count == 1


def test_dataset_kmer_summary_treats_absent_query_as_missing_not_zero() -> None:
    with_query = _facts(
        {"A": 2},
        kmers=(_kmer("AA", definite=1, possible=0),),
    )
    without_query = _facts({"C": 2})

    result = DatasetStatisticalSummarizer().summarize(
        (with_query, with_query, without_query, None)
    )
    summary = result.kmer_summaries[0]

    assert summary.query == "AA"
    assert summary.definite_match_count.available_count == 2
    assert summary.definite_match_count.missing_count == 2
    assert summary.definite_match_count.mean == 1.0
    assert summary.strand.available_count == 2
    assert summary.strand.missing_count == 2
    assert [(item.value, item.count) for item in summary.strand.counts] == [
        ("forward", 2)
    ]


def test_dataset_summary_covers_iupac_and_base_components_without_technical_data() -> None:
    shared_id = "sha256:" + ("c" * 64)
    facts = _facts(
        {"A": 1, "R": 1},
        sequence_id=shared_id,
        kmers=(
            _kmer(
                "AR",
                definite=0,
                possible=1,
                hits_path="input_processing/kmer_hits/shared.json",
            ),
        ),
    )

    result = DatasetStatisticalSummarizer().summarize((facts, facts))

    definite_a = _summary_component(result.base_counts.definite, "A").summary
    potential_a = _summary_component(result.base_counts.potential, "A").summary
    ambiguous_r = _summary_component(result.symbol_counts, "R").summary
    absent_n = _summary_component(result.symbol_counts, "N").summary
    assert result.sample_count == 2
    assert definite_a.mean == 1.0
    assert potential_a.mean == 2.0
    assert ambiguous_r.mean == 1.0
    assert absent_n.mean == 0.0

    encoded = json.dumps(result.model_dump(mode="json"), allow_nan=False)
    assert "sequence_id" not in encoded
    assert "hits_path" not in encoded
    assert '"invalid_positions":' not in encoded


def test_comparative_statistic_contracts_round_trip_and_are_frozen() -> None:
    comparison = StatisticalComparator().compare(_facts({"A": 1}), None)
    summary = DatasetStatisticalSummarizer().summarize((_facts({"A": 1}), None))

    comparison_payload = comparison.model_dump(mode="json")
    summary_payload = summary.model_dump(mode="json")
    restored_comparison = SequenceFactsComparison.model_validate(comparison_payload)
    restored_summary = SequenceFactsDatasetSummary.model_validate(summary_payload)

    assert restored_comparison == comparison
    assert restored_summary == summary
    with pytest.raises(ValidationError):
        comparison.status = ComparisonAvailability.AVAILABLE  # type: ignore[misc]
