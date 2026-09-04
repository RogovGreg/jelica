from __future__ import annotations

from pydantic import ValidationError

from .errors import (
    AnalysisConfigValidationCode,
    ConfigSchemaValidationError,
    MissingSamplesError,
    UnsupportedConfigSchemaVersionError,
    convert_config_validation_error,
)
from .models import (
    CURRENT_ANALYSIS_CONFIG_SCHEMA_VERSION,
    AnalysisAlignmentConstruction,
    AnalysisAlignmentEngine,
    AnalysisAlignmentMode,
    AnalysisCladeDetectionMethod,
    AnalysisComparativeReferenceMode,
    AnalysisConfigInput,
    AnalysisConfigResolutionResult,
    AnalysisDistanceMatrixModel,
    AnalysisExecutionConfigInput,
    AnalysisKmerStrand,
    AnalysisMafftDirectionAdjustment,
    AnalysisMafftMemoryMode,
    AnalysisMafftPhaseThreadMode,
    AnalysisMafftStrategy,
    AnalysisPairwiseOrientation,
    AnalysisPhylogeneticTreeMethod,
    AnalysisPhylogeneticTreeRooting,
    CladeDetectionConfigInput,
    ComparativeAnalysisConfigInput,
    ComparativePairwiseConfigInput,
    DistanceMatrixConfigInput,
    PhylogeneticTreeConfigInput,
    ResolvedAnalysisAlignmentConfig,
    ResolvedAnalysisConfig,
    ResolvedAnalysisExecutionConfig,
    ResolvedAnalysisMafftConfig,
    ResolvedAnalysisStatisticsConfig,
    ResolvedCladeDetectionConfig,
    ResolvedComparativeAnalysisConfig,
    ResolvedComparativePairwiseConfig,
    ResolvedComparativeReferenceConfig,
    ResolvedComparativeStatisticsConfig,
    ResolvedComparativeSymbolPolicyConfig,
    ResolvedDistanceMatrixConfig,
    ResolvedPhylogeneticTreeConfig,
    ResolvedSequenceDifferencesConfig,
    _normalize_analysis_sample_selector,
)

_INTERNAL_CONFIG_FIELDS = frozenset({"input_directory_max_depth", "ncbi_max_retries"})


def resolve_analysis_config(
    config_input: AnalysisConfigInput,
    *,
    default_alignment_mode: AnalysisAlignmentMode | str = AnalysisAlignmentMode.COMPUTE,
) -> AnalysisConfigResolutionResult:
    """Resolve final analysis configuration after all overrides are applied."""

    resolved_schema_version = config_input.schema_version
    _validate_schema_version(schema_version=resolved_schema_version)

    resolved_samples = _resolve_samples(config_input=config_input)
    resolved_priority = config_input.priority
    resolved_execution = _resolve_execution(config_input=config_input)
    resolved_alignment = _resolve_alignment(
        config_input=config_input,
        default_alignment_mode=default_alignment_mode,
    )
    resolved_reference = _resolve_reference(config_input=config_input)
    resolved_statistics = _resolve_statistics(config_input=config_input)
    resolved_comparative_analysis = _resolve_comparative_analysis(
        config_input=config_input,
        alignment=resolved_alignment,
        reference=resolved_reference,
    )
    resolved_distance_matrix = _resolve_distance_matrix(
        config_input=config_input,
        alignment=resolved_alignment,
    )
    resolved_phylogenetic_tree = _resolve_phylogenetic_tree(
        config_input=config_input,
        distance_matrix=resolved_distance_matrix,
    )
    resolved_clade_detection = _resolve_clade_detection(
        config_input=config_input,
        distance_matrix=resolved_distance_matrix,
        phylogenetic_tree=resolved_phylogenetic_tree,
    )
    if resolved_samples is None or "samples" not in config_input.model_fields_set:
        raise MissingSamplesError(empty_list=False)
    if len(resolved_samples) == 0:
        raise MissingSamplesError(empty_list=True)
    if not _contains_real_sources(resolved_samples):
        raise MissingSamplesError(empty_list=False)

    try:
        resolved_config = ResolvedAnalysisConfig(
            schema_version=resolved_schema_version,
            trace_id=config_input.trace_id,
            samples=resolved_samples,
            priority=resolved_priority,
            execution=resolved_execution,
            alignment=resolved_alignment,
            reference=resolved_reference,
            statistics=resolved_statistics,
            comparative_analysis=resolved_comparative_analysis,
            distance_matrix=resolved_distance_matrix,
            phylogenetic_tree=resolved_phylogenetic_tree,
            clade_detection=resolved_clade_detection,
        )
    except ValidationError as error:
        raise convert_config_validation_error(error) from error

    warnings = _collect_unknown_field_warnings(config_input)

    return AnalysisConfigResolutionResult(config=resolved_config, warnings=tuple(warnings))


