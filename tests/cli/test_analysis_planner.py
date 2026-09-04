from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import jelica_cli.main as cli_main
from jelica_cli.system_config import CliSystemConfigService
from jelica_core.runtime import get_service_status, stop_service
from jelica_core.tasks import AnalyticalTaskRegistryService

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stable_available_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jelica_core.system_config.resolver.detect_available_logical_cpu_count",
        lambda: 8,
    )


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


def test_analyze_without_source_matches_explicit_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "sample.fasta").write_text(">sample\nACGT\n", encoding="utf-8")
    monkeypatch.chdir(input_dir)
    config_service = CliSystemConfigService(jelica_home=jelica_home).core_service
    observed_sources: list[tuple[str, ...]] = []
    original_create = cli_main.run_create_analytical_task_from_inputs

    def _record_sources(**kwargs: Any) -> Any:
        observed_sources.append(tuple(kwargs["positional_sources"]))
        return original_create(**kwargs)

    monkeypatch.setattr(
        cli_main,
        "run_create_analytical_task_from_inputs",
        _record_sources,
    )

    try:
        implicit = _invoke(jelica_home=jelica_home, args=["analyze"])
        explicit = _invoke(jelica_home=jelica_home, args=["analyze", "."])

        assert implicit.exit_code == 0, implicit.stdout
        assert explicit.exit_code == 0, explicit.stdout
        assert observed_sources == [(".",), (".",)]
    finally:
        stop_service(
            force=True,
            core_config_service=config_service,
            timeout_seconds=5.0,
        )


def test_analyze_plan_without_source_matches_explicit_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    monkeypatch.chdir(input_dir)
    config_service = CliSystemConfigService(jelica_home=jelica_home).core_service
    resolved_config = config_service.require_initialized_config()

    def _unexpected_service_start(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"Service start was called: {args!r} {kwargs!r}")

    def _unexpected_task_creation(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"Task creation was called: {args!r} {kwargs!r}")

    monkeypatch.setattr(cli_main, "start_service", _unexpected_service_start)
    monkeypatch.setattr(
        cli_main,
        "run_create_analytical_task_from_inputs",
        _unexpected_task_creation,
    )

    implicit = _invoke(jelica_home=jelica_home, args=["analyze", "--plan"])
    explicit = _invoke(jelica_home=jelica_home, args=["analyze", ".", "--plan"])

    assert implicit.exit_code == 0, implicit.stdout
    assert explicit.exit_code == 0, explicit.stdout
    assert implicit.stdout == explicit.stdout
    assert "Sources:\n  - ." in implicit.stdout
    assert list(resolved_config.tasks_dir.iterdir()) == []
    assert get_service_status(core_config_service=config_service).running is False


def test_analyze_plan_preserves_explicit_samples_override(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)

    result = _invoke(
        jelica_home=jelica_home,
        args=["analyze", "--plan", '--samples=["override.fasta"]'],
    )

    assert result.exit_code == 0, result.stdout
    assert "Sources:\n  - override.fasta" in result.stdout
    assert "Sources:\n  - ." not in result.stdout


def test_analyze_show_plan_without_source_uses_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "sample.fasta").write_text(">sample\nACGT\n", encoding="utf-8")
    monkeypatch.chdir(input_dir)
    config_service = CliSystemConfigService(jelica_home=jelica_home).core_service
    observed_sources: list[tuple[str, ...]] = []
    original_create = cli_main.run_create_analytical_task_from_inputs

    def _record_sources(**kwargs: Any) -> Any:
        observed_sources.append(tuple(kwargs["positional_sources"]))
        return original_create(**kwargs)

    monkeypatch.setattr(
        cli_main,
        "run_create_analytical_task_from_inputs",
        _record_sources,
    )

    try:
        result = _invoke(jelica_home=jelica_home, args=["analyze", "--show-plan"])

        assert result.exit_code == 0, result.stdout
        assert "Analysis plan" in result.stdout
        assert "Sources:\n  - ." in result.stdout
        assert "Analysis task " in result.stdout
        assert observed_sources == [(".",)]
    finally:
        stop_service(
            force=True,
            core_config_service=config_service,
            timeout_seconds=5.0,
        )


