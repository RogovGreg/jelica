from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

CURRENT_ANALYSIS_CONFIG_SCHEMA_VERSION = 1
DEFAULT_ANALYSIS_EXECUTION_TARGET = "full_analysis"
AUTO_ANALYSIS_EXECUTION_FROM_PHASE = "auto"
_ALLOWED_KMER_SYMBOLS = frozenset(
    {"A", "C", "G", "T", "U", "R", "Y", "S", "W", "K", "M", "B", "D", "H", "V", "N"}
)


class AnalysisAlignmentMode(StrEnum):
    COMPUTE = "compute"
    PREALIGNED = "prealigned"
    NONE = "none"


class AnalysisAlignmentEngine(StrEnum):
    MAFFT = "mafft"


class AnalysisAlignmentConstruction(StrEnum):
    JOINT = "joint"
    REFERENCE_GUIDED = "reference_guided"


class AnalysisMafftStrategy(StrEnum):
    AUTO = "auto"
    FFT_NS_1 = "fft_ns_1"
    FFT_NS_2 = "fft_ns_2"
    FFT_NS_I = "fft_ns_i"
    NW_NS_1 = "nw_ns_1"
    NW_NS_2 = "nw_ns_2"
    NW_NS_I = "nw_ns_i"
    G_INS_I = "g_ins_i"
    L_INS_I = "l_ins_i"
    E_INS_I = "e_ins_i"


class AnalysisMafftDirectionAdjustment(StrEnum):
    NONE = "none"
    FAST = "fast"
    ACCURATE = "accurate"


class AnalysisMafftMemoryMode(StrEnum):
    AUTO = "auto"
    SAVE = "save"


class AnalysisMafftThreadMode(StrEnum):
    AUTO = "auto"


class AnalysisMafftPhaseThreadMode(StrEnum):
    AUTO = "auto"
    DISABLED = "disabled"


PositiveStrictInt: TypeAlias = Annotated[StrictInt, Field(gt=0)]
AnalysisMafftThreadsValue: TypeAlias = AnalysisMafftThreadMode | PositiveStrictInt
AnalysisMafftPhaseThreadsValue: TypeAlias = AnalysisMafftPhaseThreadMode | PositiveStrictInt
NonNegativeFiniteFloat: TypeAlias = Annotated[FiniteFloat, Field(ge=0)]
UnitIntervalFiniteFloat: TypeAlias = Annotated[FiniteFloat, Field(ge=0, le=1)]


class AnalysisKmerStrand(StrEnum):
    FORWARD = "forward"
    REVERSE_COMPLEMENT = "reverse_complement"
    BOTH = "both"


class AnalysisComparativeReferenceMode(StrEnum):
    AUTO = "auto"
    ENABLED = "enabled"
    DISABLED = "disabled"


class AnalysisPairwiseOrientation(StrEnum):
    DIRECTED = "directed"
    BIDIRECTIONAL = "bidirectional"


class AnalysisDistanceMatrixModel(StrEnum):
    P_DISTANCE = "p_distance"


class AnalysisPhylogeneticTreeMethod(StrEnum):
    NEIGHBOR_JOINING = "neighbor_joining"


class AnalysisPhylogeneticTreeRooting(StrEnum):
    MIDPOINT = "midpoint"


class AnalysisCladeDetectionMethod(StrEnum):
    MAX_PAIRWISE_DISTANCE = "max_pairwise_distance"


@dataclass(frozen=True, slots=True)
class ConfigObjectKeySegment:
    """Object-key path segment in a CLI config override."""

    key: str


@dataclass(frozen=True, slots=True)
class ConfigArrayIndexSegment:
    """Array-index path segment in a CLI config override."""

    index: int


ConfigPathSegment: TypeAlias = ConfigObjectKeySegment | ConfigArrayIndexSegment


@dataclass(frozen=True, slots=True)
class ConfigOverride:
    """Single ordered CLI override operation."""

    raw_parameter: str
    path: tuple[ConfigPathSegment, ...]
    value: Any
    order: int

    def __post_init__(self) -> None:
        if self.raw_parameter == "":
            raise ValueError("ConfigOverride.raw_parameter must not be empty.")
        if len(self.path) == 0:
            raise ValueError("ConfigOverride.path must not be empty.")
        if self.order < 0:
            raise ValueError("ConfigOverride.order must be >= 0.")


