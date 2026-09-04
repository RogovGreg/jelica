from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from itertools import combinations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jelica_core.config import (
    AnalysisComparativeReferenceMode,
    AnalysisPairwiseOrientation,
    ResolvedAnalysisConfig,
    ResolvedComparativeAnalysisConfig,
)
from jelica_core.runtime.input_processing_models import (
    InputProcessingLogicalSample,
    InputProcessingManifest,
    InputProcessingResolvedReference,
)
from jelica_core.sample_selection import (
    ResolvedSampleSelector,
    SampleSelectorCandidate,
    SampleSelectorResolutionError,
    SampleSelectorResolutionReason,
    SampleSelectorResolver,
)

from .errors import ComparisonErrorCode, ComparisonPlanningError


class ComparisonSourceKind(StrEnum):
    REFERENCE = "reference"
    ALL = "all"
    GROUP = "group"
    EXPLICIT_PAIR = "explicit_pair"


_SOURCE_ORDER = {
    ComparisonSourceKind.REFERENCE: 0,
    ComparisonSourceKind.ALL: 1,
    ComparisonSourceKind.GROUP: 2,
    ComparisonSourceKind.EXPLICIT_PAIR: 3,
}


class ComparisonPlanSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str = Field(min_length=1)
    sequence_id: str = Field(min_length=1)
    record_id: str | None = None
    source_reference: str = Field(min_length=1)
    materialized_relative_path: str = Field(min_length=1)
    input_order: int = Field(ge=0)


class DirectedLogicalComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left_sample_id: str = Field(min_length=1)
    right_sample_id: str = Field(min_length=1)
    left_sequence_id: str = Field(min_length=1)
    right_sequence_id: str = Field(min_length=1)
    source_kinds: tuple[ComparisonSourceKind, ...] = Field(min_length=1)
    source_occurrence_count: int = Field(ge=1)
    requires_scan: bool
    computation_index: int | None = Field(default=None, ge=0)
    reverse_computation: bool = False

    @model_validator(mode="after")
    def _validate_projection(self) -> DirectedLogicalComparison:
        if self.left_sample_id == self.right_sample_id:
            raise ValueError("directed logical comparison cannot be a self-comparison")
        if self.requires_scan != (self.left_sequence_id != self.right_sequence_id):
            raise ValueError("requires_scan must reflect sequence identity")
        if self.requires_scan and self.computation_index is None:
            raise ValueError("scan projection requires computation_index")
        if not self.requires_scan and (
            self.computation_index is not None or self.reverse_computation
        ):
            raise ValueError("identical sequences cannot reference a scan computation")
        return self


class SequenceComparisonComputation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    computation_index: int = Field(ge=0)
    first_sequence_id: str = Field(min_length=1)
    second_sequence_id: str = Field(min_length=1)
    logical_projection_count: int = Field(ge=1)

    @model_validator(mode="after")
    def _require_distinct_sequences(self) -> SequenceComparisonComputation:
        if self.first_sequence_id == self.second_sequence_id:
            raise ValueError("scan computation requires two distinct sequence IDs")
        return self


class ComparisonPlanCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    occurrence_count: int = Field(ge=0)
    unique_logical_operation_count: int = Field(ge=0)
    duplicate_occurrence_count: int = Field(ge=0)
    scan_computation_count: int = Field(ge=0)
    identical_sequence_projection_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_counts(self) -> ComparisonPlanCounts:
        if (
            self.duplicate_occurrence_count
            != self.occurrence_count - self.unique_logical_operation_count
        ):
            raise ValueError("duplicate occurrence count is inconsistent")
        return self


class ComparisonPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    samples: tuple[ComparisonPlanSample, ...] = Field(default_factory=tuple)
    logical_operations: tuple[DirectedLogicalComparison, ...] = Field(
        default_factory=tuple
    )
    computations: tuple[SequenceComparisonComputation, ...] = Field(
        default_factory=tuple
    )
    counts: ComparisonPlanCounts


@dataclass(frozen=True, slots=True)
class _OperationOccurrence:
    left: ComparisonPlanSample
    right: ComparisonPlanSample
    source_kind: ComparisonSourceKind


@dataclass(slots=True)
class _MergedOperation:
    left: ComparisonPlanSample
    right: ComparisonPlanSample
    source_kinds: set[ComparisonSourceKind] = field(default_factory=set)
    occurrence_count: int = 0


@dataclass(slots=True)
class _ComputationAccumulator:
    index: int
    first_sequence_id: str
    second_sequence_id: str
    logical_projection_count: int = 0


