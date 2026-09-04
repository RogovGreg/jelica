from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Callable, Final, Iterable

from jelica_core.config import (
    AnalysisAlignmentMode,
    AnalysisKmerStrand,
    ResolvedAnalysisStatisticsConfig,
)

from .input_processing_models import (
    KmerCoordinateRange,
    KmerHit,
    KmerMatchKind,
    KmerQueryHits,
    KmerQuerySummary,
    ParsedInputRecord,
    SequenceBaseCounts,
    SequenceFacts,
    SequenceStrand,
    sequence_id_from_digest,
)

INVALID_POSITIONS_LIMIT: Final = 20
CONTROL_CHECK_INTERVAL: Final = 16_384

_CANONICAL_SYMBOLS: Final[frozenset[str]] = frozenset({"A", "C", "G", "T", "U"})
_AMBIGUOUS_SYMBOLS: Final[frozenset[str]] = frozenset(
    {"R", "Y", "S", "W", "K", "M", "B", "D", "H", "V", "N"}
)
_RECOGNIZED_SYMBOLS: Final[frozenset[str]] = frozenset((*_CANONICAL_SYMBOLS, *_AMBIGUOUS_SYMBOLS))

_MASK_A: Final = 1
_MASK_C: Final = 2
_MASK_G: Final = 4
_MASK_T: Final = 8

_MATCH_MASK_BY_SYMBOL: Final[dict[str, int]] = {
    "A": _MASK_A,
    "C": _MASK_C,
    "G": _MASK_G,
    "T": _MASK_T,
    "U": _MASK_T,
    "R": _MASK_A | _MASK_G,
    "Y": _MASK_C | _MASK_T,
    "S": _MASK_G | _MASK_C,
    "W": _MASK_A | _MASK_T,
    "K": _MASK_G | _MASK_T,
    "M": _MASK_A | _MASK_C,
    "B": _MASK_C | _MASK_G | _MASK_T,
    "D": _MASK_A | _MASK_G | _MASK_T,
    "H": _MASK_A | _MASK_C | _MASK_T,
    "V": _MASK_A | _MASK_C | _MASK_G,
    "N": _MASK_A | _MASK_C | _MASK_G | _MASK_T,
}

_EXPECTED_GC_BY_SYMBOL: Final[dict[str, float]] = {
    "A": 0.0,
    "C": 1.0,
    "G": 1.0,
    "T": 0.0,
    "U": 0.0,
    "R": 0.5,
    "Y": 0.5,
    "S": 1.0,
    "W": 0.0,
    "K": 0.5,
    "M": 0.5,
    "B": 2.0 / 3.0,
    "D": 1.0 / 3.0,
    "H": 1.0 / 3.0,
    "V": 2.0 / 3.0,
    "N": 0.5,
}

_COMPLEMENT_BY_SYMBOL: Final[dict[str, str]] = {
    "A": "T",
    "C": "G",
    "G": "C",
    "T": "A",
    "U": "A",
    "R": "Y",
    "Y": "R",
    "S": "S",
    "W": "W",
    "K": "M",
    "M": "K",
    "B": "V",
    "D": "H",
    "H": "D",
    "V": "B",
    "N": "N",
}


@dataclass(frozen=True, slots=True)
class SequenceInspectionResult:
    normalized_sequence: str
    facts: SequenceFacts
    kmer_hits: tuple[KmerQueryHits, ...]


@dataclass(frozen=True, slots=True)
class _CompiledSearchPattern:
    query_index: int
    strand: SequenceStrand
    masks: tuple[int, ...]


@dataclass(slots=True)
class _MutableQueryStats:
    query: str
    definite_match_count: int = 0
    possible_match_count: int = 0
    hits: list[KmerHit] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _WindowSymbol:
    mask: int
    sequence_index: int
    alignment_index: int


@dataclass(frozen=True, slots=True)
class _CompiledKmerSearch:
    strand_mode: AnalysisKmerStrand
    query_stats: tuple[_MutableQueryStats, ...]
    patterns_by_length: dict[int, tuple[_CompiledSearchPattern, ...]]


