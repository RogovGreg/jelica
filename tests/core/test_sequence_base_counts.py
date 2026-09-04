from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from jelica_core.config import AnalysisAlignmentMode, ResolvedAnalysisStatisticsConfig
from jelica_core.runtime.input_processing_models import (
    CanonicalBaseCounts,
    SequenceFacts,
)
from jelica_core.runtime.sequence_inspector import (
    SequenceInspectionResult,
    SequenceInspector,
)


def _inspect(sequence: str | Iterable[str]) -> SequenceInspectionResult:
    return SequenceInspector().inspect_sequence(
        sequence=sequence,
        statistics_config=ResolvedAnalysisStatisticsConfig(),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )


def test_canonical_symbols_have_equal_definite_and_potential_counts() -> None:
    facts = _inspect("AACCGGTTUU").facts
    expected = CanonicalBaseCounts(A=2, C=2, G=2, T=2, U=2)

    assert facts.base_counts.definite == expected
    assert facts.base_counts.potential == expected


def test_r_increases_only_potential_a_and_g_counts() -> None:
    facts = _inspect("R").facts

    assert facts.base_counts.definite == CanonicalBaseCounts(A=0, C=0, G=0, T=0, U=0)
    assert facts.base_counts.potential == CanonicalBaseCounts(A=1, C=0, G=1, T=0, U=0)


def test_multiple_iupac_symbols_increase_their_potential_counts() -> None:
    facts = _inspect("RYSWKMBDHV").facts

    assert facts.base_counts.definite == CanonicalBaseCounts(A=0, C=0, G=0, T=0, U=0)
    assert facts.base_counts.potential == CanonicalBaseCounts(A=6, C=6, G=6, T=6, U=6)


def test_n_uses_existing_iupac_mask_for_all_supported_bases() -> None:
    facts = _inspect("N").facts

    assert facts.base_counts.definite == CanonicalBaseCounts(A=0, C=0, G=0, T=0, U=0)
    assert facts.base_counts.potential == CanonicalBaseCounts(A=1, C=1, G=1, T=1, U=1)


def test_t_and_u_counts_remain_separate() -> None:
    facts = _inspect("TTUUU").facts
    expected = CanonicalBaseCounts(A=0, C=0, G=0, T=2, U=3)

    assert facts.base_counts.definite == expected
    assert facts.base_counts.potential == expected


def test_potential_count_sum_can_exceed_sequence_length() -> None:
    facts = _inspect("N").facts
    potential = facts.base_counts.potential
    potential_sum = potential.A + potential.C + potential.G + potential.T + potential.U

    assert potential_sum == 5
    assert potential_sum > facts.source_length


@dataclass(slots=True)
class _OneShotSequence:
    payload: str
    iterated: bool = False

    def __iter__(self) -> Iterator[str]:
        if self.iterated:
            raise AssertionError("sequence was iterated more than once")
        self.iterated = True
        return iter(self.payload)


def test_base_counts_are_calculated_in_existing_single_pass() -> None:
    sequence = _OneShotSequence("A-CGN")

    facts = _inspect(sequence).facts

    assert sequence.iterated is True
    assert facts.base_counts.definite == CanonicalBaseCounts(A=1, C=1, G=1, T=0, U=0)
    assert facts.base_counts.potential == CanonicalBaseCounts(A=2, C=2, G=2, T=1, U=1)


def test_sequence_facts_base_counts_json_round_trip_is_stable() -> None:
    facts = _inspect("ARNU").facts

    encoded = json.dumps(facts.model_dump(mode="json"), allow_nan=False)
    restored = SequenceFacts.model_validate_json(encoded)

    assert restored == facts
    assert restored.model_dump(mode="json") == facts.model_dump(mode="json")


def test_legacy_sequence_facts_derive_missing_base_counts_from_symbol_counts() -> None:
    facts = _inspect("ARNU").facts
    legacy_payload = facts.model_dump(mode="json")
    del legacy_payload["base_counts"]

    restored = SequenceFacts.model_validate(legacy_payload)

    assert restored.base_counts == facts.base_counts


def test_sequence_facts_reject_inconsistent_base_counts() -> None:
    payload = _inspect("ARNU").facts.model_dump(mode="json")
    base_counts = payload["base_counts"]
    assert isinstance(base_counts, dict)
    definite = base_counts["definite"]
    assert isinstance(definite, dict)
    definite["A"] = 99

    with pytest.raises(ValidationError):
        SequenceFacts.model_validate(payload)