class ComparisonPlanBuilder:
    """Build a deterministic logical plan without reading sequence content."""

    def build_from_manifest(
        self,
        *,
        config: ResolvedAnalysisConfig,
        manifest: InputProcessingManifest,
    ) -> ComparisonPlan:
        return self.build(
            config=config.comparative_analysis,
            logical_samples=manifest.logical_samples,
            reference_selector=config.reference,
            resolved_reference=manifest.resolved_reference,
        )

    def build(
        self,
        *,
        config: ResolvedComparativeAnalysisConfig,
        logical_samples: tuple[InputProcessingLogicalSample, ...],
        reference_selector: str | None = None,
        resolved_reference: InputProcessingResolvedReference | None = None,
    ) -> ComparisonPlan:
        samples, samples_by_id = _build_sample_catalog(logical_samples)
        empty_counts = ComparisonPlanCounts(
            occurrence_count=0,
            unique_logical_operation_count=0,
            duplicate_occurrence_count=0,
            scan_computation_count=0,
            identical_sequence_projection_count=0,
        )
        if not config.enabled:
            return ComparisonPlan(samples=samples, counts=empty_counts)

        selector_resolver = SampleSelectorResolver(
            _build_selector_candidates(logical_samples)
        )
        occurrences: list[_OperationOccurrence] = []
        reference_sample = self._resolve_reference_sample(
            config=config,
            reference_selector=reference_selector,
            resolved_reference=resolved_reference,
            selector_resolver=selector_resolver,
            samples_by_id=samples_by_id,
        )
        if reference_sample is not None:
            for sample in samples:
                if sample.sample_id != reference_sample.sample_id:
                    occurrences.append(
                        _OperationOccurrence(
                            left=reference_sample,
                            right=sample,
                            source_kind=ComparisonSourceKind.REFERENCE,
                        )
                    )

        pairwise = config.pairwise
        if pairwise.enabled:
            if pairwise.all:
                _append_bidirectional_combinations(
                    occurrences=occurrences,
                    samples=samples,
                    source_kind=ComparisonSourceKind.ALL,
                )
            for group in pairwise.groups:
                resolved_group = self._resolve_group(
                    selectors=group,
                    selector_resolver=selector_resolver,
                    samples_by_id=samples_by_id,
                )
                _append_bidirectional_combinations(
                    occurrences=occurrences,
                    samples=resolved_group,
                    source_kind=ComparisonSourceKind.GROUP,
                )
            for pair in pairwise.pairs:
                left = self._resolve_selector(
                    selector=pair[0],
                    selector_resolver=selector_resolver,
                    samples_by_id=samples_by_id,
                )
                right = self._resolve_selector(
                    selector=pair[1],
                    selector_resolver=selector_resolver,
                    samples_by_id=samples_by_id,
                )
                if left.sample_id == right.sample_id:
                    raise ComparisonPlanningError(
                        code=ComparisonErrorCode.SELECTOR_RESOLVES_TO_SELF,
                        detail=(
                            "Explicit comparison selectors resolve to the same logical sample."
                        ),
                        safe_details={"sample_id": left.sample_id},
                    )
                occurrences.append(
                    _OperationOccurrence(
                        left=left,
                        right=right,
                        source_kind=ComparisonSourceKind.EXPLICIT_PAIR,
                    )
                )
                if (
                    pairwise.pairs_orientation
                    is AnalysisPairwiseOrientation.BIDIRECTIONAL
                ):
                    occurrences.append(
                        _OperationOccurrence(
                            left=right,
                            right=left,
                            source_kind=ComparisonSourceKind.EXPLICIT_PAIR,
                        )
                    )

        operations, computations = _deduplicate_plan(occurrences)
        counts = ComparisonPlanCounts(
            occurrence_count=len(occurrences),
            unique_logical_operation_count=len(operations),
            duplicate_occurrence_count=len(occurrences) - len(operations),
            scan_computation_count=len(computations),
            identical_sequence_projection_count=sum(
                1 for operation in operations if not operation.requires_scan
            ),
        )
        return ComparisonPlan(
            samples=samples,
            logical_operations=operations,
            computations=computations,
            counts=counts,
        )

    def _resolve_reference_sample(
        self,
        *,
        config: ResolvedComparativeAnalysisConfig,
        reference_selector: str | None,
        resolved_reference: InputProcessingResolvedReference | None,
        selector_resolver: SampleSelectorResolver,
        samples_by_id: dict[str, ComparisonPlanSample],
    ) -> ComparisonPlanSample | None:
        mode = config.reference.mode
        if mode is AnalysisComparativeReferenceMode.DISABLED:
            return None
        if reference_selector is None:
            if mode is AnalysisComparativeReferenceMode.ENABLED:
                raise ComparisonPlanningError(
                    code=ComparisonErrorCode.REFERENCE_REQUIRED,
                    detail="Reference comparison is enabled but no reference is configured.",
                )
            return None

        if resolved_reference is None:
            return self._resolved_selector_to_sample(
                self._resolve_with_domain_error(selector_resolver, reference_selector),
                samples_by_id,
            )

        if resolved_reference.selector != reference_selector:
            raise ComparisonPlanningError(
                code=ComparisonErrorCode.INPUT_INVARIANT_INVALID,
                detail="Published reference does not match the normalized task reference.",
            )
        sample = samples_by_id.get(resolved_reference.sample_id)
        if sample is None or sample.sequence_id != resolved_reference.sequence_id:
            raise ComparisonPlanningError(
                code=ComparisonErrorCode.INPUT_INVARIANT_INVALID,
                detail="Published reference is inconsistent with eligible logical samples.",
                safe_details={"sample_id": resolved_reference.sample_id},
            )
        return sample

    def _resolve_group(
        self,
        *,
        selectors: list[str],
        selector_resolver: SampleSelectorResolver,
        samples_by_id: dict[str, ComparisonPlanSample],
    ) -> tuple[ComparisonPlanSample, ...]:
        unique: dict[str, ComparisonPlanSample] = {}
        for selector in selectors:
            sample = self._resolve_selector(
                selector=selector,
                selector_resolver=selector_resolver,
                samples_by_id=samples_by_id,
            )
            unique.setdefault(sample.sample_id, sample)
        resolved = tuple(sorted(unique.values(), key=lambda sample: sample.input_order))
        if len(resolved) < 2:
            raise ComparisonPlanningError(
                code=ComparisonErrorCode.GROUP_RESOLVES_TOO_SMALL,
                detail=(
                    "Comparison group resolves to fewer than two unique logical samples."
                ),
            )
        return resolved

    def _resolve_selector(
        self,
        *,
        selector: str,
        selector_resolver: SampleSelectorResolver,
        samples_by_id: dict[str, ComparisonPlanSample],
    ) -> ComparisonPlanSample:
        return self._resolved_selector_to_sample(
            self._resolve_with_domain_error(selector_resolver, selector),
            samples_by_id,
        )

    @staticmethod
    def _resolve_with_domain_error(
        resolver: SampleSelectorResolver,
        selector: str,
    ) -> ResolvedSampleSelector:
        try:
            return resolver.resolve(selector)
        except SampleSelectorResolutionError as error:
            code = ComparisonErrorCode.SELECTOR_INVALID
            if error.reason is SampleSelectorResolutionReason.NOT_FOUND:
                code = ComparisonErrorCode.SELECTOR_NOT_FOUND
            elif error.reason is SampleSelectorResolutionReason.AMBIGUOUS:
                code = ComparisonErrorCode.SELECTOR_AMBIGUOUS
            safe_details: dict[str, str | int | tuple[str, ...]] = {}
            if error.selector is not None:
                safe_details["selector"] = error.selector
            if error.matched_sample_ids:
                safe_details["matched_sample_ids"] = error.matched_sample_ids
            raise ComparisonPlanningError(
                code=code,
                detail=error.detail,
                safe_details=safe_details,
            ) from error

    @staticmethod
    def _resolved_selector_to_sample(
        resolved: ResolvedSampleSelector,
        samples_by_id: dict[str, ComparisonPlanSample],
    ) -> ComparisonPlanSample:
        sample = samples_by_id.get(resolved.sample_id)
        if sample is None or sample.sequence_id != resolved.sequence_id:
            raise ComparisonPlanningError(
                code=ComparisonErrorCode.INPUT_INVARIANT_INVALID,
                detail="Resolved selector is inconsistent with the planning sample catalog.",
                safe_details={"sample_id": resolved.sample_id},
            )
        return sample


