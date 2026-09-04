from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from jelica_core.runtime.input_processing_models import (
    KmerQuerySummary,
    SequenceFacts,
)

from .errors import (
    ComparativeStatisticsError,
    ComparisonErrorCode,
)

_SYMBOL_ORDER: Final[tuple[str, ...]] = (
    "A",
    "C",
    "G",
    "T",
    "U",
    "R",
    "Y",
    "S",
    "W",
    "K",
    "M",
    "B",
    "D",
    "H",
    "V",
    "N",
)
_BASE_ORDER: Final[tuple[str, ...]] = ("A", "C", "G", "T", "U")


class ComparisonAvailability(StrEnum):
    AVAILABLE = "available"
    LEFT_MISSING = "left_missing"
    RIGHT_MISSING = "right_missing"
    BOTH_MISSING = "both_missing"


class RelativeDeltaAvailability(StrEnum):
    AVAILABLE = "available"
    LEFT_ZERO = "left_zero"
    MISSING = "missing"


class SummaryAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"


def _comparison_availability(
    left: object | None,
    right: object | None,
) -> ComparisonAvailability:
    if left is None and right is None:
        return ComparisonAvailability.BOTH_MISSING
    if left is None:
        return ComparisonAvailability.LEFT_MISSING
    if right is None:
        return ComparisonAvailability.RIGHT_MISSING
    return ComparisonAvailability.AVAILABLE


