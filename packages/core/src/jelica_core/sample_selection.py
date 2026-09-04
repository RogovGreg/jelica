from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class SampleSelectorResolutionMethod(StrEnum):
    RECORD_ID = "record_id"
    FILE_PATH_AND_RECORD_ID = "file_path_and_record_id"


class SampleSelectorResolutionReason(StrEnum):
    MALFORMED = "malformed"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    INELIGIBLE = "ineligible"


class SampleSelectorResolutionError(ValueError):
    """Safe selector error that never contains sequence content."""

    def __init__(
        self,
        *,
        reason: SampleSelectorResolutionReason,
        detail: str,
        selector: str | None = None,
        matched_sample_ids: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.selector = selector
        self.matched_sample_ids = matched_sample_ids
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class SampleSelectorCandidate:
    sample_id: str
    sequence_id: str | None
    record_id: str | None
    source_reference: str
    materialized_relative_path: str
    eligible_for_analysis: bool
    input_order: int


@dataclass(frozen=True, slots=True)
class ResolvedSampleSelector:
    original_selector: str
    sample_id: str
    sequence_id: str
    record_id: str
    source_reference: str
    materialized_relative_path: str
    input_order: int
    resolution_method: SampleSelectorResolutionMethod


class SampleSelectorResolver:
    """Resolve the shared ``record_id`` and ``path::record_id`` syntax."""

    def __init__(self, candidates: tuple[SampleSelectorCandidate, ...]) -> None:
        self._candidates = candidates

    def resolve(self, selector: str) -> ResolvedSampleSelector:
        normalized_selector = selector.strip()
        if normalized_selector == "":
            raise SampleSelectorResolutionError(
                reason=SampleSelectorResolutionReason.MALFORMED,
                detail="Sample selector must not be empty.",
            )

        if "::" not in normalized_selector:
            return self._resolve_candidates(
                selector=normalized_selector,
                candidates=tuple(
                    candidate
                    for candidate in self._candidates
                    if _optional_text(candidate.record_id) == normalized_selector
                ),
                resolution_method=SampleSelectorResolutionMethod.RECORD_ID,
                not_found_detail="Sample selector did not match any record ID.",
            )

        path_part, record_id_part = normalized_selector.rsplit("::", maxsplit=1)
        reference_path = _optional_text(path_part)
        record_id = _optional_text(record_id_part)
        if reference_path is None or record_id is None:
            raise SampleSelectorResolutionError(
                reason=SampleSelectorResolutionReason.MALFORMED,
                detail=(
                    "Qualified sample selector must use '<path>::<record_id>' form."
                ),
                selector=normalized_selector,
            )

        normalized_path = normalize_sample_path_identity(reference_path)
        candidates_in_path = tuple(
            candidate
            for candidate in self._candidates
            if normalized_path in _candidate_path_identities(candidate)
        )
        if len(candidates_in_path) == 0:
            raise SampleSelectorResolutionError(
                reason=SampleSelectorResolutionReason.NOT_FOUND,
                detail="Qualified sample selector path is not present in task inputs.",
                selector=normalized_selector,
            )

        return self._resolve_candidates(
            selector=normalized_selector,
            candidates=tuple(
                candidate
                for candidate in candidates_in_path
                if _optional_text(candidate.record_id) == record_id
            ),
            resolution_method=SampleSelectorResolutionMethod.FILE_PATH_AND_RECORD_ID,
            not_found_detail=(
                "Qualified sample selector did not match a record ID in the selected input."
            ),
        )

    def _resolve_candidates(
        self,
        *,
        selector: str,
        candidates: tuple[SampleSelectorCandidate, ...],
        resolution_method: SampleSelectorResolutionMethod,
        not_found_detail: str,
    ) -> ResolvedSampleSelector:
        if len(candidates) == 0:
            raise SampleSelectorResolutionError(
                reason=SampleSelectorResolutionReason.NOT_FOUND,
                detail=not_found_detail,
                selector=selector,
            )

        eligible = tuple(
            candidate for candidate in candidates if candidate.eligible_for_analysis
        )
        if len(eligible) > 1:
            raise SampleSelectorResolutionError(
                reason=SampleSelectorResolutionReason.AMBIGUOUS,
                detail="Sample selector matched multiple eligible logical samples.",
                selector=selector,
                matched_sample_ids=tuple(candidate.sample_id for candidate in eligible),
            )
        if len(eligible) == 0:
            raise SampleSelectorResolutionError(
                reason=SampleSelectorResolutionReason.INELIGIBLE,
                detail="Sample selector matched only ineligible logical samples.",
                selector=selector,
            )

        candidate = eligible[0]
        record_id = _optional_text(candidate.record_id)
        sequence_id = _optional_text(candidate.sequence_id)
        if record_id is None or sequence_id is None:
            raise SampleSelectorResolutionError(
                reason=SampleSelectorResolutionReason.INELIGIBLE,
                detail="Resolved logical sample lacks a required identifier.",
                selector=selector,
                matched_sample_ids=(candidate.sample_id,),
            )
        return ResolvedSampleSelector(
            original_selector=selector,
            sample_id=candidate.sample_id,
            sequence_id=sequence_id,
            record_id=record_id,
            source_reference=candidate.source_reference,
            materialized_relative_path=candidate.materialized_relative_path,
            input_order=candidate.input_order,
            resolution_method=resolution_method,
        )


def normalize_sample_path_identity(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if normalized == "":
        return ""
    is_absolute = normalized.startswith("/")
    collapsed = str(PurePosixPath(normalized))
    if is_absolute and not collapsed.startswith("/"):
        collapsed = f"/{collapsed}"
    return collapsed


def _candidate_path_identities(candidate: SampleSelectorCandidate) -> set[str]:
    return {
        normalize_sample_path_identity(candidate.source_reference),
        normalize_sample_path_identity(candidate.materialized_relative_path),
    }


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