def build_comparison_plan(
    *,
    config: ResolvedAnalysisConfig,
    manifest: InputProcessingManifest,
) -> ComparisonPlan:
    return ComparisonPlanBuilder().build_from_manifest(config=config, manifest=manifest)


def _build_sample_catalog(
    logical_samples: tuple[InputProcessingLogicalSample, ...],
) -> tuple[tuple[ComparisonPlanSample, ...], dict[str, ComparisonPlanSample]]:
    samples: list[ComparisonPlanSample] = []
    samples_by_id: dict[str, ComparisonPlanSample] = {}
    for input_order, logical_sample in enumerate(logical_samples):
        if not logical_sample.eligible_for_analysis:
            continue
        if logical_sample.sequence_id is None:
            raise ComparisonPlanningError(
                code=ComparisonErrorCode.INPUT_INVARIANT_INVALID,
                detail="Eligible logical sample lacks a sequence identifier.",
                safe_details={"sample_id": logical_sample.sample_id},
            )
        if logical_sample.sample_id in samples_by_id:
            raise ComparisonPlanningError(
                code=ComparisonErrorCode.INPUT_INVARIANT_INVALID,
                detail="Eligible logical sample identifiers must be unique.",
                safe_details={"sample_id": logical_sample.sample_id},
            )
        sample = ComparisonPlanSample(
            sample_id=logical_sample.sample_id,
            sequence_id=logical_sample.sequence_id,
            record_id=logical_sample.original_record_id,
            source_reference=(
                logical_sample.provenance.input_manifest_source_reference
            ),
            materialized_relative_path=(
                logical_sample.provenance.materialized_relative_path
            ),
            input_order=input_order,
        )
        samples.append(sample)
        samples_by_id[sample.sample_id] = sample
    return tuple(samples), samples_by_id


