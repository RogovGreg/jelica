from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

import jelica_cli.main as cli_main

runner = CliRunner()


def _invoke(args: list[str], home: Path):
    environment = dict(os.environ)
    environment["JELICA_HOME"] = str(home)
    return runner.invoke(cli_main.app, args, env=environment)


def test_storage_usage_default_is_standard_and_short_is_dense(tmp_path: Path) -> None:
    home = tmp_path / "home"
    initialized = _invoke(["config", "init", "--non-interactive"], home)
    assert initialized.exit_code == 0, initialized.stdout

    standard = _invoke(["storage", "usage"], home)
    short = _invoke(["storage", "usage", "--short"], home)

    assert standard.exit_code == 0, standard.stdout
    assert short.exit_code == 0, short.stdout
    assert "Tasks: " in standard.stdout and "workspaces" in standard.stdout
    assert "Tasks: " in short.stdout and "workspaces" not in short.stdout
    assert "\n\n" not in short.stdout


def test_storage_usage_modes_and_selectors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _invoke(["config", "init", "--non-interactive"], home).exit_code == 0

    verbose = _invoke(["storage", "usage", "--verbose", "--tasks", "--total"], home)
    conflict = _invoke(["storage", "usage", "--short", "--verbose"], home)
    selected = _invoke(["storage", "usage", "--results", "--database", "--total"], home)

    assert verbose.exit_code == 0, verbose.stdout
    assert "Tasks\n" in verbose.stdout
    assert "Total\n" in verbose.stdout
    assert "Results\n" not in verbose.stdout
    assert conflict.exit_code != 0
    assert "mutually exclusive" in conflict.output
    assert selected.exit_code == 0, selected.stdout
    assert "Results:" in selected.stdout
    assert "Database:" in selected.stdout
    assert "Total:" in selected.stdout
    assert "Tasks:" not in selected.stdout


def test_storage_usage_machine_output_contains_raw_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _invoke(["config", "init", "--non-interactive"], home).exit_code == 0

    result = _invoke(["storage", "usage", "--machine", "--results"], home)

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["results"]["bytes"] == 0
    assert payload["data"]["selected_categories"] == ["results"]


def test_config_get_reads_semantic_values_and_nested_set_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _invoke(["config", "init", "--non-interactive"], home).exit_code == 0

    disabled = _invoke(["config", "get", "tasks_retention_days"], home)
    set_retention = _invoke(["config", "set", "tasks_retention_days=30"], home)
    enabled = _invoke(["config", "get", "tasks_retention_days"], home)
    nested_set = _invoke(["config", "set", "execution.max_parallel_tasks=2"], home)
    nested_get = _invoke(["config", "get", "execution.max_parallel_tasks"], home)
    machine = _invoke(["config", "get", "--machine", "tasks_retention_days"], home)

    assert disabled.exit_code == 0 and disabled.stdout.strip() == "none"
    assert set_retention.exit_code == 0, set_retention.stdout
    assert enabled.exit_code == 0 and enabled.stdout.strip() == "30"
    assert nested_set.exit_code == 0, nested_set.stdout
    assert nested_get.exit_code == 0 and nested_get.stdout.strip() == "2"
    assert machine.exit_code == 0
    assert json.loads(machine.stdout)["data"]["value"] == 30


def test_config_get_machine_disabled_value_is_json_null_and_unknown_is_controlled(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert _invoke(["config", "init", "--non-interactive"], home).exit_code == 0
    config_path = home / "config.toml"
    before = config_path.read_bytes()

    machine = _invoke(["config", "get", "--machine", "tasks_retention_days"], home)
    unknown = _invoke(["config", "get", "does.not.exist"], home)

    assert machine.exit_code == 0
    assert config_path.read_bytes() == before
    assert json.loads(machine.stdout)["data"]["value"] is None
    assert unknown.exit_code != 0
    assert "does.not.exist" in unknown.output
