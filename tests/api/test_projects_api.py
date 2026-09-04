from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, status
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

import jelica_api.projects as projects_module
from jelica_api.api.authentication import optional_current_user
from jelica_api.api.routes.projects import create_project
from jelica_api.app import create_app
from jelica_api.auth import hash_opaque_token
from jelica_api.contracts import ProjectCreateRequest
from jelica_api.models import (
    AuthSession,
    Base,
    Project,
    ProjectHistoryEvent,
    ProjectMember,
    User,
    WebTask,
)
from jelica_api.projects import (
    ProjectConflictError,
    ProjectPermissionError,
    ProjectService,
    ProjectValidationError,
)
from jelica_api.settings import ApiSettings


@dataclass(frozen=True, slots=True)
class _ProjectHarness:
    engine: Engine
    session_factory: sessionmaker[Session]
    service: ProjectService


@dataclass(frozen=True, slots=True)
class _ProjectApiHarness:
    app: FastAPI
    user_id: str
    session_token: str


@pytest.fixture
def project_harness() -> Iterator[_ProjectHarness]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    yield _ProjectHarness(
        engine=engine,
        session_factory=session_factory,
        service=ProjectService(session_factory=session_factory),
    )
    engine.dispose()


@pytest.fixture
def project_api_harness() -> Iterator[_ProjectApiHarness]:
    app = create_app(
        settings=ApiSettings(
            app_name="JELICA Web Backend",
            api_host="127.0.0.1",
            api_port=8000,
            database_url="sqlite+pysqlite:///:memory:",
            cli_command_prefix=("jelica",),
            cli_timeout_seconds=30.0,
        )
    )
    state = app.state.jelica_api_state
    Base.metadata.create_all(state.engine)
    user = User(
        username="api-owner",
        email="api-owner@example.org",
        password_hash="test-password-hash",
        email_verified=True,
        language="en",
    )
    raw_session_token = "project-api-session"
    now = datetime.now(UTC)
    with state.session_factory() as session:
        session.add(user)
        session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_opaque_token(raw_session_token),
                created_at=now,
                expires_at=now + timedelta(days=1),
                last_used_at=now,
            )
        )
        session.commit()
    try:
        yield _ProjectApiHarness(
            app=app,
            user_id=user.id,
            session_token=raw_session_token,
        )
    finally:
        state.task_orchestrator.shutdown()
        state.engine.dispose()


def test_projects_routes_are_registered_and_require_authentication(
    project_api_harness: _ProjectApiHarness,
) -> None:
    paths = project_api_harness.app.openapi()["paths"]
    assert {"get", "post"}.issubset(paths["/api/projects"])
    assert {"get", "patch", "delete"}.issubset(paths["/api/projects/{project_id}"])
    assert "post" in paths["/api/projects/{project_id}/transfer-ownership"]
    assert {"get", "patch", "delete"}.issubset(
        {
            *paths["/api/projects/{project_id}/members"],
            *paths["/api/projects/{project_id}/members/{user_id}"],
        }
    )
    assert "post" in paths["/api/projects/{project_id}/leave"]
    assert {"get", "put", "delete"}.issubset(
        {
            *paths["/api/projects/{project_id}/tasks"],
            *paths["/api/projects/{project_id}/tasks/{task_id}"],
        }
    )
    assert "get" in paths["/api/projects/{project_id}/history"]
    repeated_filters = {
        "/api/projects": {"relation", "status"},
        "/api/projects/{project_id}/members": {"role"},
        "/api/projects/{project_id}/tasks": {"owner_user_id", "state"},
        "/api/projects/{project_id}/history": {"event_type"},
    }
    for path, expected_names in repeated_filters.items():
        parameters = paths[path]["get"]["parameters"]
        schemas = {
            parameter["name"]: parameter["schema"]
            for parameter in parameters
            if parameter["in"] == "query"
        }
        assert expected_names.issubset(schemas)
        for name in expected_names:
            variants = schemas[name].get("anyOf", [schemas[name]])
            assert any(variant.get("type") == "array" for variant in variants)

    with pytest.raises(HTTPException) as raised:
        create_project(
            ProjectCreateRequest(name="Guest project"),
            _request_for_api(project_api_harness.app, path="/api/projects"),
        )
    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert raised.value.detail["error"] == "authentication_required"