def _build_selector_candidates(
    logical_samples: tuple[InputProcessingLogicalSample, ...],
) -> tuple[SampleSelectorCandidate, ...]:
    return tuple(
        SampleSelectorCandidate(
            sample_id=sample.sample_id,
            sequence_id=sample.sequence_id,
            record_id=sample.original_record_id,
            source_reference=sample.provenance.input_manifest_source_reference,
            materialized_relative_path=sample.provenance.materialized_relative_path,
            eligible_for_analysis=sample.eligible_for_analysis,
            input_order=input_order,
        )
        for input_order, sample in enumerate(logical_samples)
    )


def _append_bidirectional_combinations(
    *,
    occurrences: list[_OperationOccurrence],
    samples: tuple[ComparisonPlanSample, ...],
    source_kind: ComparisonSourceKind,
) -> None:
    for first, second in combinations(samples, 2):
        occurrences.append(
            _OperationOccurrence(left=first, right=second, source_kind=source_kind)
        )
        occurrences.append(
            _OperationOccurrence(left=second, right=first, source_kind=source_kind)
        )


def _deduplicate_plan(
    occurrences: list[_OperationOccurrence],
) -> tuple[
    tuple[DirectedLogicalComparison, ...],
    tuple[SequenceComparisonComputation, ...],
]:
    merged_by_samples: dict[tuple[str, str], _MergedOperation] = {}
    for occurrence in occurrences:
        key = (occurrence.left.sample_id, occurrence.right.sample_id)
        merged = merged_by_samples.get(key)
        if merged is None:
            merged = _MergedOperation(left=occurrence.left, right=occurrence.right)
            merged_by_samples[key] = merged
        merged.source_kinds.add(occurrence.source_kind)
        merged.occurrence_count += 1

    merged_operations = sorted(
        merged_by_samples.values(),
        key=lambda operation: (operation.left.input_order, operation.right.input_order),
    )
    computations_by_pair: dict[frozenset[str], _ComputationAccumulator] = {}
    operation_projection_data: list[
        tuple[_MergedOperation, int | None, bool]
    ] = []
    for operation in merged_operations:
        left_sequence_id = operation.left.sequence_id
        right_sequence_id = operation.right.sequence_id
        if left_sequence_id == right_sequence_id:
            operation_projection_data.append((operation, None, False))
            continue
        computation_key = frozenset((left_sequence_id, right_sequence_id))
        computation = computations_by_pair.get(computation_key)
        if computation is None:
            computation = _ComputationAccumulator(
                index=len(computations_by_pair),
                first_sequence_id=left_sequence_id,
                second_sequence_id=right_sequence_id,
            )
            computations_by_pair[computation_key] = computation
        computation.logical_projection_count += 1
        reverse = (
            left_sequence_id != computation.first_sequence_id
            or right_sequence_id != computation.second_sequence_id
        )
        operation_projection_data.append((operation, computation.index, reverse))

    logical_operations = tuple(
        DirectedLogicalComparison(
            left_sample_id=operation.left.sample_id,
            right_sample_id=operation.right.sample_id,
            left_sequence_id=operation.left.sequence_id,
            right_sequence_id=operation.right.sequence_id,
            source_kinds=tuple(
                sorted(operation.source_kinds, key=lambda source: _SOURCE_ORDER[source])
            ),
            source_occurrence_count=operation.occurrence_count,
            requires_scan=computation_index is not None,
            computation_index=computation_index,
            reverse_computation=reverse,
        )
        for operation, computation_index, reverse in operation_projection_data
    )
    computations = tuple(
        SequenceComparisonComputation(
            computation_index=computation.index,
            first_sequence_id=computation.first_sequence_id,
            second_sequence_id=computation.second_sequence_id,
            logical_projection_count=computation.logical_projection_count,
        )
        for computation in computations_by_pair.values()
    )
    return logical_operations, computations