def _validate_schema_version(schema_version: int) -> None:
    if schema_version <= 0:
        raise ConfigSchemaValidationError(
            "Analysis config schema_version must be a positive integer."
        )
    if schema_version != CURRENT_ANALYSIS_CONFIG_SCHEMA_VERSION:
        raise UnsupportedConfigSchemaVersionError(
            schema_version=schema_version,
            supported_version=CURRENT_ANALYSIS_CONFIG_SCHEMA_VERSION,
        )


def _resolve_samples(
    config_input: AnalysisConfigInput,
) -> list[str | None] | None:
    if config_input.samples is None:
        return None
    return list(config_input.samples)


def _contains_real_sources(samples: list[str | None]) -> bool:
    for sample in samples:
        if isinstance(sample, str):
            return True
    return False


def _resolve_execution(
    *,
    config_input: AnalysisConfigInput,
) -> ResolvedAnalysisExecutionConfig:
    execution_input: AnalysisExecutionConfigInput | None = config_input.execution
    if execution_input is None:
        return ResolvedAnalysisExecutionConfig()
    defaults = ResolvedAnalysisExecutionConfig()
    return ResolvedAnalysisExecutionConfig(
        target=(execution_input.target if execution_input.target is not None else defaults.target),
        from_phase=(
            execution_input.from_phase
            if execution_input.from_phase is not None
            else defaults.from_phase
        ),
    )


def _resolve_alignment(
    *,
    config_input: AnalysisConfigInput,
    default_alignment_mode: AnalysisAlignmentMode | str,
) -> ResolvedAnalysisAlignmentConfig:
    resolved_mode = _normalize_default_alignment_mode(default_alignment_mode=default_alignment_mode)
    alignment_input = config_input.alignment
    if alignment_input is not None and alignment_input.mode is not None:
        resolved_mode = alignment_input.mode

    if resolved_mode is not AnalysisAlignmentMode.COMPUTE:
        if alignment_input is not None:
            configured_fields = [
                field_name
                for field_name in ("engine", "construction", "mafft")
                if getattr(alignment_input, field_name) is not None
            ]
            if configured_fields:
                joined_fields = ", ".join(
                    f"alignment.{field_name}" for field_name in configured_fields
                )
                raise ConfigSchemaValidationError(
                    f"{joined_fields} cannot be set when alignment.mode is '{resolved_mode.value}'"
                )
        return ResolvedAnalysisAlignmentConfig(mode=resolved_mode)

    resolved_engine = AnalysisAlignmentEngine.MAFFT
    resolved_construction = AnalysisAlignmentConstruction.JOINT
    if alignment_input is not None:
        if alignment_input.engine is not None:
            resolved_engine = alignment_input.engine
        if alignment_input.construction is not None:
            resolved_construction = alignment_input.construction

    if (
        resolved_construction is AnalysisAlignmentConstruction.REFERENCE_GUIDED
        and config_input.reference is None
    ):
        raise ConfigSchemaValidationError(
            "reference is required when alignment.construction is 'reference_guided'"
        )

    return ResolvedAnalysisAlignmentConfig(
        mode=resolved_mode,
        engine=resolved_engine,
        construction=resolved_construction,
        mafft=_resolve_mafft(config_input=config_input),
    )


def _resolve_mafft(*, config_input: AnalysisConfigInput) -> ResolvedAnalysisMafftConfig:
    mafft_input = None
    if config_input.alignment is not None:
        mafft_input = config_input.alignment.mafft

    if mafft_input is None:
        return ResolvedAnalysisMafftConfig()

    return ResolvedAnalysisMafftConfig(
        strategy=mafft_input.strategy or AnalysisMafftStrategy.AUTO,
        direction_adjustment=(
            mafft_input.direction_adjustment or AnalysisMafftDirectionAdjustment.NONE
        ),
        memory_mode=mafft_input.memory_mode or AnalysisMafftMemoryMode.AUTO,
        threads=mafft_input.threads if mafft_input.threads is not None else 1,
        gap_open_penalty=mafft_input.gap_open_penalty,
        offset=mafft_input.offset,
        progressive_threads=(
            mafft_input.progressive_threads
            if mafft_input.progressive_threads is not None
            else AnalysisMafftPhaseThreadMode.AUTO
        ),
        iterative_threads=(
            mafft_input.iterative_threads
            if mafft_input.iterative_threads is not None
            else AnalysisMafftPhaseThreadMode.AUTO
        ),
    )