def test_authenticated_project_api_creation_uses_current_session_user(
    project_api_harness: _ProjectApiHarness,
) -> None:
    response = create_project(
        ProjectCreateRequest(name="API project", description="Created through auth boundary"),
        _request_for_api(
            project_api_harness.app,
            path="/api/projects",
            session_token=project_api_harness.session_token,
        ),
    )

    assert response.created_by_user_id == project_api_harness.user_id
    assert response.owner_user_id == project_api_harness.user_id
    state = project_api_harness.app.state.jelica_api_state
    with state.session_factory() as session:
        member = session.get(ProjectMember, (response.id, project_api_harness.user_id))
        assert member is not None
        assert member.role == "supervisor"
        events = session.execute(
            select(ProjectHistoryEvent).where(ProjectHistoryEvent.project_id == response.id)
        ).scalars()
        assert "project_created" in {event.event_type for event in events}


def test_invalid_nonempty_session_cookie_does_not_silently_create_guest_context(
    project_api_harness: _ProjectApiHarness,
) -> None:
    with pytest.raises(HTTPException) as raised:
        optional_current_user(
            _request_for_api(
                project_api_harness.app,
                path="/api/tasks",
                session_token="expired-or-revoked-token",
            )
        )

    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert raised.value.detail["error"] == "authentication_required"


def test_create_project_establishes_owner_member_and_history_invariants(
    project_harness: _ProjectHarness,
) -> None:
    owner = _create_user(project_harness, label="owner")

    created = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="  Comparative genomics  ",
        description="  Primary analyses  ",
    )

    assert created.name == "Comparative genomics"
    assert created.description == "Primary analyses"
    assert created.status == "active"
    assert created.created_by_user_id == owner.id
    assert created.owner_user_id == owner.id

    with project_harness.session_factory() as session:
        project = session.get(Project, created.project_id)
        assert project is not None
        assert project.created_by.id == owner.id
        assert project.owner.id == owner.id
        assert [(member.user_id, member.role) for member in project.members] == [
            (owner.id, "supervisor")
        ]
        assert project.tasks == []
        history_by_type = {event.event_type: event for event in project.history}
        assert set(history_by_type) == {"project_created", "member_joined"}
        assert history_by_type["project_created"].actor_user_id == owner.id
        assert history_by_type["project_created"].data == {"status": "active"}
        assert history_by_type["member_joined"].actor_user_id == owner.id
        assert history_by_type["member_joined"].subject_user_id == owner.id
        assert history_by_type["member_joined"].data == {"role": "supervisor"}

        persisted_owner = session.get(User, owner.id)
        assert persisted_owner is not None
        assert [owned.id for owned in persisted_owner.owned_projects] == [project.id]
        assert [membership.project_id for membership in persisted_owner.project_members] == [
            project.id
        ]


def test_transfer_promotes_member_preserves_old_supervisor_and_rejects_outsider(
    project_harness: _ProjectHarness,
) -> None:
    old_owner = _create_user(project_harness, label="old-owner")
    new_owner = _create_user(project_harness, label="new-owner")
    outsider = _create_user(project_harness, label="outsider")
    project = project_harness.service.create_project(
        actor_user_id=old_owner.id,
        name="Ownership transfer",
        description=None,
    )
    _add_member(
        project_harness,
        project_id=project.project_id,
        user_id=new_owner.id,
        role="member",
    )

    transferred = project_harness.service.transfer_ownership(
        actor_user_id=old_owner.id,
        project_id=project.project_id,
        new_owner_user_id=new_owner.id,
    )

    assert transferred.owner_user_id == new_owner.id
    members = project_harness.service.list_members(
        actor_user_id=new_owner.id,
        project_id=project.project_id,
    )
    assert {member.user_id: member.role for member in members} == {
        old_owner.id: "supervisor",
        new_owner.id: "supervisor",
    }
    by_user = {member.user_id: member for member in members}
    assert by_user[old_owner.id].username == "old-owner"
    assert by_user[old_owner.id].email == "old-owner@example.org"
    history = project_harness.service.list_history(
        actor_user_id=new_owner.id,
        project_id=project.project_id,
    )
    assert [event.event_type for event in history] == [
        "project_created",
        "member_joined",
        "member_role_changed",
        "ownership_transferred",
    ]
    assert history[2].subject_user_id == new_owner.id
    assert history[2].data == {"previous_role": "member", "role": "supervisor"}
    assert history[3].subject_user_id == new_owner.id
    assert history[3].data == {
        "previous_owner_user_id": old_owner.id,
        "new_owner_user_id": new_owner.id,
    }

    with pytest.raises(ProjectValidationError) as raised:
        project_harness.service.transfer_ownership(
            actor_user_id=new_owner.id,
            project_id=project.project_id,
            new_owner_user_id=outsider.id,
        )
    assert raised.value.code == "project_transfer_target_not_member"

    unchanged = project_harness.service.get_project(
        actor_user_id=new_owner.id,
        project_id=project.project_id,
    )
    assert unchanged.owner_user_id == new_owner.id
    assert (
        len(
            project_harness.service.list_history(
                actor_user_id=new_owner.id,
                project_id=project.project_id,
            )
        )
        == 4
    )