def _floats_equal(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


class CountDelta(BaseModel):
    """Delta for a non-negative integer count, calculated as right minus left."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ComparisonAvailability
    left: int | None = Field(default=None, ge=0)
    right: int | None = Field(default=None, ge=0)
    delta: int | None = None
    relative_delta: FiniteFloat | None = None
    relative_delta_status: RelativeDeltaAvailability

    @model_validator(mode="after")
    def _validate_delta(self) -> CountDelta:
        expected_status = _comparison_availability(self.left, self.right)
        if self.status is not expected_status:
            raise ValueError("status must match count availability")
        if expected_status is not ComparisonAvailability.AVAILABLE:
            if self.delta is not None or self.relative_delta is not None:
                raise ValueError("missing counts cannot have deltas")
            if self.relative_delta_status is not RelativeDeltaAvailability.MISSING:
                raise ValueError("relative delta must be missing when a count is missing")
            return self

        if self.left is None or self.right is None:
            raise ValueError("available counts require both values")
        if self.delta != self.right - self.left:
            raise ValueError("count delta must equal right minus left")
        if self.left == 0:
            if self.relative_delta is not None:
                raise ValueError("relative delta is undefined for a zero left count")
            if self.relative_delta_status is not RelativeDeltaAvailability.LEFT_ZERO:
                raise ValueError("zero left count requires left_zero relative status")
            return self

        expected_relative = (self.right - self.left) / self.left
        if self.relative_delta is None or not _floats_equal(
            self.relative_delta,
            expected_relative,
        ):
            raise ValueError("relative count delta is inconsistent")
        if self.relative_delta_status is not RelativeDeltaAvailability.AVAILABLE:
            raise ValueError("relative count delta must be available")
        return self


class NumericDelta(BaseModel):
    """Delta for a non-negative numeric value, calculated as right minus left."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ComparisonAvailability
    left: FiniteFloat | None = Field(default=None, ge=0.0)
    right: FiniteFloat | None = Field(default=None, ge=0.0)
    delta: FiniteFloat | None = None
    relative_delta: FiniteFloat | None = None
    relative_delta_status: RelativeDeltaAvailability

    @model_validator(mode="after")
    def _validate_delta(self) -> NumericDelta:
        expected_status = _comparison_availability(self.left, self.right)
        if self.status is not expected_status:
            raise ValueError("status must match numeric value availability")
        if expected_status is not ComparisonAvailability.AVAILABLE:
            if self.delta is not None or self.relative_delta is not None:
                raise ValueError("missing numeric values cannot have deltas")
            if self.relative_delta_status is not RelativeDeltaAvailability.MISSING:
                raise ValueError("relative delta must be missing when a value is missing")
            return self

        if self.left is None or self.right is None:
            raise ValueError("available numeric values require both values")
        expected_delta = self.right - self.left
        if self.delta is None or not _floats_equal(self.delta, expected_delta):
            raise ValueError("numeric delta must equal right minus left")
        if self.left == 0.0:
            if self.relative_delta is not None:
                raise ValueError("relative delta is undefined for a zero left value")
            if self.relative_delta_status is not RelativeDeltaAvailability.LEFT_ZERO:
                raise ValueError("zero left value requires left_zero relative status")
            return self

        expected_relative = expected_delta / self.left
        if self.relative_delta is None or not _floats_equal(
            self.relative_delta,
            expected_relative,
        ):
            raise ValueError("relative numeric delta is inconsistent")
        if self.relative_delta_status is not RelativeDeltaAvailability.AVAILABLE:
            raise ValueError("relative numeric delta must be available")
        return self


class ProportionDelta(BaseModel):
    """Difference between proportions, including percentage-point change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ComparisonAvailability
    left: FiniteFloat | None = Field(default=None, ge=0.0, le=1.0)
    right: FiniteFloat | None = Field(default=None, ge=0.0, le=1.0)
    delta: FiniteFloat | None = Field(default=None, ge=-1.0, le=1.0)
    percentage_point_delta: FiniteFloat | None = Field(
        default=None,
        ge=-100.0,
        le=100.0,
    )

    @model_validator(mode="after")
    def _validate_delta(self) -> ProportionDelta:
        expected_status = _comparison_availability(self.left, self.right)
        if self.status is not expected_status:
            raise ValueError("status must match proportion availability")
        if expected_status is not ComparisonAvailability.AVAILABLE:
            if self.delta is not None or self.percentage_point_delta is not None:
                raise ValueError("missing proportions cannot have deltas")
            return self

        if self.left is None or self.right is None:
            raise ValueError("available proportions require both values")
        expected_delta = self.right - self.left
        if self.delta is None or not _floats_equal(self.delta, expected_delta):
            raise ValueError("proportion delta must equal right minus left")
        if self.percentage_point_delta is None or not _floats_equal(
            self.percentage_point_delta,
            expected_delta * 100.0,
        ):
            raise ValueError("percentage-point delta is inconsistent")
        return self


class CategoricalComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ComparisonAvailability
    left: str | None = None
    right: str | None = None
    equal: bool | None = None

    @model_validator(mode="after")
    def _validate_values(self) -> CategoricalComparison:
        expected_status = _comparison_availability(self.left, self.right)
        if self.status is not expected_status:
            raise ValueError("status must match categorical value availability")
        expected_equal = (
            self.left == self.right
            if expected_status is ComparisonAvailability.AVAILABLE
            else None
        )
        if self.equal is not expected_equal:
            raise ValueError("categorical equality is inconsistent")
        return self


class BooleanComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ComparisonAvailability
    left: bool | None = None
    right: bool | None = None
    equal: bool | None = None

    @model_validator(mode="after")
    def _validate_values(self) -> BooleanComparison:
        expected_status = _comparison_availability(self.left, self.right)
        if self.status is not expected_status:
            raise ValueError("status must match boolean value availability")
        expected_equal = (
            self.left == self.right
            if expected_status is ComparisonAvailability.AVAILABLE
            else None
        )
        if self.equal is not expected_equal:
            raise ValueError("boolean equality is inconsistent")
        return self


class ComponentCountDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str = Field(min_length=1)
    comparison: CountDelta


class CountDistributionComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    components: tuple[ComponentCountDelta, ...] = Field(default_factory=tuple)


class BaseCountDistributionComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definite: CountDistributionComparison
    potential: CountDistributionComparison


class SequenceCountComparisons(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_length: CountDelta
    ungapped_length: CountDelta
    recognized_nucleotide_count: CountDelta
    canonical_count: CountDelta
    ambiguous_count: CountDelta
    gap_count: CountDelta
    invalid_symbol_count: CountDelta
    gc_count: CountDelta
    u_count: CountDelta


class SequenceProportionComparisons(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gc_content_total: ProportionDelta
    resolved_gc_content: ProportionDelta
    expected_gc_content: ProportionDelta


class KmerSummaryComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1)
    status: ComparisonAvailability
    definite_match_count: CountDelta
    possible_match_count: CountDelta
    strand: CategoricalComparison


class SequenceFactsComparison(BaseModel):
    """Pairwise comparison containing only precomputed factual statistics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ComparisonAvailability
    counts: SequenceCountComparisons
    expected_gc_count: NumericDelta
    proportions: SequenceProportionComparisons
    symbol_counts: CountDistributionComparison
    invalid_symbol_counts: CountDistributionComparison
    base_counts: BaseCountDistributionComparison
    invalid_positions_truncated: BooleanComparison
    kmer_summaries: tuple[KmerSummaryComparison, ...] = Field(default_factory=tuple)


class NumericSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SummaryAvailability
    available_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None
    mean: FiniteFloat | None = None
    median: FiniteFloat | None = None
    population_standard_deviation: FiniteFloat | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _validate_summary(self) -> NumericSummary:
        values = (
            self.minimum,
            self.maximum,
            self.mean,
            self.median,
            self.population_standard_deviation,
        )
        if self.available_count == 0:
            if self.status is not SummaryAvailability.MISSING:
                raise ValueError("empty numeric summary must have missing status")
            if any(value is not None for value in values):
                raise ValueError("empty numeric summary cannot contain statistics")
            return self

        if self.status is not SummaryAvailability.AVAILABLE:
            raise ValueError("non-empty numeric summary must have available status")
        if any(value is None for value in values):
            raise ValueError("non-empty numeric summary requires all statistics")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("numeric summary minimum cannot exceed maximum")
        return self


class CategoryCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1)
    count: int = Field(gt=0)


class CategoricalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SummaryAvailability
    available_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    counts: tuple[CategoryCount, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_summary(self) -> CategoricalSummary:
        if sum(item.count for item in self.counts) != self.available_count:
            raise ValueError("category counts must equal available_count")
        expected_status = (
            SummaryAvailability.AVAILABLE
            if self.available_count > 0
            else SummaryAvailability.MISSING
        )
        if self.status is not expected_status:
            raise ValueError("categorical summary status is inconsistent")
        return self


class BooleanSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SummaryAvailability
    available_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    false_count: int = Field(ge=0)
    true_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_summary(self) -> BooleanSummary:
        if self.false_count + self.true_count != self.available_count:
            raise ValueError("boolean counts must equal available_count")
        expected_status = (
            SummaryAvailability.AVAILABLE
            if self.available_count > 0
            else SummaryAvailability.MISSING
        )
        if self.status is not expected_status:
            raise ValueError("boolean summary status is inconsistent")
        return self


class ComponentNumericSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str = Field(min_length=1)
    summary: NumericSummary


class CountDistributionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    components: tuple[ComponentNumericSummary, ...] = Field(default_factory=tuple)


class BaseCountDistributionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definite: CountDistributionSummary
    potential: CountDistributionSummary


class SequenceNumericSummaries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_length: NumericSummary
    ungapped_length: NumericSummary
    recognized_nucleotide_count: NumericSummary
    canonical_count: NumericSummary
    ambiguous_count: NumericSummary
    gap_count: NumericSummary
    invalid_symbol_count: NumericSummary
    gc_count: NumericSummary
    expected_gc_count: NumericSummary
    u_count: NumericSummary
    gc_content_total: NumericSummary
    resolved_gc_content: NumericSummary
    expected_gc_content: NumericSummary


class KmerSummaryDatasetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1)
    definite_match_count: NumericSummary
    possible_match_count: NumericSummary
    strand: CategoricalSummary