def _normalize_default_alignment_mode(
    *, default_alignment_mode: AnalysisAlignmentMode | str
) -> AnalysisAlignmentMode:
    if isinstance(default_alignment_mode, AnalysisAlignmentMode):
        return default_alignment_mode

    normalized_mode = default_alignment_mode.strip().lower()
    if normalized_mode == "":
        raise ConfigSchemaValidationError("default_alignment_mode must not be empty")
    try:
        return AnalysisAlignmentMode(normalized_mode)
    except ValueError as error:
        allowed_values = ", ".join(mode.value for mode in AnalysisAlignmentMode)
        raise ConfigSchemaValidationError(
            f"default_alignment_mode must be one of: {allowed_values}"
        ) from error


def _resolve_reference(config_input: AnalysisConfigInput) -> str | None:
    if config_input.reference is None:
        return None
    return config_input.reference


def _resolve_statistics(config_input: AnalysisConfigInput) -> ResolvedAnalysisStatisticsConfig:
    kmers: list[str] = []
    strand = AnalysisKmerStrand.FORWARD
    if config_input.statistics is not None:
        if config_input.statistics.kmers is not None:
            kmers = list(config_input.statistics.kmers)
        if config_input.statistics.kmer_strand is not None:
            strand = config_input.statistics.kmer_strand
    return ResolvedAnalysisStatisticsConfig(kmers=kmers, kmer_strand=strand)


def _resolve_comparative_analysis(
    *,
    config_input: AnalysisConfigInput,
    alignment: ResolvedAnalysisAlignmentConfig,
    reference: str | None,
) -> ResolvedComparativeAnalysisConfig:
    comparative_input = config_input.comparative_analysis
    if comparative_input is not None and comparative_input.enabled is False:
        return ResolvedComparativeAnalysisConfig()
    if comparative_input is None:
        comparative_input = ComparativeAnalysisConfigInput()

    statistics_enabled = (
        comparative_input.statistics is None or comparative_input.statistics.enabled is not False
    )
    differences_input = comparative_input.sequence_differences
    differences_enabled = differences_input is None or differences_input.enabled is not False
    if not statistics_enabled and not differences_enabled:
        raise ConfigSchemaValidationError(
            "Enabled comparative analysis must enable statistics or sequence differences.",
            code=AnalysisConfigValidationCode.COMPARATIVE_ANALYSIS_EMPTY,
            field_path="comparative_analysis",
        )

    if differences_enabled:
        substitutions = differences_input is None or differences_input.substitutions is not False
        insertions = differences_input is None or differences_input.insertions is not False
        deletions = differences_input is None or differences_input.deletions is not False
        if not (substitutions or insertions or deletions):
            raise ConfigSchemaValidationError(
                "Enabled sequence differences must enable at least one result category.",
                code=AnalysisConfigValidationCode.SEQUENCE_DIFFERENCES_EMPTY,
                field_path="comparative_analysis.sequence_differences",
            )
        if alignment.mode is AnalysisAlignmentMode.NONE:
            raise ConfigSchemaValidationError(
                "Sequence differences require alignment.mode 'compute' or 'prealigned'.",
                code=(AnalysisConfigValidationCode.SEQUENCE_DIFFERENCES_REQUIRES_ALIGNMENT),
                field_path="comparative_analysis.sequence_differences.enabled",
            )
        uracil_thymine_equivalent = bool(
            differences_input is not None
            and differences_input.symbol_policy is not None
            and differences_input.symbol_policy.uracil_thymine_equivalent is True
        )
        resolved_differences = ResolvedSequenceDifferencesConfig(
            enabled=True,
            substitutions=substitutions,
            insertions=insertions,
            deletions=deletions,
            symbol_policy=ResolvedComparativeSymbolPolicyConfig(
                uracil_thymine_equivalent=uracil_thymine_equivalent
            ),
        )
    else:
        resolved_differences = ResolvedSequenceDifferencesConfig()

    reference_mode = AnalysisComparativeReferenceMode.AUTO
    if comparative_input.reference is not None:
        reference_mode = comparative_input.reference.mode or AnalysisComparativeReferenceMode.AUTO
    if reference_mode is AnalysisComparativeReferenceMode.ENABLED and reference is None:
        raise ConfigSchemaValidationError(
            "Reference-based comparison is required but no reference selector is configured.",
            code=AnalysisConfigValidationCode.COMPARATIVE_REFERENCE_REQUIRED,
            field_path="comparative_analysis.reference.mode",
        )

    return ResolvedComparativeAnalysisConfig(
        enabled=True,
        statistics=ResolvedComparativeStatisticsConfig(enabled=statistics_enabled),
        sequence_differences=resolved_differences,
        reference=ResolvedComparativeReferenceConfig(mode=reference_mode),
        pairwise=_resolve_comparative_pairwise(comparative_input.pairwise),
    )


