from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MafftOverrides(_StrictModel):
    strategy: (
        Literal[
            "auto",
            "fft_ns_1",
            "fft_ns_2",
            "fft_ns_i",
            "nw_ns_1",
            "nw_ns_2",
            "nw_ns_i",
            "g_ins_i",
            "l_ins_i",
            "e_ins_i",
        ]
        | None
    ) = None
    direction_adjustment: Literal["none", "fast", "accurate"] | None = None
    memory_mode: Literal["auto", "save"] | None = None
    threads: Literal["auto"] | int | None = Field(default=None, gt=0)
    gap_open_penalty: float | None = Field(default=None, ge=0)
    offset: float | None = Field(default=None, ge=0)
    progressive_threads: Literal["auto", "disabled"] | int | None = Field(default=None, gt=0)
    iterative_threads: Literal["auto", "disabled"] | int | None = Field(default=None, gt=0)

    @field_validator("gap_open_penalty", "offset", mode="before")
    @classmethod
    def _reject_boolean_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("numeric override must be a number")
        return value


class AlignmentOverrides(_StrictModel):
    mode: Literal["compute", "prealigned", "none"] | None = None
    engine: Literal["mafft"] | None = None
    construction: Literal["joint", "reference_guided"] | None = None
    mafft: MafftOverrides | None = None


class StatisticsOverrides(_StrictModel):
    kmers: list[str] | None = None
    kmer_strand: Literal["forward", "reverse_complement", "both"] | None = None

    @field_validator("kmers")
    @classmethod
    def _normalize_kmers(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [value.strip().upper() for value in values]
        if any(not value for value in normalized):
            raise ValueError("kmers must not contain empty values")
        return normalized


class ComparativeStatisticsOverrides(_StrictModel):
    enabled: bool | None = None


class SymbolPolicyOverrides(_StrictModel):
    uracil_thymine_equivalent: bool | None = None


class SequenceDifferencesOverrides(_StrictModel):
    enabled: bool | None = None
    substitutions: bool | None = None
    insertions: bool | None = None
    deletions: bool | None = None
    symbol_policy: SymbolPolicyOverrides | None = None


class ComparativeReferenceOverrides(_StrictModel):
    mode: Literal["auto", "enabled", "disabled"] | None = None


class PairwiseOverrides(_StrictModel):
    enabled: bool | None = None
    all: bool | None = None
    pairs_orientation: Literal["directed", "bidirectional"] | None = None
    groups: list[list[str]] | None = None
    pairs: list[list[str]] | None = None


class ComparativeOverrides(_StrictModel):
    enabled: bool | None = None
    statistics: ComparativeStatisticsOverrides | None = None
    sequence_differences: SequenceDifferencesOverrides | None = None
    reference: ComparativeReferenceOverrides | None = None
    pairwise: PairwiseOverrides | None = None


class DistanceMatrixOverrides(_StrictModel):
    enabled: bool | None = None
    model: Literal["p_distance"] | None = None


class PhylogeneticTreeOverrides(_StrictModel):
    enabled: bool | None = None
    method: Literal["neighbor_joining"] | None = None
    rooting: Literal["midpoint"] | None = None


class CladeOverrides(_StrictModel):
    enabled: bool | None = None
    method: Literal["max_pairwise_distance"] | None = None
    max_within_clade_distance: float | None = Field(default=None, ge=0, le=1)

    @field_validator("max_within_clade_distance", mode="before")
    @classmethod
    def _reject_boolean_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("numeric override must be a number")
        return value


class AnalysisOverrides(_StrictModel):
    alignment: AlignmentOverrides | None = None
    reference: str | None = None
    statistics: StatisticsOverrides | None = None
    comparative_analysis: ComparativeOverrides | None = None
    distance_matrix: DistanceMatrixOverrides | None = None
    phylogenetic_tree: PhylogeneticTreeOverrides | None = None
    clade_detection: CladeOverrides | None = None

    @field_validator("reference")
    @classmethod
    def _safe_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or normalized in {".", ".."} or normalized.startswith(("/", "\\", "~")):
            raise ValueError("reference must be a logical source selector, not a filesystem path")
        if len(normalized) >= 2 and normalized[1] == ":":
            raise ValueError("reference must be a logical source selector, not a filesystem path")
        if "\\" in normalized or "/" in normalized:
            raise ValueError("reference must use a logical source selector")
        return normalized


def cli_override_arguments(overrides: AnalysisOverrides | None) -> tuple[str, ...]:
    """Serialize only explicitly supplied fields to existing --path=<JSON value> syntax."""
    if overrides is None:
        return ()
    payload = overrides.model_dump(exclude_none=True, mode="json")
    result: list[str] = []

    def visit(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}.{key}" if path else key)
            return
        result.append(f"--{path}={json.dumps(value, separators=(',', ':'))}")

    for key, value in payload.items():
        visit(value, key)
    return tuple(result)


__all__ = ["AnalysisOverrides", "cli_override_arguments"]
