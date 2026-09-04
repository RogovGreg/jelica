from __future__ import annotations

import pytest

from jelica_core.comparative_analysis.errors import (
    ComparisonErrorCode,
    ComparisonPlanningError,
)
from jelica_core.comparative_analysis.planning import (
    ComparisonPlan,
    ComparisonPlanBuilder,
    ComparisonSourceKind,
)
from jelica_core.config import (
    AnalysisComparativeReferenceMode,
    AnalysisPairwiseOrientation,
    ResolvedComparativeAnalysisConfig,
    ResolvedComparativePairwiseConfig,
    ResolvedComparativeReferenceConfig,
    ResolvedComparativeStatisticsConfig,
)
from jelica_core.runtime.input_processing_models import (
    InputProcessingLogicalSample,
    LogicalSampleProvenance,
    SampleValidationStatus,
)
from jelica_core.sample_selection import (
    SampleSelectorCandidate,
    SampleSelectorResolutionError,
    SampleSelectorResolutionReason,
    SampleSelectorResolver,
)


def _sequence_id(symbol: str) -> str:
    return f"sha256:{symbol * 64}"


def _sample(
    name: str,
    *,
    sequence_symbol: str | None = None,
    record_id: str | None = None,
    source_path: str | None = None,
    materialized_path: str | None = None,
    eligible: bool = True,
) -> InputProcessingLogicalSample:
    resolved_source = source_path or f"data/{name}.fasta"
    resolved_materialized = materialized_path or f"inputs/files/{name}.fasta"
    return InputProcessingLogicalSample(
        sample_id=f"sample-{name}",
        provenance=LogicalSampleProvenance(
            input_manifest_source_reference=resolved_source,
            materialized_relative_path=resolved_materialized,
            record_index=0,
            format_hint=".fasta",
        ),
        original_record_id=record_id or name,
        validation_status=(
            SampleValidationStatus.VALID
            if eligible
            else SampleValidationStatus.INVALID
        ),
        sequence_id=_sequence_id(sequence_symbol or name.lower()),
        eligible_for_analysis=eligible,
    )


def _candidates(
    samples: tuple[InputProcessingLogicalSample, ...],
) -> tuple[SampleSelectorCandidate, ...]:
    return tuple(
        SampleSelectorCandidate(
            sample_id=sample.sample_id,
            sequence_id=sample.sequence_id,
            record_id=sample.original_record_id,
            source_reference=sample.provenance.input_manifest_source_reference,
            materialized_relative_path=sample.provenance.materialized_relative_path,
            eligible_for_analysis=sample.eligible_for_analysis,
            input_order=index,
        )
        for index, sample in enumerate(samples)
    )


def _config(
    *,
    reference_mode: AnalysisComparativeReferenceMode = (
        AnalysisComparativeReferenceMode.DISABLED
    ),
    pairwise: ResolvedComparativePairwiseConfig | None = None,
) -> ResolvedComparativeAnalysisConfig:
    return ResolvedComparativeAnalysisConfig(
        enabled=True,
        statistics=ResolvedComparativeStatisticsConfig(enabled=True),
        reference=ResolvedComparativeReferenceConfig(mode=reference_mode),
        pairwise=pairwise or ResolvedComparativePairwiseConfig(),
    )


def _pairwise(
    *,
    all_samples: bool = False,
    groups: list[list[str]] | None = None,
    pairs: list[list[str]] | None = None,
    orientation: AnalysisPairwiseOrientation = AnalysisPairwiseOrientation.DIRECTED,
) -> ResolvedComparativePairwiseConfig:
    return ResolvedComparativePairwiseConfig(
        enabled=True,
        all=all_samples,
        groups=groups or [],
        pairs=pairs or [],
        pairs_orientation=orientation,
    )


def _build(
    samples: tuple[InputProcessingLogicalSample, ...],
    *,
    config: ResolvedComparativeAnalysisConfig,
    reference_selector: str | None = None,
) -> ComparisonPlan:
    return ComparisonPlanBuilder().build(
        config=config,
        logical_samples=samples,
        reference_selector=reference_selector,
    )