def _normalize_sample_values(values: list[str | None]) -> list[str | None]:
    normalized: list[str | None] = []
    for index, value in enumerate(values):
        if value is None:
            normalized.append(None)
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"samples[{index}] must be a string or null, got {type(value).__name__}"
            )
        stripped_value = value.strip()
        if stripped_value == "":
            raise ValueError(f"samples[{index}] must not be empty")
        normalized.append(stripped_value)
    return normalized


def _normalize_reference_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped_value = value.strip()
    if stripped_value == "":
        raise ValueError("reference must not be empty")
    return stripped_value


def _normalize_analysis_sample_selector(value: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError("sample selector must not be empty")
    if "::" not in normalized:
        return normalized

    path_part, record_id_part = normalized.rsplit("::", maxsplit=1)
    normalized_path = path_part.strip()
    normalized_record_id = record_id_part.strip()
    if normalized_path == "" or normalized_record_id == "":
        raise ValueError("qualified sample selector must use '<path>::<record_id>' form")
    return f"{normalized_path}::{normalized_record_id}"


def _normalize_kmer_values(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        stripped_value = value.strip()
        if stripped_value == "":
            raise ValueError(f"statistics.kmers[{index}] must not be empty")
        normalized_value = stripped_value.upper()
        if len(normalized_value) < 2:
            raise ValueError(
                f"statistics.kmers[{index}] must contain at least 2 symbols after normalization"
            )
        for symbol in normalized_value:
            if symbol == "-":
                raise ValueError(f"statistics.kmers[{index}] must not contain gap symbol '-'")
            if symbol not in _ALLOWED_KMER_SYMBOLS:
                raise ValueError(
                    f"statistics.kmers[{index}] contains unsupported symbol '{symbol}'"
                )
        if normalized_value in seen:
            continue
        seen.add(normalized_value)
        normalized.append(normalized_value)
    return normalized


class AnalysisMafftConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: AnalysisMafftStrategy | None = None
    direction_adjustment: AnalysisMafftDirectionAdjustment | None = None
    memory_mode: AnalysisMafftMemoryMode | None = None
    threads: AnalysisMafftThreadsValue | None = None
    gap_open_penalty: NonNegativeFiniteFloat | None = None
    offset: NonNegativeFiniteFloat | None = None
    progressive_threads: AnalysisMafftPhaseThreadsValue | None = None
    iterative_threads: AnalysisMafftPhaseThreadsValue | None = None

    @field_validator("gap_open_penalty", "offset", mode="before")
    @classmethod
    def _require_json_number(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("value must be a finite number >= 0")
        return value

    @model_validator(mode="after")
    def _validate_strategy_overrides(self) -> AnalysisMafftConfigInput:
        resolved_strategy = self.strategy or AnalysisMafftStrategy.AUTO
        if resolved_strategy is not AnalysisMafftStrategy.AUTO:
            return self

        forbidden_fields = (
            "gap_open_penalty",
            "offset",
            "progressive_threads",
            "iterative_threads",
        )
        configured_fields = [
            field_name for field_name in forbidden_fields if getattr(self, field_name) is not None
        ]
        if configured_fields:
            joined_fields = ", ".join(
                f"alignment.mafft.{field_name}" for field_name in configured_fields
            )
            raise ValueError(
                f"{joined_fields} cannot be set when alignment.mafft.strategy is 'auto'"
            )
        return self


class AnalysisAlignmentConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AnalysisAlignmentMode | None = None
    engine: AnalysisAlignmentEngine | None = None
    construction: AnalysisAlignmentConstruction | None = None
    mafft: AnalysisMafftConfigInput | None = None

    @model_validator(mode="after")
    def _reject_compute_settings_for_non_compute_modes(
        self,
    ) -> AnalysisAlignmentConfigInput:
        if self.mode not in {
            AnalysisAlignmentMode.PREALIGNED,
            AnalysisAlignmentMode.NONE,
        }:
            return self
        configured_fields = [
            field_name
            for field_name in ("engine", "construction", "mafft")
            if getattr(self, field_name) is not None
        ]
        if configured_fields:
            joined_fields = ", ".join(f"alignment.{field_name}" for field_name in configured_fields)
            raise ValueError(
                f"{joined_fields} cannot be set when alignment.mode is '{self.mode.value}'"
            )
        return self


class AnalysisStatisticsConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kmers: list[StrictStr] | None = None
    kmer_strand: AnalysisKmerStrand | None = None

    @field_validator("kmers")
    @classmethod
    def _normalize_kmers(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_kmer_values(value)


class ComparativeStatisticsConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None


class ComparativeSymbolPolicyConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uracil_thymine_equivalent: StrictBool | None = None


class SequenceDifferencesConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None
    substitutions: StrictBool | None = None
    insertions: StrictBool | None = None
    deletions: StrictBool | None = None
    symbol_policy: ComparativeSymbolPolicyConfigInput | None = None


class ComparativeReferenceConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AnalysisComparativeReferenceMode | None = None


class ComparativePairwiseConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None
    all: StrictBool | None = None
    pairs_orientation: AnalysisPairwiseOrientation | None = None
    groups: list[list[StrictStr]] | None = None
    pairs: list[list[StrictStr]] | None = None


class ComparativeAnalysisConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None
    statistics: ComparativeStatisticsConfigInput | None = None
    sequence_differences: SequenceDifferencesConfigInput | None = None
    reference: ComparativeReferenceConfigInput | None = None
    pairwise: ComparativePairwiseConfigInput | None = None


class DistanceMatrixConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None
    model: AnalysisDistanceMatrixModel | None = None


class PhylogeneticTreeConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None
    method: AnalysisPhylogeneticTreeMethod | None = None
    rooting: AnalysisPhylogeneticTreeRooting | None = None


class CladeDetectionConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None
    method: AnalysisCladeDetectionMethod | None = None
    max_within_clade_distance: UnitIntervalFiniteFloat | None = None

    @field_validator("max_within_clade_distance", mode="before")
    @classmethod
    def _require_json_number(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("max_within_clade_distance must be a finite number in [0, 1]")
        return value


class ResolvedAnalysisMafftConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: AnalysisMafftStrategy = AnalysisMafftStrategy.AUTO
    direction_adjustment: AnalysisMafftDirectionAdjustment = AnalysisMafftDirectionAdjustment.NONE
    memory_mode: AnalysisMafftMemoryMode = AnalysisMafftMemoryMode.AUTO
    threads: AnalysisMafftThreadsValue = 1
    gap_open_penalty: NonNegativeFiniteFloat | None = None
    offset: NonNegativeFiniteFloat | None = None
    progressive_threads: AnalysisMafftPhaseThreadsValue = AnalysisMafftPhaseThreadMode.AUTO
    iterative_threads: AnalysisMafftPhaseThreadsValue = AnalysisMafftPhaseThreadMode.AUTO

    @field_validator("gap_open_penalty", "offset", mode="before")
    @classmethod
    def _require_json_number(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("value must be a finite number >= 0")
        return value

    @model_validator(mode="after")
    def _validate_strategy_overrides(self) -> ResolvedAnalysisMafftConfig:
        if self.strategy is not AnalysisMafftStrategy.AUTO:
            return self
        if self.gap_open_penalty is not None or self.offset is not None:
            raise ValueError(
                "scoring overrides cannot be set when alignment.mafft.strategy is 'auto'"
            )
        if self.progressive_threads is not AnalysisMafftPhaseThreadMode.AUTO:
            raise ValueError(
                "progressive_threads cannot be overridden when alignment.mafft.strategy is 'auto'"
            )
        if self.iterative_threads is not AnalysisMafftPhaseThreadMode.AUTO:
            raise ValueError(
                "iterative_threads cannot be overridden when alignment.mafft.strategy is 'auto'"
            )
        return self


class ResolvedAnalysisAlignmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: AnalysisAlignmentMode
    engine: AnalysisAlignmentEngine | None = None
    construction: AnalysisAlignmentConstruction | None = None
    mafft: ResolvedAnalysisMafftConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _apply_compute_defaults(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        raw_mode = value.get("mode")
        if raw_mode not in {AnalysisAlignmentMode.COMPUTE, AnalysisAlignmentMode.COMPUTE.value}:
            return value
        normalized = dict(value)
        normalized.setdefault("engine", AnalysisAlignmentEngine.MAFFT)
        normalized.setdefault("construction", AnalysisAlignmentConstruction.JOINT)
        normalized.setdefault("mafft", {})
        return normalized

    @model_validator(mode="after")
    def _validate_mode_settings(self) -> ResolvedAnalysisAlignmentConfig:
        if self.mode is AnalysisAlignmentMode.COMPUTE:
            if self.engine is None or self.construction is None or self.mafft is None:
                raise ValueError(
                    "compute alignment requires resolved engine, construction, and mafft settings"
                )
            return self
        if self.engine is not None or self.construction is not None or self.mafft is not None:
            raise ValueError(
                f"alignment engine settings must be absent when mode is '{self.mode.value}'"
            )
        return self


class ResolvedAnalysisStatisticsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kmers: list[StrictStr] = Field(default_factory=list)
    kmer_strand: AnalysisKmerStrand = AnalysisKmerStrand.FORWARD

    @field_validator("kmers")
    @classmethod
    def _normalize_kmers(cls, value: list[str]) -> list[str]:
        normalized = _normalize_kmer_values(value)
        if normalized is None:
            return []
        return normalized


class ResolvedComparativeStatisticsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False


class ResolvedComparativeSymbolPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uracil_thymine_equivalent: StrictBool = False


class ResolvedSequenceDifferencesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    substitutions: StrictBool = False
    insertions: StrictBool = False
    deletions: StrictBool = False
    symbol_policy: ResolvedComparativeSymbolPolicyConfig = Field(
        default_factory=ResolvedComparativeSymbolPolicyConfig
    )

    @model_validator(mode="after")
    def _require_enabled_category(self) -> ResolvedSequenceDifferencesConfig:
        if self.enabled and not (self.substitutions or self.insertions or self.deletions):
            raise ValueError(
                "enabled sequence differences require substitutions, insertions, or deletions"
            )
        return self


class ResolvedComparativeReferenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: AnalysisComparativeReferenceMode = AnalysisComparativeReferenceMode.AUTO


class ResolvedComparativePairwiseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    all: StrictBool = False
    pairs_orientation: AnalysisPairwiseOrientation = AnalysisPairwiseOrientation.DIRECTED
    groups: list[list[StrictStr]] = Field(default_factory=list)
    pairs: list[list[StrictStr]] = Field(default_factory=list)

    @field_validator("groups", "pairs")
    @classmethod
    def _normalize_selectors(cls, value: list[list[str]]) -> list[list[str]]:
        return [
            [_normalize_analysis_sample_selector(selector) for selector in selection]
            for selection in value
        ]

    @model_validator(mode="after")
    def _validate_selection(self) -> ResolvedComparativePairwiseConfig:
        has_explicit_selection = bool(self.groups or self.pairs)
        if not self.enabled:
            if self.all or has_explicit_selection:
                raise ValueError("disabled pairwise configuration must not contain a selection")
            return self
        if self.all and has_explicit_selection:
            raise ValueError("pairwise all cannot be combined with groups or pairs")
        if not self.all and not has_explicit_selection:
            raise ValueError("enabled pairwise configuration requires a selection")
        for group in self.groups:
            if len(set(group)) < 2:
                raise ValueError("pairwise groups must contain at least two unique selectors")
        for pair in self.pairs:
            if len(pair) != 2:
                raise ValueError("pairwise pairs must contain exactly two selectors")
            if pair[0] == pair[1]:
                raise ValueError("pairwise pairs cannot compare a selector with itself")
        return self


class ResolvedComparativeAnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    statistics: ResolvedComparativeStatisticsConfig = Field(
        default_factory=ResolvedComparativeStatisticsConfig
    )
    sequence_differences: ResolvedSequenceDifferencesConfig = Field(
        default_factory=ResolvedSequenceDifferencesConfig
    )
    reference: ResolvedComparativeReferenceConfig = Field(
        default_factory=ResolvedComparativeReferenceConfig
    )
    pairwise: ResolvedComparativePairwiseConfig = Field(
        default_factory=ResolvedComparativePairwiseConfig
    )

    @model_validator(mode="after")
    def _require_enabled_operation(self) -> ResolvedComparativeAnalysisConfig:
        if self.enabled and not (self.statistics.enabled or self.sequence_differences.enabled):
            raise ValueError(
                "enabled comparative analysis requires statistics or sequence differences"
            )
        return self


class ResolvedDistanceMatrixConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    model: AnalysisDistanceMatrixModel = AnalysisDistanceMatrixModel.P_DISTANCE


class ResolvedPhylogeneticTreeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    method: AnalysisPhylogeneticTreeMethod = AnalysisPhylogeneticTreeMethod.NEIGHBOR_JOINING
    rooting: AnalysisPhylogeneticTreeRooting = AnalysisPhylogeneticTreeRooting.MIDPOINT


class ResolvedCladeDetectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    method: AnalysisCladeDetectionMethod = AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE
    max_within_clade_distance: UnitIntervalFiniteFloat | None = None

    @field_validator("max_within_clade_distance", mode="before")
    @classmethod
    def _require_json_number(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("max_within_clade_distance must be a finite number in [0, 1]")
        return value

    @model_validator(mode="after")
    def _require_threshold_when_enabled(self) -> ResolvedCladeDetectionConfig:
        if self.enabled and self.max_within_clade_distance is None:
            raise ValueError("enabled clade detection requires max_within_clade_distance to be set")
        return self


def _default_enabled_comparative_analysis_config() -> ResolvedComparativeAnalysisConfig:
    return ResolvedComparativeAnalysisConfig(
        enabled=True,
        statistics=ResolvedComparativeStatisticsConfig(enabled=True),
        sequence_differences=ResolvedSequenceDifferencesConfig(
            enabled=True,
            substitutions=True,
            insertions=True,
            deletions=True,
        ),
    )


def _default_enabled_distance_matrix_config() -> ResolvedDistanceMatrixConfig:
    return ResolvedDistanceMatrixConfig(enabled=True)


def _default_enabled_phylogenetic_tree_config() -> ResolvedPhylogeneticTreeConfig:
    return ResolvedPhylogeneticTreeConfig(enabled=True)


def _default_disabled_clade_detection_config() -> ResolvedCladeDetectionConfig:
    return ResolvedCladeDetectionConfig(enabled=False)


def _normalize_execution_selection_value(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized == "":
        raise ValueError(f"execution.{field_name} must be a non-empty string")
    return normalized


class AnalysisExecutionConfigInput(BaseModel):
    """Partial execution-boundary selection for one analysis."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    target: StrictStr | None = None
    from_phase: StrictStr | None = None

    @field_validator("target")
    @classmethod
    def _normalize_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_execution_selection_value(value, field_name="target")

    @field_validator("from_phase")
    @classmethod
    def _normalize_from_phase(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_execution_selection_value(value, field_name="from_phase")


class ResolvedAnalysisExecutionConfig(BaseModel):
    """Normalized execution-boundary selection persisted with the task config."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    target: StrictStr = DEFAULT_ANALYSIS_EXECUTION_TARGET
    from_phase: StrictStr = AUTO_ANALYSIS_EXECUTION_FROM_PHASE

    @field_validator("target")
    @classmethod
    def _normalize_target(cls, value: str) -> str:
        return _normalize_execution_selection_value(value, field_name="target")

    @field_validator("from_phase")
    @classmethod
    def _normalize_from_phase(cls, value: str) -> str:
        return _normalize_execution_selection_value(value, field_name="from_phase")


def _default_analysis_execution_config() -> ResolvedAnalysisExecutionConfig:
    return ResolvedAnalysisExecutionConfig()


class AnalysisConfigInput(BaseModel):
    """Partial user-provided analysis configuration."""

    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    schema_version: int = Field(default=CURRENT_ANALYSIS_CONFIG_SCHEMA_VERSION, gt=0)
    trace_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    samples: list[StrictStr | None] | None = None
    priority: int = Field(default=1, ge=1)
    execution: AnalysisExecutionConfigInput | None = None
    alignment: AnalysisAlignmentConfigInput | None = None
    reference: StrictStr | None = None
    statistics: AnalysisStatisticsConfigInput | None = None
    comparative_analysis: ComparativeAnalysisConfigInput | None = None
    distance_matrix: DistanceMatrixConfigInput | None = None
    phylogenetic_tree: PhylogeneticTreeConfigInput | None = None
    clade_detection: CladeDetectionConfigInput | None = None

    @field_validator("samples")
    @classmethod
    def _normalize_samples(cls, value: list[str | None] | None) -> list[str | None] | None:
        if value is None:
            return None
        return _normalize_sample_values(value)

    @field_validator("reference")
    @classmethod
    def _normalize_reference(cls, value: str | None) -> str | None:
        return _normalize_reference_value(value)

    @model_validator(mode="after")
    def _require_reference_for_reference_guided(self) -> AnalysisConfigInput:
        if (
            self.alignment is not None
            and self.alignment.construction is AnalysisAlignmentConstruction.REFERENCE_GUIDED
            and self.reference is None
        ):
            raise ValueError(
                "reference is required when alignment.construction is 'reference_guided'"
            )
        return self


class ResolvedAnalysisConfig(BaseModel):
    """Final analysis configuration resolved from all value sources."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    schema_version: int = Field(gt=0)
    trace_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    samples: list[StrictStr | None] = Field(min_length=1)
    priority: int = Field(ge=1)
    execution: ResolvedAnalysisExecutionConfig = Field(
        default_factory=_default_analysis_execution_config
    )
    alignment: ResolvedAnalysisAlignmentConfig
    reference: StrictStr | None = None
    statistics: ResolvedAnalysisStatisticsConfig
    comparative_analysis: ResolvedComparativeAnalysisConfig = Field(
        default_factory=_default_enabled_comparative_analysis_config
    )
    distance_matrix: ResolvedDistanceMatrixConfig = Field(
        default_factory=_default_enabled_distance_matrix_config
    )
    phylogenetic_tree: ResolvedPhylogeneticTreeConfig = Field(
        default_factory=_default_enabled_phylogenetic_tree_config
    )
    clade_detection: ResolvedCladeDetectionConfig = Field(
        default_factory=_default_disabled_clade_detection_config
    )

    @field_validator("samples")
    @classmethod
    def _normalize_samples(cls, value: list[str | None]) -> list[str | None]:
        return _normalize_sample_values(value)

    @field_validator("reference")
    @classmethod
    def _normalize_reference(cls, value: str | None) -> str | None:
        return _normalize_reference_value(value)

    @model_validator(mode="after")
    def _require_reference_for_reference_guided(self) -> ResolvedAnalysisConfig:
        if (
            self.alignment.construction is AnalysisAlignmentConstruction.REFERENCE_GUIDED
            and self.reference is None
        ):
            raise ValueError(
                "reference is required when alignment.construction is 'reference_guided'"
            )
        if (
            self.comparative_analysis.enabled
            and self.comparative_analysis.reference.mode is AnalysisComparativeReferenceMode.ENABLED
            and self.reference is None
        ):
            raise ValueError(
                "reference is required when comparative_analysis.reference.mode is 'enabled'"
            )
        if (
            self.comparative_analysis.enabled
            and self.comparative_analysis.sequence_differences.enabled
            and self.alignment.mode is AnalysisAlignmentMode.NONE
        ):
            raise ValueError(
                "sequence differences require alignment.mode 'compute' or 'prealigned'"
            )
        if self.distance_matrix.enabled and self.alignment.mode is AnalysisAlignmentMode.NONE:
            raise ValueError("distance matrix requires alignment.mode 'compute' or 'prealigned'")
        if self.phylogenetic_tree.enabled and not self.distance_matrix.enabled:
            raise ValueError("phylogenetic tree requires distance_matrix.enabled to be true")
        if self.clade_detection.enabled and not self.distance_matrix.enabled:
            raise ValueError("clade detection requires distance_matrix.enabled to be true")
        if self.clade_detection.enabled and not self.phylogenetic_tree.enabled:
            raise ValueError("clade detection requires phylogenetic_tree.enabled to be true")
        return self


class AnalysisConfigResolutionResult(BaseModel):
    """Result of resolving analysis configuration values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config: ResolvedAnalysisConfig
    warnings: tuple[str, ...] = Field(default_factory=tuple)
