from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import fields
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import jelica_cli.main as cli_main
import jelica_cli.system_config as cli_system_config
from jelica_cli.system_config import CliConfig, CliSystemConfigService
from jelica_cli.terminal import create_terminal_presenter
from jelica_core.config import AnalysisConfigInput, resolve_analysis_config
from jelica_core.runtime import WorkerLaunchSpec
from jelica_core.runtime.artifacts import StageArtifactManifest
from jelica_core.system_config import (
    CoreConfigInput,
    CoreConfigMissingFieldError,
    CoreConfigService,
    CoreConfigUnknownFieldError,
    CoreConfigValidationError,
    ResolvedCoreConfig,
)
from jelica_core.tasks.storage import compute_config_hash

runner = CliRunner()
ANSI_ESCAPE = "\x1b["


def _invoke_cli(*, args: list[str], jelica_home: Path) -> Any:
    environment = dict(os.environ)
    environment["JELICA_HOME"] = str(jelica_home)
    return runner.invoke(cli_main.app, args, env=environment)


def _invoke_cli_with_captured_terminal(
    *,
    args: list[str],
    jelica_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_terminal: bool,
) -> tuple[Any, str]:
    stream = StringIO()
    forced_terminal = force_terminal

    def _capturing_terminal_factory(
        *,
        color: bool = True,
        emoji: bool = True,
        file: Any = None,
        force_terminal: bool | None = None,
    ) -> Any:
        _ = (file, force_terminal)
        return create_terminal_presenter(
            color=color,
            emoji=emoji,
            file=stream,
            force_terminal=forced_terminal,
        )

    monkeypatch.setattr(cli_main, "create_terminal_presenter", _capturing_terminal_factory)
    result = _invoke_cli(args=args, jelica_home=jelica_home)
    return result, stream.getvalue()


def _init_combined_config(jelica_home: Path) -> Path:
    result = _invoke_cli(
        args=["config", "init", "--non-interactive"],
        jelica_home=jelica_home,
    )
    assert result.exit_code == 0, result.stdout
    return CliSystemConfigService(jelica_home=jelica_home).get_config_path()


def _append_cli_section(config_path: Path, *, color: str = "true", emoji: str = "true") -> None:
    core_payload = config_path.read_text(encoding="utf-8").rstrip("\n")
    config_path.write_text(
        f"{core_payload}\n\n[cli]\ncolor = {color}\nemoji = {emoji}\n",
        encoding="utf-8",
    )


def _assert_model_document_is_complete(
    *,
    model_type: type[BaseModel],
    document: dict[str, object],
) -> None:
    assert set(document) == set(model_type.model_fields)
    for field_name, field in model_type.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            nested = document[field_name]
            assert isinstance(nested, dict)
            _assert_model_document_is_complete(
                model_type=annotation,
                document=nested,
            )