def test_transfer_rolls_back_owner_role_and_history_on_late_failure(
    project_harness: _ProjectHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _create_user(project_harness, label="rollback-owner")
    candidate = _create_user(project_harness, label="rollback-candidate")
    project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="Rollback ownership",
        description=None,
    )
    _add_member(
        project_harness,
        project_id=project.project_id,
        user_id=candidate.id,
        role="member",
    )
    original_add_history_event = projects_module._add_history_event

    def fail_on_ownership_event(**kwargs: object) -> None:
        if kwargs["event_type"] == "ownership_transferred":
            raise RuntimeError("simulated history write failure")
        original_add_history_event(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(projects_module, "_add_history_event", fail_on_ownership_event)

    with pytest.raises(RuntimeError, match="simulated history write failure"):
        project_harness.service.transfer_ownership(
            actor_user_id=owner.id,
            project_id=project.project_id,
            new_owner_user_id=candidate.id,
        )

    with project_harness.session_factory() as session:
        persisted_project = session.get(Project, project.project_id)
        candidate_member = session.get(ProjectMember, (project.project_id, candidate.id))
        history = session.execute(
            select(ProjectHistoryEvent).where(ProjectHistoryEvent.project_id == project.project_id)
        ).scalars()
        assert persisted_project is not None
        assert persisted_project.owner_user_id == owner.id
        assert candidate_member is not None
        assert candidate_member.role == "member"
        assert {event.event_type for event in history} == {
            "project_created",
            "member_joined",
        }


def test_owner_cannot_be_demoted_removed_or_leave(
    project_harness: _ProjectHarness,
) -> None:
    owner = _create_user(project_harness, label="protected-owner")
    project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="Protected ownership",
        description=None,
    )

    with pytest.raises(ProjectConflictError) as demotion:
        project_harness.service.update_member_role(
            actor_user_id=owner.id,
            project_id=project.project_id,
            user_id=owner.id,
            role="member",
        )
    assert demotion.value.code == "project_owner_role_protected"

    with pytest.raises(ProjectConflictError) as removal:
        project_harness.service.remove_member(
            actor_user_id=owner.id,
            project_id=project.project_id,
            user_id=owner.id,
        )
    assert removal.value.code == "project_owner_membership_protected"

    with pytest.raises(ProjectConflictError) as leave:
        project_harness.service.leave_project(
            actor_user_id=owner.id,
            project_id=project.project_id,
        )
    assert leave.value.code == "project_owner_membership_protected"

    members = project_harness.service.list_members(
        actor_user_id=owner.id,
        project_id=project.project_id,
    )
    assert [(member.user_id, member.role) for member in members] == [(owner.id, "supervisor")]


