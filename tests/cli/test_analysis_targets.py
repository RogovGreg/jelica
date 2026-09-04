from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

import jelica_cli.main as cli_main
import jelica_core.events.operations as core_operations

runner = CliRunner()


def _invoke(*, jelica_home: Path, args: list[str]) -> Any:
    environment = dict(os.environ)
    environment["JELICA_HOME"] = str(jelica_home)
    return runner.invoke(cli_main.app, args, env=environment)


def _initialize(jelica_home: Path) -> None:
    result = _invoke(
        jelica_home=jelica_home,
        args=["config", "init", "--non-interactive"],
    )
    assert result.exit_code == 0, result.stdout


@pytest.mark.parametrize("command", ("analyze", "align", "statistics", "distance", "tree"))
def test_analysis_commands_share_public_options(tmp_path: Path, command: str) -> None:
    result = _invoke(jelica_home=tmp_path / "home", args=[command, "--help"])

    assert result.exit_code == 0, result.stdout
    for option in (
        "--plan",
        "--show-plan",
        "--name",
        "--trace-id",
        "--target",
        "--from-phase",
        "--machine",
        "--verbose",
        "--quiet",
    ):
        assert option in result.stdout


@pytest.mark.parametrize(
    ("command", "target"),
    (
        ("align", "alignment"),
        ("statistics", "sequence_statistics"),
        ("distance", "distance_matrix"),
        ("tree", "phylogenetic_tree"),
    ),
)
def test_analysis_alias_plan_passes_all_inputs_and_fixed_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    target: str,
) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    config_path = tmp_path / "analysis.json"
    config_path.write_text('{"priority":2}\n', encoding="utf-8")
    source = tmp_path / "sample.fasta"
    trace_id = "00000000-0000-4000-8000-000000000123"
    observed: list[dict[str, Any]] = []
    original_plan = cli_main.plan_analysis_from_inputs

    def _capture_plan(**kwargs: Any) -> Any:
        observed.append(kwargs)
        return original_plan(**kwargs)

    def _unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"execution was unexpectedly started: {args!r} {kwargs!r}")

    monkeypatch.setattr(cli_main, "plan_analysis_from_inputs", _capture_plan)
    monkeypatch.setattr(cli_main, "run_create_analytical_task_from_inputs", _unexpected)
    monkeypatch.setattr(cli_main, "start_service", _unexpected)

    result = _invoke(
        jelica_home=jelica_home,
        args=[
            command,
            "--plan",
            "--machine",
            "--trace-id",
            trace_id,
            "--from-phase",
            "auto",
            str(config_path),
            str(source),
            "--priority=7",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert len(observed) == 1
    assert observed[0]["config_json"] == config_path.read_text(encoding="utf-8")
    assert observed[0]["positional_sources"] == (str(source),)
    assert observed[0]["raw_overrides"] == (
        "--priority=7",
        f"--execution.target={target}",
        "--execution.from_phase=auto",
    )
    payload = json.loads(result.stdout)
    assert payload["trace_id"] == trace_id
    assert payload["data"]["plan"]["target"] == target
    assert payload["data"]["plan"]["from_phase"] == "auto"


def test_analysis_alias_plan_defaults_source_and_prints_selection(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)

    result = _invoke(jelica_home=jelica_home, args=["align", "--plan"])

    assert result.exit_code == 0, result.stdout
    assert "Sources:\n  - ." in result.stdout
    assert "Target: alignment" in result.stdout
    assert "From phase: auto" in result.stdout
    assert "Resolved start phase: input_processing" in result.stdout
    assert "(skipped:" in result.stdout


def test_analyze_dedicated_execution_flags_reach_machine_plan(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)

    result = _invoke(
        jelica_home=jelica_home,
        args=[
            "analyze",
            "--target",
            "distance_matrix",
            "--from-phase",
            "auto",
            "--plan",
            "--machine",
        ],
    )

    assert result.exit_code == 0, result.stdout
    plan = json.loads(result.stdout)["data"]["plan"]
    assert plan["target"] == "distance_matrix"
    assert plan["from_phase"] == "auto"
    assert plan["resolved_start_phase"] == "input_processing"


def test_analysis_alias_rejects_conflicting_explicit_target(tmp_path: Path) -> None:
    result = _invoke(
        jelica_home=tmp_path / "home",
        args=[
            "align",
            "--target",
            "distance_matrix",
            "--plan",
            "--machine",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["name"] == "CLI_ANALYZE_ARGUMENT_INVALID"
    assert "fixes --target=alignment" in payload["error"]["message"]
    UUID(payload["command_id"])


def test_analysis_alias_show_plan_uses_same_target_for_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")

    monkeypatch.setattr(cli_main, "_ensure_execution_service", lambda **_kwargs: None)
    monkeypatch.setattr(
        core_operations,
        "launch_background_runtime",
        lambda **_kwargs: 12345,
    )

    def _completed_watch(*, task_id: str, **_kwargs: Any) -> cli_main.WatchCliOutcome:
        return cli_main.WatchCliOutcome(
            rows=(
                cli_main.WatchTaskRow(
                    task_id=task_id,
                    job_id="test-job",
                    state="completed",
                    stage="result_package",
                    progress=100,
                    warning_count=0,
                ),
            ),
            missing_task_ids=tuple(),
            inactive_tasks=tuple(),
            events=tuple(),
            interrupted=False,
        )

    monkeypatch.setattr(cli_main, "_watch_execution_task", _completed_watch)

    result = _invoke(
        jelica_home=jelica_home,
        args=["statistics", "--show-plan", "--machine", str(sample)],
    )

    assert result.exit_code == 0, result.stdout
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["plan"]["target"] == "sequence_statistics"
    assert payload["data"]["task"]["config"]["execution"]["target"] == "sequence_statistics"


def test_verbose_analysis_prints_execution_selection_only_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")

    monkeypatch.setattr(cli_main, "_ensure_execution_service", lambda **_kwargs: None)
    monkeypatch.setattr(
        core_operations,
        "launch_background_runtime",
        lambda **_kwargs: 12345,
    )

    def _completed_watch(*, task_id: str, **_kwargs: Any) -> cli_main.WatchCliOutcome:
        return cli_main.WatchCliOutcome(
            rows=(
                cli_main.WatchTaskRow(
                    task_id=task_id,
                    job_id="test-job",
                    state="completed",
                    stage="result_package",
                    progress=100,
                    warning_count=0,
                ),
            ),
            missing_task_ids=tuple(),
            inactive_tasks=tuple(),
            events=tuple(),
            interrupted=False,
        )

    monkeypatch.setattr(cli_main, "_watch_execution_task", _completed_watch)

    standard = _invoke(
        jelica_home=jelica_home,
        args=["statistics", "--name", "standard-target", str(sample)],
    )
    verbose = _invoke(
        jelica_home=jelica_home,
        args=["statistics", "--name", "verbose-target", "--verbose", str(sample)],
    )

    assert standard.exit_code == 0, standard.stdout
    assert verbose.exit_code == 0, verbose.stdout
    assert "Execution selection:" not in standard.stdout
    assert "Execution selection:" in verbose.stdout
    assert "target: sequence_statistics" in verbose.stdout
    assert "resolved_start_phase: input_processing" in verbose.stdout


def test_dedicated_execution_flags_are_last_overrides() -> None:
    parsed = cli_main._parse_analyze_arguments(
        [
            "--execution.target=full_analysis",
            "--execution.from_phase=alignment",
            "sample.fasta",
        ]
    )

    effective = cli_main._with_execution_overrides(
        parsed,
        target="distance_matrix",
        from_phase="auto",
    )

    assert effective.raw_overrides == (
        "--execution.target=full_analysis",
        "--execution.from_phase=alignment",
        "--execution.target=distance_matrix",
        "--execution.from_phase=auto",
    )