def test_config_init_writes_complete_combined_document_and_strictly_reloads(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    config_path = _init_combined_config(jelica_home)

    with config_path.open("rb") as config_file:
        document = tomllib.load(config_file)

    cli_document = document.pop("cli")
    assert isinstance(cli_document, dict)
    _assert_model_document_is_complete(
        model_type=CoreConfigInput,
        document=document,
    )
    assert cli_document == {"color": True, "emoji": True}
    assert document["ncbi_api_key"] == ""
    logging_document = document["logging"]
    tools_document = document["tools"]
    assert isinstance(logging_document, dict)
    assert isinstance(tools_document, dict)
    mafft_document = tools_document["mafft"]
    assert isinstance(mafft_document, dict)
    assert logging_document["system_level"] == ""
    assert logging_document["task_level"] == ""
    assert mafft_document["executable"] == ""

    loaded = CliSystemConfigService(jelica_home=jelica_home).load()
    assert loaded.cli == CliConfig(color=True, emoji=True)
    assert loaded.resolved_core.mafft_executable is None


def test_missing_cli_section_is_rejected_with_safe_path(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    CoreConfigService(jelica_home=jelica_home).initialize_system_config()

    with pytest.raises(CoreConfigMissingFieldError, match=r"cli") as captured:
        CliSystemConfigService(jelica_home=jelica_home).load()

    assert captured.value.field_path == "cli"


@pytest.mark.parametrize(
    ("cli_lines", "field_path"),
    (
        ("", "cli.color"),
        ("color = true\n", "cli.emoji"),
        ("emoji = true\n", "cli.color"),
    ),
)
def test_incomplete_cli_section_is_rejected(
    tmp_path: Path,
    cli_lines: str,
    field_path: str,
) -> None:
    jelica_home = tmp_path / field_path.replace(".", "-")
    core_service = CoreConfigService(jelica_home=jelica_home)
    core_service.initialize_system_config()
    config_path = core_service.get_config_path()
    config_path.write_text(
        f"{config_path.read_text(encoding='utf-8').rstrip()}\n\n[cli]\n{cli_lines}",
        encoding="utf-8",
    )

    with pytest.raises(CoreConfigMissingFieldError) as captured:
        CliSystemConfigService(jelica_home=jelica_home).load()

    assert captured.value.field_path == field_path


@pytest.mark.parametrize(
    ("color", "emoji"),
    ((False, True), (True, False), (False, False)),
)
def test_explicit_false_cli_values_are_valid(
    tmp_path: Path,
    color: bool,
    emoji: bool,
) -> None:
    jelica_home = tmp_path / f"{color}-{emoji}"
    core_service = CoreConfigService(jelica_home=jelica_home)
    core_service.initialize_system_config()
    _append_cli_section(
        core_service.get_config_path(),
        color=str(color).lower(),
        emoji=str(emoji).lower(),
    )

    loaded = CliSystemConfigService(jelica_home=jelica_home).load()

    assert loaded.cli.color is color
    assert loaded.cli.emoji is emoji


def test_cli_invalid_type_and_unknown_field_are_rejected(tmp_path: Path) -> None:
    invalid_type_home = tmp_path / "invalid-type"
    invalid_type_core = CoreConfigService(jelica_home=invalid_type_home)
    invalid_type_core.initialize_system_config()
    _append_cli_section(invalid_type_core.get_config_path(), color='"true"')

    with pytest.raises(CoreConfigValidationError, match=r"cli\.color"):
        CliSystemConfigService(jelica_home=invalid_type_home).load()

    unknown_home = tmp_path / "unknown"
    unknown_core = CoreConfigService(jelica_home=unknown_home)
    unknown_core.initialize_system_config()
    _append_cli_section(unknown_core.get_config_path())
    with unknown_core.get_config_path().open("a", encoding="utf-8") as config_file:
        config_file.write("decoration = true\n")

    with pytest.raises(CoreConfigUnknownFieldError) as captured:
        CliSystemConfigService(jelica_home=unknown_home).load()

    assert captured.value.field_path == "cli.decoration"


def test_unknown_application_namespace_is_rejected(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    config_path = _init_combined_config(jelica_home)
    with config_path.open("a", encoding="utf-8") as config_file:
        config_file.write("\n[unknown]\nvalue = true\n")

    with pytest.raises(CoreConfigUnknownFieldError) as captured:
        CliSystemConfigService(jelica_home=jelica_home).load()

    assert captured.value.field_path == "unknown"


def test_typo_in_core_section_is_rejected_as_unknown_namespace(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    config_path = _init_combined_config(jelica_home)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("[logging]", "[loging]"),
        encoding="utf-8",
    )

    with pytest.raises(CoreConfigUnknownFieldError) as captured:
        CliSystemConfigService(jelica_home=jelica_home).load()

    assert captured.value.field_path == "loging"


def test_validate_and_show_reject_incomplete_file_without_default_completion(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    config_path = _init_combined_config(jelica_home)
    original = config_path.read_text(encoding="utf-8")
    incomplete = original.replace("emoji = true\n", "")
    config_path.write_text(incomplete, encoding="utf-8")

    validate_result = _invoke_cli(
        args=["config", "validate"],
        jelica_home=jelica_home,
    )
    show_result = _invoke_cli(
        args=["config", "show"],
        jelica_home=jelica_home,
    )

    assert validate_result.exit_code != 0
    assert show_result.exit_code != 0
    assert "cli.emoji" in validate_result.stdout
    assert "cli.emoji" in show_result.stdout
    assert config_path.read_text(encoding="utf-8") == incomplete


def test_config_show_reports_persisted_core_and_cli_namespaces(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_combined_config(jelica_home)

    result = _invoke_cli(args=["config", "show"], jelica_home=jelica_home)
    shown = json.loads(result.stdout)

    assert result.exit_code == 0
    assert shown["schema_version"] == 1
    assert shown["data"]["directory"] == "data"
    assert shown["execution"]["max_parallel_tasks"] == 1
    assert shown["cli"] == {"color": True, "emoji": True}


def test_config_set_and_unset_keep_cli_keys_explicit(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    config_path = _init_combined_config(jelica_home)

    color_result = _invoke_cli(
        args=["config", "set", "cli.color", "false"],
        jelica_home=jelica_home,
    )
    emoji_result = _invoke_cli(
        args=["config", "set", "cli.emoji", "false"],
        jelica_home=jelica_home,
    )
    unset_result = _invoke_cli(
        args=["config", "unset", "cli.color"],
        jelica_home=jelica_home,
    )

    assert color_result.exit_code == 0
    assert emoji_result.exit_code == 0
    assert unset_result.exit_code == 0
    with config_path.open("rb") as config_file:
        document = tomllib.load(config_file)
    assert document["cli"] == {"color": True, "emoji": False}


def test_analytical_command_stops_before_task_creation_for_incomplete_config(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    config_path = _init_combined_config(jelica_home)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("emoji = true\n", ""),
        encoding="utf-8",
    )
    sample_path = tmp_path / "neutral.fasta"
    sample_path.write_text(">record\nACGT\n", encoding="utf-8")

    result = _invoke_cli(
        args=["analyze", str(sample_path)],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "cli.emoji" in result.stdout
    tasks_dir = jelica_home / "data" / "tasks"
    assert not any(tasks_dir.iterdir())


def test_invalid_core_bootstrap_error_uses_red_style_when_terminal_supports_color(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    config_path = _init_combined_config(jelica_home)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("input_directory_max_depth = 3\n", ""),
        encoding="utf-8",
    )

    result, rendered = _invoke_cli_with_captured_terminal(
        args=["config", "validate"],
        jelica_home=jelica_home,
        monkeypatch=monkeypatch,
        force_terminal=True,
    )

    assert result.exit_code != 0
    assert "Error: System config is invalid:" in rendered
    assert re.search(r"\x1b\[[0-9;]*31m", rendered) is not None
    assert "🌲" not in rendered
    assert "🎄" not in rendered


def test_missing_cli_bootstrap_error_uses_red_style_without_decorative_symbol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    config_path = _init_combined_config(jelica_home)
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        config_text.split("\n\n[cli]\n", maxsplit=1)[0] + "\n",
        encoding="utf-8",
    )

    result, rendered = _invoke_cli_with_captured_terminal(
        args=["config", "validate"],
        jelica_home=jelica_home,
        monkeypatch=monkeypatch,
        force_terminal=True,
    )

    assert result.exit_code != 0
    assert "Error: System config is invalid:" in rendered
    assert "missing required field 'cli'" in rendered
    assert re.search(r"\x1b\[[0-9;]*31m", rendered) is not None
    assert "🌲" not in rendered
    assert "🎄" not in rendered


def test_bootstrap_error_on_non_color_stream_has_plain_text_without_ansi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    config_path = _init_combined_config(jelica_home)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("emoji = true\n", ""),
        encoding="utf-8",
    )

    result, rendered = _invoke_cli_with_captured_terminal(
        args=["config", "validate"],
        jelica_home=jelica_home,
        monkeypatch=monkeypatch,
        force_terminal=False,
    )

    assert result.exit_code != 0
    assert ANSI_ESCAPE not in rendered
    assert "Error: System config is invalid:" in rendered


def test_valid_config_with_cli_color_false_keeps_output_plain_after_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_combined_config(jelica_home)
    set_color = _invoke_cli(
        args=["config", "set", "cli.color", "false"],
        jelica_home=jelica_home,
    )
    assert set_color.exit_code == 0

    result, rendered = _invoke_cli_with_captured_terminal(
        args=["about"],
        jelica_home=jelica_home,
        monkeypatch=monkeypatch,
        force_terminal=True,
    )

    assert result.exit_code == 0
    assert ANSI_ESCAPE not in rendered
    assert "JELICA — Juxtaposing Evolutionary Lineages in Comparative Analysis" in rendered


def test_cli_emoji_setting_is_loaded_once_and_applied_to_about(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_combined_config(jelica_home)
    set_result = _invoke_cli(
        args=["config", "set", "cli.emoji", "false"],
        jelica_home=jelica_home,
    )
    assert set_result.exit_code == 0

    read_count = 0
    original_reader = cli_system_config._read_toml_document

    def counted_reader(*, config_path: Path) -> dict[str, object]:
        nonlocal read_count
        read_count += 1
        return original_reader(config_path=config_path)

    monkeypatch.setattr("jelica_cli.system_config._read_toml_document", counted_reader)

    result = _invoke_cli(args=["about"], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert read_count == 1
    assert result.stdout.startswith("JELICA —")
    assert "🌲" not in result.stdout
    assert "🎄" not in result.stdout


def test_cli_settings_are_isolated_from_task_config_hash_and_runtime_models() -> None:
    resolved_task_config = resolve_analysis_config(
        AnalysisConfigInput(samples=["neutral-source"])
    ).config
    task_document = resolved_task_config.model_dump(mode="json")
    baseline_hash = compute_config_hash(task_document)

    for cli_config in (
        CliConfig(color=True, emoji=True),
        CliConfig(color=False, emoji=True),
        CliConfig(color=True, emoji=False),
    ):
        assert compute_config_hash(task_document) == baseline_hash
        assert set(cli_config.model_dump()) == {"color", "emoji"}

    assert "cli" not in task_document
    assert "color" not in task_document
    assert "emoji" not in task_document
    assert "cli" not in CoreConfigInput.model_fields
    assert "cli" not in ResolvedCoreConfig.model_fields
    worker_launch_fields = {field.name for field in fields(WorkerLaunchSpec)}
    assert {"cli", "color", "emoji"}.isdisjoint(worker_launch_fields)
    assert {"cli", "color", "emoji"}.isdisjoint(StageArtifactManifest.model_fields)