def test_attach_detach_and_move_enforce_task_owner_membership(
    project_harness: _ProjectHarness,
) -> None:
    owner = _create_user(project_harness, label="project-owner")
    task_owner = _create_user(project_harness, label="task-owner")
    outsider = _create_user(project_harness, label="task-outsider")
    first_project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="First project",
        description=None,
    )
    second_project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="Second project",
        description=None,
    )
    for project_id in (first_project.project_id, second_project.project_id):
        _add_member(
            project_harness,
            project_id=project_id,
            user_id=task_owner.id,
            role="member",
        )

    owned_task = _create_task(project_harness, owner_user_id=task_owner.id, status="running")
    guest_task = _create_task(project_harness, owner_user_id=None, status="waiting")
    outsider_task = _create_task(
        project_harness,
        owner_user_id=outsider.id,
        status="completed",
    )

    attached = project_harness.service.attach_task(
        actor_user_id=task_owner.id,
        project_id=first_project.project_id,
        task_id=owned_task.core_task_id,
    )
    assert attached.task_id == owned_task.core_task_id
    assert attached.owner_user_id == task_owner.id
    assert attached.project_id == first_project.project_id

    detached = project_harness.service.detach_task(
        actor_user_id=task_owner.id,
        project_id=first_project.project_id,
        task_id=owned_task.core_task_id,
    )
    assert detached.owner_user_id == task_owner.id
    assert detached.project_id is None

    with pytest.raises(ProjectValidationError) as guest_rejection:
        project_harness.service.attach_task(
            actor_user_id=owner.id,
            project_id=first_project.project_id,
            task_id=guest_task.core_task_id,
        )
    assert guest_rejection.value.code == "guest_task_cannot_join_project"

    with pytest.raises(ProjectValidationError) as outsider_rejection:
        project_harness.service.attach_task(
            actor_user_id=owner.id,
            project_id=first_project.project_id,
            task_id=outsider_task.core_task_id,
        )
    assert outsider_rejection.value.code == "project_task_owner_not_member"

    project_harness.service.attach_task(
        actor_user_id=task_owner.id,
        project_id=first_project.project_id,
        task_id=owned_task.core_task_id,
    )
    project_harness.service.remove_member(
        actor_user_id=owner.id,
        project_id=second_project.project_id,
        user_id=task_owner.id,
    )
    with pytest.raises(ProjectValidationError) as move_rejection:
        project_harness.service.attach_task(
            actor_user_id=owner.id,
            project_id=second_project.project_id,
            task_id=owned_task.core_task_id,
        )
    assert move_rejection.value.code == "project_task_owner_not_member"
    with project_harness.session_factory() as session:
        unchanged_task = session.execute(
            select(WebTask).where(WebTask.core_task_id == owned_task.core_task_id)
        ).scalar_one()
        assert unchanged_task.project_id == first_project.project_id
    _add_member(
        project_harness,
        project_id=second_project.project_id,
        user_id=task_owner.id,
        role="member",
    )
    moved = project_harness.service.attach_task(
        actor_user_id=task_owner.id,
        project_id=second_project.project_id,
        task_id=owned_task.core_task_id,
    )
    assert moved.owner_user_id == task_owner.id
    assert moved.project_id == second_project.project_id

    first_history = project_harness.service.list_history(
        actor_user_id=task_owner.id,
        project_id=first_project.project_id,
        event_types=("task_attached", "task_detached"),
    )
    second_history = project_harness.service.list_history(
        actor_user_id=task_owner.id,
        project_id=second_project.project_id,
        event_types=("task_attached", "task_detached"),
    )
    assert [event.event_type for event in first_history] == [
        "task_attached",
        "task_detached",
        "task_attached",
        "task_detached",
    ]
    assert first_history[-1].data == {
        "task_id": owned_task.core_task_id,
        "to_project_id": second_project.project_id,
    }
    assert [event.event_type for event in second_history] == ["task_attached"]
    assert second_history[0].data == {
        "task_id": owned_task.core_task_id,
        "from_project_id": first_project.project_id,
    }

    with project_harness.session_factory() as session:
        persisted = session.execute(
            select(WebTask).where(WebTask.core_task_id == owned_task.core_task_id)
        ).scalar_one()
        assert persisted.owner.id == task_owner.id
        assert persisted.project is not None
        assert persisted.project.id == second_project.project_id


def test_target_supervisor_cannot_move_another_members_task_without_source_admin(
    project_harness: _ProjectHarness,
) -> None:
    source_owner = _create_user(project_harness, label="source-owner")
    target_owner = _create_user(project_harness, label="target-owner")
    task_owner = _create_user(project_harness, label="cross-project-task-owner")
    source = project_harness.service.create_project(
        actor_user_id=source_owner.id,
        name="Source project",
        description=None,
    )
    target = project_harness.service.create_project(
        actor_user_id=target_owner.id,
        name="Target project",
        description=None,
    )
    for project_id in (source.project_id, target.project_id):
        _add_member(
            project_harness,
            project_id=project_id,
            user_id=task_owner.id,
            role="member",
        )
    task = _create_task(
        project_harness,
        owner_user_id=task_owner.id,
        project_id=source.project_id,
        status="running",
    )

    with pytest.raises(ProjectPermissionError) as raised:
        project_harness.service.attach_task(
            actor_user_id=target_owner.id,
            project_id=target.project_id,
            task_id=task.core_task_id,
        )

    assert raised.value.code == "source_project_supervisor_required"
    with project_harness.session_factory() as session:
        assert session.get(WebTask, task.id).project_id == source.project_id
    assert (
        project_harness.service.list_history(
            actor_user_id=task_owner.id,
            project_id=source.project_id,
            event_types=("task_detached",),
        )
        == ()
    )
    assert (
        project_harness.service.list_history(
            actor_user_id=task_owner.id,
            project_id=target.project_id,
            event_types=("task_attached",),
        )
        == ()
    )


