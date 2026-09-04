from __future__ import annotations

from enum import StrEnum
from typing import TypeAlias


class ComparisonErrorCategory(StrEnum):
    PLANNING = "comparison_planning"
    ALIGNED_INPUT = "comparison_aligned_input"
    STATISTICS = "comparison_statistics"


class ComparisonErrorCode(StrEnum):
    SELECTOR_NOT_FOUND = "COMPARISON_SELECTOR_NOT_FOUND"
    SELECTOR_AMBIGUOUS = "COMPARISON_SELECTOR_AMBIGUOUS"
    SELECTOR_INVALID = "COMPARISON_SELECTOR_INVALID"
    SELECTOR_RESOLVES_TO_SELF = "COMPARISON_SELECTOR_RESOLVES_TO_SELF"
    GROUP_RESOLVES_TOO_SMALL = "COMPARISON_GROUP_RESOLVES_TOO_SMALL"
    REFERENCE_REQUIRED = "COMPARISON_REFERENCE_REQUIRED"
    INPUT_INVARIANT_INVALID = "COMPARISON_INPUT_INVARIANT_INVALID"
    ALIGNED_SEQUENCE_MISSING = "COMPARISON_ALIGNED_SEQUENCE_MISSING"
    ALIGNED_LENGTH_MISMATCH = "COMPARISON_ALIGNED_LENGTH_MISMATCH"
    ALIGNED_SYMBOL_INVALID = "COMPARISON_ALIGNED_SYMBOL_INVALID"
    IDENTICAL_SEQUENCE_MISMATCH = "COMPARISON_IDENTICAL_SEQUENCE_MISMATCH"
    STATISTICS_INPUT_INVALID = "COMPARISON_STATISTICS_INPUT_INVALID"
    STATISTICS_KMER_DUPLICATE = "COMPARISON_STATISTICS_KMER_DUPLICATE"


SafeDetailValue: TypeAlias = str | int | tuple[str, ...]


class ComparisonDomainError(ValueError):
    """Structured, sequence-safe failure from the comparative domain core."""

    def __init__(
        self,
        *,
        code: ComparisonErrorCode,
        category: ComparisonErrorCategory,
        detail: str,
        safe_details: dict[str, SafeDetailValue] | None = None,
    ) -> None:
        self.code = code
        self.category = category
        self.detail = detail
        self.safe_details = safe_details or {}
        super().__init__(detail)


class ComparisonPlanningError(ComparisonDomainError):
    def __init__(
        self,
        *,
        code: ComparisonErrorCode,
        detail: str,
        safe_details: dict[str, SafeDetailValue] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            category=ComparisonErrorCategory.PLANNING,
            detail=detail,
            safe_details=safe_details,
        )


class AlignedComparisonError(ComparisonDomainError):
    def __init__(
        self,
        *,
        code: ComparisonErrorCode,
        detail: str,
        safe_details: dict[str, SafeDetailValue] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            category=ComparisonErrorCategory.ALIGNED_INPUT,
            detail=detail,
            safe_details=safe_details,
        )


class ComparativeStatisticsError(ComparisonDomainError):
    def __init__(
        self,
        *,
        code: ComparisonErrorCode,
        detail: str,
        safe_details: dict[str, SafeDetailValue] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            category=ComparisonErrorCategory.STATISTICS,
            detail=detail,
            safe_details=safe_details,
        )
