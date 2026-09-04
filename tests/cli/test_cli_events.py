from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

import jelica_cli.main as cli_main
from jelica_cli.system_config import CliSystemConfigService
from jelica_core.system_config import CoreConfigService, ResolvedCoreConfig

runner = CliRunner()


def _invoke_cli(*, args: list[str], jelica_home: Path) -> Any:
    env = dict(os.environ)
    env["JELICA_HOME"] = str(jelica_home)
    return runner.invoke(cli_main.app, args, env=env)


def _init_config(jelica_home: Path) -> None:
    result = _invoke_cli(
        args=["config", "init", "--non-interactive"],
        jelica_home=jelica_home,
    )
    assert result.exit_code == 0


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _load_resolved_core_config(jelica_home: Path) -> ResolvedCoreConfig:
    return CliSystemConfigService(jelica_home=jelica_home).load_resolved_core_config()


def test_cli_config_validate_writes_significant_system_event(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)

    result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert "System config is valid." in result.stdout

    resolved = _load_resolved_core_config(jelica_home)
    system_log = resolved.logs_dir / "system-events.jsonl"
    events = _read_jsonl(system_log)
    assert any(event["name"] == "CORE_TASK_REGISTRY_SCHEMA_VALIDATED" for event in events)
    assert any(event["name"] == "CORE_SYSTEM_CONFIG_VALIDATED" for event in events)


def test_cli_config_validate_error_is_clear_and_hides_traceback(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    config_path = CoreConfigService(jelica_home=jelica_home).get_config_path()
    config_path.write_text("schema_version =\n", encoding="utf-8")

    result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "System config is invalid" in result.stdout
    assert "Traceback" not in result.stdout


def test_cli_analyze_success_creates_task_log(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample_a = tmp_path / "a.fasta"
    sample_b = tmp_path / "b.fasta"
    sample_a.write_text(">a\nACGT\n", encoding="utf-8")
    sample_b.write_text(">b\nACGG\n", encoding="utf-8")

    result = _invoke_cli(
        args=["analyze", str(sample_a), str(sample_b)],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    resolved = _load_resolved_core_config(jelica_home)
    task_dirs = sorted(path for path in resolved.tasks_dir.iterdir() if path.is_dir())
    assert len(task_dirs) == 1
    task_log = task_dirs[0] / "task-events.jsonl"
    assert task_log.is_file()
    task_events = _read_jsonl(task_log)
    assert any(event["name"] == "CORE_ANALYZE_TASK_INITIALIZED" for event in task_events)


def test_cli_analyze_missing_source_returns_core_event_without_traceback(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)

    result = _invoke_cli(
        args=["analyze", str(tmp_path / "missing.fasta")],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "Input path was not found" in result.stdout
    assert "Traceback" not in result.stdout


def test_cli_analyze_invalid_json_config_returns_core_error(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    invalid_config = tmp_path / "invalid.json"
    invalid_config.write_text('{"samples":[}', encoding="utf-8")

    result = _invoke_cli(args=["analyze", str(invalid_config)], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "configuration is invalid" in result.stdout
    assert "Traceback" not in result.stdout


def test_cli_analyze_unknown_parameter_creates_warning_event(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample_a = tmp_path / "a.fasta"
    sample_b = tmp_path / "b.fasta"
    sample_a.write_text(">a\nACGT\n", encoding="utf-8")
    sample_b.write_text(">b\nACGG\n", encoding="utf-8")

    result = _invoke_cli(
        args=["analyze", str(sample_a), str(sample_b), "--unknown.flag=true"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    assert "Warning:" in result.stdout
    assert "Unknown analysis config parameter" in result.stdout

    resolved = _load_resolved_core_config(jelica_home)
    task_dirs = sorted(path for path in resolved.tasks_dir.iterdir() if path.is_dir())
    task_events = _read_jsonl(task_dirs[0] / "task-events.jsonl")
    assert any(event["name"] == "CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED" for event in task_events)
