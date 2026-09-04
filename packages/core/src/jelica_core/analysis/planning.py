from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from jelica_core.config import (
    AUTO_ANALYSIS_EXECUTION_FROM_PHASE,
    AnalysisAlignmentMode,
    AnalysisConfigResolutionResult,
    ConfigSchemaValidationError,
    ResolvedAnalysisConfig,
)
from jelica_core.runtime.models import DEFAULT_PIPELINE_NAME
from jelica_core.runtime.pipeline import (
    PipelineDefinition,
    analysis_target_terminal_stage,
    build_pipeline_definition,
)

_INITIAL_ANALYSIS_PHASE = "input_processing"
_FULL_ANALYSIS_FROM_PHASE = "full_analysis"
_LEGACY_AUTO_FROM_PHASE = "raw"


@dataclass(frozen=True, slots=True)
class AnalysisExecutionSelection:
    """Validated technical stage bounds for one resolved analysis config."""

    target: str
    from_phase: str
    resolved_start_phase: str
    selected_phase_names: frozenset[str]


class AnalysisPlanPhase(BaseModel):
    """One technical pipeline phase and its configured availability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    enabled: bool
    disabled_reason: str | None = None
    selected: bool = True
    skipped_reason: str | None = None


class AnalysisPlan(BaseModel):
    """A configuration-only preview of one potential analysis execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[str | None, ...]
    resolved_config: ResolvedAnalysisConfig
    target: str = Field(min_length=1)
    from_phase: str = Field(min_length=1)
    resolved_start_phase: str = Field(min_length=1)
    potential_phases: tuple[AnalysisPlanPhase, ...]
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    input_validation_performed: Literal[False] = False


def build_analysis_plan(*, resolution: AnalysisConfigResolutionResult) -> AnalysisPlan:
    """Build a plan without acquiring inputs, validating them, or executing stages."""

    config = resolution.config
    pipeline = build_pipeline_definition(pipeline_name=DEFAULT_PIPELINE_NAME)
    selection = resolve_analysis_execution_selection(
        config=config,
        pipeline=pipeline,
        allow_explicit_from_phase=True,
    )
    phase_states = _configured_phase_states(config=config)
    phases = tuple(
        AnalysisPlanPhase(
            name=stage.stage_id,
            enabled=phase_states.get(stage.stage_id, (True, None))[0],
            disabled_reason=phase_states.get(stage.stage_id, (True, None))[1],
            selected=stage.stage_id in selection.selected_phase_names,
            skipped_reason=_selection_skip_reason(
                stage_id=stage.stage_id,
                selection=selection,
                pipeline=pipeline,
            ),
        )
        for stage in pipeline.stages
    )
    return AnalysisPlan(
        sources=tuple(config.samples),
        resolved_config=config,
        target=selection.target,
        from_phase=selection.from_phase,
        resolved_start_phase=selection.resolved_start_phase,
        potential_phases=phases,
        warnings=resolution.warnings,
    )


