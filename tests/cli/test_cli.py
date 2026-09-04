from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import tomli_w
from typer.testing import CliRunner

import jelica_cli.main as cli_main
import jelica_cli.results_export as cli_results_export
import jelica_cli.system_config as cli_system_config
import jelica_cli.terminal as cli_terminal
import jelica_core.events.operations as core_operations
from jelica_cli.results_export import (
    ReportExportError,
    ReportExportErrorCode,
    ReportOpenResult,
    ReportOpenWarningCode,
)
from jelica_cli.system_config import CliSystemConfigService
from jelica_contracts import Event
from jelica_core import get_core_info
from jelica_core.events import run_initialize_analysis_task_from_inputs
from jelica_core.result_package import (
    JELICA_PACKAGE_CONFIGURATION_PATH,
    JELICA_PACKAGE_FORMAT,
    JELICA_PACKAGE_FORMAT_VERSION,
    JELICA_PACKAGE_INPUT_MANIFEST_PATH,
    JELICA_PACKAGE_MANIFEST_PATH,
    JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
    JELICA_PACKAGE_NOTES_PATH,
    JELICA_PACKAGE_TASK_PATH,
    RESULT_PACKAGE_DIRECTORY_NAME,
    JelicaPackageManifest,
    ResultPackageArtifactInfo,
    ResultPackageLink,
    ResultPackageProducerInfo,
    ResultPackageStageInfo,
    ResultPackageTaskInfo,
    ResultPackageTaskStatus,
    compute_content_id,
    content_digest_from_content_id,
    infer_media_type,
    relative_package_path_from_task,
    serialize_stable_json,
    write_result_package_link,
)
from jelica_core.runtime import (
    ServiceLogs,
    ServiceRestartResult,
    ServiceStartResult,
    ServiceState,
    ServiceStatus,
    ServiceStopResult,
)
from jelica_core.runtime import (
    stop_service as stop_core_service,
)
from jelica_core.system_config import CoreConfigService, ResolvedCoreConfig, core_config_field_paths
from jelica_core.tasks import AnalyticalTaskRegistryService, AnalyticalTaskSnapshot

runner = CliRunner()
_INVOKED_JELICA_HOMES: set[Path] = set()


@pytest.fixture(autouse=True)
def _stop_persistent_test_services() -> Any:
    homes_before_test = set(_INVOKED_JELICA_HOMES)
    yield
    for jelica_home in _INVOKED_JELICA_HOMES - homes_before_test:
        config_service = CoreConfigService(jelica_home=jelica_home)
        if not config_service.get_config_path().is_file():
            continue
        try:
            stop_core_service(
                force=True,
                core_config_service=config_service,
                timeout_seconds=5.0,
            )
        except Exception:
            # Cleanup must not obscure the test assertion that already ran.
            continue


@pytest.fixture(autouse=True)
def _stable_available_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jelica_core.system_config.resolver.detect_available_logical_cpu_count",
        lambda: 8,
    )


def _invoke_cli(
    *,
    args: list[str],
    jelica_home: Path,
    input_text: str | None = None,
    color: bool = False,
) -> Any:
    _INVOKED_JELICA_HOMES.add(jelica_home)
    env = dict(os.environ)
    env["JELICA_HOME"] = str(jelica_home)
    return runner.invoke(cli_main.app, args, env=env, input=input_text, color=color)


def _run_non_interactive_init(jelica_home: Path, *extra_args: str) -> Any:
    return _invoke_cli(
        args=["config", "init", "--non-interactive", *extra_args],
        jelica_home=jelica_home,
    )


def _load_config_document(jelica_home: Path) -> dict[str, Any]:
    config_path = CoreConfigService(jelica_home=jelica_home).get_config_path()
    with config_path.open("rb") as file:
        return tomllib.load(file)


def _load_resolved_core_config(jelica_home: Path) -> ResolvedCoreConfig:
    return CliSystemConfigService(jelica_home=jelica_home).load_resolved_core_config()


def _write_config_document(jelica_home: Path, document: dict[str, Any]) -> Path:
    config_path = CoreConfigService(jelica_home=jelica_home).get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(tomli_w.dumps(document), encoding="utf-8")
    return config_path


def _delete_document_path(document: dict[str, Any], *, field_path: str) -> None:
    keys = field_path.split(".")
    current: dict[str, Any] = document
    for key in keys[:-1]:
        nested = current[key]
        assert isinstance(nested, dict)
        current = nested
    del current[keys[-1]]


def _document_leaf_paths(document: dict[str, Any], *, prefix: str = "") -> set[str]:
    result: set[str] = set()
    for key, value in document.items():
        field_path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(_document_leaf_paths(value, prefix=field_path))
            continue
        result.add(field_path)
    return result


def _assert_complete_combined_config_document(document: dict[str, Any]) -> None:
    assert document["cli"] == {"color": True, "emoji": True}
    core_document = {key: value for key, value in document.items() if key != "cli"}
    assert _document_leaf_paths(core_document) == set(core_config_field_paths())


def _extract_started_task_id(stdout: str) -> str:
    match = re.search(r"Analysis task ([a-f0-9-]{36}) was created and started\.", stdout)
    assert match is not None
    return match.group(1)


def _single_task_config_path(jelica_home: Path) -> Path:
    resolved = _load_resolved_core_config(jelica_home)
    task_dirs = sorted(path for path in resolved.tasks_dir.iterdir() if path.is_dir())
    assert len(task_dirs) == 1
    return task_dirs[0] / "config.json"


def _single_task_id(jelica_home: Path) -> str:
    resolved = _load_resolved_core_config(jelica_home)
    task_dirs = sorted(path for path in resolved.tasks_dir.iterdir() if path.is_dir())
    assert len(task_dirs) == 1
    return task_dirs[0].name


def _registry_service(jelica_home: Path) -> AnalyticalTaskRegistryService:
    resolved = _load_resolved_core_config(jelica_home)
    return AnalyticalTaskRegistryService(database_path=resolved.database_path)


def _task_name(*, jelica_home: Path, task_id: str) -> str:
    name = _registry_service(jelica_home).get_task(task_id=task_id).name
    assert name is not None
    return name


def _wait_for_task_state(
    *,
    registry: AnalyticalTaskRegistryService,
    task_id: str,
    expected_state: str,
    timeout_seconds: float = 10.0,
) -> AnalyticalTaskSnapshot:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        snapshot = registry.get_task_snapshot(task_id=task_id)
        if snapshot.task.state.value == expected_state:
            return snapshot
        time.sleep(0.05)
    snapshot = registry.get_task_snapshot(task_id=task_id)
    pytest.fail(
        f"Task {task_id} did not reach {expected_state}; current state is "
        f"{snapshot.task.state.value}."
    )


def _service_status(
    tmp_path: Path,
    *,
    running: bool = True,
    running_task_ids: tuple[str, ...] = tuple(),
    queued_task_ids: tuple[str, ...] = tuple(),
    version_compatible: bool | None = True,
) -> ServiceStatus:
    now = datetime.now(UTC)
    return ServiceStatus(
        running=running,
        service_id="service-1" if running else None,
        pid=12345 if running else None,
        jelica_version=get_core_info()["version"] if running else None,
        cli_jelica_version=get_core_info()["version"],
        version_compatible=version_compatible if running else None,
        started_at=now if running else None,
        last_heartbeat=now if running else None,
        state=ServiceState.RUNNING if running else ServiceState.STOPPED,
        configured_workers=1,
        active_workers=len(running_task_ids),
        queued_tasks=len(queued_task_ids),
        running_tasks=len(running_task_ids),
        queued_task_ids=queued_task_ids,
        running_task_ids=running_task_ids,
        active_worker_task_ids=running_task_ids,
        log_path=tmp_path / "system-events.jsonl",
    )


def _install_inline_runtime_service(
    *,
    monkeypatch: pytest.MonkeyPatch,
    expected_jelica_home: Path,
    tmp_path: Path,
) -> list[threading.Thread]:
    runtime_threads: list[threading.Thread] = []
    status = _service_status(tmp_path)

    def _fake_start_service(
        *,
        core_config_service: CoreConfigService,
        runner_module: str,
    ) -> ServiceStartResult:
        _ = core_config_service
        assert runner_module == "jelica_cli.service_runner"
        return ServiceStartResult(status=status, already_running=True)

    def _launch_inline_runtime(
        *,
        jelica_home: Path | None = None,
        runner_module: str,
    ) -> int:
        assert runner_module == "jelica_cli.service_runner"
        assert jelica_home is not None
        assert jelica_home == expected_jelica_home
        runtime_thread = threading.Thread(
            target=core_operations.run_runtime_continue,
            kwargs={
                "core_config_service": CliSystemConfigService(jelica_home=jelica_home).core_service,
            },
            name="inline-test-service-runtime",
        )
        runtime_threads.append(runtime_thread)
        runtime_thread.start()
        return os.getpid()

    monkeypatch.setattr(cli_main, "start_service", _fake_start_service)
    monkeypatch.setattr(core_operations, "launch_background_runtime", _launch_inline_runtime)
    return runtime_threads