def _resolve_comparative_pairwise(
    pairwise_input: ComparativePairwiseConfigInput | None,
) -> ResolvedComparativePairwiseConfig:
    if pairwise_input is None or pairwise_input.enabled is not True:
        return ResolvedComparativePairwiseConfig()

    groups = _normalize_pairwise_groups(pairwise_input.groups or [])
    pairs = _normalize_pairwise_pairs(pairwise_input.pairs or [])
    has_explicit_selection = bool(groups or pairs)

    if pairwise_input.all is True and has_explicit_selection:
        raise ConfigSchemaValidationError(
            "Pairwise all-vs-all cannot be combined with groups or explicit pairs.",
            code=AnalysisConfigValidationCode.PAIRWISE_ALL_WITH_EXPLICIT_SELECTION,
            field_path="comparative_analysis.pairwise.all",
        )
    if pairwise_input.all is False and not has_explicit_selection:
        raise ConfigSchemaValidationError(
            "Enabled pairwise analysis with all=false requires groups or pairs.",
            code=AnalysisConfigValidationCode.PAIRWISE_SELECTION_EMPTY,
            field_path="comparative_analysis.pairwise",
        )

    all_pairs = pairwise_input.all is True or (
        pairwise_input.all is None and not has_explicit_selection
    )
    return ResolvedComparativePairwiseConfig(
        enabled=True,
        all=all_pairs,
        pairs_orientation=(
            pairwise_input.pairs_orientation or AnalysisPairwiseOrientation.DIRECTED
        ),
        groups=groups,
        pairs=pairs,
    )


def _resolve_distance_matrix(
    *,
    config_input: AnalysisConfigInput,
    alignment: ResolvedAnalysisAlignmentConfig,
) -> ResolvedDistanceMatrixConfig:
    distance_input = config_input.distance_matrix
    if distance_input is not None and distance_input.enabled is False:
        return ResolvedDistanceMatrixConfig(
            enabled=False,
            model=distance_input.model or AnalysisDistanceMatrixModel.P_DISTANCE,
        )
    if distance_input is None:
        distance_input = DistanceMatrixConfigInput()
    if alignment.mode is AnalysisAlignmentMode.NONE:
        raise ConfigSchemaValidationError(
            "Distance matrix requires alignment.mode 'compute' or 'prealigned'.",
            code=AnalysisConfigValidationCode.DISTANCE_MATRIX_REQUIRES_ALIGNMENT,
            field_path="distance_matrix.enabled",
        )
    return ResolvedDistanceMatrixConfig(
        enabled=True,
        model=distance_input.model or AnalysisDistanceMatrixModel.P_DISTANCE,
    )


def _resolve_phylogenetic_tree(
    *,
    config_input: AnalysisConfigInput,
    distance_matrix: ResolvedDistanceMatrixConfig,
) -> ResolvedPhylogeneticTreeConfig:
    tree_input = config_input.phylogenetic_tree
    if tree_input is not None and tree_input.enabled is False:
        return ResolvedPhylogeneticTreeConfig(
            enabled=False,
            method=(tree_input.method or AnalysisPhylogeneticTreeMethod.NEIGHBOR_JOINING),
            rooting=tree_input.rooting or AnalysisPhylogeneticTreeRooting.MIDPOINT,
        )
    if tree_input is None:
        tree_input = PhylogeneticTreeConfigInput()
    if not distance_matrix.enabled:
        raise ConfigSchemaValidationError(
            "Phylogenetic tree requires distance_matrix.enabled to be true.",
            code=(AnalysisConfigValidationCode.PHYLOGENETIC_TREE_REQUIRES_DISTANCE_MATRIX),
            field_path="phylogenetic_tree.enabled",
        )
    return ResolvedPhylogeneticTreeConfig(
        enabled=True,
        method=tree_input.method or AnalysisPhylogeneticTreeMethod.NEIGHBOR_JOINING,
        rooting=tree_input.rooting or AnalysisPhylogeneticTreeRooting.MIDPOINT,
    )