def test_member_removal_and_voluntary_leave_detach_tasks_without_deleting_them(
    project_harness: _ProjectHarness,
) -> None:
    owner = _create_user(project_harness, label="removal-owner")
    removed_member = _create_user(project_harness, label="removed-member")
    leaving_member = _create_user(project_harness, label="leaving-member")
    project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="Membership lifecycle",
        description=None,
    )
    other_project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="Unaffected membership project",
        description=None,
    )
    for user in (removed_member, leaving_member):
        _add_member(
            project_harness,
            project_id=project.project_id,
            user_id=user.id,
            role="member",
        )
    _add_member(
        project_harness,
        project_id=other_project.project_id,
        user_id=removed_member.id,
        role="member",
    )

    removed_tasks = (
        _create_task(
            project_harness,
            owner_user_id=removed_member.id,
            project_id=project.project_id,
            status="running",
        ),
        _create_task(
            project_harness,
            owner_user_id=removed_member.id,
            project_id=project.project_id,
            status="completed",
        ),
    )
    leaving_task = _create_task(
        project_harness,
        owner_user_id=leaving_member.id,
        project_id=project.project_id,
        status="failed",
    )
    owner_task = _create_task(
        project_harness,
        owner_user_id=owner.id,
        project_id=project.project_id,
        status="running",
    )
    other_project_task = _create_task(
        project_harness,
        owner_user_id=removed_member.id,
        project_id=other_project.project_id,
        status="waiting",
    )

    project_harness.service.remove_member(
        actor_user_id=owner.id,
        project_id=project.project_id,
        user_id=removed_member.id,
    )
    project_harness.service.leave_project(
        actor_user_id=leaving_member.id,
        project_id=project.project_id,
    )

    task_ids = {task.id for task in (*removed_tasks, leaving_task)}
    with project_harness.session_factory() as session:
        persisted_tasks = (
            session.execute(select(WebTask).where(WebTask.id.in_(task_ids))).scalars().all()
        )
        assert len(persisted_tasks) == 3
        assert {task.owner_user_id for task in persisted_tasks} == {
            removed_member.id,
            leaving_member.id,
        }
        assert all(task.project_id is None for task in persisted_tasks)
        assert session.get(ProjectMember, (project.project_id, removed_member.id)) is None
        assert session.get(ProjectMember, (project.project_id, leaving_member.id)) is None
        assert session.get(WebTask, owner_task.id).project_id == project.project_id
        assert session.get(WebTask, other_project_task.id).project_id == other_project.project_id

    history = project_harness.service.list_history(
        actor_user_id=owner.id,
        project_id=project.project_id,
        event_types=("task_detached", "member_removed"),
    )
    assert [event.event_type for event in history].count("task_detached") == 3
    assert [event.event_type for event in history].count("member_removed") == 2
    removal_events = [event for event in history if event.event_type == "member_removed"]
    assert {event.subject_user_id for event in removal_events} == {
        removed_member.id,
        leaving_member.id,
    }
    assert {event.actor_user_id for event in removal_events} == {
        owner.id,
        leaving_member.id,
    }
    assert {
        event.data["task_id"]
        for event in history
        if event.event_type == "task_detached" and event.data is not None
    } == {task.core_task_id for task in (*removed_tasks, leaving_task)}


