from __future__ import annotations

from jelica_core.config import parse_cli_overrides
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import InitializedAnalysisTask, LocalTaskStorage

from .models import InitializeAnalysisTaskRequest
from .orchestrator import AnalysisOrchestrator
from .planning import AnalysisPlan, build_analysis_plan


def initialize_analysis_task(
    *,
    request: InitializeAnalysisTaskRequest,
    orchestrator: AnalysisOrchestrator | None = None,
    core_config_service: CoreConfigService | None = None,
) -> InitializedAnalysisTask:
    selected_config_service = core_config_service or CoreConfigService()
    resolved_core_config = selected_config_service.require_initialized_config()
    task_storage = LocalTaskStorage(tasks_dir=resolved_core_config.tasks_dir)
    selected_orchestrator = orchestrator or AnalysisOrchestrator()
    return selected_orchestrator.initialize_task(
        request=request,
        task_storage=task_storage,
        default_alignment_mode=resolved_core_config.default_alignment_mode,
    )


def plan_analysis(
    *,
    request: InitializeAnalysisTaskRequest,
    orchestrator: AnalysisOrchestrator | None = None,
    core_config_service: CoreConfigService | None = None,
) -> AnalysisPlan:
    """Resolve and describe a potential execution without creating a task."""

    selected_config_service = core_config_service or CoreConfigService()
    resolved_core_config = selected_config_service.require_initialized_config()
    selected_orchestrator = orchestrator or AnalysisOrchestrator()
    resolution = selected_orchestrator.resolve_request(
        request=request,
        default_alignment_mode=resolved_core_config.default_alignment_mode,
    )
    return build_analysis_plan(resolution=resolution)


def plan_analysis_from_inputs(
    *,
    config_json: str | None,
    raw_overrides: tuple[str, ...],
    positional_sources: tuple[str, ...],
    core_config_service: CoreConfigService | None = None,
) -> AnalysisPlan:
    """Build a plan from the same input forms accepted by task initialization."""

    request = InitializeAnalysisTaskRequest(
        config_json=config_json,
        overrides=tuple(parse_cli_overrides(raw_overrides)),
        positional_sources=positional_sources,
    )
    return plan_analysis(
        request=request,
        core_config_service=core_config_service,
    )