def resolve_analysis_execution_selection(
    *,
    config: ResolvedAnalysisConfig,
    pipeline: PipelineDefinition | None = None,
    allow_explicit_from_phase: bool,
) -> AnalysisExecutionSelection:
    """Validate and resolve requested execution bounds against the real pipeline."""

    selected_pipeline = pipeline or build_pipeline_definition(pipeline_name=DEFAULT_PIPELINE_NAME)
    ordered_phase_names = tuple(stage.stage_id for stage in selected_pipeline.stages)
    target = config.execution.target
    from_phase = config.execution.from_phase

    terminal_phase = _resolve_public_phase(
        value=target,
        field_path="execution.target",
    )
    terminal_index = _phase_index(
        phase=terminal_phase,
        ordered_phase_names=ordered_phase_names,
        field_path="execution.target",
    )

    if from_phase in {AUTO_ANALYSIS_EXECUTION_FROM_PHASE, _LEGACY_AUTO_FROM_PHASE}:
        resolved_start_phase = _INITIAL_ANALYSIS_PHASE
        start_index = 0
    else:
        if from_phase == _FULL_ANALYSIS_FROM_PHASE:
            raise ConfigSchemaValidationError(
                "'full_analysis' is a target, not an execution start phase.",
                field_path="execution.from_phase",
            )
        resolved_start_phase = _resolve_public_phase(
            value=from_phase,
            field_path="execution.from_phase",
        )
        start_index = _phase_index(
            phase=resolved_start_phase,
            ordered_phase_names=ordered_phase_names,
            field_path="execution.from_phase",
        )
        if start_index > terminal_index:
            raise ConfigSchemaValidationError(
                f"Start phase '{from_phase}' is after execution target '{target}'.",
                field_path="execution.from_phase",
            )
        if not allow_explicit_from_phase:
            raise ConfigSchemaValidationError(
                "An explicit execution.from_phase requires compatible committed "
                "artifacts from the same task; raw analysis inputs must use 'auto'.",
                field_path="execution.from_phase",
            )

    selected_phase_names = frozenset(
        phase_name
        for index, phase_name in enumerate(ordered_phase_names)
        if start_index <= index <= terminal_index or phase_name == "result_package"
    )
    return AnalysisExecutionSelection(
        target=target,
        from_phase=from_phase,
        resolved_start_phase=resolved_start_phase,
        selected_phase_names=selected_phase_names,
    )


def _resolve_public_phase(*, value: str, field_path: str) -> str:
    try:
        return analysis_target_terminal_stage(value)
    except ValueError as error:
        raise ConfigSchemaValidationError(
            str(error).capitalize() + ".",
            field_path=field_path,
        ) from error


def _phase_index(
    *,
    phase: str,
    ordered_phase_names: tuple[str, ...],
    field_path: str,
) -> int:
    try:
        return ordered_phase_names.index(phase)
    except ValueError as error:
        raise ConfigSchemaValidationError(
            f"Phase '{phase}' is unavailable in the selected analysis pipeline.",
            field_path=field_path,
        ) from error


def _selection_skip_reason(
    *,
    stage_id: str,
    selection: AnalysisExecutionSelection,
    pipeline: PipelineDefinition,
) -> str | None:
    if stage_id in selection.selected_phase_names:
        return None
    ordered_phase_names = tuple(stage.stage_id for stage in pipeline.stages)
    stage_index = ordered_phase_names.index(stage_id)
    start_index = ordered_phase_names.index(selection.resolved_start_phase)
    if stage_index < start_index:
        return f"before resolved start phase '{selection.resolved_start_phase}'"
    return f"after execution target '{selection.target}'"


def _configured_phase_states(
    *,
    config: ResolvedAnalysisConfig,
) -> dict[str, tuple[bool, str | None]]:
    alignment_enabled = config.alignment.mode is not AnalysisAlignmentMode.NONE
    return {
        "alignment": (
            alignment_enabled,
            None if alignment_enabled else "alignment.mode is 'none'",
        ),
        "comparative_analysis": (
            config.comparative_analysis.enabled,
            None
            if config.comparative_analysis.enabled
            else "comparative_analysis.enabled is false",
        ),
        "distance_matrix": (
            config.distance_matrix.enabled,
            None if config.distance_matrix.enabled else "distance_matrix.enabled is false",
        ),
        "phylogenetic_tree": (
            config.phylogenetic_tree.enabled,
            None if config.phylogenetic_tree.enabled else "phylogenetic_tree.enabled is false",
        ),
        "clade_detection": (
            config.clade_detection.enabled,
            None if config.clade_detection.enabled else "clade_detection.enabled is false",
        ),
    }


__all__ = [
    "AnalysisExecutionSelection",
    "AnalysisPlan",
    "AnalysisPlanPhase",
    "build_analysis_plan",
    "resolve_analysis_execution_selection",
]