def test_delete_project_detaches_and_preserves_tasks(
    project_harness: _ProjectHarness,
) -> None:
    owner = _create_user(project_harness, label="delete-owner")
    member = _create_user(project_harness, label="delete-member")
    project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="Disposable project",
        description=None,
    )
    other_project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="Preserved project",
        description=None,
    )
    _add_member(
        project_harness,
        project_id=project.project_id,
        user_id=member.id,
        role="member",
    )
    tasks = (
        _create_task(
            project_harness,
            owner_user_id=owner.id,
            project_id=project.project_id,
            status="completed",
        ),
        _create_task(
            project_harness,
            owner_user_id=member.id,
            project_id=project.project_id,
            status="failed",
        ),
    )
    other_task = _create_task(
        project_harness,
        owner_user_id=owner.id,
        project_id=other_project.project_id,
        status="running",
    )

    with pytest.raises(ProjectPermissionError) as forbidden:
        project_harness.service.delete_project(
            actor_user_id=member.id,
            project_id=project.project_id,
        )
    assert forbidden.value.code == "project_owner_required"

    project_harness.service.delete_project(
        actor_user_id=owner.id,
        project_id=project.project_id,
    )

    task_ids = {task.id for task in tasks}
    with project_harness.session_factory() as session:
        assert session.get(Project, project.project_id) is None
        persisted_tasks = (
            session.execute(select(WebTask).where(WebTask.id.in_(task_ids))).scalars().all()
        )
        assert len(persisted_tasks) == 2
        assert all(task.project_id is None for task in persisted_tasks)
        assert {task.owner_user_id for task in persisted_tasks} == {owner.id, member.id}
        assert session.get(WebTask, other_task.id).project_id == other_project.project_id
        assert (
            session.execute(
                select(ProjectMember).where(ProjectMember.project_id == project.project_id)
            )
            .scalars()
            .all()
            == []
        )
        assert (
            session.execute(
                select(ProjectHistoryEvent).where(
                    ProjectHistoryEvent.project_id == project.project_id
                )
            )
            .scalars()
            .all()
            == []
        )


def test_project_relation_and_status_filters_accept_multiple_values(
    project_harness: _ProjectHarness,
) -> None:
    actor = _create_user(project_harness, label="filter-actor")
    participant_owner = _create_user(project_harness, label="participant-owner")
    second_participant_owner = _create_user(
        project_harness,
        label="second-participant-owner",
    )
    unrelated_owner = _create_user(project_harness, label="unrelated-owner")

    owned_active = project_harness.service.create_project(
        actor_user_id=actor.id,
        name="Owned active",
        description=None,
    )
    owned_frozen = project_harness.service.create_project(
        actor_user_id=actor.id,
        name="Owned frozen",
        description=None,
        status="frozen",
    )
    participating_active = project_harness.service.create_project(
        actor_user_id=participant_owner.id,
        name="Participating active",
        description=None,
    )
    participating_frozen = project_harness.service.create_project(
        actor_user_id=second_participant_owner.id,
        name="Participating frozen",
        description=None,
        status="frozen",
    )
    project_harness.service.create_project(
        actor_user_id=unrelated_owner.id,
        name="Unrelated",
        description=None,
    )
    for project in (participating_active, participating_frozen):
        _add_member(
            project_harness,
            project_id=project.project_id,
            user_id=actor.id,
            role="viewer",
        )

    owned = project_harness.service.list_projects(
        actor_user_id=actor.id,
        relations=("owned",),
        statuses=("active", "frozen"),
    )
    participating = project_harness.service.list_projects(
        actor_user_id=actor.id,
        relations=("participating",),
        statuses=("active", "frozen"),
    )
    combined = project_harness.service.list_projects(
        actor_user_id=actor.id,
        relations=("owned", "participating"),
        statuses=("active", "frozen"),
    )
    active_any = project_harness.service.list_projects(
        actor_user_id=actor.id,
        relations=("any",),
        statuses=("active",),
    )

    assert {project.project_id for project in owned} == {
        owned_active.project_id,
        owned_frozen.project_id,
    }
    assert {project.project_id for project in participating} == {
        participating_active.project_id,
        participating_frozen.project_id,
    }
    assert {project.project_id for project in combined} == {
        owned_active.project_id,
        owned_frozen.project_id,
        participating_active.project_id,
        participating_frozen.project_id,
    }
    assert {project.project_id for project in active_any} == {
        owned_active.project_id,
        participating_active.project_id,
    }


def test_member_role_filter_accepts_multiple_values(
    project_harness: _ProjectHarness,
) -> None:
    owner = _create_user(project_harness, label="member-filter-owner")
    viewer = _create_user(project_harness, label="viewer")
    commenter = _create_user(project_harness, label="commenter")
    member = _create_user(project_harness, label="member")
    project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="Member filters",
        description=None,
    )
    for user, role in ((viewer, "viewer"), (commenter, "commenter"), (member, "member")):
        _add_member(
            project_harness,
            project_id=project.project_id,
            user_id=user.id,
            role=role,
        )

    filtered = project_harness.service.list_members(
        actor_user_id=owner.id,
        project_id=project.project_id,
        roles=("viewer", "member"),
    )

    assert {(row.user_id, row.role) for row in filtered} == {
        (viewer.id, "viewer"),
        (member.id, "member"),
    }


def test_project_task_owner_and_state_filters_accept_multiple_values(
    project_harness: _ProjectHarness,
) -> None:
    owner = _create_user(project_harness, label="task-filter-owner")
    first_member = _create_user(project_harness, label="first-task-filter-member")
    second_member = _create_user(project_harness, label="second-task-filter-member")
    project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="Task filters",
        description=None,
    )
    for member in (first_member, second_member):
        _add_member(
            project_harness,
            project_id=project.project_id,
            user_id=member.id,
            role="member",
        )

    first_selected = _create_task(
        project_harness,
        owner_user_id=first_member.id,
        project_id=project.project_id,
        status="failed",
    )
    second_selected = _create_task(
        project_harness,
        owner_user_id=second_member.id,
        project_id=project.project_id,
        status="completed",
    )
    _create_task(
        project_harness,
        owner_user_id=first_member.id,
        project_id=project.project_id,
        status="running",
    )
    _create_task(
        project_harness,
        owner_user_id=owner.id,
        project_id=project.project_id,
        status="completed",
    )

    filtered = project_harness.service.list_tasks(
        actor_user_id=owner.id,
        project_id=project.project_id,
        owner_user_ids=(first_member.id, second_member.id),
        states=("completed", "failed"),
    )

    assert {(task.task_id, task.owner_user_id, task.state) for task in filtered} == {
        (first_selected.core_task_id, first_member.id, "failed"),
        (second_selected.core_task_id, second_member.id, "completed"),
    }


def test_history_event_type_filter_accepts_multiple_values(
    project_harness: _ProjectHarness,
) -> None:
    owner = _create_user(project_harness, label="history-filter-owner")
    project = project_harness.service.create_project(
        actor_user_id=owner.id,
        name="History filters",
        description=None,
    )
    project_harness.service.update_project(
        actor_user_id=owner.id,
        project_id=project.project_id,
        changes={"name": "Updated history filters"},
    )
    project_harness.service.update_project(
        actor_user_id=owner.id,
        project_id=project.project_id,
        changes={"status": "frozen"},
    )

    filtered = project_harness.service.list_history(
        actor_user_id=owner.id,
        project_id=project.project_id,
        event_types=("project_updated", "project_frozen"),
    )

    assert [event.event_type for event in filtered] == [
        "project_updated",
        "project_frozen",
    ]
    assert filtered[0].data == {"fields": ["name"]}
    assert filtered[1].data == {"previous_status": "active"}


def _create_user(project_harness: _ProjectHarness, *, label: str) -> User:
    user = User(
        username=label,
        email=f"{label}@example.org",
        password_hash="test-password-hash",
        email_verified=True,
        language="en",
    )
    with project_harness.session_factory() as session:
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def _add_member(
    project_harness: _ProjectHarness,
    *,
    project_id: str,
    user_id: str,
    role: str,
) -> None:
    with project_harness.session_factory() as session:
        session.add(
            ProjectMember(
                project_id=project_id,
                user_id=user_id,
                role=role,
                joined_at=datetime.now(UTC),
            )
        )
        session.commit()


def _create_task(
    project_harness: _ProjectHarness,
    *,
    owner_user_id: str | None,
    status: str,
    project_id: str | None = None,
) -> WebTask:
    task = WebTask(
        core_task_id=str(uuid4()),
        name=None,
        status=status,
        owner_user_id=owner_user_id,
        project_id=project_id,
    )
    with project_harness.session_factory() as session:
        session.add(task)
        session.commit()
        session.refresh(task)
    return task


def _request_for_api(
    app: FastAPI,
    *,
    path: str,
    session_token: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if session_token is not None:
        headers.append((b"cookie", f"jelica_session={session_token}".encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": headers,
            "app": app,
        }
    )