def _operation_pairs(plan: ComparisonPlan) -> list[tuple[str, str]]:
    return [
        (operation.left_sample_id, operation.right_sample_id)
        for operation in plan.logical_operations
    ]


def test_selector_resolves_bare_record_id_with_input_order_and_identity() -> None:
    samples = (_sample("A"), _sample("B"))

    resolved = SampleSelectorResolver(_candidates(samples)).resolve(" B ")

    assert resolved.original_selector == "B"
    assert resolved.sample_id == "sample-B"
    assert resolved.sequence_id == _sequence_id("b")
    assert resolved.record_id == "B"
    assert resolved.source_reference == "data/B.fasta"
    assert resolved.materialized_relative_path == "inputs/files/B.fasta"
    assert resolved.input_order == 1


def test_selector_resolves_qualified_path_with_shared_normalization() -> None:
    samples = (
        _sample(
            "A",
            record_id="record",
            source_path="data\\input.fasta",
        ),
    )

    resolved = SampleSelectorResolver(_candidates(samples)).resolve(
        "data/input.fasta::record"
    )

    assert resolved.sample_id == "sample-A"


def test_selector_not_found_is_structured_planning_error() -> None:
    samples = (_sample("A"), _sample("B"))
    config = _config(pairwise=_pairwise(pairs=[["A", "missing"]]))

    with pytest.raises(ComparisonPlanningError) as error_info:
        _build(samples, config=config)

    assert error_info.value.code is ComparisonErrorCode.SELECTOR_NOT_FOUND
    assert error_info.value.safe_details["selector"] == "missing"


def test_bare_record_id_ambiguity_is_structured() -> None:
    samples = (
        _sample("A", record_id="shared"),
        _sample("B", record_id="shared"),
    )

    with pytest.raises(SampleSelectorResolutionError) as error_info:
        SampleSelectorResolver(_candidates(samples)).resolve("shared")

    assert error_info.value.reason is SampleSelectorResolutionReason.AMBIGUOUS
    assert error_info.value.matched_sample_ids == ("sample-A", "sample-B")


def test_distinct_selectors_resolving_to_one_sample_are_rejected() -> None:
    samples = (_sample("A", record_id="record", source_path="data/a.fasta"),)
    config = _config(
        pairwise=_pairwise(pairs=[["record", "data/a.fasta::record"]])
    )

    with pytest.raises(ComparisonPlanningError) as error_info:
        _build(samples, config=config)

    assert error_info.value.code is ComparisonErrorCode.SELECTOR_RESOLVES_TO_SELF


def test_reference_disabled_creates_no_reference_operations() -> None:
    samples = (_sample("A"), _sample("B"))

    plan = _build(samples, config=_config(), reference_selector="A")

    assert plan.logical_operations == ()


def test_reference_auto_without_selector_creates_no_operations() -> None:
    samples = (_sample("A"), _sample("B"))

    plan = _build(
        samples,
        config=_config(reference_mode=AnalysisComparativeReferenceMode.AUTO),
    )

    assert plan.logical_operations == ()


def test_reference_auto_creates_reference_to_every_other_sample() -> None:
    samples = (_sample("A"), _sample("B"), _sample("C"))

    plan = _build(
        samples,
        config=_config(reference_mode=AnalysisComparativeReferenceMode.AUTO),
        reference_selector="B",
    )

    assert _operation_pairs(plan) == [
        ("sample-B", "sample-A"),
        ("sample-B", "sample-C"),
    ]
    assert all(
        operation.source_kinds == (ComparisonSourceKind.REFERENCE,)
        for operation in plan.logical_operations
    )


def test_reference_enabled_requires_selector() -> None:
    with pytest.raises(ComparisonPlanningError) as error_info:
        _build(
            (_sample("A"),),
            config=_config(reference_mode=AnalysisComparativeReferenceMode.ENABLED),
        )

    assert error_info.value.code is ComparisonErrorCode.REFERENCE_REQUIRED