@dataclass(slots=True)
class SequenceInspector:
    invalid_positions_limit: int = INVALID_POSITIONS_LIMIT
    control_check_interval: int = CONTROL_CHECK_INTERVAL

    def inspect(
        self,
        parsed_record: ParsedInputRecord,
        *,
        statistics_config: ResolvedAnalysisStatisticsConfig,
        alignment_mode: AnalysisAlignmentMode | str,
        control_check: Callable[[], None] | None = None,
    ) -> SequenceInspectionResult:
        if parsed_record.raw_sequence is None:
            raise ValueError("parsed_record.raw_sequence is required for sequence inspection")
        return self.inspect_sequence(
            sequence=parsed_record.raw_sequence,
            statistics_config=statistics_config,
            alignment_mode=alignment_mode,
            control_check=control_check,
        )

    def inspect_sequence(
        self,
        *,
        sequence: str | Iterable[str],
        statistics_config: ResolvedAnalysisStatisticsConfig,
        alignment_mode: AnalysisAlignmentMode | str,
        control_check: Callable[[], None] | None = None,
    ) -> SequenceInspectionResult:
        if self.control_check_interval <= 0:
            raise ValueError("control_check_interval must be > 0")
        resolved_alignment_mode = _resolve_alignment_mode(alignment_mode)
        compiled_search = _compile_kmer_search(statistics_config=statistics_config)
        window_buffers: dict[int, deque[_WindowSymbol]] = {
            length: deque(maxlen=length) for length in compiled_search.patterns_by_length
        }

        hasher = sha256()
        normalized_sequence_chars: list[str] = []
        source_length = 0
        ungapped_length = 0
        recognized_nucleotide_count = 0
        symbol_counts: dict[str, int] = {}
        canonical_count = 0
        ambiguous_count = 0
        gap_count = 0
        invalid_symbol_count = 0
        invalid_symbol_counts: dict[str, int] = {}
        invalid_positions: list[int] = []
        invalid_positions_truncated = False
        gc_count = 0
        expected_gc_count = 0.0
        u_count = 0
        next_ungapped_index = 0

        for alignment_index, raw_symbol in enumerate(iter(sequence)):
            if not isinstance(raw_symbol, str) or len(raw_symbol) != 1:
                raise ValueError("sequence iterable must yield single-character strings")

            symbol = raw_symbol.upper()
            normalized_sequence_chars.append(symbol)
            hasher.update(symbol.encode("utf-8"))
            source_length += 1
            if control_check is not None and source_length % self.control_check_interval == 0:
                control_check()

            if symbol in _RECOGNIZED_SYMBOLS:
                ungapped_length += 1
                recognized_nucleotide_count += 1
                symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
                expected_gc_count += _EXPECTED_GC_BY_SYMBOL[symbol]

                if symbol in _CANONICAL_SYMBOLS:
                    canonical_count += 1
                else:
                    ambiguous_count += 1
                if symbol == "U":
                    u_count += 1
                if symbol in {"G", "C"}:
                    gc_count += 1

                sequence_index = next_ungapped_index
                next_ungapped_index += 1
                window_symbol = _WindowSymbol(
                    mask=_MATCH_MASK_BY_SYMBOL[symbol],
                    sequence_index=sequence_index,
                    alignment_index=alignment_index,
                )
                _advance_kmer_windows(
                    window_symbol=window_symbol,
                    window_buffers=window_buffers,
                    compiled_search=compiled_search,
                    alignment_mode=resolved_alignment_mode,
                )
                continue

            if symbol == "-":
                gap_count += 1
                if resolved_alignment_mode != AnalysisAlignmentMode.PREALIGNED:
                    _clear_windows(window_buffers)
                continue

            invalid_symbol_count += 1
            invalid_symbol_counts[symbol] = invalid_symbol_counts.get(symbol, 0) + 1
            if len(invalid_positions) < self.invalid_positions_limit:
                invalid_positions.append(alignment_index)
            else:
                invalid_positions_truncated = True
            ungapped_length += 1
            next_ungapped_index += 1
            _clear_windows(window_buffers)

        gc_content_total = _safe_ratio(gc_count, recognized_nucleotide_count)
        resolved_gc_content = _safe_ratio(gc_count, canonical_count)
        expected_gc_content = _safe_ratio(expected_gc_count, recognized_nucleotide_count)
        base_counts = SequenceBaseCounts.from_symbol_counts(symbol_counts)

        normalized_sequence = "".join(normalized_sequence_chars)
        facts = SequenceFacts(
            source_length=source_length,
            ungapped_length=ungapped_length,
            recognized_nucleotide_count=recognized_nucleotide_count,
            symbol_counts=symbol_counts,
            base_counts=base_counts,
            canonical_count=canonical_count,
            ambiguous_count=ambiguous_count,
            gap_count=gap_count,
            invalid_symbol_count=invalid_symbol_count,
            invalid_symbol_counts=invalid_symbol_counts,
            invalid_positions=tuple(invalid_positions),
            invalid_positions_truncated=invalid_positions_truncated,
            gc_count=gc_count,
            gc_content_total=gc_content_total,
            resolved_gc_content=resolved_gc_content,
            expected_gc_count=expected_gc_count,
            expected_gc_content=expected_gc_content,
            u_count=u_count,
            sequence_id=sequence_id_from_digest(hasher.hexdigest()),
            kmer_summaries=tuple(
                KmerQuerySummary(
                    query=item.query,
                    definite_match_count=item.definite_match_count,
                    possible_match_count=item.possible_match_count,
                    strand=compiled_search.strand_mode,
                )
                for item in compiled_search.query_stats
            ),
        )
        kmer_hits = tuple(
            KmerQueryHits(
                query=item.query,
                strand=compiled_search.strand_mode,
                hits=tuple(item.hits),
            )
            for item in compiled_search.query_stats
        )
        return SequenceInspectionResult(
            normalized_sequence=normalized_sequence,
            facts=facts,
            kmer_hits=kmer_hits,
        )


