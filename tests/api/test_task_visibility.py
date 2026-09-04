from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from jelica_api.api.routes.tasks import (
    GUEST_SESSION_COOKIE_NAME,
    create_task,
    get_task_result,
    get_task_status,
    list_tasks,
)
from jelica_api.app import create_app
from jelica_api.auth import hash_opaque_token
from jelica_api.contracts import (
    TaskResultPackageReference,
    TaskStatusSnapshot,
    TaskSubmissionRequest,
    TaskSubmissionResult,
)
from jelica_api.models import AuthSession, Base, ProjectMember, User, WebTask
from jelica_api.settings import ApiSettings
from jelica_api.task_orchestration import TaskOrchestrator


@dataclass(frozen=True, slots=True)
class _TaskHarness:
    app: FastAPI
    user_ids: dict[str, str]
    session_tokens: dict[str, str]
    cli: _TaskCliStub


class _TaskCliStub:
    def __init__(self) -> None:
        self.counter = 0
        self.states: dict[str, str] = {}
        self.status_calls: list[str] = []

    def create_and_start_task(
        self,
        *,
        request: TaskSubmissionRequest,
        wait_for_completion: bool = True,
        timeout_seconds: float | None = None,
    ) -> TaskSubmissionResult:
        _ = (wait_for_completion, timeout_seconds)
        self.counter += 1
        task_id = f"task-{self.counter}"
        self.states[task_id] = "running"
        return TaskSubmissionResult(
            task_id=task_id,
            final_state="running",
            trace_id=request.trace_id,
            command_id=f"command-{self.counter}",
        )

    def find_task_by_trace_id(
        self,
        *,
        trace_id: str,
        require_active_job: bool,
        timeout_seconds: float | None = None,
        page_limit: int = 200,
    ) -> TaskStatusSnapshot | None:
        _ = (trace_id, require_active_job, timeout_seconds, page_limit)
        return None

    def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
        self.status_calls.append(task_reference)
        task_state = self.states[task_reference]
        return TaskStatusSnapshot(
            task_id=task_reference,
            trace_id=f"trace-{task_reference}",
            state=task_state,
            active_job_state=task_state,
            current_stage=None,
            progress=None,
            command_id=f"status-{task_reference}",
        )

    def resolve_result_package_reference(
        self,
        *,
        task_reference: str,
    ) -> TaskResultPackageReference:
        return TaskResultPackageReference(
            content_id=f"sha256:{task_reference}",
            package_path=f"/tmp/{task_reference}.jelica",
            command_id=f"result-{task_reference}",
        )


@pytest.fixture
def task_harness() -> Iterator[_TaskHarness]:
    app = create_app(
        settings=ApiSettings(
            app_name="JELICA Web Backend",
            api_host="127.0.0.1",
            api_port=8000,
            database_url="sqlite+pysqlite:///:memory:",
            cli_command_prefix=("jelica",),
            cli_timeout_seconds=30.0,
            auth_cookie_secure=True,
        )
    )
    state = app.state.jelica_api_state
    Base.metadata.create_all(state.engine)
    now = datetime.now(UTC)
    user_ids: dict[str, str] = {}
    session_tokens: dict[str, str] = {}
    with state.session_factory() as session:
        for label in ("alice", "bob", "carol"):
            user = User(
                username=label,
                email=f"{label}@example.org",
                password_hash="test-password-hash",
                email_verified=True,
                language="en",
            )
            session.add(user)
            session.flush()
            token = f"{label}-session-token"
            session.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=hash_opaque_token(token),
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                    last_used_at=now,
                )
            )
            user_ids[label] = user.id
            session_tokens[label] = token
        session.commit()

    cli = _TaskCliStub()
    app.state.jelica_api_state = replace(
        state,
        cli_client=cli,
        task_orchestrator=TaskOrchestrator(
            cli_client=cli,
            projection_store=state.web_task_projection_store,
        ),
    )
    try:
        yield _TaskHarness(
            app=app,
            user_ids=user_ids,
            session_tokens=session_tokens,
            cli=cli,
        )
    finally:
        app.state.jelica_api_state.task_orchestrator.shutdown()
        state.engine.dispose()


def test_guest_tasks_are_isolated_and_never_claimed_on_login(
    task_harness: _TaskHarness,
) -> None:
    guest_a_response = Response()
    guest_a_task = create_task(
        TaskSubmissionRequest(sources=("guest-a.fasta",)),
        _request(task_harness.app, path="/api/tasks", method="POST"),
        guest_a_response,
    )
    guest_a_token = _guest_cookie_value(guest_a_response)
    set_cookie = guest_a_response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Path=/" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" in set_cookie

    guest_b_response = Response()
    guest_b_task = create_task(
        TaskSubmissionRequest(sources=("guest-b.fasta",)),
        _request(task_harness.app, path="/api/tasks", method="POST"),
        guest_b_response,
    )
    guest_b_token = _guest_cookie_value(guest_b_response)
    assert guest_a_token != guest_b_token

    state = task_harness.app.state.jelica_api_state
    with state.session_factory() as session:
        guest_a_row = session.execute(
            select(WebTask).where(WebTask.core_task_id == guest_a_task.task_id)
        ).scalar_one()
        guest_b_row = session.execute(
            select(WebTask).where(WebTask.core_task_id == guest_b_task.task_id)
        ).scalar_one()
        session.add(
            WebTask(
                core_task_id="legacy-ownerless",
                name=None,
                status="running",
                owner_user_id=None,
                guest_session_hash=None,
            )
        )
        session.commit()
        assert guest_a_row.owner_user_id is None
        assert guest_a_row.guest_session_hash == hash_opaque_token(guest_a_token)
        assert guest_a_row.guest_session_hash != guest_a_token
        assert guest_b_row.guest_session_hash == hash_opaque_token(guest_b_token)

    guest_a_request = _request(
        task_harness.app,
        path="/api/tasks",
        cookies={GUEST_SESSION_COOKIE_NAME: guest_a_token},
    )
    guest_b_request = _request(
        task_harness.app,
        path="/api/tasks",
        cookies={GUEST_SESSION_COOKIE_NAME: guest_b_token},
    )
    assert _listed_ids(list_tasks(guest_a_request)) == {guest_a_task.task_id}
    assert _listed_ids(list_tasks(guest_b_request)) == {guest_b_task.task_id}
    assert list_tasks(_request(task_harness.app, path="/api/tasks")).items == ()

    task_harness.cli.status_calls.clear()
    _assert_hidden(
        lambda: get_task_status(
            guest_b_task.task_id,
            _request(
                task_harness.app,
                path=f"/api/tasks/{guest_b_task.task_id}",
                cookies={GUEST_SESSION_COOKIE_NAME: guest_a_token},
            ),
        )
    )
    _assert_hidden(
        lambda: get_task_result(
            guest_b_task.task_id,
            _request(
                task_harness.app,
                path=f"/api/tasks/{guest_b_task.task_id}/result",
                cookies={GUEST_SESSION_COOKIE_NAME: guest_a_token},
            ),
        )
    )
    assert task_harness.cli.status_calls == []

    authenticated_after_login = _request(
        task_harness.app,
        path="/api/tasks",
        cookies={
            "jelica_session": task_harness.session_tokens["alice"],
            GUEST_SESSION_COOKIE_NAME: guest_a_token,
        },
    )
    assert list_tasks(authenticated_after_login).items == ()
    _assert_hidden(
        lambda: get_task_status(
            guest_a_task.task_id,
            _request(
                task_harness.app,
                path=f"/api/tasks/{guest_a_task.task_id}",
                cookies={
                    "jelica_session": task_harness.session_tokens["alice"],
                    GUEST_SESSION_COOKIE_NAME: guest_a_token,
                },
            ),
        )
    )
    _assert_hidden(
        lambda: get_task_status(
            "legacy-ownerless",
            _request(
                task_harness.app,
                path="/api/tasks/legacy-ownerless",
                cookies={GUEST_SESSION_COOKIE_NAME: guest_a_token},
            ),
        )
    )


def test_authenticated_and_project_visibility_with_filters_and_detach(
    task_harness: _TaskHarness,
) -> None:
    alice_task = _create_authenticated_task(task_harness, user="alice")
    bob_task = _create_authenticated_task(task_harness, user="bob")
    state = task_harness.app.state.jelica_api_state

    assert _listed_ids(_list_as(task_harness, user="alice")) == {alice_task.task_id}
    assert _listed_ids(_list_as(task_harness, user="bob")) == {bob_task.task_id}
    assert _list_as(task_harness, user="carol").items == ()
    _assert_hidden(lambda: _status_as(task_harness, task_id=bob_task.task_id, user="alice"))
    _assert_hidden(lambda: _status_as(task_harness, task_id=alice_task.task_id, user="bob"))

    project = state.project_service.create_project(
        actor_user_id=task_harness.user_ids["bob"],
        name="Shared project",
        description=None,
    )
    with state.session_factory() as session:
        session.add(
            ProjectMember(
                project_id=project.project_id,
                user_id=task_harness.user_ids["alice"],
                role="member",
            )
        )
        session.commit()
    state.project_service.attach_task(
        actor_user_id=task_harness.user_ids["alice"],
        project_id=project.project_id,
        task_id=alice_task.task_id,
    )
    state.web_task_projection_store.upsert_task(
        core_task_id=alice_task.task_id,
        name=None,
        status="completed",
    )
    state.web_task_projection_store.upsert_task(
        core_task_id=bob_task.task_id,
        name=None,
        status="failed",
    )
    task_harness.cli.states[alice_task.task_id] = "completed"
    task_harness.cli.states[bob_task.task_id] = "failed"

    assert _listed_ids(_list_as(task_harness, user="bob")) == {
        alice_task.task_id,
        bob_task.task_id,
    }
    assert _status_as(task_harness, task_id=alice_task.task_id, user="bob").state == ("completed")
    assert _list_as(task_harness, user="carol").items == ()
    _assert_hidden(lambda: _status_as(task_harness, task_id=alice_task.task_id, user="carol"))

    assert _listed_ids(_list_as(task_harness, user="bob", project_id=[project.project_id])) == {
        alice_task.task_id
    }
    assert _listed_ids(_list_as(task_harness, user="bob", project="none")) == {bob_task.task_id}
    assert _listed_ids(_list_as(task_harness, user="bob", owner="me")) == {bob_task.task_id}
    assert _listed_ids(_list_as(task_harness, user="bob", states=["completed", "running"])) == {
        alice_task.task_id
    }
    assert (
        _list_as(
            task_harness,
            user="carol",
            project_id=[project.project_id],
            states=["completed"],
        ).items
        == ()
    )

    state.project_service.remove_member(
        actor_user_id=task_harness.user_ids["bob"],
        project_id=project.project_id,
        user_id=task_harness.user_ids["alice"],
    )
    assert _listed_ids(_list_as(task_harness, user="bob")) == {bob_task.task_id}
    _assert_hidden(lambda: _status_as(task_harness, task_id=alice_task.task_id, user="bob"))
    assert _listed_ids(_list_as(task_harness, user="alice")) == {alice_task.task_id}
    assert _status_as(task_harness, task_id=alice_task.task_id, user="alice").state == ("completed")
    with state.session_factory() as session:
        detached = session.execute(
            select(WebTask).where(WebTask.core_task_id == alice_task.task_id)
        ).scalar_one()
        assert detached.owner_user_id == task_harness.user_ids["alice"]
        assert detached.guest_session_hash is None
        assert detached.project_id is None


def test_projection_identity_is_immutable_and_database_rejects_dual_identity(
    task_harness: _TaskHarness,
) -> None:
    state = task_harness.app.state.jelica_api_state
    guest_hash = hash_opaque_token("immutable-guest")
    state.web_task_projection_store.upsert_task(
        core_task_id="immutable-guest-task",
        name="Guest task",
        status="running",
        guest_session_hash=guest_hash,
    )
    state.web_task_projection_store.upsert_task(
        core_task_id="immutable-guest-task",
        name=None,
        status="completed",
        owner_user_id=task_harness.user_ids["alice"],
    )
    with state.session_factory() as session:
        preserved = session.execute(
            select(WebTask).where(WebTask.core_task_id == "immutable-guest-task")
        ).scalar_one()
        assert preserved.owner_user_id is None
        assert preserved.guest_session_hash == guest_hash
        assert preserved.project_id is None

    with pytest.raises(ValueError, match="cannot both"):
        state.web_task_projection_store.upsert_task(
            core_task_id="invalid-dual-task",
            name=None,
            status="running",
            owner_user_id=task_harness.user_ids["alice"],
            guest_session_hash=guest_hash,
        )
    with state.session_factory() as session, pytest.raises(IntegrityError):
        session.add(
            WebTask(
                core_task_id="invalid-direct-dual-task",
                name=None,
                status="running",
                owner_user_id=task_harness.user_ids["alice"],
                guest_session_hash=guest_hash,
            )
        )
        session.commit()


def _create_authenticated_task(
    task_harness: _TaskHarness,
    *,
    user: str,
) -> TaskSubmissionResult:
    response = Response()
    result = create_task(
        TaskSubmissionRequest(sources=(f"{user}.fasta",)),
        _request(
            task_harness.app,
            path="/api/tasks",
            method="POST",
            cookies={"jelica_session": task_harness.session_tokens[user]},
        ),
        response,
    )
    assert GUEST_SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")
    with task_harness.app.state.jelica_api_state.session_factory() as session:
        task = session.execute(
            select(WebTask).where(WebTask.core_task_id == result.task_id)
        ).scalar_one()
        assert task.owner_user_id == task_harness.user_ids[user]
        assert task.guest_session_hash is None
    return result


def _list_as(
    task_harness: _TaskHarness,
    *,
    user: str,
    project_id: list[str] | None = None,
    project: str | None = None,
    owner: str | None = None,
    states: list[str] | None = None,
):
    return list_tasks(
        _request(
            task_harness.app,
            path="/api/tasks",
            cookies={"jelica_session": task_harness.session_tokens[user]},
        ),
        project_id=project_id,
        project=project,
        owner=owner,
        state=states,
    )


def _status_as(
    task_harness: _TaskHarness,
    *,
    task_id: str,
    user: str,
) -> TaskStatusSnapshot:
    return get_task_status(
        task_id,
        _request(
            task_harness.app,
            path=f"/api/tasks/{task_id}",
            cookies={"jelica_session": task_harness.session_tokens[user]},
        ),
    )


def _request(
    app: FastAPI,
    *,
    path: str,
    method: str = "GET",
    cookies: dict[str, str] | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
        headers.append((b"cookie", cookie_header.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": headers,
            "app": app,
        }
    )


def _guest_cookie_value(response: Response) -> str:
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    return cookies[GUEST_SESSION_COOKIE_NAME].value


def _listed_ids(response: object) -> set[str]:
    return {item.task_id for item in response.items}


def _assert_hidden(action: object) -> None:
    with pytest.raises(HTTPException) as raised:
        action()
    assert raised.value.status_code == status.HTTP_404_NOT_FOUND
    assert raised.value.detail == {
        "error": "task_not_found",
        "message": "Task was not found.",
    }