def test_reference_never_compares_with_itself() -> None:
    samples = (_sample("A"), _sample("B"))

    plan = _build(
        samples,
        config=_config(reference_mode=AnalysisComparativeReferenceMode.ENABLED),
        reference_selector="A",
    )

    assert _operation_pairs(plan) == [("sample-A", "sample-B")]


def test_reference_projection_with_same_sequence_id_is_kept_without_scan() -> None:
    samples = (
        _sample("A", sequence_symbol="a"),
        _sample("B", sequence_symbol="a"),
    )

    plan = _build(
        samples,
        config=_config(reference_mode=AnalysisComparativeReferenceMode.AUTO),
        reference_selector="A",
    )

    assert len(plan.logical_operations) == 1
    assert plan.logical_operations[0].requires_scan is False
    assert plan.computations == ()
    assert plan.counts.identical_sequence_projection_count == 1


def test_all_for_three_samples_creates_six_directions() -> None:
    samples = (_sample("A"), _sample("B"), _sample("C"))

    plan = _build(samples, config=_config(pairwise=_pairwise(all_samples=True)))

    assert len(plan.logical_operations) == 6
    assert set(_operation_pairs(plan)) == {
        ("sample-A", "sample-B"),
        ("sample-B", "sample-A"),
        ("sample-A", "sample-C"),
        ("sample-C", "sample-A"),
        ("sample-B", "sample-C"),
        ("sample-C", "sample-B"),
    }


def test_group_of_three_creates_six_directions() -> None:
    samples = (_sample("A"), _sample("B"), _sample("C"))

    plan = _build(
        samples,
        config=_config(pairwise=_pairwise(groups=[["A", "B", "C"]])),
    )

    assert len(plan.logical_operations) == 6


def test_separate_groups_do_not_create_cross_group_operations() -> None:
    samples = (_sample("A"), _sample("B"), _sample("C"), _sample("D"))

    plan = _build(
        samples,
        config=_config(
            pairwise=_pairwise(groups=[["A", "B"], ["C", "D"]])
        ),
    )

    assert set(_operation_pairs(plan)) == {
        ("sample-A", "sample-B"),
        ("sample-B", "sample-A"),
        ("sample-C", "sample-D"),
        ("sample-D", "sample-C"),
    }


def test_group_is_validated_after_aliases_resolve_to_sample_ids() -> None:
    samples = (_sample("A", record_id="record", source_path="data/a.fasta"),)
    config = _config(
        pairwise=_pairwise(groups=[["record", "data/a.fasta::record"]])
    )

    with pytest.raises(ComparisonPlanningError) as error_info:
        _build(samples, config=config)

    assert error_info.value.code is ComparisonErrorCode.GROUP_RESOLVES_TOO_SMALL


def test_directed_pair_creates_only_requested_direction() -> None:
    samples = (_sample("A"), _sample("B"))

    plan = _build(
        samples,
        config=_config(pairwise=_pairwise(pairs=[["B", "A"]])),
    )

    assert _operation_pairs(plan) == [("sample-B", "sample-A")]


def test_two_opposite_directed_pairs_remain_distinct() -> None:
    samples = (_sample("A"), _sample("B"))

    plan = _build(
        samples,
        config=_config(
            pairwise=_pairwise(pairs=[["A", "B"], ["B", "A"]])
        ),
    )

    assert set(_operation_pairs(plan)) == {
        ("sample-A", "sample-B"),
        ("sample-B", "sample-A"),
    }
    assert plan.counts.unique_logical_operation_count == 2


def test_bidirectional_pair_creates_both_directions() -> None:
    samples = (_sample("A"), _sample("B"))

    plan = _build(
        samples,
        config=_config(
            pairwise=_pairwise(
                pairs=[["A", "B"]],
                orientation=AnalysisPairwiseOrientation.BIDIRECTIONAL,
            )
        ),
    )

    assert set(_operation_pairs(plan)) == {
        ("sample-A", "sample-B"),
        ("sample-B", "sample-A"),
    }