def _resolve_alignment_mode(alignment_mode: AnalysisAlignmentMode | str) -> AnalysisAlignmentMode:
    if isinstance(alignment_mode, AnalysisAlignmentMode):
        return alignment_mode
    return AnalysisAlignmentMode(alignment_mode.strip().lower())


def _compile_kmer_search(
    *,
    statistics_config: ResolvedAnalysisStatisticsConfig,
) -> _CompiledKmerSearch:
    query_stats = tuple(_MutableQueryStats(query=query) for query in statistics_config.kmers)
    patterns_by_length: dict[int, list[_CompiledSearchPattern]] = {}
    strand_mode = statistics_config.kmer_strand

    for index, query in enumerate(statistics_config.kmers):
        forward_masks = tuple(_MATCH_MASK_BY_SYMBOL[symbol] for symbol in query)
        reverse_masks = tuple(
            _MATCH_MASK_BY_SYMBOL[_COMPLEMENT_BY_SYMBOL[symbol]]
            for symbol in reversed(query)
        )
        length = len(query)

        if strand_mode == AnalysisKmerStrand.FORWARD:
            patterns_by_length.setdefault(length, []).append(
                _CompiledSearchPattern(
                    query_index=index,
                    strand=SequenceStrand.PLUS,
                    masks=forward_masks,
                )
            )
            continue

        if strand_mode == AnalysisKmerStrand.REVERSE_COMPLEMENT:
            patterns_by_length.setdefault(length, []).append(
                _CompiledSearchPattern(
                    query_index=index,
                    strand=SequenceStrand.MINUS,
                    masks=reverse_masks,
                )
            )
            continue

        patterns_by_length.setdefault(length, []).append(
            _CompiledSearchPattern(
                query_index=index,
                strand=SequenceStrand.PLUS,
                masks=forward_masks,
            )
        )
        if reverse_masks != forward_masks:
            patterns_by_length[length].append(
                _CompiledSearchPattern(
                    query_index=index,
                    strand=SequenceStrand.MINUS,
                    masks=reverse_masks,
                )
            )

    return _CompiledKmerSearch(
        strand_mode=strand_mode,
        query_stats=query_stats,
        patterns_by_length={
            length: tuple(patterns)
            for length, patterns in sorted(patterns_by_length.items(), key=lambda item: item[0])
        },
    )


def _advance_kmer_windows(
    *,
    window_symbol: _WindowSymbol,
    window_buffers: dict[int, deque[_WindowSymbol]],
    compiled_search: _CompiledKmerSearch,
    alignment_mode: AnalysisAlignmentMode,
) -> None:
    for length, patterns in compiled_search.patterns_by_length.items():
        window = window_buffers[length]
        window.append(window_symbol)
        if len(window) < length:
            continue

        window_masks = tuple(item.mask for item in window)
        window_start = window[0]
        window_end = window[-1]
        sequence_range = KmerCoordinateRange(
            start=window_start.sequence_index,
            end=window_end.sequence_index + 1,
        )
        alignment_range: KmerCoordinateRange | None = None
        if alignment_mode == AnalysisAlignmentMode.PREALIGNED:
            alignment_range = KmerCoordinateRange(
                start=window_start.alignment_index,
                end=window_end.alignment_index + 1,
            )

        for pattern in patterns:
            match_kind = _classify_match_kind(window_masks=window_masks, query_masks=pattern.masks)
            if match_kind is None:
                continue
            query_stats = compiled_search.query_stats[pattern.query_index]
            if match_kind == KmerMatchKind.DEFINITE:
                query_stats.definite_match_count += 1
            else:
                query_stats.possible_match_count += 1
            query_stats.hits.append(
                KmerHit(
                    match_kind=match_kind,
                    strand=pattern.strand,
                    sequence_range=sequence_range,
                    alignment_range=alignment_range,
                )
            )


def _classify_match_kind(
    *,
    window_masks: tuple[int, ...],
    query_masks: tuple[int, ...],
) -> KmerMatchKind | None:
    definite = True
    for window_mask, query_mask in zip(window_masks, query_masks, strict=True):
        intersection = window_mask & query_mask
        if intersection == 0:
            return None
        if window_mask & ~query_mask:
            definite = False
    if definite:
        return KmerMatchKind.DEFINITE
    return KmerMatchKind.POSSIBLE


def _clear_windows(window_buffers: dict[int, deque[_WindowSymbol]]) -> None:
    for window in window_buffers.values():
        window.clear()


def _safe_ratio(numerator: float, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator
