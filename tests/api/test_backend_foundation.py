from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response, status
from starlette.requests import Request

from jelica_api.api.routes import health_router
from jelica_api.api.routes.health import health_check
from jelica_api.api.routes.internal_reconciliation import (
    get_reconciliation_report,
    run_reconciliation,
)
from jelica_api.api.routes.tasks import (
    create_task,
    get_task_result,
    get_task_status,
    list_tasks,
)
from jelica_api.app import create_app
from jelica_api.cli import (
    JelicaCliClient,
    JelicaCliCommandError,
    JelicaCliInvocationError,
    MachineErrorPayload,
    MachineResponseEnvelope,
)
from jelica_api.contracts import (
    TaskResultPackageReference,
    TaskStatusSnapshot,
    TaskSubmissionRequest,
    TaskSubmissionResult,
)
from jelica_api.database import DatabaseUnavailableError
from jelica_api.settings import ApiSettings, ApiSettingsError, load_api_settings
from jelica_api.task_access import WebTaskActor
from jelica_api.task_reconciliation import ReconciliationDiagnostics, ReconciliationReport


def _settings() -> ApiSettings:
    return ApiSettings(
        app_name="JELICA Web Backend",
        api_host="127.0.0.1",
        api_port=8000,
        database_url="postgresql+psycopg://jelica:secret@localhost:5432/jelica_web",
        cli_command_prefix=("jelica",),
        cli_timeout_seconds=30.0,
    )


def _request_for_app(
    app: object,
    *,
    path: str,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers or [],
        "app": app,
    }
    return Request(scope)


def test_load_api_settings_rejects_non_postgresql_url() -> None:
    with pytest.raises(ApiSettingsError, match="PostgreSQL"):
        load_api_settings(
            {
                "DATABASE_URL": "sqlite:///tmp/jelica.db",
            }
        )


def test_create_app_registers_health_routes() -> None:
    app = create_app(settings=_settings())
    openapi_paths = set(app.openapi().get("paths", {}).keys())
    assert "/health" in openapi_paths
    hidden_paths = {route.path for route in health_router.routes if hasattr(route, "path")}
    assert "/api/health" in hidden_paths
    app.state.jelica_api_state.task_orchestrator.shutdown()
    app.state.jelica_api_state.engine.dispose()


def test_health_check_reports_ok_when_database_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings=_settings())
    monkeypatch.setattr("jelica_api.api.routes.health.probe_database", lambda *, engine: None)

    request = _request_for_app(app, path="/health")
    response = Response()
    payload = health_check(request, response)

    assert response.status_code == status.HTTP_200_OK
    assert payload.status == "ok"
    assert payload.database == "ok"
    app.state.jelica_api_state.task_orchestrator.shutdown()
    app.state.jelica_api_state.engine.dispose()


def test_health_check_reports_degraded_when_database_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings=_settings())

    def _raise_probe_error(*, engine: object) -> None:
        _ = engine
        raise DatabaseUnavailableError("db offline")

    monkeypatch.setattr("jelica_api.api.routes.health.probe_database", _raise_probe_error)

    request = _request_for_app(app, path="/health")
    response = Response()
    payload = health_check(request, response)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert payload.status == "degraded"
    assert payload.database == "error"
    assert payload.detail is not None
    app.state.jelica_api_state.task_orchestrator.shutdown()
    app.state.jelica_api_state.engine.dispose()


def test_health_check_includes_reconciliation_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings=_settings())
    monkeypatch.setattr("jelica_api.api.routes.health.probe_database", lambda *, engine: None)
    last_run_at = datetime.now(UTC)
    app.state.jelica_api_state.web_task_reconciler._last_diagnostics = ReconciliationDiagnostics(
        scanned=7,
        updated=3,
        errors=1,
        last_run_at=last_run_at,
    )

    request = _request_for_app(app, path="/health")
    response = Response()
    payload = health_check(request, response)

    assert response.status_code == status.HTTP_200_OK
    assert payload.reconciliation.scanned == 7
    assert payload.reconciliation.updated == 3
    assert payload.reconciliation.errors == 1
    assert payload.reconciliation.last_run_at == last_run_at
    app.state.jelica_api_state.task_orchestrator.shutdown()
    app.state.jelica_api_state.engine.dispose()


def test_cli_client_appends_machine_flag_and_parses_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_command: list[str] = []

    def _fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = (capture_output, text, check, timeout)
        captured_command.extend(command)
        payload = {
            "machine_protocol_version": "1",
            "jelica_version": "0.1.0",
            "trace_id": None,
            "command_id": "cmd-1",
            "ok": True,
            "data": {"value": "ok"},
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("jelica_api.cli.client.subprocess.run", _fake_run)
    client = JelicaCliClient(command_prefix=("jelica",), default_timeout_seconds=30.0)
    envelope = client.run_machine_command(args=("config", "path"))

    assert envelope.ok is True
    assert "--machine" in captured_command


def test_cli_client_create_and_start_task_extracts_task_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = (command, capture_output, text, check, timeout)
        payload = {
            "machine_protocol_version": "1",
            "jelica_version": "0.1.0",
            "trace_id": "trace-1",
            "command_id": "cmd-2",
            "ok": True,
            "data": {
                "task": {"task_id": "task-123"},
                "execution": {"runtime_instance_id": "runtime-1"},
                "final_state": "completed",
            },
        }
        return subprocess.CompletedProcess(
            ["jelica", "analyze", "--machine"],
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("jelica_api.cli.client.subprocess.run", _fake_run)
    client = JelicaCliClient(command_prefix=("jelica",), default_timeout_seconds=30.0)
    result = client.create_and_start_task(
        request=TaskSubmissionRequest(
            sources=("sample.fasta",),
        )
    )

    assert result.task_id == "task-123"
    assert result.final_state == "completed"
    assert result.trace_id == "trace-1"


def test_cli_client_create_and_start_task_uses_no_watch_for_submit_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_command: list[str] = []

    def _fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = (capture_output, text, check, timeout)
        captured_command.extend(command)
        payload = {
            "machine_protocol_version": "1",
            "jelica_version": "0.1.0",
            "trace_id": "trace-1",
            "command_id": "cmd-2",
            "ok": True,
            "data": {
                "task": {"task_id": "task-123"},
                "final_state": "running",
            },
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("jelica_api.cli.client.subprocess.run", _fake_run)
    client = JelicaCliClient(command_prefix=("jelica",), default_timeout_seconds=30.0)
    result = client.create_and_start_task(
        request=TaskSubmissionRequest(sources=("sample.fasta",)),
        wait_for_completion=False,
    )

    assert "--no-watch" in captured_command
    assert result.final_state == "running"


def test_cli_client_get_task_status_extracts_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = (command, capture_output, text, check, timeout)
        payload = {
            "machine_protocol_version": "1",
            "jelica_version": "0.1.0",
            "trace_id": "trace-2",
            "command_id": "cmd-3",
            "ok": True,
            "data": {
                "count": 1,
                "tasks": [
                    {
                        "task_id": "task-123",
                        "state": "running",
                        "trace_id": "trace-2",
                        "active_or_latest_job": {
                            "state": "running",
                            "current_stage": "align",
                            "progress": 48,
                        },
                    }
                ],
            },
        }
        return subprocess.CompletedProcess(
            ["jelica", "tasks", "show", "task-123", "--machine"],
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("jelica_api.cli.client.subprocess.run", _fake_run)
    client = JelicaCliClient(command_prefix=("jelica",), default_timeout_seconds=30.0)
    result = client.get_task_status(task_reference="task-123")

    assert result.task_id == "task-123"
    assert result.trace_id == "trace-2"
    assert result.state == "running"
    assert result.current_stage == "align"
    assert result.progress == 48
    assert result.command_id == "cmd-3"


def test_cli_client_resolve_result_reference_extracts_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = (command, capture_output, text, check, timeout)
        payload = {
            "machine_protocol_version": "1",
            "jelica_version": "0.1.0",
            "trace_id": None,
            "command_id": "cmd-4",
            "ok": True,
            "data": {
                "content_id": "sha256:abc",
                "path": "/tmp/result.jelica",
            },
        }
        return subprocess.CompletedProcess(
            ["jelica", "results", "path", "task-123", "--machine"],
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("jelica_api.cli.client.subprocess.run", _fake_run)
    client = JelicaCliClient(command_prefix=("jelica",), default_timeout_seconds=30.0)
    result = client.resolve_result_package_reference(task_reference="task-123")

    assert result.content_id == "sha256:abc"
    assert result.package_path == "/tmp/result.jelica"
    assert result.command_id == "cmd-4"


def test_cli_client_find_task_by_trace_id_returns_matching_active_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = (command, capture_output, text, check, timeout)
        payload = {
            "machine_protocol_version": "1",
            "jelica_version": "0.1.0",
            "trace_id": None,
            "command_id": "cmd-list",
            "ok": True,
            "data": {
                "count": 2,
                "tasks": [
                    {
                        "task_id": "task-inactive",
                        "state": "waiting",
                        "trace_id": "trace-123",
                        "active_or_latest_job": None,
                    },
                    {
                        "task_id": "task-active",
                        "state": "running",
                        "trace_id": "trace-123",
                        "active_or_latest_job": {
                            "state": "running",
                            "current_stage": "align",
                            "progress": 12,
                        },
                    },
                ],
            },
        }
        return subprocess.CompletedProcess(
            ["jelica", "tasks", "list", "--machine"],
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("jelica_api.cli.client.subprocess.run", _fake_run)
    client = JelicaCliClient(command_prefix=("jelica",), default_timeout_seconds=30.0)
    match = client.find_task_by_trace_id(trace_id="trace-123", require_active_job=True)

    assert match is not None
    assert match.task_id == "task-active"
    assert match.state == "running"
    assert match.current_stage == "align"
    assert match.command_id == "cmd-list"


def test_create_app_registers_tasks_routes() -> None:
    app = create_app(settings=_settings())
    openapi = app.openapi()
    openapi_paths = set(openapi.get("paths", {}).keys())
    assert "/api/tasks" in openapi_paths
    assert "/api/tasks/{task_id}" in openapi_paths
    assert "/api/tasks/{task_id}/result" in openapi_paths
    assert "/api/internal/reconciliation/run" not in openapi_paths
    assert "/api/internal/reconciliation/report" not in openapi_paths
    task_list_parameters = {
        parameter["name"]: parameter["schema"]
        for parameter in openapi["paths"]["/api/tasks"]["get"]["parameters"]
    }
    project_id_variants = task_list_parameters["project_id"].get(
        "anyOf", [task_list_parameters["project_id"]]
    )
    assert any(variant.get("type") == "array" for variant in project_id_variants)
    assert {"project", "owner", "state"}.issubset(task_list_parameters)
    assert "owner_user_id" not in task_list_parameters
    app.state.jelica_api_state.task_orchestrator.shutdown()
    app.state.jelica_api_state.engine.dispose()


def test_list_tasks_endpoint_returns_projection_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection_store = _ProjectionStoreStub()
    first_created_at = datetime(2026, 8, 23, 19, 0, tzinfo=UTC)
    first_updated_at = datetime(2026, 8, 23, 19, 5, tzinfo=UTC)
    second_created_at = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)
    second_updated_at = datetime(2026, 8, 23, 20, 15, tzinfo=UTC)
    projection_store.list_records = (
        _ProjectionRecord(
            core_task_id="task-2",
            name="Task 2",
            status="completed",
            created_at=second_created_at,
            updated_at=second_updated_at,
        ),
        _ProjectionRecord(
            core_task_id="task-1",
            name="Task 1",
            status="running",
            created_at=first_created_at,
            updated_at=first_updated_at,
        ),
    )
    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=object(), projection_store=projection_store),
    )

    request = _request_for_app(object(), path="/api/tasks")
    payload = list_tasks(request)

    assert projection_store.list_recent_calls == 1
    assert projection_store.last_list_filters == {
        "actor": WebTaskActor(),
        "project_ids": (),
        "project_none": False,
        "owner_user_id": None,
        "states": (),
    }
    assert [item.task_id for item in payload.items] == ["task-2", "task-1"]
    assert payload.items[0].created_at == second_created_at
    assert payload.items[0].updated_at == second_updated_at
    assert payload.items[0].state_source == "projection_cache"
    assert payload.items[0].authoritative is False
    assert payload.items[0].projection_updated_at == second_updated_at
    assert payload.items[0].stale_state is True


def test_list_tasks_endpoint_returns_empty_items_for_empty_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection_store = _ProjectionStoreStub()
    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=object(), projection_store=projection_store),
    )

    request = _request_for_app(object(), path="/api/tasks")
    payload = list_tasks(request)

    assert projection_store.list_recent_calls == 1
    assert payload.items == ()


def test_list_tasks_endpoint_forwards_project_none_and_owner_me_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection_store = _ProjectionStoreStub()
    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=object(), projection_store=projection_store),
    )
    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.require_current_user",
        lambda _request: SimpleNamespace(user_id="user-1"),
    )

    payload = list_tasks(
        _request_for_app(object(), path="/api/tasks"),
        project="none",
        owner="me",
    )

    assert payload.items == ()
    assert projection_store.last_list_filters == {
        "actor": WebTaskActor(user_id="user-1"),
        "project_ids": (),
        "project_none": True,
        "owner_user_id": "user-1",
        "states": (),
    }


def test_list_tasks_endpoint_rejects_conflicting_project_filters() -> None:
    with pytest.raises(HTTPException) as raised:
        list_tasks(
            _request_for_app(object(), path="/api/tasks"),
            project_id=["project-1", "project-2"],
            project="none",
        )

    assert raised.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert raised.value.detail["error"] == "task_project_filter_conflict"


@pytest.mark.parametrize("project_id", ["", "   "])
def test_list_tasks_endpoint_rejects_blank_project_id(project_id: str) -> None:
    with pytest.raises(HTTPException) as raised:
        list_tasks(
            _request_for_app(object(), path="/api/tasks"),
            project_id=[project_id],
        )

    assert raised.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert raised.value.detail["error"] == "task_project_id_invalid"


def test_list_tasks_owner_me_requires_authentication() -> None:
    app = create_app(settings=_settings())
    try:
        with pytest.raises(HTTPException) as raised:
            list_tasks(_request_for_app(app, path="/api/tasks"), owner="me")
        assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert raised.value.detail["error"] == "authentication_required"
    finally:
        app.state.jelica_api_state.task_orchestrator.shutdown()
        app.state.jelica_api_state.engine.dispose()


def test_list_tasks_forwards_multiple_authorized_project_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection_store = _ProjectionStoreStub()
    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: SimpleNamespace(web_task_projection_store=projection_store),
    )
    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.optional_current_user",
        lambda _request: SimpleNamespace(user_id="user-1"),
    )

    payload = list_tasks(
        _request_for_app(object(), path="/api/tasks"),
        project_id=["project-1", "project-2"],
    )

    assert payload.items == ()
    assert projection_store.last_list_filters == {
        "actor": WebTaskActor(user_id="user-1"),
        "project_ids": ("project-1", "project-2"),
        "project_none": False,
        "owner_user_id": None,
        "states": (),
    }


def test_list_tasks_endpoint_contract_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection_store = _ProjectionStoreStub()
    created_at = datetime(2026, 8, 23, 21, 0, tzinfo=UTC)
    updated_at = datetime(2026, 8, 23, 21, 5, tzinfo=UTC)
    projection_store.list_records = (
        _ProjectionRecord(
            core_task_id="task-stable",
            name=None,
            status="failed",
            created_at=created_at,
            updated_at=updated_at,
        ),
    )
    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=object(), projection_store=projection_store),
    )

    request = _request_for_app(object(), path="/api/tasks")
    payload = list_tasks(request)
    dumped = payload.model_dump(mode="json")

    assert set(dumped.keys()) == {"items"}
    assert len(dumped["items"]) == 1
    assert set(dumped["items"][0].keys()) == {
        "task_id",
        "owner_user_id",
        "project_id",
        "trace_id",
        "state",
        "active_job_state",
        "current_stage",
        "progress",
        "command_id",
        "created_at",
        "updated_at",
        "state_source",
        "authoritative",
        "projection_updated_at",
        "stale_state",
        "detail",
    }
    assert dumped["items"][0]["task_id"] == "task-stable"
    assert dumped["items"][0]["state"] == "failed"
    assert dumped["items"][0]["state_source"] == "projection_cache"


def test_create_task_endpoint_returns_submission_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = TaskSubmissionResult(
        task_id="task-1",
        final_state="completed",
        trace_id="trace-1",
        command_id="cmd-1",
    )

    class StubOrchestrator:
        def submit_task(
            self,
            *,
            request: TaskSubmissionRequest,
            guest_session_hash: str,
        ) -> TaskSubmissionResult:
            assert request.sources == ("sample.fasta",)
            assert len(guest_session_hash) == 64
            return expected

    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: SimpleNamespace(
            task_orchestrator=StubOrchestrator(),
            settings=SimpleNamespace(auth_cookie_secure=False),
        ),
    )

    request = _request_for_app(object(), path="/api/tasks")
    payload = create_task(
        TaskSubmissionRequest(sources=("sample.fasta",)),
        request,
        Response(),
    )
    assert payload == expected


def test_create_task_endpoint_passes_authenticated_owner_to_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class StubOrchestrator:
        def submit_task(
            self,
            *,
            request: TaskSubmissionRequest,
            owner_user_id: str | None = None,
        ) -> TaskSubmissionResult:
            calls.append({"request": request, "owner_user_id": owner_user_id})
            return TaskSubmissionResult(
                task_id="task-owned",
                final_state="running",
                trace_id="trace-owned",
                command_id="cmd-owned",
            )

    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: SimpleNamespace(task_orchestrator=StubOrchestrator()),
    )
    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.optional_current_user",
        lambda _request: SimpleNamespace(user_id="user-1"),
    )
    request = _request_for_app(object(), path="/api/tasks")
    submitted = TaskSubmissionRequest(sources=("sample.fasta",))

    payload = create_task(submitted, request, Response())

    assert payload.task_id == "task-owned"
    assert calls == [{"request": submitted, "owner_user_id": "user-1"}]


def test_get_task_status_endpoint_maps_missing_task_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            _ = task_reference
            raise _command_error(name="CORE_ANALYTICAL_TASK_NOT_FOUND")

    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=StubCli()),
    )

    request = _request_for_app(object(), path="/api/tasks/missing")
    with pytest.raises(HTTPException) as raised:
        get_task_status("missing", request)
    assert raised.value.status_code == status.HTTP_404_NOT_FOUND
    assert raised.value.detail["error"] == "CORE_ANALYTICAL_TASK_NOT_FOUND"


def test_get_task_result_endpoint_returns_unavailable_for_non_completed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_snapshot = TaskStatusSnapshot(
        task_id="task-1",
        trace_id="trace-1",
        state="running",
        active_job_state="running",
        current_stage="align",
        progress=25,
        command_id="cmd-status",
    )

    class StubCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            _ = task_reference
            return status_snapshot

        def resolve_result_package_reference(
            self, *, task_reference: str
        ) -> TaskResultPackageReference:
            _ = task_reference
            raise AssertionError("result lookup should not be called for non-completed task")

    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=StubCli()),
    )

    request = _request_for_app(object(), path="/api/tasks/task-1/result")
    payload = get_task_result("task-1", request)
    assert payload.available is False
    assert payload.state == "running"
    assert payload.result_reference is None


def test_get_task_result_endpoint_returns_reference_for_completed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_snapshot = TaskStatusSnapshot(
        task_id="task-1",
        trace_id="trace-1",
        state="completed",
        active_job_state="completed",
        current_stage="completed",
        progress=100,
        command_id="cmd-status",
    )
    result_reference = TaskResultPackageReference(
        content_id="sha256:abc",
        package_path="/tmp/result.jelica",
        command_id="cmd-result",
    )

    class StubCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            _ = task_reference
            return status_snapshot

        def resolve_result_package_reference(
            self, *, task_reference: str
        ) -> TaskResultPackageReference:
            assert task_reference == "task-1"
            return result_reference

    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=StubCli()),
    )

    request = _request_for_app(object(), path="/api/tasks/task-1/result")
    payload = get_task_result("task-1", request)
    assert payload.available is True
    assert payload.result_reference == result_reference


def test_get_task_result_endpoint_maps_result_task_not_found_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_snapshot = TaskStatusSnapshot(
        task_id="task-1",
        trace_id="trace-1",
        state="completed",
        active_job_state="completed",
        current_stage="completed",
        progress=100,
        command_id="cmd-status",
    )

    class StubCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            _ = task_reference
            return status_snapshot

        def resolve_result_package_reference(
            self, *, task_reference: str
        ) -> TaskResultPackageReference:
            _ = task_reference
            raise _command_error(
                name="CLI_RESULT_PACKAGE_RESOLUTION_FAILED",
                details={"result_package_error_code": "task_not_found"},
            )

    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=StubCli()),
    )

    request = _request_for_app(object(), path="/api/tasks/task-1/result")
    with pytest.raises(HTTPException) as raised:
        get_task_result("task-1", request)
    assert raised.value.status_code == status.HTTP_404_NOT_FOUND


def test_get_task_status_endpoint_syncs_projection_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_snapshot = TaskStatusSnapshot(
        task_id="task-1",
        trace_id="trace-1",
        state="running",
        active_job_state="running",
        current_stage="align",
        progress=42,
        command_id="cmd-status",
    )

    class StubCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            assert task_reference == "task-1"
            return status_snapshot

    state = _state_for_routes(cli_client=StubCli())
    monkeypatch.setattr("jelica_api.api.routes.tasks.get_app_state", lambda _request: state)

    request = _request_for_app(object(), path="/api/tasks/task-1")
    payload = get_task_status("task-1", request)

    assert payload == status_snapshot
    assert state.web_task_projection_store.calls == [
        {"core_task_id": "task-1", "name": None, "status": "running"}
    ]


def test_get_task_status_endpoint_maps_interrupted_error_to_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            _ = task_reference
            raise _command_error(name="CLI_COMMAND_INTERRUPTED")

    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=StubCli()),
    )

    request = _request_for_app(object(), path="/api/tasks/interrupted")
    with pytest.raises(HTTPException) as raised:
        get_task_status("interrupted", request)
    assert raised.value.status_code == status.HTTP_409_CONFLICT


def test_get_task_status_endpoint_returns_cached_projection_when_cli_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            _ = task_reference
            raise JelicaCliInvocationError("runtime unavailable")

    projection_store = _ProjectionStoreStub()
    projection_updated_at = datetime.now(UTC)
    projection_store.cached_projection = _ProjectionRecord(
        core_task_id="task-1",
        name="Cached task",
        status="running",
        created_at=datetime.now(UTC),
        updated_at=projection_updated_at,
    )
    monkeypatch.setattr(
        "jelica_api.api.routes.tasks.get_app_state",
        lambda _request: _state_for_routes(cli_client=StubCli(), projection_store=projection_store),
    )

    request = _request_for_app(object(), path="/api/tasks/task-1")
    payload = get_task_status("task-1", request)
    assert payload.task_id == "task-1"
    assert payload.state == "running"
    assert payload.state_source == "projection_cache"
    assert payload.authoritative is False
    assert payload.command_id is None
    assert payload.projection_updated_at == projection_updated_at
    assert payload.stale_state is True


def test_run_reconciliation_endpoint_returns_current_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_run_at = datetime.now(UTC)

    class StubReconciler:
        def __init__(self) -> None:
            self.run_calls = 0

        def reconcile(self) -> ReconciliationReport:
            self.run_calls += 1
            return ReconciliationReport(
                scanned=4,
                updated=2,
                unchanged=1,
                errors=1,
            )

        def get_diagnostics(self) -> ReconciliationDiagnostics:
            return ReconciliationDiagnostics(
                scanned=4,
                updated=2,
                errors=1,
                last_run_at=last_run_at,
            )

    stub_reconciler = StubReconciler()
    monkeypatch.setattr(
        "jelica_api.api.routes.internal_reconciliation.get_app_state",
        lambda _request: _internal_state(stub_reconciler),
    )

    request = _internal_request(path="/api/internal/reconciliation/run", method="POST")
    payload = run_reconciliation(request)

    assert stub_reconciler.run_calls == 1
    assert payload.scanned == 4
    assert payload.updated == 2
    assert payload.errors == 1
    assert payload.last_run_at == last_run_at


def test_get_reconciliation_report_endpoint_returns_without_new_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_run_at = datetime.now(UTC)

    class StubReconciler:
        def __init__(self) -> None:
            self.run_calls = 0

        def get_diagnostics(self) -> ReconciliationDiagnostics:
            return ReconciliationDiagnostics(
                scanned=5,
                updated=3,
                errors=0,
                last_run_at=last_run_at,
            )

    stub_reconciler = StubReconciler()
    monkeypatch.setattr(
        "jelica_api.api.routes.internal_reconciliation.get_app_state",
        lambda _request: _internal_state(stub_reconciler),
    )

    request = _internal_request(path="/api/internal/reconciliation/report")
    payload = get_reconciliation_report(request)

    assert stub_reconciler.run_calls == 0
    assert payload.scanned == 5
    assert payload.updated == 3
    assert payload.errors == 0
    assert payload.last_run_at == last_run_at


def test_run_reconciliation_endpoint_maps_failure_to_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubReconciler:
        def reconcile(self) -> ReconciliationReport:
            raise RuntimeError("db temporarily unavailable")

        def get_diagnostics(self) -> ReconciliationDiagnostics:
            return ReconciliationDiagnostics(
                scanned=0,
                updated=0,
                errors=0,
                last_run_at=None,
            )

    monkeypatch.setattr(
        "jelica_api.api.routes.internal_reconciliation.get_app_state",
        lambda _request: _internal_state(StubReconciler()),
    )

    request = _internal_request(path="/api/internal/reconciliation/run", method="POST")
    with pytest.raises(HTTPException) as raised:
        run_reconciliation(request)
    assert raised.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert raised.value.detail["error"] == "reconciliation_failed"


def _internal_state(reconciler: object) -> SimpleNamespace:
    return SimpleNamespace(
        web_task_reconciler=reconciler,
        settings=SimpleNamespace(internal_api_enabled=True, internal_api_token="test-secret"),
    )


def _internal_request(*, path: str, method: str = "GET") -> Request:
    return _request_for_app(
        object(),
        path=path,
        method=method,
        headers=[(b"x-jelica-internal-token", b"test-secret")],
    )


def _state_for_routes(
    *,
    cli_client: object,
    projection_store: "_ProjectionStoreStub | None" = None,
) -> SimpleNamespace:
    resolved_projection_store = (
        _ProjectionStoreStub() if projection_store is None else projection_store
    )
    return SimpleNamespace(
        cli_client=cli_client,
        web_task_projection_store=resolved_projection_store,
        settings=SimpleNamespace(auth_cookie_secure=False),
    )


class _ProjectionStoreStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []
        self.cached_projection: _ProjectionRecord | None = None
        self.list_recent_calls = 0
        self.last_list_filters: dict[str, object] | None = None
        self.list_records: tuple[_ProjectionRecord, ...] = ()

    def upsert_task(
        self,
        *,
        core_task_id: str,
        name: str | None,
        status: str,
    ) -> None:
        self.calls.append(
            {
                "core_task_id": core_task_id,
                "name": name,
                "status": status,
            }
        )

    def get_task(self, *, core_task_id: str) -> "_ProjectionRecord | None":
        if self.cached_projection is None:
            return None
        if self.cached_projection.core_task_id != core_task_id:
            return None
        return self.cached_projection

    def get_visible_task(
        self,
        *,
        core_task_id: str,
        actor: WebTaskActor,
    ) -> "_ProjectionRecord | None":
        _ = actor
        if self.cached_projection is not None:
            return self.get_task(core_task_id=core_task_id)
        now = datetime.now(UTC)
        return _ProjectionRecord(
            core_task_id=core_task_id,
            name=None,
            status="running",
            created_at=now,
            updated_at=now,
        )

    def list_recent_tasks(
        self,
        *,
        actor: WebTaskActor,
        project_ids: tuple[str, ...] = (),
        project_none: bool = False,
        owner_user_id: str | None = None,
        states: tuple[str, ...] = (),
    ) -> tuple["_ProjectionRecord", ...]:
        self.list_recent_calls += 1
        self.last_list_filters = {
            "actor": actor,
            "project_ids": project_ids,
            "project_none": project_none,
            "owner_user_id": owner_user_id,
            "states": states,
        }
        return self.list_records


class _ProjectionRecord:
    def __init__(
        self,
        *,
        core_task_id: str,
        name: str | None,
        status: str,
        created_at: datetime,
        updated_at: datetime,
        project_id: str | None = None,
    ) -> None:
        self.core_task_id = core_task_id
        self.name = name
        self.status = status
        self.created_at = created_at
        self.updated_at = updated_at
        self.project_id = project_id


def _command_error(
    *,
    name: str,
    message: str = "failed",
    code: int = 1,
    details: dict[str, object] | None = None,
) -> JelicaCliCommandError:
    envelope = MachineResponseEnvelope(
        machine_protocol_version="1",
        jelica_version="0.1.0",
        trace_id=None,
        command_id="cmd-error",
        ok=False,
        error=MachineErrorPayload(
            code=code,
            name=name,
            message=message,
            details={} if details is None else details,
        ),
    )
    return JelicaCliCommandError(envelope=envelope)