def _initialize_task_without_start(
    *,
    jelica_home: Path,
    sample_paths: list[Path],
    raw_overrides: tuple[str, ...] = (),
) -> str:
    service = CliSystemConfigService(jelica_home=jelica_home).core_service
    result = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=raw_overrides,
        positional_sources=tuple(str(path) for path in sample_paths),
        core_config_service=service,
    )
    assert result.ok is True
    assert result.value is not None
    return result.value.task_id


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _build_validation_package(
    package_path: Path,
    *,
    broken_manifest: bool = False,
    notes: bytes | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> str:
    payloads = {
        JELICA_PACKAGE_TASK_PATH: (
            b'{"task_id":"task-1","status":"completed","created_at":"2026-08-01T00:00:00Z",'
            b'"completed_at":"2026-08-01T00:00:01Z"}\n'
        ),
        JELICA_PACKAGE_CONFIGURATION_PATH: b'{"alignment":{"mode":"none"}}\n',
        JELICA_PACKAGE_INPUT_MANIFEST_PATH: b'{"sources":[]}\n',
        JELICA_PACKAGE_NORMALIZED_FASTA_PATH: b">sample\nACGT\n",
        "results/comparative_analysis/manifest.json": b'{"ok":true}\n',
    }
    artifacts = tuple(
        ResultPackageArtifactInfo(
            path=path,
            stage=(
                "input_acquisition"
                if path == JELICA_PACKAGE_INPUT_MANIFEST_PATH
                else (
                    "input_processing"
                    if path == JELICA_PACKAGE_NORMALIZED_FASTA_PATH
                    else "comparative_analysis"
                    if path.startswith("results/")
                    else None
                )
            ),
            media_type=infer_media_type(path),
            size=len(payload),
            sha256=_sha256(payload),
        )
        for path, payload in sorted(payloads.items())
    )
    manifest = JelicaPackageManifest(
        format=JELICA_PACKAGE_FORMAT,
        format_version=JELICA_PACKAGE_FORMAT_VERSION,
        content_id=compute_content_id(artifacts=artifacts),
        producer=ResultPackageProducerInfo(version="1.0.0-test"),
        package_created_at="2026-08-01T00:00:02Z",
        task=ResultPackageTaskInfo(
            task_id="task-1",
            status=ResultPackageTaskStatus.COMPLETED,
            created_at="2026-08-01T00:00:00Z",
            completed_at="2026-08-01T00:00:01Z",
        ),
        stages=(
            ResultPackageStageInfo(
                name="input_acquisition",
                status="completed",
                artifacts=(JELICA_PACKAGE_INPUT_MANIFEST_PATH,),
            ),
            ResultPackageStageInfo(
                name="input_processing",
                status="completed",
                artifacts=(JELICA_PACKAGE_NORMALIZED_FASTA_PATH,),
            ),
            ResultPackageStageInfo(
                name="comparative_analysis",
                status="completed",
                artifacts=("results/comparative_analysis/manifest.json",),
            ),
        ),
        artifacts=artifacts,
    )
    manifest_payload = manifest.model_dump(mode="json")
    if broken_manifest:
        manifest_payload["content_id"] = "sha256:" + ("0" * 64)

    with zipfile.ZipFile(package_path, mode="w", compression=compression) as archive:
        for entry_path, payload in sorted(payloads.items()):
            archive.writestr(entry_path, payload)
        if notes is not None:
            archive.writestr(JELICA_PACKAGE_NOTES_PATH, notes)
        archive.writestr(
            JELICA_PACKAGE_MANIFEST_PATH,
            serialize_stable_json(manifest_payload).encode("utf-8"),
        )
    return manifest.content_id


def _result_packages_dir(jelica_home: Path) -> Path:
    return jelica_home / RESULT_PACKAGE_DIRECTORY_NAME


def _register_task_with_result_package_link(
    *,
    jelica_home: Path,
    task_id: str,
    content_id: str,
    name: str | None = None,
) -> Path:
    resolved = _load_resolved_core_config(jelica_home)
    task_dir = resolved.tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    registry = _registry_service(jelica_home)
    registry.register_task(
        task_id=task_id,
        name=name,
        task_dir_relative_path=task_id,
        current_config_relative_path="configs/000001.json",
        current_config_hash="a" * 64,
    )
    package_path = _result_packages_dir(jelica_home) / (
        f"{content_digest_from_content_id(content_id)}.jelica"
    )
    link = ResultPackageLink(
        content_id=content_id,
        path=relative_package_path_from_task(task_dir=task_dir, package_path=package_path),
        format_version="1.0",
    )
    write_result_package_link(task_dir=task_dir, link=link)
    return task_dir


def _extract_export_path(stdout: str) -> Path:
    for line in stdout.splitlines():
        if line.startswith("Path: "):
            return Path(line[len("Path: ") :].strip())
    raise AssertionError("Path line is missing in export output")


def _prepare_imported_package_for_export(tmp_path: Path) -> tuple[Path, Path, str]:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    package_path = tmp_path / "package.jelica"
    content_id = _build_validation_package(package_path)
    imported = _invoke_cli(
        args=["results", "import", str(package_path)],
        jelica_home=jelica_home,
    )
    assert imported.exit_code == 0
    return jelica_home, package_path, content_id


def test_cli_help_exits_successfully_without_initialized_core(tmp_path: Path) -> None:
    result = _invoke_cli(args=["--help"], jelica_home=tmp_path / "home")

    assert result.exit_code == 0
    assert "JELICA command-line interface." in result.stdout
    assert "analyze" in result.stdout
    assert "config" in result.stdout
    assert "service" in result.stdout


def test_runtime_continue_is_not_public_cli_command(tmp_path: Path) -> None:
    result = _invoke_cli(
        args=["runtime", "continue"],
        jelica_home=tmp_path / "home",
    )

    assert result.exit_code != 0


def test_service_start_is_idempotent_and_status_is_rendered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _service_status(tmp_path)
    results = iter(
        (
            ServiceStartResult(status=status, already_running=False, launched_pid=12345),
            ServiceStartResult(status=status, already_running=True),
        )
    )

    def _fake_start_service(
        *,
        core_config_service: CoreConfigService,
        runner_module: str,
    ) -> ServiceStartResult:
        _ = core_config_service
        assert runner_module == "jelica_cli.service_runner"
        return next(results)

    monkeypatch.setattr(cli_main, "start_service", _fake_start_service)

    started = _invoke_cli(args=["service", "start"], jelica_home=tmp_path / "home")
    already_running = _invoke_cli(
        args=["service", "start"],
        jelica_home=tmp_path / "home",
    )

    assert started.exit_code == 0
    assert "JELICA Service started." in started.stdout
    assert "status: running" in started.stdout
    assert "service_id: service-1" in started.stdout
    assert already_running.exit_code == 0
    assert "already running" in already_running.stdout


def test_service_stop_refuses_running_tasks_and_force_is_forwarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def _fake_stop_service(
        *,
        force: bool,
        core_config_service: CoreConfigService,
    ) -> ServiceStopResult:
        _ = core_config_service
        calls.append(force)
        if not force:
            raise cli_main.ServiceRunningTasksError(task_ids=("task-a", "task-b"))
        return ServiceStopResult(
            status=_service_status(tmp_path, running=False),
            already_stopped=False,
            interrupted_task_ids=("task-a", "task-b"),
        )

    monkeypatch.setattr(cli_main, "stop_service", _fake_stop_service)

    refused = _invoke_cli(args=["service", "stop"], jelica_home=tmp_path / "home")
    forced = _invoke_cli(
        args=["service", "stop", "--force"],
        jelica_home=tmp_path / "home",
    )

    assert refused.exit_code != 0
    assert "2 running task(s): task-a, task-b" in refused.stdout
    assert forced.exit_code == 0
    assert "JELICA Service stopped." in forced.stdout
    assert "Interrupted tasks: task-a, task-b" in forced.stdout
    assert calls == [False, True]


def test_service_restart_status_detailed_and_logs_options_are_forwarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _service_status(
        tmp_path,
        running_task_ids=("task-running",),
        queued_task_ids=("task-queued",),
    )
    restart_forces: list[bool] = []
    log_tails: list[int] = []

    def _fake_restart_service(
        *,
        force: bool,
        core_config_service: CoreConfigService,
        runner_module: str,
    ) -> ServiceRestartResult:
        _ = core_config_service
        assert runner_module == "jelica_cli.service_runner"
        restart_forces.append(force)
        return ServiceRestartResult(
            status=status,
            interrupted_task_ids=("task-running",),
            resumed_task_ids=("task-running",),
        )

    def _fake_status(*, core_config_service: CoreConfigService) -> ServiceStatus:
        _ = core_config_service
        return status

    def _fake_logs(
        *,
        tail: int,
        core_config_service: CoreConfigService,
    ) -> ServiceLogs:
        _ = core_config_service
        log_tails.append(tail)
        return ServiceLogs(path=status.log_path, lines=("first", "second"))

    monkeypatch.setattr(cli_main, "restart_service", _fake_restart_service)
    monkeypatch.setattr(cli_main, "get_service_status", _fake_status)
    monkeypatch.setattr(cli_main, "read_service_logs", _fake_logs)

    restarted = _invoke_cli(
        args=["service", "restart", "--force"],
        jelica_home=tmp_path / "home",
    )
    detailed = _invoke_cli(
        args=["service", "status", "--detailed"],
        jelica_home=tmp_path / "home",
    )
    logs = _invoke_cli(
        args=["service", "logs", "--tail", "2"],
        jelica_home=tmp_path / "home",
    )

    assert restarted.exit_code == 0
    assert "Returned to execution: task-running" in restarted.stdout
    assert restart_forces == [True]
    assert detailed.exit_code == 0
    assert "queued_task_ids: task-queued" in detailed.stdout
    assert "running_task_ids: task-running" in detailed.stdout
    assert "active_worker_task_ids: task-running" in detailed.stdout
    assert logs.exit_code == 0
    assert "first\nsecond" in logs.stdout
    assert log_tails == [2]


def test_cli_version_exits_successfully_without_initialized_core(tmp_path: Path) -> None:
    result = _invoke_cli(args=["--version"], jelica_home=tmp_path / "home")

    assert result.exit_code == 0
    assert result.stdout.strip() == get_core_info()["version"]


def test_cli_all_versions_exits_successfully_without_initialized_core(tmp_path: Path) -> None:
    result = _invoke_cli(args=["--all-versions"], jelica_home=tmp_path / "home")

    assert result.exit_code == 0
    assert "jelica-cli" in result.stdout
    assert get_core_info()["package"] in result.stdout


def test_python_module_entrypoint_matches_cli_app() -> None:
    direct_invocation = runner.invoke(cli_main.app, ["--version"])
    module_invocation = subprocess.run(
        [sys.executable, "-m", "jelica_cli", "--version"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert direct_invocation.exit_code == 0
    assert module_invocation.returncode == 0
    assert module_invocation.stdout == direct_invocation.stdout


def test_config_path_is_available_before_initialization(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    result = _invoke_cli(args=["config", "path"], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert result.stdout.strip() == str(jelica_home / "config.toml")


def test_config_path_is_independent_of_current_directory(tmp_path: Path, monkeypatch: Any) -> None:
    jelica_home = tmp_path / "home"
    different_cwd = tmp_path / "other-cwd"
    different_cwd.mkdir(parents=True)
    monkeypatch.chdir(different_cwd)

    result = _invoke_cli(args=["config", "path"], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert result.stdout.strip() == str(jelica_home / "config.toml")


def test_analyze_requires_initialized_core(tmp_path: Path) -> None:
    result = _invoke_cli(
        args=["analyze", "Sample_1.fasta", "Sample_2.fasta"],
        jelica_home=tmp_path / "home",
    )

    assert result.exit_code != 0
    assert "not initialized" in result.stdout


def test_analyze_attached_watch_uses_composed_config_and_receives_created_task_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">watch\nACGT\n", encoding="utf-8")

    observed_task_ids: list[tuple[str, ...]] = []
    observed_core_services: list[CoreConfigService | None] = []
    original_watch_init = cli_main.TaskWatchService.__init__

    def wrapped_watch_init(self: Any, *args: Any, **kwargs: Any) -> None:
        observed_core_services.append(kwargs.get("core_config_service"))
        original_watch_init(self, *args, **kwargs)

    def fake_run_watch_session(
        *,
        service: Any,
        task_ids: tuple[str, ...],
        mode: Any,
        render: bool,
        include_explicit_inactive: bool = False,
        stop_condition: Any | None = None,
        wait_for_initial_rows: bool = False,
    ) -> Any:
        _ = (
            service,
            mode,
            render,
            include_explicit_inactive,
            stop_condition,
            wait_for_initial_rows,
        )
        observed_task_ids.append(task_ids)
        return cli_main.WatchCliOutcome(
            rows=(
                cli_main.WatchTaskRow(
                    task_id=task_ids[0],
                    job_id="job-1",
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

    monkeypatch.setattr(cli_main.TaskWatchService, "__init__", wrapped_watch_init)
    monkeypatch.setattr(cli_main, "_run_watch_session", fake_run_watch_session)

    result = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)

    assert result.exit_code == 0
    started_task_id = _extract_started_task_id(result.stdout)
    assert observed_task_ids == [(started_task_id,)]
    assert len(observed_core_services) == 1
    assert observed_core_services[0] is not None
    assert "unknown field 'cli'" not in result.stdout
    assert "Unexpected CLI error" not in result.stdout


def test_analyze_attached_watch_reuses_loaded_combined_snapshot_without_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">watch\nACGT\n", encoding="utf-8")

    read_count = 0
    original_reader = cli_system_config._read_toml_document

    def counted_reader(*, config_path: Path) -> dict[str, object]:
        nonlocal read_count
        read_count += 1
        return original_reader(config_path=config_path)

    def fake_run_watch_session(
        *,
        service: Any,
        task_ids: tuple[str, ...],
        mode: Any,
        render: bool,
        include_explicit_inactive: bool = False,
        stop_condition: Any | None = None,
        wait_for_initial_rows: bool = False,
    ) -> Any:
        _ = (
            service,
            mode,
            render,
            include_explicit_inactive,
            stop_condition,
            wait_for_initial_rows,
        )
        return cli_main.WatchCliOutcome(
            rows=(
                cli_main.WatchTaskRow(
                    task_id=task_ids[0],
                    job_id="job-1",
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

    monkeypatch.setattr("jelica_cli.system_config._read_toml_document", counted_reader)
    monkeypatch.setattr(cli_main, "_run_watch_session", fake_run_watch_session)

    result = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert read_count == 1
    assert "unknown field 'cli'" not in result.stdout


def test_config_init_non_interactive_writes_defaults_and_directories(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    result = _run_non_interactive_init(jelica_home)

    assert result.exit_code == 0

    config_document = _load_config_document(jelica_home)
    assert config_document == {
        "schema_version": 1,
        "input_directory_max_depth": 3,
        "ncbi_api_key": "",
        "ncbi_max_retries": 3,
        "default_alignment_mode": "compute",
        "data": {"directory": "data"},
        "execution": {
            "max_parallel_tasks": 1,
            "scheduler_poll_interval_seconds": 0.25,
            "heartbeat_interval_seconds": 1.0,
            "lease_timeout_seconds": 5.0,
            "progress_flush_interval_seconds": 1.0,
            "max_recovery_attempts": 3,
        },
        "logging": {
            "level": "INFO",
            "system_level": "",
            "task_level": "",
            "include_diagnostics": False,
            "diagnostic_field_limit": 8192,
        },
        "tools": {"mafft": {"executable": ""}},
        "cli": {"color": True, "emoji": True},
    }

    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.data_dir.is_dir()
    assert resolved.tasks_dir.is_dir()
    assert resolved.temp_dir.is_dir()
    assert resolved.logs_dir.is_dir()
    assert resolved.database_path.is_file()


def test_config_init_interactive_accepts_prompt_defaults(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    result = _invoke_cli(
        args=["config", "init"],
        jelica_home=jelica_home,
        input_text="\n\n\n",
    )

    assert result.exit_code == 0
    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.data_dir == jelica_home / "data"
    assert resolved.max_workers == 1
    assert resolved.log_level == "INFO"


def test_config_init_non_interactive_writes_explicit_values(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    result = _run_non_interactive_init(
        jelica_home,
        "--data-dir",
        "custom-data",
        "--max-workers",
        "4",
        "--log-level",
        "debug",
    )

    assert result.exit_code == 0

    config_document = _load_config_document(jelica_home)
    assert config_document["data"]["directory"] == "custom-data"
    assert config_document["execution"]["max_parallel_tasks"] == 4
    assert config_document["logging"]["level"] == "debug"

    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.max_workers == 4
    assert resolved.log_level == "DEBUG"
    assert resolved.data_dir == (jelica_home / "custom-data")


def test_config_init_is_idempotent_when_config_and_database_exist(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    first = _run_non_interactive_init(jelica_home)
    config_path = CoreConfigService(jelica_home=jelica_home).get_config_path()
    before = config_path.read_text(encoding="utf-8")
    second = _run_non_interactive_init(jelica_home)
    after = config_path.read_text(encoding="utf-8")

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert before == after


def test_config_init_recreates_missing_database_from_existing_valid_toml(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    first = _run_non_interactive_init(jelica_home, "--max-workers", "3")
    resolved = _load_resolved_core_config(jelica_home)
    config_path = CoreConfigService(jelica_home=jelica_home).get_config_path()
    before = config_path.read_text(encoding="utf-8")
    resolved.database_path.unlink()
    assert not resolved.database_path.exists()
    second = _run_non_interactive_init(jelica_home, "--max-workers", "9")
    after = config_path.read_text(encoding="utf-8")

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert before == after
    assert resolved.database_path.is_file()
    assert _load_config_document(jelica_home)["execution"]["max_parallel_tasks"] == 3


def _prepare_sparse_existing_config(
    jelica_home: Path,
    *,
    missing_paths: tuple[str, ...] = tuple(),
    remove_cli_section: bool = False,
) -> Path:
    _run_non_interactive_init(jelica_home)
    document = _load_config_document(jelica_home)
    for field_path in missing_paths:
        _delete_document_path(document, field_path=field_path)
    if remove_cli_section:
        document.pop("cli", None)
    return _write_config_document(jelica_home, document)


def test_config_init_recovers_existing_invalid_toml_and_writes_complete_document(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    service = CoreConfigService(jelica_home=jelica_home)
    config_path = service.get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("schema_version =\n", encoding="utf-8")
    result = _run_non_interactive_init(jelica_home)

    assert result.exit_code == 0
    assert "System config initialized successfully." in result.stdout
    document = _load_config_document(jelica_home)
    _assert_complete_combined_config_document(document)
    assert "Traceback" not in result.stdout
    validate_result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)
    assert validate_result.exit_code == 0


def test_config_init_interactive_recovers_existing_file_missing_required_core_field(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _prepare_sparse_existing_config(
        jelica_home,
        missing_paths=("input_directory_max_depth",),
    )

    result = _invoke_cli(
        args=["config", "init"],
        jelica_home=jelica_home,
        input_text="\n\n\n",
    )

    assert result.exit_code == 0
    assert "missing required field 'input_directory_max_depth'" not in result.stdout
    document = _load_config_document(jelica_home)
    assert document["input_directory_max_depth"] == 3
    _assert_complete_combined_config_document(document)
    validate_result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)
    assert validate_result.exit_code == 0
    resolved = _load_resolved_core_config(jelica_home)
    assert list(resolved.tasks_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("variant_name", "missing_paths", "remove_cli_section"),
    (
        ("missing-cli", tuple(), True),
        (
            "missing-multiple-defaulted-fields",
            ("logging.include_diagnostics", "execution.progress_flush_interval_seconds"),
            False,
        ),
    ),
)
def test_config_init_recovers_other_sparse_existing_variants(
    tmp_path: Path,
    variant_name: str,
    missing_paths: tuple[str, ...],
    remove_cli_section: bool,
) -> None:
    jelica_home = tmp_path / f"home-{variant_name}"
    _prepare_sparse_existing_config(
        jelica_home,
        missing_paths=missing_paths,
        remove_cli_section=remove_cli_section,
    )

    result = _run_non_interactive_init(jelica_home)

    assert result.exit_code == 0
    document = _load_config_document(jelica_home)
    _assert_complete_combined_config_document(document)
    validate_result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)
    assert validate_result.exit_code == 0


def test_config_init_abort_keeps_existing_file_unchanged(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    config_path = _prepare_sparse_existing_config(
        jelica_home,
        missing_paths=("input_directory_max_depth",),
    )
    before = config_path.read_bytes()

    result = _invoke_cli(
        args=["config", "init"],
        jelica_home=jelica_home,
        input_text="\x03",
    )

    assert result.exit_code != 0
    assert config_path.read_bytes() == before


def test_config_init_write_failure_preserves_old_file_and_cleans_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    config_path = _prepare_sparse_existing_config(
        jelica_home,
        missing_paths=("input_directory_max_depth",),
    )
    before = config_path.read_text(encoding="utf-8")

    def _failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("jelica_cli.system_config.os.replace", _failing_replace)

    result = _run_non_interactive_init(jelica_home)

    assert result.exit_code != 0
    assert "Cannot write system config" in result.stdout
    assert config_path.read_text(encoding="utf-8") == before
    assert list(config_path.parent.glob("config.*.tmp")) == []


def test_config_show_outputs_masked_raw_config_json(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(args=["config", "show"], jelica_home=jelica_home)
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    expected = _load_config_document(jelica_home)
    expected["ncbi_api_key"] = "<not configured>"
    assert payload == expected


def test_config_validate_succeeds_for_valid_file(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert "System config is valid." in result.stdout


def test_config_validate_reports_missing_registry_database(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    database_path = _load_resolved_core_config(jelica_home).database_path
    database_path.unlink()

    result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "Task registry database is unavailable" in result.stdout
    assert "Traceback" not in result.stdout


def test_config_validate_reports_corrupted_registry_database(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    database_path = _load_resolved_core_config(jelica_home).database_path
    database_path.write_bytes(b"corrupted-db")

    result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "Task registry database is corrupted" in result.stdout
    assert "Traceback" not in result.stdout


def test_config_validate_reports_invalid_toml(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    config_path = CoreConfigService(jelica_home=jelica_home).get_config_path()
    config_path.write_text("schema_version =\n", encoding="utf-8")

    result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "System config is invalid:" in result.stdout
    assert "Invalid TOML" in result.stdout
    assert "Failed to complete system config operation" not in result.stdout


def test_config_validate_reports_unknown_field_as_invalid_config(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    config_path = CoreConfigService(jelica_home=jelica_home).get_config_path()
    config_text = config_path.read_text(encoding="utf-8")
    execution_header = "[execution]\n"
    assert execution_header in config_text
    config_path.write_text(
        config_text.replace(execution_header, f"{execution_header}min_workers = 1\n", 1),
        encoding="utf-8",
    )

    result = _invoke_cli(args=["config", "validate"], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "System config is invalid:" in result.stdout
    assert "unknown field 'execution.min_workers'" in result.stdout
    assert "Failed to complete system config operation" not in result.stdout


def test_config_set_updates_data_directory(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(
        args=["config", "set", "data.directory", "custom-data"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.data_dir == jelica_home / "custom-data"
    assert resolved.tasks_dir.is_dir()


@pytest.mark.parametrize(
    ("assignment", "expected_parameter", "expected_value"),
    (
        ("notifications.device.enabled=true", "notifications.device.enabled", True),
        (
            "notifications.device.events.task.completed=true",
            "notifications.device.events.task.completed",
            True,
        ),
        ("notifications.sound.enabled=false", "notifications.sound.enabled", False),
    ),
)
def test_config_set_accepts_canonical_key_value_assignments(
    tmp_path: Path,
    assignment: str,
    expected_parameter: str,
    expected_value: bool,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(args=["config", "set", assignment], jelica_home=jelica_home)

    assert result.exit_code == 0
    document = CliSystemConfigService(jelica_home=jelica_home).show_document()
    if expected_parameter == "notifications.device.events.task.completed":
        value = document["notifications"]["device"]["events"]["task.completed"]
    else:
        section, subsection, field = expected_parameter.split(".")
        value = document[section][subsection][field]
    assert value is expected_value


def test_config_set_rejects_malformed_assignment_without_equals(tmp_path: Path) -> None:
    result = _invoke_cli(
        args=["config", "set", "notifications.device.enabled"],
        jelica_home=tmp_path / "home",
    )

    assert result.exit_code == 2
    assert "KEY=VALUE" in result.output


def test_config_set_updates_max_workers(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(
        args=["config", "set", "execution.max_parallel_tasks", "8"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.max_workers == 8


def test_config_set_accepts_short_parameter_alias(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(
        args=["config", "set", "max_workers", "5"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.max_workers == 5


def test_config_set_updates_log_level_and_normalizes_case(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(
        args=["config", "set", "logging.level", "warning"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.log_level == "WARNING"


def test_config_set_rejects_unknown_parameter(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(
        args=["config", "set", "unknown.value", "123"],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "Unknown system config parameter" in result.stdout


def test_config_set_rejects_schema_version(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(
        args=["config", "set", "schema_version", "2"],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "cannot be changed" in result.stdout


def test_config_set_rejects_missing_value_argument(tmp_path: Path) -> None:
    result = _invoke_cli(
        args=["config", "set", "data.directory"],
        jelica_home=tmp_path / "home",
    )

    assert result.exit_code == 2
    assert "Usage:" in result.output


def test_config_set_rejects_empty_value(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(
        args=["config", "set", "data.directory", ""],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "empty values are not allowed" in result.stdout


def test_config_unset_data_directory_reverts_to_explicit_default(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home, "--data-dir", "custom-data")

    result = _invoke_cli(
        args=["config", "unset", "data.directory"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    config_document = _load_config_document(jelica_home)
    assert config_document["data"]["directory"] == "data"

    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.data_dir == jelica_home / "data"


def test_config_unset_execution_max_workers_reverts_to_explicit_default(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home, "--max-workers", "4")

    result = _invoke_cli(
        args=["config", "unset", "execution.max_parallel_tasks"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    config_document = _load_config_document(jelica_home)
    assert config_document["execution"]["max_parallel_tasks"] == 1

    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.max_workers == 1


def test_config_unset_accepts_short_parameter_alias(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home, "--max-workers", "5")

    result = _invoke_cli(
        args=["config", "unset", "max_workers"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.max_workers == 1


def test_config_unset_logging_level_reverts_to_explicit_default(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home, "--log-level", "ERROR")

    result = _invoke_cli(
        args=["config", "unset", "logging.level"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    config_document = _load_config_document(jelica_home)
    assert config_document["logging"]["level"] == "INFO"

    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.log_level == "INFO"


def test_config_unset_rejects_schema_version(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(
        args=["config", "unset", "schema_version"],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "cannot be removed" in result.stdout


def test_config_unset_rejects_unknown_parameter(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(
        args=["config", "unset", "unknown.value"],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "Unknown system config parameter" in result.stdout


def test_config_unset_rejects_parameter_that_is_already_set_to_default(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home, "--max-workers", "4")
    first_unset = _invoke_cli(
        args=["config", "unset", "execution.max_parallel_tasks"],
        jelica_home=jelica_home,
    )
    second_unset = _invoke_cli(
        args=["config", "unset", "execution.max_parallel_tasks"],
        jelica_home=jelica_home,
    )

    assert first_unset.exit_code == 0
    assert second_unset.exit_code != 0
    assert "already set to its default value" in second_unset.stdout


def test_analyze_creates_task_in_resolved_tasks_dir_after_initialization(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    (tmp_path / "Sample_1.fasta").write_text(">s1\nACGT\n", encoding="utf-8")
    (tmp_path / "Sample_2.fasta").write_text(">s2\nACGG\n", encoding="utf-8")

    result = _invoke_cli(
        args=["analyze", str(tmp_path / "Sample_1.fasta"), str(tmp_path / "Sample_2.fasta")],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    task_config_path = _single_task_config_path(jelica_home)
    saved_config = json.loads(task_config_path.read_text(encoding="utf-8"))
    assert saved_config["samples"] == [
        str(tmp_path / "Sample_1.fasta"),
        str(tmp_path / "Sample_2.fasta"),
    ]


def test_analyze_uses_custom_absolute_data_directory(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    custom_data_dir = tmp_path / "external-data"
    _run_non_interactive_init(jelica_home, "--data-dir", str(custom_data_dir))
    (tmp_path / "Sample_1.fasta").write_text(">s1\nACGT\n", encoding="utf-8")
    (tmp_path / "Sample_2.fasta").write_text(">s2\nACGG\n", encoding="utf-8")

    result = _invoke_cli(
        args=["analyze", str(tmp_path / "Sample_1.fasta"), str(tmp_path / "Sample_2.fasta")],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    resolved = _load_resolved_core_config(jelica_home)
    assert resolved.tasks_dir == custom_data_dir / "tasks"
    assert _single_task_config_path(jelica_home).is_file()


def test_analyze_accepts_relative_sample_paths_from_arbitrary_current_directory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    (samples_dir / "Sample_1.fasta").write_text(">s1\nACGT\n", encoding="utf-8")
    (samples_dir / "Sample_2.fasta").write_text(">s2\nACGG\n", encoding="utf-8")
    monkeypatch.chdir(samples_dir)

    result = _invoke_cli(
        args=["analyze", "Sample_1.fasta", "Sample_2.fasta"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    task_config_path = _single_task_config_path(jelica_home)
    saved_config = json.loads(task_config_path.read_text(encoding="utf-8"))
    assert saved_config["samples"] == ["Sample_1.fasta", "Sample_2.fasta"]


def test_analyze_saves_default_priority_in_normalized_config(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "Sample_1.fasta"
    sample.write_text(">s1\nACGT\n", encoding="utf-8")

    result = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)

    assert result.exit_code == 0
    task_config_path = _single_task_config_path(jelica_home)
    saved_config = json.loads(task_config_path.read_text(encoding="utf-8"))
    assert saved_config["priority"] == 1


def test_analyze_priority_override_is_saved_in_normalized_config(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "Sample_1.fasta"
    sample.write_text(">s1\nACGT\n", encoding="utf-8")

    result = _invoke_cli(
        args=["analyze", str(sample), "--priority=7"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    task_config_path = _single_task_config_path(jelica_home)
    saved_config = json.loads(task_config_path.read_text(encoding="utf-8"))
    assert saved_config["priority"] == 7


@pytest.mark.parametrize(
    "reference_selector",
    ("NC_045512.2", "data/alignment.afa::NC_045512.2"),
)
def test_analyze_reference_override_is_saved_as_selector(
    tmp_path: Path, reference_selector: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    if "::" in reference_selector:
        sample = tmp_path / "data" / "alignment.afa"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text(">NC_045512.2\nACGT\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        analyze_args = [
            "analyze",
            "data/alignment.afa",
            "--alignment.mode=prealigned",
            f"--reference={reference_selector}",
        ]
        expected_samples = ["data/alignment.afa"]
    else:
        sample = tmp_path / "Sample_1.fasta"
        sample.write_text(">NC_045512.2\nACGT\n", encoding="utf-8")
        analyze_args = ["analyze", str(sample), f"--reference={reference_selector}"]
        expected_samples = [str(sample)]

    result = _invoke_cli(args=analyze_args, jelica_home=jelica_home)

    assert result.exit_code == 0
    task_config_path = _single_task_config_path(jelica_home)
    saved_config = json.loads(task_config_path.read_text(encoding="utf-8"))
    assert saved_config["reference"] == reference_selector
    assert saved_config["samples"] == expected_samples


def test_analyze_rejects_priority_below_minimum(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "Sample_1.fasta"
    sample.write_text(">s1\nACGT\n", encoding="utf-8")

    result = _invoke_cli(
        args=["analyze", str(sample), "--priority=0"],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "Input should be greater than or equal to 1" in result.stdout
    assert "Traceback" not in result.stdout


def test_tasks_list_reports_empty_registry(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(args=["tasks", "list"], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert result.stdout.strip() == "Analytical tasks were not found."


def test_tasks_list_outputs_multiple_tasks_sorted_by_updated_at_desc(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample_a = tmp_path / "sample-a.fasta"
    sample_b = tmp_path / "sample-b.fasta"
    sample_a.write_text(">a\nACGT\n", encoding="utf-8")
    sample_b.write_text(">b\nACGG\n", encoding="utf-8")

    first = _invoke_cli(args=["analyze", str(sample_a)], jelica_home=jelica_home)
    second = _invoke_cli(args=["analyze", str(sample_b)], jelica_home=jelica_home)
    assert first.exit_code == 0
    assert second.exit_code == 0
    first_task_id = _extract_started_task_id(first.stdout)
    second_task_id = _extract_started_task_id(second.stdout)

    registry = _registry_service(jelica_home)
    first_record = registry.get_task(task_id=first_task_id)
    registry.set_priority(
        task_id=first_task_id,
        priority=9,
        expected_version=first_record.record_version,
    )

    result = _invoke_cli(args=["tasks", "list"], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert result.stdout.index(first_task_id) < result.stdout.index(second_task_id)
    assert f"name={first_record.name}" in result.stdout


def test_tasks_list_supports_state_filter_limit_offset_and_multiple_state_values(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample_a = tmp_path / "sample-a.fasta"
    sample_b = tmp_path / "sample-b.fasta"
    sample_c = tmp_path / "sample-c.fasta"
    sample_a.write_text(">a\nACGT\n", encoding="utf-8")
    sample_b.write_text(">b\nACGG\n", encoding="utf-8")
    sample_c.write_text(">c\nACGA\n", encoding="utf-8")

    _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample_a])
    second_task_id = _initialize_task_without_start(
        jelica_home=jelica_home,
        sample_paths=[sample_b],
    )
    third_task_id = _initialize_task_without_start(
        jelica_home=jelica_home,
        sample_paths=[sample_c],
    )

    registry = _registry_service(jelica_home)
    registry.start(task_id=second_task_id)
    registry.cancel(task_id=second_task_id)
    registry.start(task_id=third_task_id)
    registry.cancel(task_id=third_task_id)

    filtered = _invoke_cli(
        args=["tasks", "list", "--state", "cancelled"],
        jelica_home=jelica_home,
    )
    assert filtered.exit_code == 0
    assert "state=cancelled" in filtered.stdout
    assert "state=waiting" not in filtered.stdout

    limited = _invoke_cli(
        args=[
            "tasks",
            "list",
            "--state",
            "waiting",
            "--state",
            "cancelled",
            "--limit",
            "2",
            "--offset",
            "1",
        ],
        jelica_home=jelica_home,
    )
    assert limited.exit_code == 0
    assert limited.stdout.count("| state=") == 2


def test_tasks_list_rejects_invalid_state_and_limit(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    invalid_state = _invoke_cli(
        args=["tasks", "list", "--state", "unknown"],
        jelica_home=jelica_home,
    )
    invalid_limit = _invoke_cli(
        args=["tasks", "list", "--limit", "0"],
        jelica_home=jelica_home,
    )

    assert invalid_state.exit_code != 0
    assert "unknown" in invalid_state.stdout
    assert "Traceback" not in invalid_state.stdout
    assert invalid_limit.exit_code != 0
    assert "limit must be > 0" in invalid_limit.stdout
    assert "Traceback" not in invalid_limit.stdout


def test_tasks_show_outputs_known_task_details(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    analyze = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)
    assert analyze.exit_code == 0
    task_id = _single_task_id(jelica_home)

    show_text = _invoke_cli(args=["tasks", "show", task_id], jelica_home=jelica_home)
    assert show_text.exit_code == 0
    assert f"task_id: {task_id}" in show_text.stdout
    assert "state: completed" in show_text.stdout
    assert "active_or_latest_job:" in show_text.stdout


def test_tasks_show_accepts_names_and_multiple_uuid_refs(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    first_sample = tmp_path / "first.fasta"
    second_sample = tmp_path / "second.fasta"
    first_sample.write_text(">first\nACGT\n", encoding="utf-8")
    second_sample.write_text(">second\nACGA\n", encoding="utf-8")
    first_task_id = _initialize_task_without_start(
        jelica_home=jelica_home,
        sample_paths=[first_sample],
    )
    second_task_id = _initialize_task_without_start(
        jelica_home=jelica_home,
        sample_paths=[second_sample],
    )
    first_name = _task_name(jelica_home=jelica_home, task_id=first_task_id)

    text_result = _invoke_cli(
        args=["tasks", "show", first_name.upper(), second_task_id],
        jelica_home=jelica_home,
    )
    assert text_result.exit_code == 0
    assert f"task_id: {first_task_id}" in text_result.stdout
    assert f"name: {first_name}" in text_result.stdout
    assert f"task_id: {second_task_id}" in text_result.stdout


def test_tasks_show_returns_not_found_without_traceback(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    missing_task_id = "00000000-0000-4000-8000-000000000000"

    result = _invoke_cli(args=["tasks", "show", missing_task_id], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "was not found" in result.stdout
    assert "Traceback" not in result.stdout


def test_tasks_commands_surface_core_sqlite_error_without_cli_reclassification(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    resolved = _load_resolved_core_config(jelica_home)
    resolved.database_path.write_bytes(b"corrupted-db")

    result = _invoke_cli(args=["tasks", "list"], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "Task registry database is corrupted" in result.stdout
    assert "Traceback" not in result.stdout


def test_tasks_commands_work_after_separate_cli_invocations(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    analyze = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)
    assert analyze.exit_code == 0
    task_id = _single_task_id(jelica_home)

    first_list = _invoke_cli(args=["tasks", "list"], jelica_home=jelica_home)
    second_show = _invoke_cli(args=["tasks", "show", task_id], jelica_home=jelica_home)

    assert first_list.exit_code == 0
    assert second_show.exit_code == 0
    assert task_id in first_list.stdout
    assert f"task_id: {task_id}" in second_show.stdout


def test_read_only_task_commands_do_not_start_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    def _unexpected_start(*args: Any, **kwargs: Any) -> Any:
        _ = (args, kwargs)
        raise AssertionError("read-only command started JELICA Service")

    monkeypatch.setattr(cli_main, "start_service", _unexpected_start)

    listed = _invoke_cli(args=["tasks", "list"], jelica_home=jelica_home)
    shown = _invoke_cli(args=["tasks", "show", task_id], jelica_home=jelica_home)

    assert listed.exit_code == 0
    assert shown.exit_code == 0


def test_tasks_start_completes_job_and_publishes_initialize_stage(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    start = _invoke_cli(args=["tasks", "start", task_id], jelica_home=jelica_home)
    assert start.exit_code == 0
    assert f"Task {task_id} started as job" in start.stdout
    assert "(completed)." in start.stdout

    resolved = _load_resolved_core_config(jelica_home)
    snapshot = _registry_service(jelica_home).get_task_snapshot(task_id=task_id)
    job_id = snapshot.task.latest_job_id
    assert job_id is not None
    assert snapshot.task.state is not None
    assert snapshot.task.state.value == "completed"
    assert snapshot.task.active_job_id is None
    assert snapshot.task.latest_job_id == job_id

    stage_dir = resolved.tasks_dir / task_id / "jobs" / job_id / "stages" / "initialize_job"
    stage_manifest_path = stage_dir / "stage_manifest.json"
    execution_manifest_path = stage_dir / "execution_manifest.json"
    assert stage_manifest_path.is_file()
    assert execution_manifest_path.is_file()

    stage_manifest = json.loads(stage_manifest_path.read_text(encoding="utf-8"))
    assert stage_manifest["stage_id"] == "initialize_job"
    assert stage_manifest["job_id"] == job_id
    assert stage_manifest["artifacts"] == ["execution_manifest.json"]


def test_tasks_start_text_output_and_persistent_service_status(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    start_result = _invoke_cli(args=["tasks", "start", task_id], jelica_home=jelica_home)
    assert start_result.exit_code == 0
    assert f"Task {task_id} started as job" in start_result.stdout
    assert "(completed)." in start_result.stdout

    service_status = _invoke_cli(
        args=["service", "status"],
        jelica_home=jelica_home,
    )
    assert service_status.exit_code == 0
    assert "status: running" in service_status.stdout


def test_tasks_start_and_resume_multiple_uuid_refs_enqueue_before_one_common_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    first_sample = tmp_path / "first.fasta"
    second_sample = tmp_path / "second.fasta"
    first_sample.write_text(">first\nACGT\n", encoding="utf-8")
    second_sample.write_text(">second\nACGA\n", encoding="utf-8")
    task_ids = (
        _initialize_task_without_start(
            jelica_home=jelica_home,
            sample_paths=[first_sample],
        ),
        _initialize_task_without_start(
            jelica_home=jelica_home,
            sample_paths=[second_sample],
        ),
    )
    status = _service_status(tmp_path)
    calls: list[str] = []
    original_start = cli_main.run_start_analytical_task
    original_resume = cli_main.run_resume_analytical_task

    def _fake_start_service(
        *,
        core_config_service: CoreConfigService,
        runner_module: str,
    ) -> ServiceStartResult:
        _ = core_config_service
        assert runner_module == "jelica_cli.service_runner"
        calls.append("service")
        return ServiceStartResult(status=status, already_running=True)

    def _wrapped_start(*args: Any, **kwargs: Any) -> Any:
        _ = args
        assert kwargs["detached"] is True
        assert kwargs["background_runner_module"] == "jelica_cli.service_runner"
        calls.append(f"start:{kwargs['task_id']}")
        return original_start(**kwargs)

    def _wrapped_resume(*args: Any, **kwargs: Any) -> Any:
        _ = args
        assert kwargs["detached"] is True
        assert kwargs["background_runner_module"] == "jelica_cli.service_runner"
        calls.append(f"resume:{kwargs['task_id']}")
        return original_resume(**kwargs)

    def _fake_background_runtime(
        *,
        jelica_home: Path | None = None,
        runner_module: str,
    ) -> int:
        _ = jelica_home
        assert runner_module == "jelica_cli.service_runner"
        return 12345

    def _completed_batch_watch(
        *,
        task_ids: tuple[str, ...],
        event_since: datetime,
        mode: Any,
        render: bool,
        output_format: str,
        verbose: bool,
    ) -> cli_main.WatchCliOutcome:
        _ = (event_since, mode, render, output_format, verbose)
        operation_name = "resume" if any(item.startswith("resume:") for item in calls) else "start"
        assert calls[-2:] == [
            f"{operation_name}:{task_ids[0]}",
            f"{operation_name}:{task_ids[1]}",
        ]
        calls.append(f"watch:{operation_name}")
        registry = _registry_service(jelica_home)
        rows = []
        for task_id in task_ids:
            snapshot = registry.get_task_snapshot(task_id=task_id)
            assert snapshot.active_or_latest_job is not None
            rows.append(
                cli_main.WatchTaskRow(
                    task_id=task_id,
                    job_id=snapshot.active_or_latest_job.job_id,
                    state="completed",
                    stage="completed",
                    progress=100,
                    warning_count=0,
                )
            )
        return cli_main.WatchCliOutcome(
            rows=tuple(rows),
            missing_task_ids=tuple(),
            inactive_tasks=tuple(),
            events=tuple(),
            interrupted=False,
        )

    monkeypatch.setattr(cli_main, "start_service", _fake_start_service)
    monkeypatch.setattr(cli_main, "run_start_analytical_task", _wrapped_start)
    monkeypatch.setattr(cli_main, "run_resume_analytical_task", _wrapped_resume)
    monkeypatch.setattr(core_operations, "launch_background_runtime", _fake_background_runtime)
    monkeypatch.setattr(cli_main, "_watch_execution_tasks", _completed_batch_watch)

    started = _invoke_cli(args=["tasks", "start", *task_ids], jelica_home=jelica_home)
    assert started.exit_code == 0
    assert all(f"Task {task_id} started as job" in started.stdout for task_id in task_ids)
    registry = _registry_service(jelica_home)
    for task_id in task_ids:
        paused = registry.pause(task_id=task_id)
        assert paused.result_type.value == "applied"

    resumed = _invoke_cli(args=["tasks", "resume", *task_ids], jelica_home=jelica_home)

    assert resumed.exit_code == 0
    assert all(f"Task {task_id} resumed as job" in resumed.stdout for task_id in task_ids)
    assert calls == [
        "service",
        f"start:{task_ids[0]}",
        f"start:{task_ids[1]}",
        "watch:start",
        "service",
        f"resume:{task_ids[0]}",
        f"resume:{task_ids[1]}",
        "watch:resume",
    ]


def test_tasks_start_rejects_completed_task_without_traceback(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    first_start = _invoke_cli(args=["tasks", "start", task_id], jelica_home=jelica_home)
    second_start = _invoke_cli(args=["tasks", "start", task_id], jelica_home=jelica_home)

    assert first_start.exit_code == 0
    assert second_start.exit_code != 0
    assert "Task start was rejected" in second_start.stdout
    assert "Traceback" not in second_start.stdout


def test_service_start_recovers_expired_running_job_to_waiting(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    registry = _registry_service(jelica_home)
    start_result = registry.start(task_id=task_id)
    assert start_result.result_type.value == "applied"
    claim = registry.claim_next_queued_job_for_worker(
        worker_instance_id="stale-worker",
        lease_token="stale-token",
        lease_timeout_seconds=0.001,
    )
    assert claim is not None
    time.sleep(0.05)

    service_result = _invoke_cli(args=["service", "start"], jelica_home=jelica_home)
    _wait_for_task_state(
        registry=registry,
        task_id=task_id,
        expected_state="waiting",
    )
    show_result = _invoke_cli(args=["tasks", "show", task_id], jelica_home=jelica_home)

    assert service_result.exit_code == 0
    assert "status: running" in service_result.stdout
    assert show_result.exit_code == 0
    assert "state: waiting" in show_result.stdout


def test_tasks_pause_resume_cancel_text_smoke_without_traceback(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    sample_a = tmp_path / "sample-a.fasta"
    sample_a.write_text(">a\nACGT\n", encoding="utf-8")
    task_a = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample_a])
    queued_a = _registry_service(jelica_home).start(task_id=task_a)
    assert queued_a.result_type.value == "applied"

    pause_result = _invoke_cli(args=["tasks", "pause", task_a], jelica_home=jelica_home)
    assert pause_result.exit_code == 0
    assert "Traceback" not in pause_result.stdout
    assert f"Task {task_a}: applied" in pause_result.stdout
    assert _registry_service(jelica_home).get_task(task_id=task_a).state.value == "paused"

    resume_result = _invoke_cli(args=["tasks", "resume", task_a], jelica_home=jelica_home)
    assert resume_result.exit_code == 0
    assert "Traceback" not in resume_result.stdout
    assert f"Task {task_a} resumed as job" in resume_result.stdout
    assert _registry_service(jelica_home).get_task(task_id=task_a).state.value == "completed"

    sample_b = tmp_path / "sample-b.fasta"
    sample_b.write_text(">b\nACGT\n", encoding="utf-8")
    task_b = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample_b])
    queued_b = _registry_service(jelica_home).start(task_id=task_b)
    assert queued_b.result_type.value == "applied"

    cancel_result = _invoke_cli(args=["tasks", "cancel", task_b], jelica_home=jelica_home)
    assert cancel_result.exit_code == 0
    assert "Traceback" not in cancel_result.stdout
    assert f"Task {task_b}: applied" in cancel_result.stdout
    assert _registry_service(jelica_home).get_task(task_id=task_b).state.value == "cancelled"

    pause_completed = _invoke_cli(args=["tasks", "pause", task_a], jelica_home=jelica_home)
    assert pause_completed.exit_code != 0
    assert "Traceback" not in pause_completed.stdout


def test_tasks_pause_stop_and_cancel_accept_text_names_and_multiple_refs(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    task_ids: list[str] = []
    for index in range(4):
        sample = tmp_path / f"sample-{index}.fasta"
        sample.write_text(f">sample-{index}\nACGT\n", encoding="utf-8")
        task_ids.append(
            _initialize_task_without_start(
                jelica_home=jelica_home,
                sample_paths=[sample],
            )
        )
    registry = _registry_service(jelica_home)
    for task_id in task_ids:
        queued = registry.start(task_id=task_id)
        assert queued.result_type.value == "applied"
    task_names = [_task_name(jelica_home=jelica_home, task_id=task_id) for task_id in task_ids]

    paused = _invoke_cli(
        args=["tasks", "pause", task_names[0].upper(), task_ids[1]],
        jelica_home=jelica_home,
    )
    stopped = _invoke_cli(
        args=["tasks", "stop", task_names[2]],
        jelica_home=jelica_home,
    )
    cancelled = _invoke_cli(
        args=["tasks", "cancel", task_names[3].upper()],
        jelica_home=jelica_home,
    )

    assert paused.exit_code == 0
    assert stopped.exit_code == 0
    assert cancelled.exit_code == 0
    assert registry.get_task(task_id=task_ids[0]).state.value == "paused"
    assert registry.get_task(task_id=task_ids[1]).state.value == "paused"
    assert registry.get_task(task_id=task_ids[2]).state.value == "paused"
    assert registry.get_task(task_id=task_ids[3]).state.value == "cancelled"


def test_tasks_update_and_reprioritize_text_smoke_without_traceback(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    sample_a = tmp_path / "sample-a.fasta"
    sample_b = tmp_path / "sample-b.fasta"
    sample_a.write_text(">a\nACGT\n", encoding="utf-8")
    sample_b.write_text(">b\nACGG\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample_a])

    update_config = tmp_path / "update.json"
    update_config.write_text(
        json.dumps({"schema_version": 1, "samples": [str(sample_b)], "priority": 3}),
        encoding="utf-8",
    )

    update_result = _invoke_cli(
        args=["tasks", "update", task_id, str(update_config), "--priority=5"],
        jelica_home=jelica_home,
    )
    assert update_result.exit_code == 0
    assert "Traceback" not in update_result.stdout
    assert f"Task {task_id} updated: config revision 2, priority 5." in update_result.stdout

    queued = _registry_service(jelica_home).start(task_id=task_id)
    assert queued.result_type.value == "applied"

    reprioritize_result = _invoke_cli(
        args=["tasks", "reprioritize", task_id, "9"],
        jelica_home=jelica_home,
    )
    assert reprioritize_result.exit_code == 0
    assert "Traceback" not in reprioritize_result.stdout
    assert f"Task {task_id} priority changed from 5 to 9." in reprioritize_result.stdout

    update_rejected = _invoke_cli(
        args=["tasks", "update", task_id, "--priority=7"],
        jelica_home=jelica_home,
    )
    assert update_rejected.exit_code != 0
    assert "Traceback" not in update_rejected.stdout
    assert "Task update was rejected" in update_rejected.stdout

    invalid_priority = _invoke_cli(
        args=["tasks", "reprioritize", task_id, "0"],
        jelica_home=jelica_home,
    )
    assert invalid_priority.exit_code != 0
    assert "Traceback" not in invalid_priority.stdout
    assert "priority must be >= 1" in invalid_priority.stdout


def test_tasks_single_reference_commands_accept_case_insensitive_text_name(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">sample\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    task_name = _task_name(jelica_home=jelica_home, task_id=task_id).upper()

    updated = _invoke_cli(
        args=["tasks", "update", task_name, "--priority=5"],
        jelica_home=jelica_home,
    )
    registry = _registry_service(jelica_home)
    queued = registry.start(task_id=task_id)
    assert queued.result_type.value == "applied"
    reprioritized = _invoke_cli(
        args=["tasks", "reprioritize", task_name, "7"],
        jelica_home=jelica_home,
    )
    jobs = _invoke_cli(
        args=["tasks", "jobs", task_name],
        jelica_home=jelica_home,
    )

    assert updated.exit_code == 0
    assert reprioritized.exit_code == 0
    assert jobs.exit_code == 0
    assert f"Jobs for task {task_id}" in jobs.stdout
    snapshot = registry.get_task_snapshot(task_id=task_id)
    assert snapshot.task.default_priority == 5
    assert snapshot.active_or_latest_job is not None
    assert snapshot.active_or_latest_job.priority == 7


def test_tasks_update_round_trips_saved_normalized_config(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    result = _invoke_cli(
        args=["tasks", "update", task_id, "--priority=4"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    assert f"Task {task_id} updated: config revision 2, priority 4." in result.stdout
    saved = json.loads(_single_task_config_path(jelica_home).read_text(encoding="utf-8"))
    assert saved["alignment"]["mafft"]["strategy"] == "auto"
    assert saved["alignment"]["mafft"]["progressive_threads"] == "auto"
    assert saved["alignment"]["mafft"]["iterative_threads"] == "auto"


def test_tasks_delete_reports_partial_exit_code_in_text_mode(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    missing_task_id = "00000000-0000-4000-8000-000000000000"

    result = _invoke_cli(
        args=["tasks", "delete", task_id, missing_task_id, task_id, "--yes"],
        jelica_home=jelica_home,
    )
    assert result.exit_code == 1
    assert f"{task_id}: deleted" in result.stdout
    assert f"{missing_task_id}: not_found" in result.stdout
    assert "Task deletion: 1 deleted" in result.stdout

    resolved = _load_resolved_core_config(jelica_home)
    assert not (resolved.tasks_dir / task_id).exists()


def test_tasks_delete_confirmation_decline_preserves_task(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    task_name = _task_name(jelica_home=jelica_home, task_id=task_id)

    result = _invoke_cli(
        args=["tasks", "delete", task_name.upper()],
        jelica_home=jelica_home,
        input_text="n\n",
    )
    assert result.exit_code == 1
    assert "Delete 1 analytical tasks and their associated files?" in result.stdout

    resolved = _load_resolved_core_config(jelica_home)
    assert (resolved.tasks_dir / task_id).exists()


def test_tasks_watch_uses_composed_config_service_with_combined_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">watch\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    task_name = _task_name(jelica_home=jelica_home, task_id=task_id)

    observed_core_services: list[CoreConfigService | None] = []
    original_watch_init = cli_main.TaskWatchService.__init__

    def wrapped_watch_init(self: Any, *args: Any, **kwargs: Any) -> None:
        observed_core_services.append(kwargs.get("core_config_service"))
        original_watch_init(self, *args, **kwargs)

    def fake_run_watch_session(
        *,
        service: Any,
        task_ids: tuple[str, ...],
        mode: Any,
        render: bool,
        include_explicit_inactive: bool = False,
        stop_condition: Any | None = None,
        wait_for_initial_rows: bool = False,
    ) -> Any:
        _ = (
            service,
            mode,
            render,
            include_explicit_inactive,
            stop_condition,
            wait_for_initial_rows,
        )
        assert task_ids == (task_id,)
        return cli_main.WatchCliOutcome(
            rows=(
                cli_main.WatchTaskRow(
                    task_id=task_id,
                    job_id="job-1",
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

    monkeypatch.setattr(cli_main.TaskWatchService, "__init__", wrapped_watch_init)
    monkeypatch.setattr(cli_main, "_run_watch_session", fake_run_watch_session)

    watch_result = _invoke_cli(
        args=["tasks", "watch", task_name.upper()],
        jelica_home=jelica_home,
    )

    assert watch_result.exit_code == 0
    assert len(observed_core_services) == 1
    assert observed_core_services[0] is not None
    assert "unknown field 'cli'" not in watch_result.stdout
    assert "Unexpected CLI error" not in watch_result.stdout


def test_tasks_watch_text_returns_terminal_result_with_persistent_service(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    start_result = _invoke_cli(args=["tasks", "start", task_id], jelica_home=jelica_home)
    assert start_result.exit_code == 0
    registry = _registry_service(jelica_home)
    assert registry.get_execution_runtime_lease() is not None

    watch_result = _invoke_cli(args=["tasks", "watch", task_id], jelica_home=jelica_home)
    assert watch_result.exit_code == 0
    assert "Traceback" not in watch_result.stdout
    assert task_id in watch_result.stdout
    assert "completed" in watch_result.stdout
    assert registry.get_execution_runtime_lease() is not None


def test_tasks_watch_rejects_task_without_job(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    watch_result = _invoke_cli(args=["tasks", "watch", task_id], jelica_home=jelica_home)
    assert watch_result.exit_code != 0
    assert f"Task {task_id} is waiting; it was not added to watch." in watch_result.stdout


def test_tasks_watch_multiple_reports_inactive_and_missing_tasks(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    missing_task_id = "00000000-0000-4000-8000-000000000000"

    result = _invoke_cli(
        args=["tasks", "watch", task_id, missing_task_id],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert f"Task {task_id} is waiting; it was not added to watch." in result.stdout
    assert f"Task {missing_task_id} was not found." in result.stdout


def test_tasks_watch_ctrl_c_detaches_without_changing_task_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    registry = _registry_service(jelica_home)
    queued = registry.start(task_id=task_id)
    assert queued.result_type.value == "applied"

    def _interrupt_watch(
        _service: Any,
        _callback: Any,
        *,
        stop_condition: Any | None = None,
        wait_for_observed_rows: bool = False,
    ) -> Any:
        _ = (stop_condition, wait_for_observed_rows)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main.TaskWatchService, "watch", _interrupt_watch)
    result = _invoke_cli(args=["tasks", "watch", task_id], jelica_home=jelica_home)

    assert result.exit_code == 130
    assert f"Watching stopped. Task {task_id} continues running." in result.stdout
    assert f"Resume watching: jelica tasks watch {task_id}" in result.stdout
    assert f"Cancel task: jelica tasks cancel {task_id}" in result.stdout
    assert registry.get_task(task_id=task_id).state.value == "queued"


def test_tasks_watch_without_references_includes_unfinished_task_without_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    task_name = _task_name(jelica_home=jelica_home, task_id=task_id)
    registry = _registry_service(jelica_home)

    def _interrupt_watch(
        _service: Any,
        _callback: Any,
        *,
        stop_condition: Any | None = None,
        wait_for_observed_rows: bool = False,
    ) -> Any:
        _ = (stop_condition, wait_for_observed_rows)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main.TaskWatchService, "watch", _interrupt_watch)

    result = _invoke_cli(args=["tasks", "watch"], jelica_home=jelica_home)

    assert result.exit_code == 130
    assert "Task name" in result.stdout
    assert task_name[:12] in result.stdout
    assert task_id in result.stdout
    assert registry.get_task(task_id=task_id).state.value == "waiting"


def test_tasks_start_ctrl_c_stops_only_observation_after_service_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    calls: list[str] = []
    status = _service_status(tmp_path)
    original_start = cli_main.run_start_analytical_task

    def _fake_start_service(
        *,
        core_config_service: CoreConfigService,
        runner_module: str,
    ) -> ServiceStartResult:
        _ = core_config_service
        assert runner_module == "jelica_cli.service_runner"
        calls.append("service")
        return ServiceStartResult(status=status, already_running=False, launched_pid=12345)

    def _wrapped_start(*args: Any, **kwargs: Any) -> Any:
        _ = args
        calls.append("task")
        assert kwargs["detached"] is True
        assert kwargs["background_runner_module"] == "jelica_cli.service_runner"
        return original_start(**kwargs)

    def _fake_background_runtime(
        *,
        jelica_home: Path | None = None,
        runner_module: str,
    ) -> int:
        _ = jelica_home
        assert runner_module == "jelica_cli.service_runner"
        return 12345

    def _interrupt_watch(
        _service: Any,
        _callback: Any,
        *,
        stop_condition: Any | None = None,
        wait_for_observed_rows: bool = False,
    ) -> Any:
        _ = (stop_condition, wait_for_observed_rows)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "start_service", _fake_start_service)
    monkeypatch.setattr(cli_main, "run_start_analytical_task", _wrapped_start)
    monkeypatch.setattr(core_operations, "launch_background_runtime", _fake_background_runtime)
    monkeypatch.setattr(cli_main.TaskWatchService, "watch", _interrupt_watch)

    result = _invoke_cli(args=["tasks", "start", task_id], jelica_home=jelica_home)

    assert result.exit_code == 130
    assert calls == ["service", "task"]
    assert f"Watching stopped. Task {task_id} continues running." in result.stdout
    assert _registry_service(jelica_home).get_task(task_id=task_id).state.value == "queued"


def test_tasks_resume_ctrl_c_stops_only_observation_after_service_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    registry = _registry_service(jelica_home)
    queued = registry.start(task_id=task_id)
    assert queued.result_type.value == "applied"
    paused = registry.pause(task_id=task_id)
    assert paused.result_type.value == "applied"
    calls: list[str] = []
    status = _service_status(tmp_path)
    original_resume = cli_main.run_resume_analytical_task

    def _fake_start_service(
        *,
        core_config_service: CoreConfigService,
        runner_module: str,
    ) -> ServiceStartResult:
        _ = core_config_service
        assert runner_module == "jelica_cli.service_runner"
        calls.append("service")
        return ServiceStartResult(status=status, already_running=True)

    def _wrapped_resume(*args: Any, **kwargs: Any) -> Any:
        _ = args
        calls.append("task")
        assert kwargs["detached"] is True
        assert kwargs["background_runner_module"] == "jelica_cli.service_runner"
        return original_resume(**kwargs)

    def _fake_background_runtime(
        *,
        jelica_home: Path | None = None,
        runner_module: str,
    ) -> int:
        _ = jelica_home
        assert runner_module == "jelica_cli.service_runner"
        return 12345

    def _interrupt_watch(
        _service: Any,
        _callback: Any,
        *,
        stop_condition: Any | None = None,
        wait_for_observed_rows: bool = False,
    ) -> Any:
        _ = (stop_condition, wait_for_observed_rows)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "start_service", _fake_start_service)
    monkeypatch.setattr(cli_main, "run_resume_analytical_task", _wrapped_resume)
    monkeypatch.setattr(core_operations, "launch_background_runtime", _fake_background_runtime)
    monkeypatch.setattr(cli_main.TaskWatchService, "watch", _interrupt_watch)

    result = _invoke_cli(args=["tasks", "resume", task_id], jelica_home=jelica_home)

    assert result.exit_code == 130
    assert calls == ["service", "task"]
    assert f"Watching stopped. Task {task_id} continues running." in result.stdout
    assert registry.get_task(task_id=task_id).state.value == "queued"


def test_analyze_ctrl_c_stops_only_observation_of_service_owned_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    calls: list[str] = []
    status = _service_status(tmp_path)
    original_start = cli_main.run_start_analytical_task

    def _fake_start_service(
        *,
        core_config_service: CoreConfigService,
        runner_module: str,
    ) -> ServiceStartResult:
        _ = core_config_service
        assert runner_module == "jelica_cli.service_runner"
        calls.append("service")
        return ServiceStartResult(status=status, already_running=False, launched_pid=12345)

    def _wrapped_start(*args: Any, **kwargs: Any) -> Any:
        _ = args
        calls.append("task")
        assert kwargs["detached"] is True
        assert kwargs["background_runner_module"] == "jelica_cli.service_runner"
        return original_start(**kwargs)

    def _fake_background_runtime(
        *,
        jelica_home: Path | None = None,
        runner_module: str,
    ) -> int:
        _ = jelica_home
        assert runner_module == "jelica_cli.service_runner"
        return 12345

    def _interrupt_watch(
        _service: Any,
        _callback: Any,
        *,
        stop_condition: Any | None = None,
        wait_for_observed_rows: bool = False,
    ) -> Any:
        _ = (stop_condition, wait_for_observed_rows)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "start_service", _fake_start_service)
    monkeypatch.setattr(cli_main, "run_start_analytical_task", _wrapped_start)
    monkeypatch.setattr(core_operations, "launch_background_runtime", _fake_background_runtime)
    monkeypatch.setattr(cli_main.TaskWatchService, "watch", _interrupt_watch)

    result = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)

    task_id = _single_task_id(jelica_home)
    assert result.exit_code == 130
    assert calls == ["service", "task"]
    assert f"Watching stopped. Task {task_id} continues running." in result.stdout
    assert _registry_service(jelica_home).get_task(task_id=task_id).state.value == "queued"


def test_analyze_streams_live_events_and_progress_before_runtime_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">live\nACGT\n", encoding="utf-8")
    runtime_threads = _install_inline_runtime_service(
        monkeypatch=monkeypatch,
        expected_jelica_home=jelica_home,
        tmp_path=tmp_path,
    )

    runtime_entered_first_barrier = threading.Event()
    runtime_entered_second_barrier = threading.Event()
    allow_runtime_progress = threading.Event()
    allow_runtime_finish = threading.Event()
    stage_started_rendered = threading.Event()
    comparative_progress_rendered = threading.Event()
    command_finished = threading.Event()
    result_holder: dict[str, Any] = {}
    rendered_messages: list[str] = []

    original_terminal_event = cli_terminal.TerminalPresenter.event
    original_watch_poll = cli_main.TaskWatchService.poll

    def _capturing_terminal_event(
        self: Any,
        event: Event,
        *,
        mode: cli_terminal.TerminalMode = cli_terminal.TerminalMode.STANDARD,
    ) -> None:
        original_terminal_event(self, event, mode=mode)
        message = cli_terminal._standard_event_message(event)
        if message is not None:
            rendered_messages.append(message)
        if message == "Stage started: input_processing":
            stage_started_rendered.set()

    def _capturing_watch_poll(self: Any) -> Any:
        update = original_watch_poll(self)
        if any(row.stage == "comparative_analysis · pairwise 1/3" for row in update.rows):
            comparative_progress_rendered.set()
        return update

    def _controlled_runtime_run(self: Any, *, auto_queue_waiting_jobs: bool = False) -> Any:
        _ = auto_queue_waiting_jobs
        claimed = self._registry_service.claim_next_queued_job_for_worker(
            worker_instance_id="test-worker",
            lease_token="test-worker-lease",
            lease_timeout_seconds=self._runtime_config.lease_timeout_seconds,
        )
        assert claimed is not None
        task_record, job_record = claimed
        task_id = task_record.task_id
        job_id = job_record.job_id
        self._emit(
            core_operations.RUNTIME_EVENT_JOB_CLAIMED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
            },
        )
        self._registry_service.update_active_job_progress(
            task_id=task_id,
            progress=15,
            current_stage="input_processing",
        )
        self._emit(
            core_operations.RUNTIME_EVENT_STAGE_STARTED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
                "stage_id": "input_processing",
            },
        )
        self._emit(
            "INPUT_PROCESSING_STARTED",
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
                "input_file_count": 1,
            },
        )
        runtime_entered_first_barrier.set()
        if not allow_runtime_progress.wait(timeout=10):
            raise RuntimeError("timed out waiting for progress release in test runtime")
        self._emit(
            "COMPARATIVE_ANALYSIS_PROGRESS",
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
                "operation_kind": "pairwise_comparison",
                "completed": 1,
                "total": 3,
            },
        )
        self._registry_service.update_active_job_progress(
            task_id=task_id,
            progress=70,
            current_stage="comparative_analysis",
        )
        runtime_entered_second_barrier.set()
        if not allow_runtime_finish.wait(timeout=10):
            raise RuntimeError("timed out waiting for finish release in test runtime")
        self._emit(
            core_operations.RUNTIME_EVENT_STAGE_COMMITTED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
                "stage_id": "comparative_analysis",
            },
        )
        self._registry_service.update_active_job_progress(
            task_id=task_id,
            progress=100,
            current_stage=None,
        )
        completed = self._registry_service.transition_active_job_state(
            task_id=task_id,
            to_state=core_operations.AnalyticalTaskState.COMPLETED,
        )
        assert completed.result_type is core_operations.AnalyticalTaskMutationResultType.APPLIED
        self._emit(
            core_operations.RUNTIME_EVENT_JOB_COMPLETED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
            },
        )
        return core_operations.RuntimeContinueResult(
            runtime_instance_id=self._runtime_instance_id,
            recovered_jobs=0,
            claimed_jobs=1,
            completed_jobs=1,
            failed_jobs=0,
            interrupted=False,
        )

    def _run_cli() -> None:
        result_holder["result"] = _invoke_cli(
            args=["analyze", str(sample)],
            jelica_home=jelica_home,
        )
        command_finished.set()

    monkeypatch.setattr(cli_terminal.TerminalPresenter, "event", _capturing_terminal_event)
    monkeypatch.setattr(cli_main.TaskWatchService, "poll", _capturing_watch_poll)
    monkeypatch.setattr(core_operations.ExecutionRuntime, "run", _controlled_runtime_run)

    cli_thread = threading.Thread(target=_run_cli, name="analyze-live-temporal-test")
    cli_thread.start()
    try:
        assert runtime_entered_first_barrier.wait(timeout=10)
        assert stage_started_rendered.wait(timeout=5)
        assert not command_finished.is_set()

        allow_runtime_progress.set()
        assert runtime_entered_second_barrier.wait(timeout=10)
        assert comparative_progress_rendered.wait(timeout=5)
        assert not command_finished.is_set()

        allow_runtime_finish.set()
        cli_thread.join(timeout=15)
        assert not cli_thread.is_alive()
    finally:
        allow_runtime_progress.set()
        allow_runtime_finish.set()
        cli_thread.join(timeout=15)
        for runtime_thread in runtime_threads:
            runtime_thread.join(timeout=15)

    result = result_holder["result"]
    assert all(not runtime_thread.is_alive() for runtime_thread in runtime_threads)
    assert result.exit_code == 0
    assert "Stage started: input_processing" in result.stdout
    assert "Task " in result.stdout and " completed." in result.stdout
    assert rendered_messages.count("Stage started: input_processing") == 1
    stage_started_index = rendered_messages.index("Stage started: input_processing")
    task_completed_index = next(
        index
        for index, message in enumerate(rendered_messages)
        if message.startswith("Task ") and message.endswith(" completed.")
    )
    assert stage_started_index < task_completed_index


def test_analyze_uses_persistent_service_and_completes_without_cli_owned_runtime(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")

    result = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)

    assert result.exit_code == 0
    task_id = _extract_started_task_id(result.stdout)
    registry = _registry_service(jelica_home)
    snapshot = registry.get_task_snapshot(task_id=task_id)
    job = snapshot.active_or_latest_job
    assert job is not None
    assert snapshot.task.state.value == "completed"
    assert snapshot.task.active_job_id is None
    assert job.state.value == "completed"
    assert job.progress == 100
    assert job.first_started_at is not None
    assert job.last_started_at is not None

    resolved = _load_resolved_core_config(jelica_home)
    stage_manifest_path = (
        resolved.tasks_dir
        / task_id
        / "jobs"
        / job.job_id
        / "stages"
        / "initialize_job"
        / "stage_manifest.json"
    )
    assert stage_manifest_path.is_file()

    service_status = _invoke_cli(
        args=["service", "status"],
        jelica_home=jelica_home,
    )
    assert service_status.exit_code == 0
    assert "status: running" in service_status.stdout
    assert "unknown field 'cli'" not in result.stdout
    assert "Unexpected CLI error" not in result.stdout


def test_analyze_terminal_before_first_watch_update_completes_cleanly(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">fast\nACGT\n", encoding="utf-8")
    result = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)

    assert result.exit_code == 0
    task_id = _extract_started_task_id(result.stdout)
    snapshot = _registry_service(jelica_home).get_task_snapshot(task_id=task_id)
    job = snapshot.active_or_latest_job
    assert job is not None
    assert snapshot.task.state.value == "completed"
    assert job.state.value == "completed"
    assert f"Task {task_id} completed." in result.stdout


def test_analyze_service_startup_failure_is_reported_and_does_not_hang(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")

    def _failing_service_start(
        *,
        core_config_service: CoreConfigService,
        runner_module: str,
    ) -> ServiceStartResult:
        _ = core_config_service
        assert runner_module == "jelica_cli.service_runner"
        raise cli_main.ServiceError("simulated Service startup failure")

    monkeypatch.setattr(cli_main, "start_service", _failing_service_start)

    result = _invoke_cli(
        args=["analyze", str(sample), "--verbose"],
        jelica_home=jelica_home,
    )
    assert result.exit_code != 0
    assert _single_task_id(jelica_home) in result.stdout
    assert "simulated Service startup failure" in result.stdout
    assert "Traceback" not in result.stdout


def test_analyze_runtime_failure_after_emitting_events_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">failed\nACGT\n", encoding="utf-8")
    runtime_threads = _install_inline_runtime_service(
        monkeypatch=monkeypatch,
        expected_jelica_home=jelica_home,
        tmp_path=tmp_path,
    )

    def _runtime_fails_after_events(self: Any, *, auto_queue_waiting_jobs: bool = False) -> Any:
        _ = auto_queue_waiting_jobs
        claimed = self._registry_service.claim_next_queued_job_for_worker(
            worker_instance_id="test-worker",
            lease_token="test-worker-lease",
            lease_timeout_seconds=self._runtime_config.lease_timeout_seconds,
        )
        assert claimed is not None
        task_record, job_record = claimed
        task_id = task_record.task_id
        job_id = job_record.job_id
        self._registry_service.update_active_job_progress(
            task_id=task_id,
            progress=30,
            current_stage="input_processing",
        )
        self._emit(
            core_operations.RUNTIME_EVENT_STAGE_STARTED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
                "stage_id": "input_processing",
            },
        )
        failed = self._registry_service.transition_active_job_state(
            task_id=task_id,
            to_state=core_operations.AnalyticalTaskState.FAILED,
            finished_reason="simulated runtime failure",
        )
        assert failed.result_type is core_operations.AnalyticalTaskMutationResultType.APPLIED
        self._emit(
            core_operations.RUNTIME_EVENT_JOB_FAILED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
                "detail": "simulated runtime failure",
                "reason": "simulated runtime failure",
            },
        )
        return core_operations.RuntimeContinueResult(
            runtime_instance_id=self._runtime_instance_id,
            recovered_jobs=0,
            claimed_jobs=1,
            completed_jobs=0,
            failed_jobs=1,
            interrupted=False,
        )

    monkeypatch.setattr(core_operations.ExecutionRuntime, "run", _runtime_fails_after_events)
    result = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)
    for runtime_thread in runtime_threads:
        runtime_thread.join(timeout=10)

    assert result.exit_code != 0
    assert all(not runtime_thread.is_alive() for runtime_thread in runtime_threads)
    assert _single_task_id(jelica_home) in result.stdout
    assert "simulated runtime failure" in result.stdout
    assert "Traceback" not in result.stdout


def test_analyze_watcher_failure_does_not_stop_service_owned_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">watcher\nACGT\n", encoding="utf-8")
    runtime_threads = _install_inline_runtime_service(
        monkeypatch=monkeypatch,
        expected_jelica_home=jelica_home,
        tmp_path=tmp_path,
    )

    runtime_started = threading.Event()
    allow_runtime_finish = threading.Event()

    def _runtime_waits_for_release(self: Any, *, auto_queue_waiting_jobs: bool = False) -> Any:
        _ = auto_queue_waiting_jobs
        claimed = self._registry_service.claim_next_queued_job_for_worker(
            worker_instance_id="test-worker",
            lease_token="test-worker-lease",
            lease_timeout_seconds=self._runtime_config.lease_timeout_seconds,
        )
        assert claimed is not None
        task_record, job_record = claimed
        task_id = task_record.task_id
        job_id = job_record.job_id
        self._emit(
            core_operations.RUNTIME_EVENT_STAGE_STARTED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
                "stage_id": "input_processing",
            },
        )
        runtime_started.set()
        if not allow_runtime_finish.wait(timeout=10):
            raise RuntimeError("timed out waiting for runtime release in watcher cleanup test")
        self._registry_service.update_active_job_progress(
            task_id=task_id,
            progress=100,
            current_stage=None,
        )
        completed = self._registry_service.transition_active_job_state(
            task_id=task_id,
            to_state=core_operations.AnalyticalTaskState.COMPLETED,
        )
        assert completed.result_type is core_operations.AnalyticalTaskMutationResultType.APPLIED
        self._emit(
            core_operations.RUNTIME_EVENT_JOB_COMPLETED,
            {
                "runtime_instance_id": self._runtime_instance_id,
                "task_id": task_id,
                "job_id": job_id,
            },
        )
        return core_operations.RuntimeContinueResult(
            runtime_instance_id=self._runtime_instance_id,
            recovered_jobs=0,
            claimed_jobs=1,
            completed_jobs=1,
            failed_jobs=0,
            interrupted=False,
        )

    def _watcher_failure(
        *,
        service: Any,
        task_ids: tuple[str, ...],
        mode: Any,
        render: bool,
        include_explicit_inactive: bool = False,
        stop_condition: Any | None = None,
        wait_for_initial_rows: bool = False,
    ) -> Any:
        _ = (
            service,
            task_ids,
            mode,
            render,
            include_explicit_inactive,
            stop_condition,
            wait_for_initial_rows,
        )
        raise RuntimeError("simulated watcher failure")

    monkeypatch.setattr(core_operations.ExecutionRuntime, "run", _runtime_waits_for_release)
    monkeypatch.setattr(cli_main, "_run_watch_session", _watcher_failure)

    result = _invoke_cli(
        args=["analyze", str(sample)],
        jelica_home=jelica_home,
    )
    try:
        assert runtime_started.wait(timeout=10)
        task_id = _single_task_id(jelica_home)
        assert _registry_service(jelica_home).get_task(task_id=task_id).state.value == "running"
        assert result.exit_code != 0
        assert "Cannot watch task" in result.stdout
        assert "simulated watcher failure" in result.stdout

        allow_runtime_finish.set()
        for runtime_thread in runtime_threads:
            runtime_thread.join(timeout=15)
        assert all(not runtime_thread.is_alive() for runtime_thread in runtime_threads)
        assert _registry_service(jelica_home).get_task(task_id=task_id).state.value == "completed"
    finally:
        allow_runtime_finish.set()
        for runtime_thread in runtime_threads:
            runtime_thread.join(timeout=15)


def test_analyze_respects_cli_color_and_emoji_disable_for_live_output(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    set_color = _invoke_cli(
        args=["config", "set", "cli.color", "false"],
        jelica_home=jelica_home,
    )
    set_emoji = _invoke_cli(
        args=["config", "set", "cli.emoji", "false"],
        jelica_home=jelica_home,
    )
    assert set_color.exit_code == 0
    assert set_emoji.exit_code == 0

    sample = tmp_path / "sample.fasta"
    sample.write_text(">plain\nACGT\n", encoding="utf-8")
    result = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert "\u001b[" not in result.stdout
    assert "🌲" not in result.stdout
    assert "🎄" not in result.stdout
    first_non_empty_line = next(line for line in result.stdout.splitlines() if line.strip() != "")
    assert first_non_empty_line.startswith("Analysis task ")
    assert "Stage started:" in result.stdout


def test_analyze_with_existing_queued_job_preserves_watch_target_and_completes_both(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    queued_sample = tmp_path / "queued.fasta"
    queued_sample.write_text(">queued\nACGT\n", encoding="utf-8")
    queued_task_id = _initialize_task_without_start(
        jelica_home=jelica_home,
        sample_paths=[queued_sample],
    )
    queued_result = _registry_service(jelica_home).start(task_id=queued_task_id)
    assert queued_result.result_type.value == "applied"

    analyze_sample = tmp_path / "analyze.fasta"
    analyze_sample.write_text(">analyze\nACGA\n", encoding="utf-8")
    result = _invoke_cli(args=["analyze", str(analyze_sample)], jelica_home=jelica_home)

    assert result.exit_code == 0
    analyze_task_id = _extract_started_task_id(result.stdout)
    assert analyze_task_id != queued_task_id
    assert queued_task_id not in result.stdout

    registry = _registry_service(jelica_home)
    queued_snapshot = registry.get_task_snapshot(task_id=queued_task_id)
    analyze_snapshot = registry.get_task_snapshot(task_id=analyze_task_id)
    queued_job = queued_snapshot.active_or_latest_job
    analyze_job = analyze_snapshot.active_or_latest_job
    assert queued_job is not None
    assert analyze_job is not None
    assert queued_job.state.value == "completed"
    assert analyze_job.state.value == "completed"
    assert queued_job.first_started_at is not None
    assert analyze_job.first_started_at is not None


def test_service_start_processes_prequeued_job_and_remains_running(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">queued\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    registry = _registry_service(jelica_home)
    queued = registry.start(task_id=task_id)
    assert queued.result_type.value == "applied"
    before = registry.get_task_snapshot(task_id=task_id).active_or_latest_job
    assert before is not None
    assert before.state.value == "queued"
    assert before.first_started_at is None

    result = _invoke_cli(args=["service", "start"], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert "status: running" in result.stdout

    completed = _wait_for_task_state(
        registry=registry,
        task_id=task_id,
        expected_state="completed",
    )
    after = completed.active_or_latest_job
    assert after is not None
    assert after.state.value == "completed"
    assert after.first_started_at is not None
    assert after.last_started_at is not None


def test_analyze_text_reports_single_sample_input_processing_summary(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">single\nACGT\n", encoding="utf-8")

    result = _invoke_cli(args=["analyze", str(sample)], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert result.stdout.count("Input processing started: 1 files") == 1
    assert result.stdout.count("Input processing completed: 1 valid, 0 invalid.") == 1
    assert "Alignment skipped:" in result.stdout
    assert "completed." in result.stdout


def test_analyze_text_reports_dataset_ready_but_comparative_not_executed(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    first = tmp_path / "first.fasta"
    second = tmp_path / "second.fasta"
    first.write_text(">a\nACGT\n", encoding="utf-8")
    second.write_text(">b\nACGA\n", encoding="utf-8")

    result = _invoke_cli(args=["analyze", str(first), str(second)], jelica_home=jelica_home)

    assert result.exit_code == 0
    assert "Input processing completed: 2 valid, 0 invalid." in result.stdout
    assert "Alignment result published." in result.stdout
    assert "Alignment completed." in result.stdout


def test_analyze_text_reports_dataset_validation_failure_with_manifest_path(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    invalid = tmp_path / "invalid.fasta"
    invalid.write_text(">bad\nXXXX\n", encoding="utf-8")

    result = _invoke_cli(args=["analyze", str(invalid)], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "Error:" in result.stdout
    assert result.stdout.count("dataset validation failed") == 1
    assert "input_processing/input_processing_manifest.json" in result.stdout
    assert "failed" in result.stdout
    assert "Traceback" not in result.stdout


def test_tasks_watch_text_reports_completed_task_without_adding_it_to_table(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">watch\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    start_result = _invoke_cli(args=["tasks", "start", task_id], jelica_home=jelica_home)
    assert start_result.exit_code == 0

    watch_result = _invoke_cli(args=["tasks", "watch", task_id], jelica_home=jelica_home)
    assert watch_result.exit_code == 0
    assert f"Task {task_id} is completed; it was not added to watch." in watch_result.stdout
    assert "Input processing" not in watch_result.stdout


def test_runtime_event_renderer_reports_failed_input_file_without_completed_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_main._render_input_processing_runtime_event(
        event_name="INPUT_PROCESSING_FILE_PROCESSED",
        context={
            "file_index": 2,
            "total_file_count": 2,
            "processing_status": "failed",
            "primary_issue_code": "input_file_not_found",
            "primary_issue_message": (
                "Materialized file was not found: inputs/files/0002_inline_sequence.fasta."
            ),
        },
    )

    output = capsys.readouterr().out
    assert "Error: Input file 2/2 failed (malformed_input_file)." in output
    assert "Materialized file was not found: inputs/files/0002_inline_sequence.fasta." in output
    assert "completed" not in output.lower()


def test_manifest_summary_reports_failed_input_file_counts(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    monkeypatch.setenv("JELICA_HOME", str(jelica_home))
    resolved = _load_resolved_core_config(jelica_home)
    task_id = "task-summary"
    job_id = "job-summary"
    manifest_path = (
        resolved.tasks_dir
        / task_id
        / "jobs"
        / job_id
        / "stages"
        / "input_processing"
        / "input_processing"
        / "input_processing_manifest.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_payload = {
        "dataset_summary": {
            "discovered_record_count": 1,
            "valid_sample_count": 1,
            "invalid_sample_count": 0,
            "unique_sequence_count": 1,
            "duplicate_logical_sample_count": 0,
            "comparative_analysis_available": False,
        },
        "processed_files": [
            {
                "relative_path": "inputs/files/0001_NC_000913.3.gb",
                "status": "processed",
                "validation_issues": [],
            },
            {
                "relative_path": "inputs/files/0002_inline_sequence.fasta",
                "status": "failed",
                "validation_issues": [
                    {
                        "code": "malformed_input_file",
                        "message": "Materialized file was not found.",
                    }
                ],
            },
        ],
        "dataset_issues": [],
    }
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summary_context = cli_main._load_input_processing_summary_context(
        task_id=task_id,
        job_id=job_id,
    )
    assert summary_context is not None
    cli_main._print_input_processing_terminal_summary(context=summary_context)
    output = capsys.readouterr().out
    assert "Input processing completed: 1 valid, 0 invalid." in output
    assert "Warning: 1 input files failed." in output


def test_config_show_masks_ncbi_api_key(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    default_show = _invoke_cli(args=["config", "show"], jelica_home=jelica_home)
    assert default_show.exit_code == 0
    assert json.loads(default_show.stdout)["ncbi_api_key"] == "<not configured>"

    set_result = _invoke_cli(
        args=["config", "set", "ncbi_api_key", "super-secret-key"],
        jelica_home=jelica_home,
    )
    assert set_result.exit_code == 0

    configured_show = _invoke_cli(args=["config", "show"], jelica_home=jelica_home)
    assert configured_show.exit_code == 0
    payload = json.loads(configured_show.stdout)
    assert payload["ncbi_api_key"] == "<configured>"
    assert "super-secret-key" not in configured_show.stdout


def test_analyze_accepts_direct_inline_sequence_of_length_128(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(args=["analyze", "A" * 128], jelica_home=jelica_home)

    assert result.exit_code == 0
    task_config = json.loads(_single_task_config_path(jelica_home).read_text(encoding="utf-8"))
    assert task_config["samples"] == ["A" * 128]


def test_analyze_rejects_direct_inline_sequence_of_length_129(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)

    result = _invoke_cli(args=["analyze", "A" * 129], jelica_home=jelica_home)

    assert result.exit_code != 0
    assert "Direct inline sequence input via CLI is limited to 128" in result.stdout
    assert "Traceback" not in result.stdout


def test_tasks_samples_list_add_and_remove(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])
    task_reference = _task_name(jelica_home=jelica_home, task_id=task_id).upper()

    initial_list = _invoke_cli(
        args=["tasks", "samples", "list", task_reference],
        jelica_home=jelica_home,
    )
    assert initial_list.exit_code == 0
    assert "samples_count: 1" in initial_list.stdout
    assert f"0: {sample}" in initial_list.stdout

    added = _invoke_cli(
        args=["tasks", "samples", "add", task_reference, "ACGT ACGT", str(sample)],
        jelica_home=jelica_home,
    )
    assert added.exit_code == 0

    listed_after_add = _invoke_cli(
        args=["tasks", "samples", "list", task_reference],
        jelica_home=jelica_home,
    )
    assert listed_after_add.exit_code == 0
    assert "samples_count: 3" in listed_after_add.stdout
    assert "inline_sequence(length=8" in listed_after_add.stdout
    assert "ACGT ACGT" not in listed_after_add.stdout

    removed = _invoke_cli(
        args=["tasks", "samples", "remove", task_reference, "1", "1"],
        jelica_home=jelica_home,
    )
    assert removed.exit_code == 0

    listed_after_remove = _invoke_cli(
        args=["tasks", "samples", "list", task_reference],
        jelica_home=jelica_home,
    )
    assert listed_after_remove.exit_code == 0
    assert "samples_count: 2" in listed_after_remove.stdout
    assert "inline_sequence(" not in listed_after_remove.stdout


def test_tasks_samples_remove_rejects_invalid_index(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    result = _invoke_cli(
        args=["tasks", "samples", "remove", task_id, "9"],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "out of range" in result.stdout


def test_tasks_samples_update_is_rejected_for_completed_task(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    sample = tmp_path / "sample.fasta"
    sample.write_text(">a\nACGT\n", encoding="utf-8")
    task_id = _initialize_task_without_start(jelica_home=jelica_home, sample_paths=[sample])

    started = _invoke_cli(args=["tasks", "start", task_id], jelica_home=jelica_home)
    assert started.exit_code == 0

    result = _invoke_cli(
        args=["tasks", "samples", "add", task_id, "ACGT"],
        jelica_home=jelica_home,
    )

    assert result.exit_code != 0
    assert "Error:" in result.stdout
    assert "Task update was rejected" in result.stdout
    assert "completed task config is immutable" in result.stdout


def test_results_validate_returns_zero_for_valid_package(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    package_path = tmp_path / "valid.jelica"
    _build_validation_package(package_path, broken_manifest=False)

    result = _invoke_cli(
        args=["results", "validate", str(package_path)],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    assert "Valid JELICA result package" in result.stdout
    assert "Format version: 1.0" in result.stdout
    assert "Content ID: sha256:" in result.stdout


def test_results_validate_returns_non_zero_and_privacy_safe_errors(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    package_path = tmp_path / "invalid.jelica"
    _build_validation_package(package_path, broken_manifest=True)

    result = _invoke_cli(
        args=["results", "validate", str(package_path)],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 1
    assert "Invalid JELICA result package" in result.stdout
    assert "[content_id_mismatch]" in result.stdout
    assert "Traceback" not in result.stdout
    assert str(tmp_path) not in result.stdout


def test_results_validate_uses_color_for_success_and_failure_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    monkeypatch.setattr(
        cli_main,
        "create_terminal_presenter",
        lambda *, color, emoji: cli_terminal.create_terminal_presenter(
            color=color,
            emoji=emoji,
            force_terminal=True,
        ),
    )
    valid_path = tmp_path / "valid.jelica"
    _build_validation_package(valid_path, broken_manifest=False)
    valid_result = _invoke_cli(
        args=["results", "validate", str(valid_path)],
        jelica_home=jelica_home,
        color=True,
    )
    assert valid_result.exit_code == 0
    assert re.search(r"\x1b\[[0-9;]*32mValid JELICA result package", valid_result.stdout)

    invalid_path = tmp_path / "invalid.jelica"
    _build_validation_package(invalid_path, broken_manifest=True)
    invalid_result = _invoke_cli(
        args=["results", "validate", str(invalid_path)],
        jelica_home=jelica_home,
        color=True,
    )
    assert invalid_result.exit_code == 1
    assert re.search(r"\x1b\[[0-9;]*31mInvalid JELICA result package", invalid_result.stdout)


def test_results_import_new_and_repeat_are_successful(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    package_path = tmp_path / "package.jelica"
    content_id = _build_validation_package(package_path, broken_manifest=False)
    digest = content_digest_from_content_id(content_id)

    first = _invoke_cli(
        args=["results", "import", str(package_path)],
        jelica_home=jelica_home,
    )
    assert first.exit_code == 0
    assert "JELICA result package imported" in first.stdout
    assert f"Content ID: {content_id}" in first.stdout
    assert (_result_packages_dir(jelica_home) / f"{digest}.jelica").is_file()

    second = _invoke_cli(
        args=["results", "import", str(package_path)],
        jelica_home=jelica_home,
    )
    assert second.exit_code == 0
    assert "JELICA result package already exists" in second.stdout
    assert f"Content ID: {content_id}" in second.stdout


def test_results_import_rejects_notes_conflict(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    first_path = tmp_path / "first.jelica"
    second_path = tmp_path / "second.jelica"
    content_id_a = _build_validation_package(first_path, notes=b"a\n")
    content_id_b = _build_validation_package(second_path, notes=b"b\n")
    assert content_id_a == content_id_b
    first_import = _invoke_cli(
        args=["results", "import", str(first_path)],
        jelica_home=jelica_home,
    )
    assert first_import.exit_code == 0

    second_import = _invoke_cli(
        args=["results", "import", str(second_path)],
        jelica_home=jelica_home,
    )
    assert second_import.exit_code != 0
    assert "[notes_conflict]" in second_import.stdout
    assert "Traceback" not in second_import.stdout


def test_results_list_outputs_expected_fields(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    package_path = tmp_path / "package.jelica"
    content_id = _build_validation_package(package_path)
    imported = _invoke_cli(
        args=["results", "import", str(package_path)],
        jelica_home=jelica_home,
    )
    assert imported.exit_code == 0

    listed = _invoke_cli(args=["results", "list"], jelica_home=jelica_home)
    assert listed.exit_code == 0
    assert "File name:" in listed.stdout
    assert f"Content ID: {content_id}" in listed.stdout
    assert "Task ID: task-1" in listed.stdout
    assert "Status: completed" in listed.stdout
    assert "Format version: 1.0" in listed.stdout


def test_results_path_outputs_path_for_content_and_task(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    _run_non_interactive_init(jelica_home)
    package_path = tmp_path / "package.jelica"
    content_id = _build_validation_package(package_path)
    imported = _invoke_cli(
        args=["results", "import", str(package_path)],
        jelica_home=jelica_home,
    )
    assert imported.exit_code == 0
    digest = content_digest_from_content_id(content_id)
    expected_path = _result_packages_dir(jelica_home) / f"{digest}.jelica"

    by_content = _invoke_cli(
        args=["results", "path", content_id],
        jelica_home=jelica_home,
    )
    assert by_content.exit_code == 0
    assert by_content.stdout.strip() == str(expected_path.resolve(strict=False))

    _register_task_with_result_package_link(
        jelica_home=jelica_home,
        task_id="00000000-0000-4000-8000-000000000091",
        name="task-for-result",
        content_id=content_id,
    )
    by_task = _invoke_cli(
        args=["results", "path", "TASK-FOR-RESULT"],
        jelica_home=jelica_home,
    )
    assert by_task.exit_code == 0
    assert by_task.stdout.strip() == str(expected_path.resolve(strict=False))


def test_results_export_help_uses_canonical_equals_forms(tmp_path: Path) -> None:
    result = _invoke_cli(
        args=["results", "export", "--help"],
        jelica_home=tmp_path / "home",
    )

    assert result.exit_code == 0
    assert "--format=pdf" in result.stdout
    assert "--output=report.pdf" in result.stdout
    assert "--open=true" in result.stdout


def test_results_export_with_format_equals_creates_pdf(tmp_path: Path, monkeypatch: Any) -> None:
    jelica_home, _, content_id = _prepare_imported_package_for_export(tmp_path)
    digest = content_digest_from_content_id(content_id)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)

    result = _invoke_cli(
        args=["results", "export", content_id, "--format=pdf"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    assert "PDF report created" in result.stdout
    output_path = _extract_export_path(result.stdout)
    expected_path = (cwd / f"jelica-report-{digest}.pdf").resolve(strict=False)
    assert output_path == expected_path
    assert output_path.is_file()
    assert output_path.read_bytes().startswith(b"%PDF-")


def test_results_export_default_output_name_is_based_on_digest_for_all_source_types(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home, package_path, content_id = _prepare_imported_package_for_export(tmp_path)
    digest = content_digest_from_content_id(content_id)
    task_id = "00000000-0000-4000-8000-000000000092"
    task_name = "task-export-default-name"
    _register_task_with_result_package_link(
        jelica_home=jelica_home,
        task_id=task_id,
        name=task_name,
        content_id=content_id,
    )
    expected_name = f"jelica-report-{digest}.pdf"
    source_refs = (
        content_id,
        digest,
        task_name,
        str(package_path),
    )
    result_packages_dir = _result_packages_dir(jelica_home).resolve(strict=False)

    for index, source_ref in enumerate(source_refs):
        cwd = tmp_path / f"cwd-{index}"
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        result = _invoke_cli(
            args=["results", "export", source_ref, "--format=pdf"],
            jelica_home=jelica_home,
        )
        assert result.exit_code == 0
        output_path = _extract_export_path(result.stdout)
        expected_path = (cwd / expected_name).resolve(strict=False)
        assert output_path == expected_path
        assert output_path.is_file()
        assert result_packages_dir not in output_path.parents


def test_results_export_explicit_relative_output_is_resolved_from_cwd(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home, _, content_id = _prepare_imported_package_for_export(tmp_path)
    cwd = tmp_path / "cwd"
    reports_dir = cwd / "reports"
    reports_dir.mkdir(parents=True)
    monkeypatch.chdir(cwd)

    result = _invoke_cli(
        args=[
            "results",
            "export",
            content_id,
            "--format=pdf",
            "--output=reports/analysis-report.pdf",
        ],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    output_path = _extract_export_path(result.stdout)
    expected_path = (reports_dir / "analysis-report.pdf").resolve(strict=False)
    assert output_path == expected_path
    assert output_path.is_file()


def test_results_export_rejects_missing_output_parent_directory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home, _, content_id = _prepare_imported_package_for_export(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)

    result = _invoke_cli(
        args=[
            "results",
            "export",
            content_id,
            "--format=pdf",
            "--output=missing/report.pdf",
        ],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 1
    assert "[output_directory_not_found]" in result.stdout
    assert not (cwd / "missing").exists()


def test_results_export_replaces_existing_output_after_successful_render(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home, _, content_id = _prepare_imported_package_for_export(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    output_path = cwd / "analysis-report.pdf"
    output_path.write_bytes(b"old-pdf-bytes")

    result = _invoke_cli(
        args=[
            "results",
            "export",
            content_id,
            "--format=pdf",
            "--output=analysis-report.pdf",
        ],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    refreshed_payload = output_path.read_bytes()
    assert refreshed_payload != b"old-pdf-bytes"
    assert refreshed_payload.startswith(b"%PDF-")


def test_results_export_keeps_existing_output_when_render_fails(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home, _, content_id = _prepare_imported_package_for_export(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    output_path = cwd / "analysis-report.pdf"
    output_path.write_bytes(b"old-pdf-bytes")

    def failing_export(*, package_path: Path, output: str | None) -> Path:
        _ = (package_path, output)
        raise ReportExportError(
            code=ReportExportErrorCode.PDF_RENDER_FAILED,
            message="forced render failure",
        )

    monkeypatch.setattr(
        cli_results_export,
        "_export_report_pdf",
        failing_export,
    )
    result = _invoke_cli(
        args=[
            "results",
            "export",
            content_id,
            "--format=pdf",
            "--output=analysis-report.pdf",
        ],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 1
    assert "[pdf_render_failed]" in result.stdout
    assert output_path.read_bytes() == b"old-pdf-bytes"


def test_results_export_open_defaults_to_false_and_does_not_call_opener(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home, _, content_id = _prepare_imported_package_for_export(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    opener_calls: list[Path] = []

    def fake_open_report(path: Path) -> ReportOpenResult:
        opener_calls.append(path)
        return ReportOpenResult(opened=True)

    monkeypatch.setattr(cli_results_export, "open_report_file", fake_open_report)
    result = _invoke_cli(
        args=["results", "export", content_id, "--format=pdf"],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    assert opener_calls == []


def test_results_export_open_true_calls_opener_once_with_published_absolute_path(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home, _, content_id = _prepare_imported_package_for_export(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    observed_paths: list[Path] = []
    observed_files: list[bool] = []
    observed_pdf_headers: list[bool] = []

    def fake_open_report(path: Path) -> ReportOpenResult:
        observed_paths.append(path)
        observed_files.append(path.is_file())
        observed_pdf_headers.append(path.read_bytes().startswith(b"%PDF-"))
        return ReportOpenResult(opened=True)

    monkeypatch.setattr(cli_results_export, "open_report_file", fake_open_report)
    result = _invoke_cli(
        args=[
            "results",
            "export",
            content_id,
            "--format=pdf",
            "--output=analysis-report.pdf",
            "--open=true",
        ],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    expected_path = (cwd / "analysis-report.pdf").resolve(strict=False)
    assert observed_paths == [expected_path]
    assert observed_files == [True]
    assert observed_pdf_headers == [True]
    assert ".tmp" not in str(observed_paths[0])
    assert "Report opened in the default application." in result.stdout


def test_results_export_does_not_call_opener_when_generation_fails(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home, _, content_id = _prepare_imported_package_for_export(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    opener_calls: list[Path] = []

    def fake_open_report(path: Path) -> ReportOpenResult:
        opener_calls.append(path)
        return ReportOpenResult(opened=True)

    def failing_export(*, package_path: Path, output: str | None) -> Path:
        _ = (package_path, output)
        raise ReportExportError(
            code=ReportExportErrorCode.PDF_RENDER_FAILED,
            message="forced render failure",
        )

    monkeypatch.setattr(cli_results_export, "open_report_file", fake_open_report)
    monkeypatch.setattr(
        cli_results_export,
        "_export_report_pdf",
        failing_export,
    )
    result = _invoke_cli(
        args=[
            "results",
            "export",
            content_id,
            "--format=pdf",
            "--open=true",
        ],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 1
    assert opener_calls == []


def test_results_export_keeps_pdf_and_warns_when_auto_open_fails(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home, _, content_id = _prepare_imported_package_for_export(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)

    def failing_open_report(path: Path) -> ReportOpenResult:
        _ = path
        return ReportOpenResult(
            opened=False,
            warning_code=ReportOpenWarningCode.REPORT_OPEN_FAILED,
        )

    monkeypatch.setattr(cli_results_export, "open_report_file", failing_open_report)
    result = _invoke_cli(
        args=[
            "results",
            "export",
            content_id,
            "--format=pdf",
            "--output=analysis-report.pdf",
            "--open=true",
        ],
        jelica_home=jelica_home,
    )

    assert result.exit_code == 0
    output_path = (cwd / "analysis-report.pdf").resolve(strict=False)
    assert output_path.is_file()
    assert "PDF report created" in result.stdout
    assert (
        "Warning: [report_open_failed] The report could not be opened automatically."
        in result.stdout
    )
    assert "Traceback" not in result.stdout


def test_open_report_file_uses_macos_open_command(monkeypatch: Any, tmp_path: Path) -> None:
    report_path = (tmp_path / "report.pdf").resolve(strict=False)
    report_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    observed: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> Any:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(cli_results_export.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli_results_export.subprocess, "Popen", fake_popen)
    result = cli_results_export.open_report_file(report_path)

    assert result.opened is True
    assert observed["args"] == ["open", str(report_path)]
    assert "shell" not in observed["kwargs"]


def test_open_report_file_uses_windows_startfile(monkeypatch: Any, tmp_path: Path) -> None:
    report_path = (tmp_path / "report.pdf").resolve(strict=False)
    report_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    observed_paths: list[str] = []

    def fake_startfile(path: str) -> None:
        observed_paths.append(path)

    def fail_popen(*args: Any, **kwargs: Any) -> Any:
        _ = (args, kwargs)
        raise AssertionError("subprocess.Popen must not be used on Windows branch")

    monkeypatch.setattr(cli_results_export.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli_results_export.os, "startfile", fake_startfile, raising=False)
    monkeypatch.setattr(cli_results_export.subprocess, "Popen", fail_popen)
    result = cli_results_export.open_report_file(report_path)

    assert result.opened is True
    assert observed_paths == [str(report_path)]


def test_open_report_file_uses_xdg_open_on_linux(monkeypatch: Any, tmp_path: Path) -> None:
    report_path = (tmp_path / "report.pdf").resolve(strict=False)
    report_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    observed: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> Any:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(cli_results_export.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli_results_export.subprocess, "Popen", fake_popen)
    result = cli_results_export.open_report_file(report_path)

    assert result.opened is True
    assert observed["args"] == ["xdg-open", str(report_path)]
    assert "shell" not in observed["kwargs"]
