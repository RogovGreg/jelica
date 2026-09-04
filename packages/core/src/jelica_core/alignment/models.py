from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jelica_core.config import (
    AnalysisAlignmentConstruction,
    AnalysisAlignmentEngine,
    AnalysisAlignmentMode,
    AnalysisMafftStrategy,
    ResolvedAnalysisMafftConfig,
)

ALIGNMENT_STAGE_ID: Final = "alignment"
ALIGNMENT_MANIFEST_SCHEMA_VERSION: Final = 1
ALIGNMENT_COORDINATE_MAP_SCHEMA_VERSION: Final = 1
ALIGNMENT_MANIFEST_RELATIVE_PATH: Final = "alignment/alignment_manifest.json"
ALIGNMENT_FASTA_RELATIVE_PATH: Final = "alignment/aligned_sequences.fasta"
ALIGNMENT_REFERENCE_MAP_RELATIVE_PATH: Final = "alignment/reference_coordinate_map.json"
ALIGNMENT_DIAGNOSTICS_RELATIVE_PATH: Final = "alignment/diagnostics/mafft.stderr.log"


class AlignmentStageOutcome(StrEnum):
    COMPLETED = "completed"
    SKIPPED_NOT_REQUIRED = "skipped_not_required"
    SKIPPED_DISABLED = "skipped_disabled"


class AlignmentExecutionPlanKind(StrEnum):
    DISABLED = "disabled"
    DIRECT = "direct"
    PREALIGNED = "prealigned"
    ENGINE = "engine"


@dataclass(frozen=True, slots=True)
class AlignmentExecutionPlan:
    kind: AlignmentExecutionPlanKind
    logical_sample_count: int
    unique_sequence_count: int
    reason: str


@dataclass(frozen=True, slots=True)
class AlignmentInputSequence:
    sequence_id: str
    sequence: str
    logical_sample_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlignmentEngineRequest:
    sequences: tuple[AlignmentInputSequence, ...]
    construction: AnalysisAlignmentConstruction
    reference_sequence_id: str | None
    mafft_config: ResolvedAnalysisMafftConfig
    working_directory: Path
    control_check: Callable[[], None] | None = None
    process_started: Callable[[int], None] | None = None
    process_stopped: Callable[[int], None] | None = None


@dataclass(frozen=True, slots=True)
class AlignmentToolAvailability:
    available: bool
    executable: Path | None
    version: str | None
    version_parts: tuple[int, ...] | None
    source: str
    error_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AlignmentEngineResult:
    output_path: Path
    diagnostics_path: Path | None
    version: str
    effective_arguments: tuple[str, ...]
    internal_record_ids: dict[str, str]
    reverse_marked_record_ids: frozenset[str]


class AlignmentEngine(Protocol):
    @property
    def name(self) -> str: ...

    def probe(self, *, explicit_executable: str | None = None) -> AlignmentToolAvailability: ...

    def align(
        self,
        *,
        availability: AlignmentToolAvailability,
        request: AlignmentEngineRequest,
    ) -> AlignmentEngineResult: ...


class AlignmentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = ALIGNMENT_MANIFEST_SCHEMA_VERSION
    stage_id: str = ALIGNMENT_STAGE_ID
    task_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    mode: AnalysisAlignmentMode
    construction: AnalysisAlignmentConstruction | None = None
    requested_engine: AnalysisAlignmentEngine | None = None
    resolved_engine: AnalysisAlignmentEngine | None = None
    engine_version: str | None = None
    requested_strategy: AnalysisMafftStrategy | None = None
    effective_arguments: tuple[str, ...] = Field(default_factory=tuple)
    mafft_settings: dict[str, object] | None = None
    logical_sample_count: int = Field(ge=0)
    unique_sequence_count: int = Field(ge=0)
    alignment_length: int | None = Field(default=None, ge=0)
    reference_sample_id: str | None = None
    reference_sequence_id: str | None = None
    reoriented_sequence_ids: tuple[str, ...] = Field(default_factory=tuple)
    reoriented_sample_ids: tuple[str, ...] = Field(default_factory=tuple)
    aligned_fasta_path: str | None = None
    reference_coordinate_map_path: str | None = None
    diagnostics_path: str | None = None
    input_set_sha256: str = Field(min_length=64, max_length=64)
    result_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)
    outcome: AlignmentStageOutcome

    @field_validator(
        "aligned_fasta_path",
        "reference_coordinate_map_path",
        "diagnostics_path",
    )
    @classmethod
    def _validate_relative_paths(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().replace("\\", "/")
        posix = PurePosixPath(normalized)
        windows = PureWindowsPath(normalized)
        if normalized == "" or posix.is_absolute() or windows.is_absolute():
            raise ValueError("artifact path must be relative")
        if ".." in posix.parts or ".." in windows.parts:
            raise ValueError("artifact path must not escape the stage directory")
        return posix.as_posix()


class ReferenceCoordinateMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = ALIGNMENT_COORDINATE_MAP_SCHEMA_VERSION
    coordinate_system: str = "one_based_reference_positions"
    alignment_length: int = Field(gt=0)
    reference_sample_id: str = Field(min_length=1)
    reference_sequence_id: str = Field(min_length=1)
    reference_positions: tuple[int | None, ...]

    @model_validator(mode="after")
    def _validate_positions(self) -> ReferenceCoordinateMap:
        if len(self.reference_positions) != self.alignment_length:
            raise ValueError("reference_positions length must equal alignment_length")
        previous = 0
        for position in self.reference_positions:
            if position is None:
                continue
            if position != previous + 1:
                raise ValueError("reference positions must be contiguous and one-based")
            previous = position
        if previous == 0:
            raise ValueError("reference coordinate map must contain a reference position")
        return self
