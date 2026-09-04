from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

import jelica_cli.main as cli_main
from jelica_cli.machine_protocol import (
    MACHINE_PROTOCOL_VERSION,
    create_machine_invocation,
    machine_error_payload,
    machine_success_payload,
    serialize_machine_event,
    serialize_machine_payload,
)
from jelica_cli.system_config import CliSystemConfigService
from jelica_contracts import Event, EventComponent, EventType, PublicError
from jelica_core.events import reset_command_id, run_initialize_analysis_task_from_inputs
from jelica_core.result_package import (
    ResolvedResultPackagePath,
    ResultPackageLibraryError,
    ResultPackageLibraryErrorCode,
)
from jelica_core.tasks import AnalyticalTaskRegistryService

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
    assert result.exit_code == 0, result.stdout


def _initialize_task(*, jelica_home: Path, sample: Path, name: str) -> str:
    result = run_initialize_analysis_task_from_inputs(
        name=name,
        config_json=None,
        raw_overrides=tuple(),
        positional_sources=(str(sample),),
        core_config_service=CliSystemConfigService(jelica_home=jelica_home).core_service,
    )
    assert result.ok
    assert result.value is not None
    return result.value.task_id


def _parse_single_response(stdout: str) -> dict[str, Any]:
    lines = stdout.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def _parse_jsonl(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


def test_machine_success_and_error_envelopes_are_exclusive() -> None:
    invocation = create_machine_invocation()
    event = Event(
        code=2210,
        name="CORE_ANALYTICAL_TASK_NOT_FOUND",
        type=EventType.ERROR,
        title="Analytical task not found",
        message="Analytical task 'missing' was not found.",
        component=EventComponent.CORE,
    )
    error = PublicError(event=event, safe_details={"task_id": "missing"})

    success = machine_success_payload(invocation=invocation, data={"value": 1})
    failure = machine_error_payload(invocation=invocation, error=error)

    assert success["machine_protocol_version"] == MACHINE_PROTOCOL_VERSION
    assert success["ok"] is True
    assert success["data"] == {"value": 1}
    assert "error" not in success
    assert "command" not in success
    UUID(str(success["command_id"]))

    assert failure["ok"] is False
    assert "data" not in failure
    assert failure["error"] == {
        "code": 2210,
        "name": "CORE_ANALYTICAL_TASK_NOT_FOUND",
        "message": "Analytical task 'missing' was not found.",
        "details": {"task_id": "missing"},
    }


def test_machine_event_serializer_produces_one_jsonl_record() -> None:
    invocation = create_machine_invocation(trace_id="00000000-0000-4000-8000-000000000001")
    event = Event(
        code=2000,
        name="CORE_TEST_EVENT",
        type=EventType.INFO,
        title="Test event",
        message="Ready",
        component=EventComponent.CORE,
        trace_id=UUID(invocation.trace_id),
        command_id=UUID(invocation.command_id),
    )

    line = serialize_machine_event(event=event)

    assert "\n" not in line
    payload = json.loads(line)
    assert payload["machine_protocol_version"] == MACHINE_PROTOCOL_VERSION
    assert payload["trace_id"] == invocation.trace_id
    assert payload["command_id"] == invocation.command_id
    assert payload["name"] == "CORE_TEST_EVENT"
    assert serialize_machine_payload(payload) == line


def test_tasks_list_machine_returns_one_success_response(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)

    result = _invoke_cli(args=["tasks", "list", "--machine"], jelica_home=jelica_home)

    assert result.exit_code == 0, result.stdout
    payload = _parse_single_response(result.stdout)
    assert payload["machine_protocol_version"] == "1"
    assert payload["ok"] is True
    assert payload["trace_id"] is None
    assert payload["data"] == {"count": 0, "limit": 50, "offset": 0, "tasks": []}
    assert "Analytical tasks were not found" not in result.stdout
    resolved = CliSystemConfigService(jelica_home=jelica_home).load_resolved_core_config()
    events = [
        json.loads(line)
        for line in (resolved.logs_dir / "system-events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    listed_event = next(
        event for event in reversed(events) if event["name"] == "CORE_ANALYTICAL_TASKS_LISTED"
    )
    assert listed_event["command_id"] == payload["command_id"]


def test_tasks_show_machine_accepts_name_and_preserves_human_mode(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    task_id = _initialize_task(jelica_home=jelica_home, sample=sample, name="Sample-Task")

    machine = _invoke_cli(
        args=["tasks", "show", "sample-task", "--machine"],
        jelica_home=jelica_home,
    )
    human = _invoke_cli(
        args=["tasks", "show", task_id],
        jelica_home=jelica_home,
    )

    assert machine.exit_code == 0, machine.stdout
    payload = _parse_single_response(machine.stdout)
    assert payload["ok"] is True
    assert payload["data"]["count"] == 1
    assert payload["data"]["tasks"][0]["task_id"] == task_id
    assert payload["data"]["tasks"][0]["name"] == "Sample-Task"
    assert payload["trace_id"] == payload["data"]["tasks"][0]["trace_id"]
    UUID(payload["trace_id"])
    assert human.exit_code == 0, human.stdout
    assert f"task_id: {task_id}" in human.stdout
    assert "machine_protocol_version" not in human.stdout


def test_tasks_show_machine_returns_domain_error_with_exit_one(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    missing_task_id = "00000000-0000-4000-8000-000000000000"

    result = _invoke_cli(
        args=["tasks", "show", missing_task_id, "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 1
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is False
    assert "data" not in payload
    assert payload["error"]["name"] == "CORE_ANALYTICAL_TASK_NOT_FOUND"
    assert payload["error"]["details"] == {"task_id": missing_task_id}


def test_results_path_machine_returns_reference_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    expected_path = tmp_path / "sample-result.jelica"
    expected_path.write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(
        cli_main,
        "resolve_result_package_path",
        lambda *, task_or_content_ref, core_config_service: ResolvedResultPackagePath(
            content_id="sha256:" + ("a" * 64),
            path=expected_path,
        ),
    )

    result = _invoke_cli(
        args=["results", "path", "task-1", "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0, result.stdout
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is True
    assert payload["data"] == {
        "content_id": "sha256:" + ("a" * 64),
        "path": str(expected_path.resolve(strict=False)),
    }


def test_results_path_machine_returns_structured_error_for_library_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)

    def _raise_resolve_error(*, task_or_content_ref: str, core_config_service: object) -> None:
        _ = (task_or_content_ref, core_config_service)
        raise ResultPackageLibraryError(
            code=ResultPackageLibraryErrorCode.TASK_NOT_FOUND,
            message="task was not found",
        )

    monkeypatch.setattr(cli_main, "resolve_result_package_path", _raise_resolve_error)

    result = _invoke_cli(
        args=["results", "path", "missing-task", "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 1
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["name"] == "CLI_RESULT_PACKAGE_RESOLUTION_FAILED"
    assert payload["error"]["details"] == {
        "reference": "missing-task",
        "result_package_error_code": "task_not_found",
    }


def test_command_id_is_stable_per_invocation_and_changes_between_invocations(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)

    first = _parse_single_response(
        _invoke_cli(args=["tasks", "list", "--machine"], jelica_home=jelica_home).stdout
    )
    second = _parse_single_response(
        _invoke_cli(args=["tasks", "list", "--machine"], jelica_home=jelica_home).stdout
    )

    assert first["command_id"] != second["command_id"]
    UUID(first["command_id"])
    UUID(second["command_id"])


def test_current_command_id_is_stable_within_one_cli_invocation() -> None:
    token = cli_main._start_cli_invocation()
    try:
        first = cli_main._current_cli_invocation()
        second = cli_main._current_cli_invocation()

        assert first.command_id == second.command_id
    finally:
        reset_command_id(token)


def test_config_show_and_validate_machine_use_envelopes(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)

    shown = _invoke_cli(args=["config", "show", "--machine"], jelica_home=jelica_home)
    validated = _invoke_cli(
        args=["config", "validate", "--machine"],
        jelica_home=jelica_home,
    )

    assert shown.exit_code == 0, shown.stdout
    shown_payload = _parse_single_response(shown.stdout)
    assert shown_payload["ok"] is True
    assert shown_payload["data"]["config"]["schema_version"] == 1
    assert validated.exit_code == 0, validated.stdout
    validated_payload = _parse_single_response(validated.stdout)
    assert validated_payload["ok"] is True
    assert validated_payload["data"]["valid"] is True


def test_config_machine_lifecycle_uses_single_responses(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"

    initialized = _invoke_cli(args=["config", "init", "--machine"], jelica_home=jelica_home)
    path_result = _invoke_cli(args=["config", "path", "--machine"], jelica_home=jelica_home)
    updated = _invoke_cli(
        args=["config", "set", "logging.level", "debug", "--machine"],
        jelica_home=jelica_home,
    )
    reset = _invoke_cli(
        args=["config", "unset", "logging.level", "--machine"],
        jelica_home=jelica_home,
    )

    for result in (initialized, path_result, updated, reset):
        assert result.exit_code == 0, result.stdout
        assert _parse_single_response(result.stdout)["ok"] is True


def test_analyze_plan_machine_is_dry_run_and_accepts_trace_id(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    trace_id = "00000000-0000-4000-8000-000000000001"

    result = _invoke_cli(
        args=["analyze", "--plan", "--trace-id", trace_id, "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0, result.stdout
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is True
    assert payload["trace_id"] == trace_id
    assert payload["data"]["plan"]["sources"] == ["."]
    resolved = CliSystemConfigService(jelica_home=jelica_home).load_resolved_core_config()
    assert list(resolved.tasks_dir.iterdir()) == []


def test_invalid_trace_id_machine_is_usage_error_envelope(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)

    result = _invoke_cli(
        args=["analyze", "--plan", "--trace-id", "invalid", "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 2
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["name"] == "CLI_ANALYZE_ARGUMENT_INVALID"


def test_analyze_machine_preserves_trace_and_returns_one_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    trace_id = "00000000-0000-4000-8000-000000000002"

    monkeypatch.setattr(cli_main, "_ensure_execution_service", lambda **_kwargs: None)

    def fake_watch_execution_task(*, task_id: str, **_kwargs: Any) -> Any:
        return cli_main.WatchCliOutcome(
            rows=(
                cli_main.WatchTaskRow(
                    task_id=task_id,
                    job_id="job",
                    state="completed",
                    stage="completed",
                    progress=100,
                    warning_count=0,
                ),
            ),
            missing_task_ids=tuple(),
            inactive_tasks=tuple(),
            events=tuple(),
            interrupted=False,
        )

    monkeypatch.setattr(cli_main, "_watch_execution_task", fake_watch_execution_task)

    result = _invoke_cli(
        args=["analyze", str(sample), "--trace-id", trace_id, "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0, result.stdout
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is True
    assert payload["trace_id"] == trace_id
    assert payload["data"]["task"]["config"]["trace_id"] == trace_id
    assert payload["data"]["final_state"] == "completed"


def test_analyze_no_watch_machine_returns_started_state_without_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")

    monkeypatch.setattr(cli_main, "_ensure_execution_service", lambda **_kwargs: None)

    def _watch_should_not_run(**_kwargs: Any) -> Any:
        raise AssertionError("_watch_execution_task must not be called with --no-watch")

    monkeypatch.setattr(cli_main, "_watch_execution_task", _watch_should_not_run)

    result = _invoke_cli(
        args=["analyze", str(sample), "--no-watch", "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0, result.stdout
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["task"]["task_id"] != ""
    assert payload["data"]["final_state"] != ""


def test_analyze_machine_interruption_returns_one_error_response_and_exit_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=tuple(),
        positional_sources=(str(sample),),
        core_config_service=CliSystemConfigService(jelica_home=jelica_home).core_service,
    )
    assert initialized.ok
    assert initialized.value is not None

    monkeypatch.setattr(cli_main, "_ensure_execution_service", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "run_create_analytical_task_from_inputs",
        lambda **_kwargs: initialized,
    )
    monkeypatch.setattr(
        cli_main,
        "run_start_analytical_task",
        lambda **_kwargs: initialized,
    )
    monkeypatch.setattr(
        cli_main,
        "_watch_execution_task",
        lambda **_kwargs: cli_main.WatchCliOutcome(
            rows=tuple(),
            missing_task_ids=tuple(),
            inactive_tasks=tuple(),
            events=tuple(),
            interrupted=True,
        ),
    )

    result = _invoke_cli(
        args=["analyze", str(sample), "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 130
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is False
    assert payload["trace_id"] == str(initialized.value.config.trace_id)
    assert payload["error"]["name"] == "CLI_COMMAND_INTERRUPTED"
    assert payload["error"]["details"] == {"task_ids": [initialized.value.task_id]}

    monkeypatch.setattr(
        cli_main,
        "run_list_analytical_tasks",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    early_interrupt = _invoke_cli(
        args=["tasks", "list", "--machine"],
        jelica_home=jelica_home,
    )

    assert early_interrupt.exit_code == 130
    early_payload = _parse_single_response(early_interrupt.stdout)
    assert early_payload["ok"] is False
    assert early_payload["trace_id"] is None
    assert early_payload["error"]["name"] == "CLI_COMMAND_INTERRUPTED"


def test_task_control_machine_accepts_name_without_human_output(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    task_id = _initialize_task(jelica_home=jelica_home, sample=sample, name="Cancel-Me")
    resolved = CliSystemConfigService(jelica_home=jelica_home).load_resolved_core_config()
    AnalyticalTaskRegistryService(database_path=resolved.database_path).start(task_id=task_id)

    result = _invoke_cli(
        args=["tasks", "cancel", "cancel-me", "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0, result.stdout
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["operation"] == "cancel"
    assert payload["data"]["count"] == 1
    assert payload["data"]["tasks"][0]["task"]["task_id"] == task_id


def test_removed_machine_flags_are_usage_errors(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)

    json_flag = _invoke_cli(args=["tasks", "list", "--json"], jelica_home=jelica_home)
    output_flag = _invoke_cli(
        args=["tasks", "list", "--output=json"],
        jelica_home=jelica_home,
    )

    assert json_flag.exit_code == 2
    assert output_flag.exit_code == 2
    assert "Usage:" in json_flag.output
    assert "machine_protocol_version" not in json_flag.output


def test_missing_task_reference_machine_is_one_usage_error_envelope(tmp_path: Path) -> None:
    result = _invoke_cli(
        args=["tasks", "show", "--machine"],
        jelica_home=tmp_path / "home",
    )

    assert result.exit_code == 2
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["name"] == "CLI_USAGE_ERROR"
    assert "Missing argument" in payload["error"]["message"]
    assert "Usage:" not in result.stdout


def test_invalid_typed_machine_option_is_one_usage_error_envelope(tmp_path: Path) -> None:
    result = _invoke_cli(
        args=["tasks", "list", "--limit", "not-an-integer", "--machine"],
        jelica_home=tmp_path / "home",
    )

    assert result.exit_code == 2
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["name"] == "CLI_USAGE_ERROR"
    assert "--limit" in payload["error"]["message"]
    assert "Usage:" not in result.stdout


def test_machine_usage_errors_get_fresh_command_ids_before_root_callback(
    tmp_path: Path,
) -> None:
    first = _invoke_cli(args=["--machine"], jelica_home=tmp_path / "home")
    second = _invoke_cli(args=["--machine"], jelica_home=tmp_path / "home")

    assert first.exit_code == 2
    assert second.exit_code == 2
    first_payload = _parse_single_response(first.stdout)
    second_payload = _parse_single_response(second.stdout)
    UUID(first_payload["command_id"])
    UUID(second_payload["command_id"])
    assert first_payload["command_id"] != second_payload["command_id"]
    assert first_payload["trace_id"] is None
    assert second_payload["trace_id"] is None


def test_removed_flag_with_machine_is_one_usage_error_envelope(tmp_path: Path) -> None:
    result = _invoke_cli(
        args=["tasks", "list", "--json", "--machine"],
        jelica_home=tmp_path / "home",
    )

    assert result.exit_code == 2
    payload = _parse_single_response(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["name"] == "CLI_USAGE_ERROR"


def test_machine_flag_is_exposed_on_response_and_stream_commands(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    command_paths = (
        ["analyze"],
        ["config", "init"],
        ["config", "path"],
        ["config", "show"],
        ["config", "validate"],
        ["config", "set"],
        ["config", "unset"],
        ["tasks", "list"],
        ["tasks", "show"],
        ["tasks", "jobs"],
        ["tasks", "start"],
        ["tasks", "delete"],
        ["tasks", "update"],
        ["tasks", "reprioritize"],
        ["tasks", "pause"],
        ["tasks", "stop"],
        ["tasks", "resume"],
        ["tasks", "cancel"],
        ["tasks", "watch"],
        ["events", "watch"],
    )

    for command_path in command_paths:
        result = _invoke_cli(args=[*command_path, "--help"], jelica_home=jelica_home)
        assert result.exit_code == 0, result.stdout
        assert "--machine" in result.stdout



@pytest.mark.parametrize("use_name", (False, True))
def test_tasks_watch_machine_streams_task_updates_and_events_for_uuid_or_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_name: bool,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    task_id = _initialize_task(
        jelica_home=jelica_home,
        sample=sample,
        name="Watch-Target",
    )
    resolved = CliSystemConfigService(jelica_home=jelica_home).load_resolved_core_config()
    trace_id = AnalyticalTaskRegistryService(
        database_path=resolved.database_path
    ).get_task_trace_id(task_id=task_id)
    assert trace_id is not None
    event = Event(
        event_id=UUID("00000000-0000-4000-8000-000000000011"),
        code=2000,
        name="CORE_TEST_WATCH_EVENT",
        type=EventType.INFO,
        title="Watch event",
        message="Task changed.",
        component=EventComponent.CORE,
        task_id=task_id,
        trace_id=trace_id,
        command_id=UUID("00000000-0000-4000-8000-000000000012"),
    )

    def fake_run_watch_session(**kwargs: Any) -> Any:
        assert kwargs["task_ids"] == (task_id,)
        assert kwargs["render"] is False
        row = cli_main.WatchTaskRow(
            task_id=task_id,
            job_id="job-1",
            state="completed",
            stage="result_package",
            progress=100,
            warning_count=0,
            task_name="Watch-Target",
            trace_id=trace_id,
        )
        kwargs["row_callback"](row)
        kwargs["event_callback"](event)
        return cli_main.WatchCliOutcome(
            rows=(row,),
            missing_task_ids=tuple(),
            inactive_tasks=tuple(),
            events=(event,),
            interrupted=False,
        )

    monkeypatch.setattr(cli_main, "_run_watch_session", fake_run_watch_session)
    reference = "watch-target" if use_name else task_id

    result = _invoke_cli(
        args=["tasks", "watch", reference, "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0, result.stdout
    payloads = _parse_jsonl(result.stdout)
    assert len(payloads) == 2
    assert payloads[0]["machine_protocol_version"] == "1"
    assert payloads[0]["type"] == "task.update"
    assert payloads[0]["trace_id"] == str(trace_id)
    assert payloads[0]["data"] == {
        "job_id": "job-1",
        "progress": 100,
        "stage": "result_package",
        "status": "completed",
        "task_id": task_id,
        "task_name": "Watch-Target",
        "warning_count": 0,
    }
    assert payloads[1]["event_id"] == str(event.event_id)
    assert payloads[1]["trace_id"] == str(trace_id)
    assert payloads[1]["command_id"] == str(event.command_id)
    assert "ok" not in payloads[0]
    assert "ok" not in payloads[1]


def test_tasks_watch_machine_without_refs_ctrl_c_emits_interrupt_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    task_id = _initialize_task(
        jelica_home=jelica_home,
        sample=sample,
        name="Waiting-Watch",
    )
    resolved = CliSystemConfigService(jelica_home=jelica_home).load_resolved_core_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    trace_id = registry.get_task_trace_id(task_id=task_id)
    assert trace_id is not None

    def interrupted_watch(**kwargs: Any) -> Any:
        assert kwargs["task_ids"] == tuple()
        row = cli_main.WatchTaskRow(
            task_id=task_id,
            job_id=None,
            state="waiting",
            stage=None,
            progress=0,
            warning_count=0,
            task_name="Waiting-Watch",
            trace_id=trace_id,
        )
        kwargs["row_callback"](row)
        return cli_main.WatchCliOutcome(
            rows=(row,),
            missing_task_ids=tuple(),
            inactive_tasks=tuple(),
            events=tuple(),
            interrupted=True,
        )

    monkeypatch.setattr(cli_main, "_run_watch_session", interrupted_watch)

    result = _invoke_cli(
        args=["tasks", "watch", "--machine"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 130
    payloads = _parse_jsonl(result.stdout)
    assert len(payloads) == 2
    assert payloads[0]["type"] == "task.update"
    assert payloads[0]["data"]["task_id"] == task_id
    assert payloads[0]["data"]["status"] == "waiting"
    assert payloads[1]["ok"] is False
    assert payloads[1]["error"]["name"] == "CLI_COMMAND_INTERRUPTED"
    assert payloads[1]["error"]["details"] == {"task_ids": [task_id]}
    assert registry.get_task(task_id=task_id).state.value == "waiting"


def test_events_watch_machine_filters_by_name_resumes_after_cursor_and_interrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    task_id = _initialize_task(
        jelica_home=jelica_home,
        sample=sample,
        name="Event-Target",
    )
    resolved = CliSystemConfigService(jelica_home=jelica_home).load_resolved_core_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    trace_id = registry.get_task_trace_id(task_id=task_id)
    assert trace_id is not None
    cursor = Event(
        event_id=UUID("00000000-0000-4000-8000-000000000021"),
        code=2000,
        name="CORE_TEST_CURSOR",
        type=EventType.INFO,
        title="Cursor",
        message="Cursor.",
        component=EventComponent.CORE,
    )
    filtered = Event(
        event_id=UUID("00000000-0000-4000-8000-000000000022"),
        code=2000,
        name="CORE_TEST_OTHER_TASK",
        type=EventType.INFO,
        title="Other",
        message="Other task.",
        component=EventComponent.CORE,
        task_id="other-task",
    )
    expected = Event(
        event_id=UUID("00000000-0000-4000-8000-000000000023"),
        code=2000,
        name="CORE_TEST_TARGET_TASK",
        type=EventType.INFO,
        title="Target",
        message="Target task changed.",
        component=EventComponent.CORE,
        task_id=task_id,
        trace_id=trace_id,
        command_id=UUID("00000000-0000-4000-8000-000000000024"),
    )
    log_path = resolved.logs_dir / "system-events.jsonl"
    with log_path.open("a", encoding="utf-8") as stream:
        for event in (cursor, filtered, expected):
            stream.write(event.model_dump_json(exclude_none=True))
            stream.write("\n")

    def interrupt_watch(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main.EventWatchService, "watch", interrupt_watch)

    result = _invoke_cli(
        args=[
            "events",
            "watch",
            "--task",
            "event-target",
            "--after",
            str(cursor.event_id),
            "--machine",
        ],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 130
    payloads = _parse_jsonl(result.stdout)
    assert len(payloads) == 2
    assert payloads[0]["machine_protocol_version"] == "1"
    assert payloads[0]["event_id"] == str(expected.event_id)
    assert payloads[0]["task_id"] == task_id
    assert payloads[0]["trace_id"] == str(trace_id)
    assert payloads[0]["command_id"] == str(expected.command_id)
    assert payloads[1]["ok"] is False
    assert payloads[1]["error"]["name"] == "CLI_COMMAND_INTERRUPTED"
    assert payloads[1]["error"]["details"] == {"task_ids": [task_id]}
    assert registry.get_task(task_id=task_id).state.value == "waiting"


def test_events_watch_human_outputs_all_selected_events_and_ctrl_c(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)
    event = Event(
        code=2000,
        name="CORE_TEST_HUMAN_EVENT",
        type=EventType.DEBUG,
        title="Human event",
        message="Visible human event.",
        component=EventComponent.CORE,
    )

    def emit_then_interrupt(
        _service: Any,
        callback: Any,
        **_kwargs: Any,
    ) -> None:
        callback((event,))
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main.EventWatchService, "watch", emit_then_interrupt)

    result = _invoke_cli(args=["events", "watch"], jelica_home=jelica_home)

    assert result.exit_code == 130
    assert "CORE_TEST_HUMAN_EVENT" in result.stdout
    assert "Visible human event." in result.stdout
    assert "machine_protocol_version" not in result.stdout


def test_events_watch_machine_reports_cursor_and_usage_errors(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _init_config(jelica_home)

    missing = _invoke_cli(
        args=[
            "events",
            "watch",
            "--after",
            "00000000-0000-4000-8000-000000000099",
            "--machine",
        ],
        jelica_home=jelica_home,
    )
    invalid = _invoke_cli(
        args=["events", "watch", "--after", "invalid", "--machine"],
        jelica_home=jelica_home,
    )

    assert missing.exit_code == 1
    missing_payload = _parse_single_response(missing.stdout)
    assert missing_payload["error"]["name"] == "CLI_EVENT_CURSOR_NOT_FOUND"
    assert invalid.exit_code == 2
    invalid_payload = _parse_single_response(invalid.stdout)
    assert invalid_payload["error"]["name"] == "CLI_USAGE_ERROR"
