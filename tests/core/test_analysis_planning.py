from __future__ import annotations

import json
from pathlib import Path

import pytest

from jelica_core.analysis import plan_analysis_from_inputs, resolve_analysis_execution_selection
from jelica_core.config import (
    AnalysisConfigInput,
    ConfigSchemaValidationError,
    resolve_analysis_config,
)
from jelica_core.system_config import CoreConfigService


def _initialized_service(jelica_home: Path) -> CoreConfigService:
    service = CoreConfigService(jelica_home=jelica_home)
    service.initialize_system_config(force=True)
    return service


def test_plan_resolves_file_defaults_cli_overrides_and_positional_sources(
    tmp_path: Path,
) -> None:
    service = _initialized_service(tmp_path / "home")
    config_json = json.dumps(
        {
            "samples": ["from-config.fasta"],
            "alignment": {"mode": "none"},
            "comparative_analysis": {"enabled": False},
            "distance_matrix": {"enabled": False},
            "phylogenetic_tree": {"enabled": False},
            "clade_detection": {"enabled": False},
        }
    )

    plan = plan_analysis_from_inputs(
        config_json=config_json,
        raw_overrides=("--priority=7",),
        positional_sources=("missing-positional-source.fasta",),
        core_config_service=service,
    )

    assert plan.sources == ("missing-positional-source.fasta",)
    assert plan.resolved_config.priority == 7
    assert plan.input_validation_performed is False
    phase_states = {phase.name: phase.enabled for phase in plan.potential_phases}
    assert phase_states == {
        "initialize_job": True,
        "input_acquisition": True,
        "input_processing": True,
        "alignment": False,
        "comparative_analysis": False,
        "distance_matrix": False,
        "phylogenetic_tree": False,
        "clade_detection": False,
        "result_package": True,
    }


def test_plan_does_not_create_task_or_require_source_to_exist(tmp_path: Path) -> None:
    service = _initialized_service(tmp_path / "home")
    resolved_system_config = service.require_initialized_config()

    plan = plan_analysis_from_inputs(
        config_json=None,
        raw_overrides=tuple(),
        positional_sources=(str(tmp_path / "does-not-exist.fasta"),),
        core_config_service=service,
    )

    assert plan.sources == (str(tmp_path / "does-not-exist.fasta"),)
    assert plan.input_validation_performed is False
    assert list(resolved_system_config.tasks_dir.iterdir()) == []


def test_plan_defaults_to_full_analysis_from_initial_analysis_phase(tmp_path: Path) -> None:
    service = _initialized_service(tmp_path / "home")

    plan = plan_analysis_from_inputs(
        config_json=None,
        raw_overrides=tuple(),
        positional_sources=("sample.fasta",),
        core_config_service=service,
    )

    assert plan.target == "full_analysis"
    assert plan.from_phase == "auto"
    assert plan.resolved_start_phase == "input_processing"
    assert all(phase.selected for phase in plan.potential_phases)
    assert all(phase.skipped_reason is None for phase in plan.potential_phases)


@pytest.mark.parametrize("target", ["input_processing", "validation", "sequence_statistics"])
def test_input_processing_targets_select_prefix_and_result_package(
    tmp_path: Path,
    target: str,
) -> None:
    service = _initialized_service(tmp_path / target)

    plan = plan_analysis_from_inputs(
        config_json=None,
        raw_overrides=(f"--execution.target={target}",),
        positional_sources=("sample.fasta",),
        core_config_service=service,
    )

    selected = tuple(phase.name for phase in plan.potential_phases if phase.selected)
    assert plan.target == target
    assert selected == (
        "initialize_job",
        "input_acquisition",
        "input_processing",
        "result_package",
    )
    assert all(
        phase.skipped_reason == f"after execution target '{target}'"
        for phase in plan.potential_phases
        if not phase.selected
    )


def test_result_package_target_selects_full_pipeline(tmp_path: Path) -> None:
    service = _initialized_service(tmp_path / "home")

    plan = plan_analysis_from_inputs(
        config_json=None,
        raw_overrides=("--execution.target=result_package",),
        positional_sources=("sample.fasta",),
        core_config_service=service,
    )

    assert plan.target == "result_package"
    assert all(phase.selected for phase in plan.potential_phases)


def test_plan_rejects_unknown_target(tmp_path: Path) -> None:
    service = _initialized_service(tmp_path / "home")

    with pytest.raises(ConfigSchemaValidationError, match="Unsupported analysis execution target"):
        plan_analysis_from_inputs(
            config_json=None,
            raw_overrides=("--execution.target=unknown",),
            positional_sources=("sample.fasta",),
            core_config_service=service,
        )


def test_plan_allows_explicit_from_phase_for_raw_inputs(tmp_path: Path) -> None:
    service = _initialized_service(tmp_path / "home")

    plan = plan_analysis_from_inputs(
        config_json=None,
        raw_overrides=("--execution.from_phase=alignment",),
        positional_sources=("sample.fasta",),
        core_config_service=service,
    )

    selected = tuple(phase.name for phase in plan.potential_phases if phase.selected)
    assert plan.from_phase == "alignment"
    assert plan.resolved_start_phase == "alignment"
    assert selected == (
        "alignment",
        "comparative_analysis",
        "distance_matrix",
        "phylogenetic_tree",
        "clade_detection",
        "result_package",
    )


def test_plan_validates_from_phase_order_before_raw_input_rejection(tmp_path: Path) -> None:
    service = _initialized_service(tmp_path / "home")

    with pytest.raises(ConfigSchemaValidationError, match="is after execution target"):
        plan_analysis_from_inputs(
            config_json=None,
            raw_overrides=(
                "--execution.target=input_processing",
                "--execution.from_phase=alignment",
            ),
            positional_sources=("sample.fasta",),
            core_config_service=service,
        )


def test_explicit_from_phase_can_be_resolved_for_compatible_same_task_context() -> None:
    resolution = resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": ["sample.fasta"],
                "execution": {
                    "target": "distance_matrix",
                    "from_phase": "alignment",
                },
            }
        )
    )

    selection = resolve_analysis_execution_selection(
        config=resolution.config,
        allow_explicit_from_phase=True,
    )

    assert selection.resolved_start_phase == "alignment"
    assert selection.selected_phase_names == frozenset(
        {
            "alignment",
            "comparative_analysis",
            "distance_matrix",
            "result_package",
        }
    )


def test_explicit_from_phase_equal_to_target_selects_target_and_result_package() -> None:
    resolution = resolve_analysis_config(
        AnalysisConfigInput.model_validate(
            {
                "samples": ["sample.fasta"],
                "execution": {
                    "target": "distance_matrix",
                    "from_phase": "distance_matrix",
                },
            }
        )
    )

    selection = resolve_analysis_execution_selection(
        config=resolution.config,
        allow_explicit_from_phase=True,
    )

    assert selection.resolved_start_phase == "distance_matrix"
    assert selection.selected_phase_names == frozenset({"distance_matrix", "result_package"})