def _resolve_clade_detection(
    *,
    config_input: AnalysisConfigInput,
    distance_matrix: ResolvedDistanceMatrixConfig,
    phylogenetic_tree: ResolvedPhylogeneticTreeConfig,
) -> ResolvedCladeDetectionConfig:
    clade_input = config_input.clade_detection
    if clade_input is None:
        clade_input = CladeDetectionConfigInput()

    if clade_input.enabled is not True:
        return ResolvedCladeDetectionConfig(
            enabled=False,
            method=(clade_input.method or AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE),
            max_within_clade_distance=clade_input.max_within_clade_distance,
        )
    if not distance_matrix.enabled:
        raise ConfigSchemaValidationError(
            "Clade detection requires distance_matrix.enabled to be true.",
            code=AnalysisConfigValidationCode.CLADE_DETECTION_REQUIRES_DISTANCE_MATRIX,
            field_path="clade_detection.enabled",
        )
    if not phylogenetic_tree.enabled:
        raise ConfigSchemaValidationError(
            "Clade detection requires phylogenetic_tree.enabled to be true.",
            code=AnalysisConfigValidationCode.CLADE_DETECTION_REQUIRES_PHYLOGENETIC_TREE,
            field_path="clade_detection.enabled",
        )
    if clade_input.max_within_clade_distance is None:
        raise ConfigSchemaValidationError(
            "Clade detection requires max_within_clade_distance when enabled.",
            code=AnalysisConfigValidationCode.CLADE_DETECTION_THRESHOLD_REQUIRED,
            field_path="clade_detection.max_within_clade_distance",
        )
    return ResolvedCladeDetectionConfig(
        enabled=True,
        method=clade_input.method or AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=clade_input.max_within_clade_distance,
    )


def _normalize_pairwise_groups(groups: list[list[str]]) -> list[list[str]]:
    normalized_groups: list[list[str]] = []
    for group_index, group in enumerate(groups):
        normalized_group = [
            _normalize_pairwise_selector(
                selector,
                field_path=(
                    f"comparative_analysis.pairwise.groups[{group_index}][{selector_index}]"
                ),
            )
            for selector_index, selector in enumerate(group)
        ]
        if len(set(normalized_group)) < 2:
            raise ConfigSchemaValidationError(
                "A pairwise group must contain at least two unique selectors.",
                code=AnalysisConfigValidationCode.PAIRWISE_GROUP_TOO_SMALL,
                field_path=f"comparative_analysis.pairwise.groups[{group_index}]",
            )
        normalized_groups.append(normalized_group)
    return normalized_groups


def _normalize_pairwise_pairs(pairs: list[list[str]]) -> list[list[str]]:
    normalized_pairs: list[list[str]] = []
    for pair_index, pair in enumerate(pairs):
        pair_path = f"comparative_analysis.pairwise.pairs[{pair_index}]"
        if len(pair) != 2:
            raise ConfigSchemaValidationError(
                "A pairwise pair must contain exactly two selectors.",
                code=AnalysisConfigValidationCode.PAIRWISE_PAIR_INVALID,
                field_path=pair_path,
            )
        first = _normalize_pairwise_selector(
            pair[0],
            field_path=f"{pair_path}[0]",
        )
        second = _normalize_pairwise_selector(
            pair[1],
            field_path=f"{pair_path}[1]",
        )
        if first == second:
            raise ConfigSchemaValidationError(
                "A pairwise pair cannot compare a selector with itself.",
                code=AnalysisConfigValidationCode.PAIRWISE_SELF_PAIR,
                field_path=pair_path,
            )
        normalized_pairs.append([first, second])
    return normalized_pairs


def _normalize_pairwise_selector(selector: str, *, field_path: str) -> str:
    try:
        return _normalize_analysis_sample_selector(selector)
    except ValueError as error:
        raise ConfigSchemaValidationError(
            str(error).capitalize() + ".",
            code=AnalysisConfigValidationCode.PAIRWISE_SELECTOR_INVALID,
            field_path=field_path,
        ) from error


def _collect_unknown_field_warnings(
    config_input: AnalysisConfigInput,
) -> list[str]:
    unknown_fields = config_input.model_extra or {}
    return [
        f"Ignoring unknown analysis config field '{field_name}'."
        for field_name in sorted(unknown_fields)
        if field_name not in _INTERNAL_CONFIG_FIELDS
    ]
