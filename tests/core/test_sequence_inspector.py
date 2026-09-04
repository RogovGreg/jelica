from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from jelica_core.config import (
    AnalysisAlignmentMode,
    AnalysisKmerStrand,
    ResolvedAnalysisStatisticsConfig,
)
from jelica_core.runtime.input_processing_models import (
    InputProcessingCoordinateSystem,
    KmerMatchKind,
    ParsedInputRecord,
    sequence_id_from_digest,
)
from jelica_core.runtime.sequence_inspector import SequenceInspector


def _parsed_record(sequence: str) -> ParsedInputRecord:
    return ParsedInputRecord(
        input_manifest_source_reference="source-a",
        input_manifest_relative_path="inputs/input_manifest.json",
        materialized_relative_path="inputs/files/0001.fasta",
        format_hint=".fasta",
        record_index=0,
        record_id="rec-1",
        description=None,
        metadata={},
        raw_sequence=sequence,
    )


def _statistics(
    *,
    kmers: tuple[str, ...] = (),
    strand: AnalysisKmerStrand = AnalysisKmerStrand.FORWARD,
) -> ResolvedAnalysisStatisticsConfig:
    return ResolvedAnalysisStatisticsConfig(kmers=list(kmers), kmer_strand=strand)


def test_inspector_collects_lengths_symbol_categories_and_gc_metrics() -> None:
    inspector = SequenceInspector()
    record = _parsed_record("ACGTURYSWKMBDHVN-?")
    result = inspector.inspect(
        record,
        statistics_config=_statistics(),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    facts = result.facts

    assert facts.source_length == 18
    assert facts.ungapped_length == 17
    assert facts.recognized_nucleotide_count == 16
    assert facts.canonical_count == 5
    assert facts.ambiguous_count == 11
    assert facts.gap_count == 1
    assert facts.invalid_symbol_count == 1
    assert facts.invalid_symbol_counts == {"?": 1}
    assert facts.invalid_positions == (17,)
    assert facts.invalid_positions_truncated is False
    assert facts.u_count == 1
    assert facts.gc_count == 2
    assert facts.gc_content_total == pytest.approx(0.125)
    assert facts.resolved_gc_content == pytest.approx(0.4)
    assert facts.expected_gc_count == pytest.approx(7.5)
    assert facts.expected_gc_content == pytest.approx(7.5 / 16.0)
    assert facts.symbol_counts["T"] == 1
    assert facts.symbol_counts["U"] == 1


def test_inspector_limits_invalid_positions_and_sets_truncation_flag() -> None:
    inspector = SequenceInspector()
    record = _parsed_record("A" + ("!" * 25))
    result = inspector.inspect(
        record,
        statistics_config=_statistics(),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert result.facts.invalid_symbol_count == 25
    assert result.facts.invalid_symbol_counts == {"!": 25}
    assert result.facts.invalid_positions == tuple(range(1, 21))
    assert result.facts.invalid_positions_truncated is True


def test_gc_content_fields_are_none_when_denominator_is_zero() -> None:
    inspector = SequenceInspector()
    record = _parsed_record("--??")
    result = inspector.inspect(
        record,
        statistics_config=_statistics(),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert result.facts.gc_content_total is None
    assert result.facts.resolved_gc_content is None
    assert result.facts.expected_gc_content is None
    assert result.facts.expected_gc_count == pytest.approx(0.0)


def test_digest_is_stable_and_preserves_u_gap_and_invalid_symbols() -> None:
    inspector = SequenceInspector()
    facts_auug = inspector.inspect(
        _parsed_record("AUUG"),
        statistics_config=_statistics(),
        alignment_mode=AnalysisAlignmentMode.NONE,
    ).facts
    facts_attg = inspector.inspect(
        _parsed_record("ATTG"),
        statistics_config=_statistics(),
        alignment_mode=AnalysisAlignmentMode.NONE,
    ).facts
    facts_gapped = inspector.inspect(
        _parsed_record("AC-GT"),
        statistics_config=_statistics(),
        alignment_mode=AnalysisAlignmentMode.NONE,
    ).facts
    facts_ungapped = inspector.inspect(
        _parsed_record("ACGT"),
        statistics_config=_statistics(),
        alignment_mode=AnalysisAlignmentMode.NONE,
    ).facts

    assert facts_auug.sequence_id == sequence_id_from_digest(
        hashlib.sha256("AUUG".encode("utf-8")).hexdigest()
    )
    assert facts_auug.sequence_id != facts_attg.sequence_id
    assert facts_gapped.sequence_id != facts_ungapped.sequence_id


def test_kmer_forward_overlapping_matches() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("AAAAA"),
        statistics_config=_statistics(kmers=("AAA",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    summary = result.facts.kmer_summaries[0]
    hits = result.kmer_hits[0].hits

    assert summary.definite_match_count == 3
    assert summary.possible_match_count == 0
    assert [hit.sequence_range.start for hit in hits] == [0, 1, 2]
    assert [hit.sequence_range.end for hit in hits] == [3, 4, 5]
    assert all(hit.strand == "+" for hit in hits)


def test_kmer_query_longer_than_sequence_produces_no_hits() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("ACG"),
        statistics_config=_statistics(kmers=("ACGT",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    summary = result.facts.kmer_summaries[0]

    assert summary.definite_match_count == 0
    assert summary.possible_match_count == 0
    assert result.kmer_hits[0].hits == ()


def test_kmer_matching_treats_t_and_u_as_equivalent_for_matching_only() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("AUUG"),
        statistics_config=_statistics(kmers=("ATTG",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    summary = result.facts.kmer_summaries[0]

    assert summary.definite_match_count == 1
    assert summary.possible_match_count == 0
    assert result.normalized_sequence == "AUUG"


def test_invalid_symbol_breaks_kmer_window() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("AA?AA"),
        statistics_config=_statistics(kmers=("AAA",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert result.facts.kmer_summaries[0].definite_match_count == 0
    assert result.kmer_hits[0].hits == ()


def test_definite_and_possible_matching_are_exclusive() -> None:
    inspector = SequenceInspector()
    definite = inspector.inspect(
        _parsed_record("AA"),
        statistics_config=_statistics(kmers=("RR",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    possible_from_n = inspector.inspect(
        _parsed_record("NN"),
        statistics_config=_statistics(kmers=("RR",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    possible_from_r = inspector.inspect(
        _parsed_record("RR"),
        statistics_config=_statistics(kmers=("GG",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    no_match = inspector.inspect(
        _parsed_record("CC"),
        statistics_config=_statistics(kmers=("AA",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert definite.facts.kmer_summaries[0].definite_match_count == 1
    assert definite.facts.kmer_summaries[0].possible_match_count == 0
    assert definite.kmer_hits[0].hits[0].match_kind == KmerMatchKind.DEFINITE

    assert possible_from_n.facts.kmer_summaries[0].definite_match_count == 0
    assert possible_from_n.facts.kmer_summaries[0].possible_match_count == 1
    assert possible_from_n.kmer_hits[0].hits[0].match_kind == KmerMatchKind.POSSIBLE

    assert possible_from_r.facts.kmer_summaries[0].definite_match_count == 0
    assert possible_from_r.facts.kmer_summaries[0].possible_match_count == 1
    assert possible_from_r.kmer_hits[0].hits[0].match_kind == KmerMatchKind.POSSIBLE

    assert no_match.facts.kmer_summaries[0].definite_match_count == 0
    assert no_match.facts.kmer_summaries[0].possible_match_count == 0
    assert no_match.kmer_hits[0].hits == ()


def test_reverse_complement_strand_search() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("CAT"),
        statistics_config=_statistics(
            kmers=("ATG",),
            strand=AnalysisKmerStrand.REVERSE_COMPLEMENT,
        ),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    summary = result.facts.kmer_summaries[0]
    hits = result.kmer_hits[0].hits

    assert summary.definite_match_count == 1
    assert summary.possible_match_count == 0
    assert len(hits) == 1
    assert hits[0].strand == "-"


def test_both_strands_searches_forward_and_reverse_complement() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("ATGCAT"),
        statistics_config=_statistics(kmers=("ATG",), strand=AnalysisKmerStrand.BOTH),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    summary = result.facts.kmer_summaries[0]
    hits = result.kmer_hits[0].hits

    assert summary.definite_match_count == 2
    assert summary.possible_match_count == 0
    assert [hit.strand for hit in hits] == ["+", "-"]


def test_palindromic_kmer_in_both_mode_is_not_double_counted() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("ATATAT"),
        statistics_config=_statistics(kmers=("ATAT",), strand=AnalysisKmerStrand.BOTH),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    summary = result.facts.kmer_summaries[0]
    hits = result.kmer_hits[0].hits

    assert summary.definite_match_count == 2
    assert summary.possible_match_count == 0
    assert len(hits) == 2
    assert all(hit.strand == "+" for hit in hits)


def test_prealigned_mode_kmer_search_skips_gaps_and_sets_alignment_coordinates() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("A-CG"),
        statistics_config=_statistics(kmers=("ACG",)),
        alignment_mode=AnalysisAlignmentMode.PREALIGNED,
    )
    summary = result.facts.kmer_summaries[0]
    hit = result.kmer_hits[0].hits[0]

    assert summary.definite_match_count == 1
    assert hit.sequence_range.start == 0
    assert hit.sequence_range.end == 3
    assert hit.alignment_range is not None
    assert hit.alignment_range.start == 0
    assert hit.alignment_range.end == 4
    assert hit.coordinate_system == InputProcessingCoordinateSystem.ZERO_BASED_END_EXCLUSIVE


def test_none_mode_gap_breaks_kmer_window() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("AC-GT"),
        statistics_config=_statistics(kmers=("ACGT",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert result.facts.kmer_summaries[0].definite_match_count == 0
    assert result.kmer_hits[0].hits == ()


def test_compute_mode_gap_is_handled_without_crashing() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("AC-G"),
        statistics_config=_statistics(kmers=("ACG",)),
        alignment_mode=AnalysisAlignmentMode.COMPUTE,
    )

    assert result.facts.gap_count == 1
    assert result.facts.kmer_summaries[0].definite_match_count == 0


@dataclass(slots=True)
class _OneShotSequence:
    payload: str
    iterated: bool = False

    def __iter__(self) -> Iterator[str]:
        if self.iterated:
            raise RuntimeError("second iteration is not allowed")
        self.iterated = True
        return iter(self.payload)


def test_inspector_uses_single_pass_for_counts_gc_digest_and_kmers() -> None:
    inspector = SequenceInspector()
    one_shot = _OneShotSequence("A-CGN")
    result = inspector.inspect_sequence(
        sequence=one_shot,
        statistics_config=_statistics(kmers=("ACG",), strand=AnalysisKmerStrand.FORWARD),
        alignment_mode=AnalysisAlignmentMode.PREALIGNED,
    )

    assert result.facts.source_length == 5
    assert result.facts.gc_count == 2
    assert result.facts.sequence_id == sequence_id_from_digest(
        hashlib.sha256("A-CGN".encode("utf-8")).hexdigest()
    )
    assert result.facts.kmer_summaries[0].definite_match_count == 1


def test_inspector_calls_control_check_periodically_without_second_pass() -> None:
    inspector = SequenceInspector(control_check_interval=4)
    one_shot = _OneShotSequence("ACGT" * 5)
    callback_calls = 0

    def _control_check() -> None:
        nonlocal callback_calls
        callback_calls += 1

    result = inspector.inspect_sequence(
        sequence=one_shot,
        statistics_config=_statistics(kmers=("AC",), strand=AnalysisKmerStrand.FORWARD),
        alignment_mode=AnalysisAlignmentMode.NONE,
        control_check=_control_check,
    )

    assert callback_calls == 5
    assert one_shot.iterated is True
    assert result.facts.source_length == 20


def test_inspector_control_check_can_interrupt_single_pass() -> None:
    inspector = SequenceInspector(control_check_interval=3)
    one_shot = _OneShotSequence("A" * 20)
    callback_calls = 0

    class _InspectionStopped(RuntimeError):
        pass

    def _control_check() -> None:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 2:
            raise _InspectionStopped("cancelled")

    with pytest.raises(_InspectionStopped):
        inspector.inspect_sequence(
            sequence=one_shot,
            statistics_config=_statistics(),
            alignment_mode=AnalysisAlignmentMode.NONE,
            control_check=_control_check,
        )
    assert callback_calls == 2
    assert one_shot.iterated is True


def test_sequence_inspection_result_is_json_serializable_without_nan() -> None:
    inspector = SequenceInspector()
    result = inspector.inspect(
        _parsed_record("--??"),
        statistics_config=_statistics(kmers=("AT",)),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )
    payload = {
        "facts": result.facts.model_dump(mode="json"),
        "kmer_hits": [entry.model_dump(mode="json") for entry in result.kmer_hits],
    }
    encoded = json.dumps(payload, allow_nan=False)

    assert isinstance(encoded, str)