def test_reverse_repeat_in_bidirectional_mode_deduplicates_logical_operations() -> None:
    samples = (_sample("A"), _sample("B"))

    plan = _build(
        samples,
        config=_config(
            pairwise=_pairwise(
                pairs=[["A", "B"], ["B", "A"]],
                orientation=AnalysisPairwiseOrientation.BIDIRECTIONAL,
            )
        ),
    )

    assert plan.counts.occurrence_count == 4
    assert plan.counts.unique_logical_operation_count == 2
    assert plan.counts.duplicate_occurrence_count == 2


def test_groups_and_explicit_pairs_are_combined_with_provenance() -> None:
    samples = (_sample("A"), _sample("B"), _sample("C"))

    plan = _build(
        samples,
        config=_config(
            pairwise=_pairwise(
                groups=[["A", "B"]],
                pairs=[["A", "C"], ["A", "B"]],
            )
        ),
    )

    by_pair = {
        (operation.left_sample_id, operation.right_sample_id): operation
        for operation in plan.logical_operations
    }
    assert set(by_pair) == {
        ("sample-A", "sample-B"),
        ("sample-B", "sample-A"),
        ("sample-A", "sample-C"),
    }
    assert by_pair[("sample-A", "sample-B")].source_kinds == (
        ComparisonSourceKind.GROUP,
        ComparisonSourceKind.EXPLICIT_PAIR,
    )
    assert by_pair[("sample-A", "sample-B")].source_occurrence_count == 2


def test_duplicate_directed_occurrences_have_exact_counts() -> None:
    samples = (_sample("A"), _sample("B"))

    plan = _build(
        samples,
        config=_config(
            pairwise=_pairwise(pairs=[["A", "B"], ["A", "B"]])
        ),
    )

    assert plan.counts.occurrence_count == 2
    assert plan.counts.unique_logical_operation_count == 1
    assert plan.counts.duplicate_occurrence_count == 1
    assert plan.logical_operations[0].source_occurrence_count == 2


def test_plan_order_is_deterministic_and_based_on_manifest_sample_order() -> None:
    samples = (_sample("C"), _sample("A"), _sample("B"))
    config = _config(pairwise=_pairwise(all_samples=True))

    first = _build(samples, config=config)
    second = _build(samples, config=config)

    assert first == second
    assert _operation_pairs(first) == [
        ("sample-C", "sample-A"),
        ("sample-C", "sample-B"),
        ("sample-A", "sample-C"),
        ("sample-A", "sample-B"),
        ("sample-B", "sample-C"),
        ("sample-B", "sample-A"),
    ]


def test_same_sequence_ids_keep_logical_projection_without_computation() -> None:
    samples = (
        _sample("A", sequence_symbol="a"),
        _sample("B", sequence_symbol="a"),
    )

    plan = _build(samples, config=_config(pairwise=_pairwise(all_samples=True)))

    assert len(plan.logical_operations) == 2
    assert all(not operation.requires_scan for operation in plan.logical_operations)
    assert plan.computations == ()


def test_shared_sequence_ids_reuse_one_physical_computation_and_keep_projections() -> None:
    samples = (
        _sample("A", sequence_symbol="a"),
        _sample("B", sequence_symbol="a"),
        _sample("C", sequence_symbol="c"),
    )

    plan = _build(samples, config=_config(pairwise=_pairwise(all_samples=True)))

    assert len(plan.logical_operations) == 6
    assert len(plan.computations) == 1
    assert plan.computations[0].logical_projection_count == 4
    scan_operations = [
        operation for operation in plan.logical_operations if operation.requires_scan
    ]
    assert len(scan_operations) == 4
    assert {operation.computation_index for operation in scan_operations} == {0}
    assert {operation.reverse_computation for operation in scan_operations} == {
        False,
        True,
    }


def test_disabled_comparative_analysis_returns_empty_stable_plan() -> None:
    plan = _build(
        (_sample("A"), _sample("B")),
        config=ResolvedComparativeAnalysisConfig(),
        reference_selector="A",
    )

    assert plan.logical_operations == ()
    assert plan.computations == ()
    assert plan.counts.occurrence_count == 0