class SequenceFactsDatasetSummary(BaseModel):
    """Dataset statistics over logical sample occurrences, including duplicates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_count: int = Field(ge=0)
    facts_available_count: int = Field(ge=0)
    facts_missing_count: int = Field(ge=0)
    numeric: SequenceNumericSummaries
    symbol_counts: CountDistributionSummary
    invalid_symbol_counts: CountDistributionSummary
    base_counts: BaseCountDistributionSummary
    invalid_positions_truncated: BooleanSummary
    kmer_summaries: tuple[KmerSummaryDatasetSummary, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_sample_counts(self) -> SequenceFactsDatasetSummary:
        if self.facts_available_count + self.facts_missing_count != self.sample_count:
            raise ValueError("facts counts must equal sample_count")
        return self


def _count_delta(left: int | None, right: int | None) -> CountDelta:
    status = _comparison_availability(left, right)
    if status is not ComparisonAvailability.AVAILABLE:
        return CountDelta(
            status=status,
            left=left,
            right=right,
            delta=None,
            relative_delta=None,
            relative_delta_status=RelativeDeltaAvailability.MISSING,
        )
    if left is None or right is None:
        raise AssertionError("available count values must be present")
    delta = right - left
    if left == 0:
        return CountDelta(
            status=status,
            left=left,
            right=right,
            delta=delta,
            relative_delta=None,
            relative_delta_status=RelativeDeltaAvailability.LEFT_ZERO,
        )
    return CountDelta(
        status=status,
        left=left,
        right=right,
        delta=delta,
        relative_delta=delta / left,
        relative_delta_status=RelativeDeltaAvailability.AVAILABLE,
    )


def _numeric_delta(left: float | None, right: float | None) -> NumericDelta:
    status = _comparison_availability(left, right)
    if status is not ComparisonAvailability.AVAILABLE:
        return NumericDelta(
            status=status,
            left=left,
            right=right,
            delta=None,
            relative_delta=None,
            relative_delta_status=RelativeDeltaAvailability.MISSING,
        )
    if left is None or right is None:
        raise AssertionError("available numeric values must be present")
    delta = right - left
    if left == 0.0:
        return NumericDelta(
            status=status,
            left=left,
            right=right,
            delta=delta,
            relative_delta=None,
            relative_delta_status=RelativeDeltaAvailability.LEFT_ZERO,
        )
    return NumericDelta(
        status=status,
        left=left,
        right=right,
        delta=delta,
        relative_delta=delta / left,
        relative_delta_status=RelativeDeltaAvailability.AVAILABLE,
    )


def _proportion_delta(
    left: float | None,
    right: float | None,
) -> ProportionDelta:
    status = _comparison_availability(left, right)
    if status is not ComparisonAvailability.AVAILABLE:
        return ProportionDelta(
            status=status,
            left=left,
            right=right,
            delta=None,
            percentage_point_delta=None,
        )
    if left is None or right is None:
        raise AssertionError("available proportion values must be present")
    delta = right - left
    return ProportionDelta(
        status=status,
        left=left,
        right=right,
        delta=delta,
        percentage_point_delta=delta * 100.0,
    )


def _categorical_comparison(
    left: str | None,
    right: str | None,
) -> CategoricalComparison:
    status = _comparison_availability(left, right)
    return CategoricalComparison(
        status=status,
        left=left,
        right=right,
        equal=left == right if status is ComparisonAvailability.AVAILABLE else None,
    )


def _boolean_comparison(
    left: bool | None,
    right: bool | None,
) -> BooleanComparison:
    status = _comparison_availability(left, right)
    return BooleanComparison(
        status=status,
        left=left,
        right=right,
        equal=left == right if status is ComparisonAvailability.AVAILABLE else None,
    )


def _ordered_components(values: Sequence[Mapping[str, int] | None]) -> tuple[str, ...]:
    observed = {component for mapping in values if mapping is not None for component in mapping}
    ordered = [component for component in _SYMBOL_ORDER if component in observed]
    ordered.extend(sorted(observed.difference(_SYMBOL_ORDER)))
    return tuple(ordered)


def _mapping_distribution_comparison(
    left: Mapping[str, int] | None,
    right: Mapping[str, int] | None,
    *,
    components: Sequence[str] | None = None,
) -> CountDistributionComparison:
    resolved_components = (
        tuple(components)
        if components is not None
        else _ordered_components((left, right))
    )
    return CountDistributionComparison(
        components=tuple(
            ComponentCountDelta(
                component=component,
                comparison=_count_delta(
                    None if left is None else left.get(component, 0),
                    None if right is None else right.get(component, 0),
                ),
            )
            for component in resolved_components
        )
    )


def _base_counts_mapping(
    facts: SequenceFacts | None,
    *,
    kind: str,
) -> Mapping[str, int] | None:
    if facts is None:
        return None
    counts = facts.base_counts.definite if kind == "definite" else facts.base_counts.potential
    return {component: getattr(counts, component) for component in _BASE_ORDER}


def _kmer_summaries_by_query(
    facts: SequenceFacts | None,
) -> dict[str, KmerQuerySummary]:
    if facts is None:
        return {}
    summaries: dict[str, KmerQuerySummary] = {}
    for summary in facts.kmer_summaries:
        if summary.query in summaries:
            raise ComparativeStatisticsError(
                code=ComparisonErrorCode.STATISTICS_KMER_DUPLICATE,
                detail="K-mer query identifiers must be unique within each statistics value.",
            )
        summaries[summary.query] = summary
    return summaries


def _ordered_kmer_queries(
    mappings: Sequence[Mapping[str, KmerQuerySummary]],
) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for mapping in mappings:
        for query in mapping:
            if query in seen:
                continue
            seen.add(query)
            ordered.append(query)
    return tuple(ordered)


class StatisticalComparator:
    """Compare already computed SequenceFacts without accessing sequence strings."""

    def compare(
        self,
        left: SequenceFacts | None,
        right: SequenceFacts | None,
    ) -> SequenceFactsComparison:
        _validate_facts_value(left, side="left")
        _validate_facts_value(right, side="right")

        left_kmers = _kmer_summaries_by_query(left)
        right_kmers = _kmer_summaries_by_query(right)
        queries = _ordered_kmer_queries((left_kmers, right_kmers))

        return SequenceFactsComparison(
            status=_comparison_availability(left, right),
            counts=SequenceCountComparisons(
                source_length=_count_delta(
                    _fact_count(left, "source_length"),
                    _fact_count(right, "source_length"),
                ),
                ungapped_length=_count_delta(
                    _fact_count(left, "ungapped_length"),
                    _fact_count(right, "ungapped_length"),
                ),
                recognized_nucleotide_count=_count_delta(
                    _fact_count(left, "recognized_nucleotide_count"),
                    _fact_count(right, "recognized_nucleotide_count"),
                ),
                canonical_count=_count_delta(
                    _fact_count(left, "canonical_count"),
                    _fact_count(right, "canonical_count"),
                ),
                ambiguous_count=_count_delta(
                    _fact_count(left, "ambiguous_count"),
                    _fact_count(right, "ambiguous_count"),
                ),
                gap_count=_count_delta(
                    _fact_count(left, "gap_count"),
                    _fact_count(right, "gap_count"),
                ),
                invalid_symbol_count=_count_delta(
                    _fact_count(left, "invalid_symbol_count"),
                    _fact_count(right, "invalid_symbol_count"),
                ),
                gc_count=_count_delta(
                    _fact_count(left, "gc_count"),
                    _fact_count(right, "gc_count"),
                ),
                u_count=_count_delta(
                    _fact_count(left, "u_count"),
                    _fact_count(right, "u_count"),
                ),
            ),
            expected_gc_count=_numeric_delta(
                _fact_value(left, "expected_gc_count"),
                _fact_value(right, "expected_gc_count"),
            ),
            proportions=SequenceProportionComparisons(
                gc_content_total=_proportion_delta(
                    _fact_value(left, "gc_content_total"),
                    _fact_value(right, "gc_content_total"),
                ),
                resolved_gc_content=_proportion_delta(
                    _fact_value(left, "resolved_gc_content"),
                    _fact_value(right, "resolved_gc_content"),
                ),
                expected_gc_content=_proportion_delta(
                    _fact_value(left, "expected_gc_content"),
                    _fact_value(right, "expected_gc_content"),
                ),
            ),
            symbol_counts=_mapping_distribution_comparison(
                None if left is None else left.symbol_counts,
                None if right is None else right.symbol_counts,
                components=_SYMBOL_ORDER,
            ),
            invalid_symbol_counts=_mapping_distribution_comparison(
                None if left is None else left.invalid_symbol_counts,
                None if right is None else right.invalid_symbol_counts,
            ),
            base_counts=BaseCountDistributionComparison(
                definite=_mapping_distribution_comparison(
                    _base_counts_mapping(left, kind="definite"),
                    _base_counts_mapping(right, kind="definite"),
                    components=_BASE_ORDER,
                ),
                potential=_mapping_distribution_comparison(
                    _base_counts_mapping(left, kind="potential"),
                    _base_counts_mapping(right, kind="potential"),
                    components=_BASE_ORDER,
                ),
            ),
            invalid_positions_truncated=_boolean_comparison(
                None if left is None else left.invalid_positions_truncated,
                None if right is None else right.invalid_positions_truncated,
            ),
            kmer_summaries=tuple(
                _compare_kmer_summary(
                    query=query,
                    left=left_kmers.get(query),
                    right=right_kmers.get(query),
                )
                for query in queries
            ),
        )


def _validate_facts_value(value: object | None, *, side: str) -> None:
    if value is not None and not isinstance(value, SequenceFacts):
        raise ComparativeStatisticsError(
            code=ComparisonErrorCode.STATISTICS_INPUT_INVALID,
            detail=f"The {side} statistics value must be SequenceFacts or null.",
        )


def _fact_count(facts: SequenceFacts | None, field_name: str) -> int | None:
    if facts is None:
        return None
    value = getattr(facts, field_name)
    if type(value) is int:
        return value
    raise AssertionError(f"unsupported count SequenceFacts field: {field_name}")


def _fact_value(facts: SequenceFacts | None, field_name: str) -> float | None:
    if facts is None:
        return None
    value = getattr(facts, field_name)
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise AssertionError(f"unsupported numeric SequenceFacts field: {field_name}")


def _compare_kmer_summary(
    *,
    query: str,
    left: KmerQuerySummary | None,
    right: KmerQuerySummary | None,
) -> KmerSummaryComparison:
    return KmerSummaryComparison(
        query=query,
        status=_comparison_availability(left, right),
        definite_match_count=_count_delta(
            None if left is None else left.definite_match_count,
            None if right is None else right.definite_match_count,
        ),
        possible_match_count=_count_delta(
            None if left is None else left.possible_match_count,
            None if right is None else right.possible_match_count,
        ),
        strand=_categorical_comparison(
            None if left is None else left.strand.value,
            None if right is None else right.strand.value,
        ),
    )


def _numeric_summary(values: Sequence[int | float | None]) -> NumericSummary:
    available = [float(value) for value in values if value is not None]
    missing_count = len(values) - len(available)
    if not available:
        return NumericSummary(
            status=SummaryAvailability.MISSING,
            available_count=0,
            missing_count=missing_count,
        )
    return NumericSummary(
        status=SummaryAvailability.AVAILABLE,
        available_count=len(available),
        missing_count=missing_count,
        minimum=min(available),
        maximum=max(available),
        mean=statistics.fmean(available),
        median=statistics.median(available),
        population_standard_deviation=statistics.pstdev(available),
    )


def _categorical_summary(values: Sequence[str | None]) -> CategoricalSummary:
    available = [value for value in values if value is not None]
    counts = Counter(available)
    return CategoricalSummary(
        status=(
            SummaryAvailability.AVAILABLE
            if available
            else SummaryAvailability.MISSING
        ),
        available_count=len(available),
        missing_count=len(values) - len(available),
        counts=tuple(
            CategoryCount(value=value, count=counts[value])
            for value in sorted(counts)
        ),
    )


def _boolean_summary(values: Sequence[bool | None]) -> BooleanSummary:
    available = [value for value in values if value is not None]
    return BooleanSummary(
        status=(
            SummaryAvailability.AVAILABLE
            if available
            else SummaryAvailability.MISSING
        ),
        available_count=len(available),
        missing_count=len(values) - len(available),
        false_count=sum(value is False for value in available),
        true_count=sum(value is True for value in available),
    )


def _distribution_summary(
    mappings: Sequence[Mapping[str, int] | None],
    *,
    components: Sequence[str] | None = None,
) -> CountDistributionSummary:
    resolved_components = (
        tuple(components)
        if components is not None
        else _ordered_components(mappings)
    )
    return CountDistributionSummary(
        components=tuple(
            ComponentNumericSummary(
                component=component,
                summary=_numeric_summary(
                    tuple(
                        None if mapping is None else mapping.get(component, 0)
                        for mapping in mappings
                    )
                ),
            )
            for component in resolved_components
        )
    )


class DatasetStatisticalSummarizer:
    """Summarize SequenceFacts per logical occurrence; equal objects still count twice."""

    def summarize(
        self,
        samples: Sequence[SequenceFacts | None],
    ) -> SequenceFactsDatasetSummary:
        facts_values = tuple(samples)
        for index, facts in enumerate(facts_values):
            if facts is not None and not isinstance(facts, SequenceFacts):
                raise ComparativeStatisticsError(
                    code=ComparisonErrorCode.STATISTICS_INPUT_INVALID,
                    detail=(
                        "Each dataset statistics value must be SequenceFacts or null "
                        f"(invalid item at index {index})."
                    ),
                )

        kmer_mappings = tuple(_kmer_summaries_by_query(facts) for facts in facts_values)
        queries = _ordered_kmer_queries(kmer_mappings)
        available_count = sum(facts is not None for facts in facts_values)

        return SequenceFactsDatasetSummary(
            sample_count=len(facts_values),
            facts_available_count=available_count,
            facts_missing_count=len(facts_values) - available_count,
            numeric=SequenceNumericSummaries(
                source_length=_summarize_fact_field(facts_values, "source_length"),
                ungapped_length=_summarize_fact_field(facts_values, "ungapped_length"),
                recognized_nucleotide_count=_summarize_fact_field(
                    facts_values,
                    "recognized_nucleotide_count",
                ),
                canonical_count=_summarize_fact_field(facts_values, "canonical_count"),
                ambiguous_count=_summarize_fact_field(facts_values, "ambiguous_count"),
                gap_count=_summarize_fact_field(facts_values, "gap_count"),
                invalid_symbol_count=_summarize_fact_field(
                    facts_values,
                    "invalid_symbol_count",
                ),
                gc_count=_summarize_fact_field(facts_values, "gc_count"),
                expected_gc_count=_summarize_fact_field(
                    facts_values,
                    "expected_gc_count",
                ),
                u_count=_summarize_fact_field(facts_values, "u_count"),
                gc_content_total=_summarize_fact_field(
                    facts_values,
                    "gc_content_total",
                ),
                resolved_gc_content=_summarize_fact_field(
                    facts_values,
                    "resolved_gc_content",
                ),
                expected_gc_content=_summarize_fact_field(
                    facts_values,
                    "expected_gc_content",
                ),
            ),
            symbol_counts=_distribution_summary(
                tuple(
                    None if facts is None else facts.symbol_counts
                    for facts in facts_values
                ),
                components=_SYMBOL_ORDER,
            ),
            invalid_symbol_counts=_distribution_summary(
                tuple(
                    None if facts is None else facts.invalid_symbol_counts
                    for facts in facts_values
                )
            ),
            base_counts=BaseCountDistributionSummary(
                definite=_distribution_summary(
                    tuple(
                        _base_counts_mapping(facts, kind="definite")
                        for facts in facts_values
                    ),
                    components=_BASE_ORDER,
                ),
                potential=_distribution_summary(
                    tuple(
                        _base_counts_mapping(facts, kind="potential")
                        for facts in facts_values
                    ),
                    components=_BASE_ORDER,
                ),
            ),
            invalid_positions_truncated=_boolean_summary(
                tuple(
                    None if facts is None else facts.invalid_positions_truncated
                    for facts in facts_values
                )
            ),
            kmer_summaries=tuple(
                _summarize_kmer_query(
                    query=query,
                    summaries=tuple(mapping.get(query) for mapping in kmer_mappings),
                )
                for query in queries
            ),
        )


def _summarize_fact_field(
    facts_values: Sequence[SequenceFacts | None],
    field_name: str,
) -> NumericSummary:
    return _numeric_summary(
        tuple(_fact_value(facts, field_name) for facts in facts_values)
    )


def _summarize_kmer_query(
    *,
    query: str,
    summaries: Sequence[KmerQuerySummary | None],
) -> KmerSummaryDatasetSummary:
    return KmerSummaryDatasetSummary(
        query=query,
        definite_match_count=_numeric_summary(
            tuple(
                None if summary is None else summary.definite_match_count
                for summary in summaries
            )
        ),
        possible_match_count=_numeric_summary(
            tuple(
                None if summary is None else summary.possible_match_count
                for summary in summaries
            )
        ),
        strand=_categorical_summary(
            tuple(
                None if summary is None else summary.strand.value
                for summary in summaries
            )
        ),
    )


def compare_sequence_facts(
    left: SequenceFacts | None,
    right: SequenceFacts | None,
) -> SequenceFactsComparison:
    """Convenience wrapper around :class:`StatisticalComparator`."""

    return StatisticalComparator().compare(left, right)


def summarize_sequence_facts(
    samples: Sequence[SequenceFacts | None],
) -> SequenceFactsDatasetSummary:
    """Convenience wrapper around :class:`DatasetStatisticalSummarizer`."""

    return DatasetStatisticalSummarizer().summarize(samples)


__all__ = [
    "BaseCountDistributionComparison",
    "BaseCountDistributionSummary",
    "BooleanComparison",
    "BooleanSummary",
    "CategoricalComparison",
    "CategoricalSummary",
    "CategoryCount",
    "ComparisonAvailability",
    "ComparativeStatisticsError",
    "ComponentCountDelta",
    "ComponentNumericSummary",
    "CountDelta",
    "CountDistributionComparison",
    "CountDistributionSummary",
    "DatasetStatisticalSummarizer",
    "KmerSummaryComparison",
    "KmerSummaryDatasetSummary",
    "NumericDelta",
    "NumericSummary",
    "ProportionDelta",
    "RelativeDeltaAvailability",
    "SequenceCountComparisons",
    "SequenceFactsComparison",
    "SequenceFactsDatasetSummary",
    "SequenceNumericSummaries",
    "SequenceProportionComparisons",
    "StatisticalComparator",
    "SummaryAvailability",
    "compare_sequence_facts",
    "summarize_sequence_facts",
]