def test_analyze_plan_does_not_create_task_or_start_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    config_service = CliSystemConfigService(jelica_home=jelica_home).core_service
    resolved_config = config_service.require_initialized_config()

    def _unexpected_service_start(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"Service start was called: {args!r} {kwargs!r}")

    def _unexpected_task_creation(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"Task creation was called: {args!r} {kwargs!r}")

    monkeypatch.setattr(cli_main, "start_service", _unexpected_service_start)
    monkeypatch.setattr(
        cli_main,
        "run_create_analytical_task_from_inputs",
        _unexpected_task_creation,
    )

    result = _invoke(
        jelica_home=jelica_home,
        args=["analyze", "--plan", str(tmp_path / "missing.fasta")],
    )

    assert result.exit_code == 0, result.stdout
    assert "Potential only:" in result.stdout
    assert str(tmp_path / "missing.fasta") in result.stdout
    assert "Resolved configuration:" in result.stdout
    assert "Potential execution:" in result.stdout
    assert "input_processing" in result.stdout
    assert list(resolved_config.tasks_dir.iterdir()) == []
    assert get_service_status(core_config_service=config_service).running is False


def test_analyze_plan_does_not_apply_inline_execution_validation(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)

    result = _invoke(
        jelica_home=jelica_home,
        args=["analyze", "--plan", "A" * 129],
    )

    assert result.exit_code == 0, result.stdout
    assert "Potential execution:" in result.stdout


def test_analyze_show_plan_prints_plan_then_continues_execution(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    config_service = CliSystemConfigService(jelica_home=jelica_home).core_service

    try:
        result = _invoke(
            jelica_home=jelica_home,
            args=["analyze", "--show-plan", str(sample)],
        )

        assert result.exit_code == 0, result.stdout
        assert "Analysis plan" in result.stdout
        assert "Potential execution:" in result.stdout
        assert "Analysis task " in result.stdout
        assert result.stdout.index("Analysis plan") < result.stdout.index("Analysis task ")
        resolved_config = config_service.require_initialized_config()
        assert len(tuple(resolved_config.tasks_dir.iterdir())) == 1
    finally:
        stop_service(
            force=True,
            core_config_service=config_service,
            timeout_seconds=5.0,
        )


def test_analyze_show_plan_continues_and_explicit_name_is_persisted(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    config_service = CliSystemConfigService(jelica_home=jelica_home).core_service

    try:
        result = _invoke(
            jelica_home=jelica_home,
            args=[
                "analyze",
                "--show-plan",
                "--name",
                "Study-A",
                str(sample),
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "Analysis plan" in result.stdout
        assert "Potential execution:" in result.stdout
        assert "Task name: Study-A" in result.stdout
        resolved_config = config_service.require_initialized_config()
        tasks = AnalyticalTaskRegistryService(
            database_path=resolved_config.database_path
        ).list_tasks(limit=None)
        assert len(tasks) == 1
        assert tasks[0].name == "Study-A"
    finally:
        stop_service(
            force=True,
            core_config_service=config_service,
            timeout_seconds=5.0,
        )


@pytest.mark.parametrize(
    "legacy_arguments",
    (
        ("--json",),
        ("--json=true",),
        ("--output=json",),
        ("--output", "json"),
    ),
)
def test_analyze_rejects_removed_structured_output_flags(
    tmp_path: Path,
    legacy_arguments: tuple[str, ...],
) -> None:
    jelica_home = tmp_path / "home"
    _initialize(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    config_service = CliSystemConfigService(jelica_home=jelica_home).core_service
    resolved_config = config_service.require_initialized_config()

    result = _invoke(
        jelica_home=jelica_home,
        args=["analyze", *legacy_arguments, str(sample)],
    )

    assert result.exit_code != 0
    assert "Use '--machine' instead" in result.stdout
    assert list(resolved_config.tasks_dir.iterdir()) == []
    assert get_service_status(core_config_service=config_service).running is False


def test_public_analyze_plan_command_is_removed(tmp_path: Path) -> None:
    result = _invoke(
        jelica_home=tmp_path / "home",
        args=["analyze-plan", "sample.fasta"],
    )
    help_result = _invoke(jelica_home=tmp_path / "home", args=["--help"])

    assert result.exit_code != 0
    assert help_result.exit_code == 0
    assert "analyze-plan" not in help_result.stdout
