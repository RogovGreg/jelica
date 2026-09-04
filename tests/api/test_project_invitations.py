from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

import jelica_api.projects as projects_module
from jelica_api.api.routes.invitations import (
    accept_project_invitation,
    create_project_invitation,
    decline_project_invitation,
    list_project_invitations,
    list_received_invitations,
    revoke_project_invitation,
)
from jelica_api.app import create_app
from jelica_api.auth import hash_opaque_token
from jelica_api.contracts import ProjectInvitationCreateRequest
from jelica_api.models import (
    AuthSession,
    Base,
    Project,
    ProjectHistoryEvent,
    ProjectInvitation,
    ProjectMember,
    User,
    WebTask,
)
from jelica_api.projects import (
    PROJECT_INVITATION_EXPIRATION,
    ProjectConflictError,
    ProjectInvitationNotFoundError,
    ProjectInvitationTargetNotFoundError,
    ProjectPermissionError,
)
from jelica_api.settings import ApiSettings


@dataclass(frozen=True, slots=True)
class _InvitationHarness:
    app: FastAPI
    session_factory: sessionmaker[Session]
    user_ids: dict[str, str]
    session_tokens: dict[str, str]


@pytest.fixture
def invitation_harness() -> Iterator[_InvitationHarness]:
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
    with state.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(state.engine)
    now = datetime.now(UTC)
    user_ids: dict[str, str] = {}
    session_tokens: dict[str, str] = {}
    with state.session_factory() as session:
        for label in (
            "owner",
            "supervisor",
            "member",
            "viewer",
            "alice",
            "bob",
            "carol",
            "outsider",
        ):
            user = User(
                username=label,
                email=f"{label}@example.org",
                password_hash="test-password-hash",
                email_verified=True,
                language="en",
            )
            session.add(user)
            session.flush()
            raw_session_token = f"{label}-session-token"
            session.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=hash_opaque_token(raw_session_token),
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                    last_used_at=now,
                )
            )
            user_ids[label] = user.id
            session_tokens[label] = raw_session_token
        session.commit()
    try:
        yield _InvitationHarness(
            app=app,
            session_factory=state.session_factory,
            user_ids=user_ids,
            session_tokens=session_tokens,
        )
    finally:
        state.task_orchestrator.shutdown()
        state.engine.dispose()


def test_invitation_routes_are_authenticated_and_use_repeated_filters(
    invitation_harness: _InvitationHarness,
) -> None:
    paths = invitation_harness.app.openapi()["paths"]
    assert {"get", "post"}.issubset(paths["/api/projects/{project_id}/invitations"])
    assert "post" in paths["/api/projects/{project_id}/invitations/{invitation_id}/revoke"]
    assert "get" in paths["/api/invitations"]
    assert "post" in paths["/api/invitations/{invitation_id}/accept"]
    assert "post" in paths["/api/invitations/{invitation_id}/decline"]

    repeated_filters = {
        "/api/projects/{project_id}/invitations": {
            "status",
            "role",
            "invited_user_id",
        },
        "/api/invitations": {"status", "role", "project_id"},
    }
    for path, filter_names in repeated_filters.items():
        parameters = paths[path]["get"]["parameters"]
        schemas = {parameter["name"]: parameter["schema"] for parameter in parameters}
        assert filter_names.issubset(schemas)
        for name in filter_names:
            variants = schemas[name].get("anyOf", [schemas[name]])
            assert any(variant.get("type") == "array" for variant in variants)

    with pytest.raises(HTTPException) as received_error:
        list_received_invitations(_request(invitation_harness, path="/api/invitations"))
    assert received_error.value.status_code == status.HTTP_401_UNAUTHORIZED
    with pytest.raises(HTTPException) as create_error:
        create_project_invitation(
            "unknown-project",
            ProjectInvitationCreateRequest(
                invited_user_id=invitation_harness.user_ids["alice"],
                role="member",
            ),
            _request(invitation_harness, path="/api/projects/unknown-project/invitations"),
        )
    assert create_error.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_invitation_creation_permissions_invariants_and_relationships(
    invitation_harness: _InvitationHarness,
) -> None:
    service = invitation_harness.app.state.jelica_api_state.project_service
    project = _create_project(invitation_harness)
    _add_member(invitation_harness, project.id, "supervisor", "supervisor")
    _add_member(invitation_harness, project.id, "member", "member")
    _add_member(invitation_harness, project.id, "viewer", "viewer")

    created = create_project_invitation(
        project.id,
        ProjectInvitationCreateRequest(
            invited_user_id=invitation_harness.user_ids["alice"],
            role="supervisor",
        ),
        _request(
            invitation_harness,
            path=f"/api/projects/{project.id}/invitations",
            user="supervisor",
        ),
    )

    assert created.project_id == project.id
    assert created.project_name == project.name
    assert created.invited_user_id == invitation_harness.user_ids["alice"]
    assert created.invited_username == "alice"
    assert created.invited_by_user_id == invitation_harness.user_ids["supervisor"]
    assert created.inviter_username == "supervisor"
    assert created.role == "supervisor"
    assert created.status == "pending"
    assert created.expires_at - created.invited_at == PROJECT_INVITATION_EXPIRATION
    assert created.resolved_at is None

    with invitation_harness.session_factory() as session:
        persisted_project = session.get(Project, project.id)
        invited_user = session.get(User, invitation_harness.user_ids["alice"])
        invitation = session.get(ProjectInvitation, created.invitation_id)
        assert persisted_project is not None
        assert invited_user is not None
        assert invitation is not None
        assert [item.id for item in persisted_project.invitations] == [created.invitation_id]
        assert [item.id for item in invited_user.received_project_invitations] == [
            created.invitation_id
        ]
        assert invitation.invited_user.id == invited_user.id
        assert invitation.invited_by.username == "supervisor"
        assert session.get(ProjectMember, (project.id, invited_user.id)) is None
    assert not hasattr(User, "sent_project_invitations")

    with pytest.raises(ProjectConflictError) as duplicate:
        service.create_invitation(
            actor_user_id=invitation_harness.user_ids["owner"],
            project_id=project.id,
            invited_user_id=invitation_harness.user_ids["alice"],
            role="member",
        )
    assert duplicate.value.code == "project_invitation_active_duplicate"

    with pytest.raises(ProjectConflictError) as existing_member:
        service.create_invitation(
            actor_user_id=invitation_harness.user_ids["owner"],
            project_id=project.id,
            invited_user_id=invitation_harness.user_ids["member"],
            role="viewer",
        )
    assert existing_member.value.code == "project_invitation_target_already_member"
    with pytest.raises(ProjectInvitationTargetNotFoundError):
        service.create_invitation(
            actor_user_id=invitation_harness.user_ids["owner"],
            project_id=project.id,
            invited_user_id="missing-user",
            role="viewer",
        )
    for ordinary_actor in ("member", "viewer"):
        with pytest.raises(ProjectPermissionError):
            service.create_invitation(
                actor_user_id=invitation_harness.user_ids[ordinary_actor],
                project_id=project.id,
                invited_user_id=invitation_harness.user_ids["bob"],
                role="member",
            )


def test_acceptance_is_invitee_only_and_atomically_creates_membership(
    invitation_harness: _InvitationHarness,
) -> None:
    service = invitation_harness.app.state.jelica_api_state.project_service
    project = _create_project(invitation_harness)
    invitation = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=invitation_harness.user_ids["alice"],
        role="supervisor",
    )
    with invitation_harness.session_factory() as session:
        session.add(
            WebTask(
                core_task_id="alice-standalone-task",
                name=None,
                status="running",
                owner_user_id=invitation_harness.user_ids["alice"],
                guest_session_hash=None,
                project_id=None,
            )
        )
        session.commit()

    with pytest.raises(HTTPException) as hidden:
        accept_project_invitation(
            invitation.invitation_id,
            _request(
                invitation_harness,
                path=f"/api/invitations/{invitation.invitation_id}/accept",
                user="outsider",
            ),
        )
    assert hidden.value.status_code == status.HTTP_404_NOT_FOUND
    assert hidden.value.detail["error"] == "project_invitation_not_found"

    accepted = accept_project_invitation(
        invitation.invitation_id,
        _request(
            invitation_harness,
            path=f"/api/invitations/{invitation.invitation_id}/accept",
            user="alice",
        ),
    )
    assert accepted.status == "accepted"
    assert accepted.resolved_at is not None

    with invitation_harness.session_factory() as session:
        persisted_project = session.get(Project, project.id)
        member = session.get(ProjectMember, (project.id, invitation_harness.user_ids["alice"]))
        persisted_invitation = session.get(ProjectInvitation, invitation.invitation_id)
        task = session.execute(
            select(WebTask).where(WebTask.core_task_id == "alice-standalone-task")
        ).scalar_one()
        events = session.execute(
            select(ProjectHistoryEvent).where(ProjectHistoryEvent.project_id == project.id)
        ).scalars()
        joined_events = [
            event
            for event in events
            if event.event_type == "member_joined"
            and event.subject_user_id == invitation_harness.user_ids["alice"]
        ]
        assert persisted_project is not None
        assert persisted_project.owner_user_id == invitation_harness.user_ids["owner"]
        assert member is not None
        assert member.role == "supervisor"
        assert member.joined_at is not None
        assert persisted_invitation is not None
        assert persisted_invitation.status == "accepted"
        assert persisted_invitation.resolved_at is not None
        assert len(joined_events) == 1
        assert joined_events[0].actor_user_id == invitation_harness.user_ids["alice"]
        assert joined_events[0].data == {
            "source": "invitation",
            "invitation_id": invitation.invitation_id,
            "role": "supervisor",
        }
        assert task.owner_user_id == invitation_harness.user_ids["alice"]
        assert task.project_id is None
        assert all(not event.event_type.startswith("invitation_") for event in events)

    with pytest.raises(ProjectConflictError):
        service.accept_invitation(
            actor_user_id=invitation_harness.user_ids["alice"],
            invitation_id=invitation.invitation_id,
        )
    with pytest.raises(ProjectConflictError):
        service.decline_invitation(
            actor_user_id=invitation_harness.user_ids["alice"],
            invitation_id=invitation.invitation_id,
        )
    with pytest.raises(ProjectConflictError):
        service.revoke_invitation(
            actor_user_id=invitation_harness.user_ids["owner"],
            project_id=project.id,
            invitation_id=invitation.invitation_id,
        )
    with pytest.raises(ProjectConflictError) as accepted_member_reinvite:
        service.create_invitation(
            actor_user_id=invitation_harness.user_ids["owner"],
            project_id=project.id,
            invited_user_id=invitation_harness.user_ids["alice"],
            role="viewer",
        )
    assert accepted_member_reinvite.value.code == "project_invitation_target_already_member"
    with invitation_harness.session_factory() as session:
        memberships = session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == invitation_harness.user_ids["alice"],
            )
        ).scalars()
        assert len(list(memberships)) == 1


def test_acceptance_rolls_back_membership_and_resolution_on_history_failure(
    invitation_harness: _InvitationHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = invitation_harness.app.state.jelica_api_state.project_service
    project = _create_project(invitation_harness)
    invitation = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=invitation_harness.user_ids["alice"],
        role="member",
    )
    original_add_history_event = projects_module._add_history_event

    def fail_invitation_join(**kwargs: object) -> None:
        if kwargs.get("event_type") == "member_joined" and kwargs.get("data") is not None:
            raise RuntimeError("simulated invitation history failure")
        original_add_history_event(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(projects_module, "_add_history_event", fail_invitation_join)
    with pytest.raises(RuntimeError, match="simulated invitation history failure"):
        service.accept_invitation(
            actor_user_id=invitation_harness.user_ids["alice"],
            invitation_id=invitation.invitation_id,
        )

    with invitation_harness.session_factory() as session:
        persisted = session.get(ProjectInvitation, invitation.invitation_id)
        member = session.get(ProjectMember, (project.id, invitation_harness.user_ids["alice"]))
        assert persisted is not None
        assert persisted.status == "pending"
        assert persisted.resolved_at is None
        assert member is None


def test_expiration_frozen_acceptance_and_reinvitation_semantics(
    invitation_harness: _InvitationHarness,
) -> None:
    service = invitation_harness.app.state.jelica_api_state.project_service
    project = _create_project(invitation_harness)
    expired = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=invitation_harness.user_ids["alice"],
        role="viewer",
    )
    assert (
        service.list_received_invitations(
            actor_user_id=invitation_harness.user_ids["alice"],
            statuses=("pending",),
        )[0].status
        == "pending"
    )
    _expire_invitation(invitation_harness, expired.invitation_id)

    expired_records = service.list_received_invitations(
        actor_user_id=invitation_harness.user_ids["alice"],
        statuses=("expired",),
    )
    assert [record.invitation_id for record in expired_records] == [expired.invitation_id]
    with invitation_harness.session_factory() as session:
        stored = session.get(ProjectInvitation, expired.invitation_id)
        assert stored is not None
        assert stored.status == "pending"
        assert stored.resolved_at is None
    with pytest.raises(ProjectConflictError) as accept_expired:
        service.accept_invitation(
            actor_user_id=invitation_harness.user_ids["alice"],
            invitation_id=expired.invitation_id,
        )
    assert accept_expired.value.code == "project_invitation_expired"
    with pytest.raises(ProjectConflictError) as decline_expired:
        service.decline_invitation(
            actor_user_id=invitation_harness.user_ids["alice"],
            invitation_id=expired.invitation_id,
        )
    assert decline_expired.value.code == "project_invitation_expired"
    with pytest.raises(ProjectConflictError) as revoke_expired:
        service.revoke_invitation(
            actor_user_id=invitation_harness.user_ids["owner"],
            project_id=project.id,
            invitation_id=expired.invitation_id,
        )
    assert revoke_expired.value.code == "project_invitation_expired"

    replacement = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=invitation_harness.user_ids["alice"],
        role="member",
    )
    _set_project_status(invitation_harness, project.id, "frozen")
    with pytest.raises(ProjectConflictError) as frozen_create:
        service.create_invitation(
            actor_user_id=invitation_harness.user_ids["owner"],
            project_id=project.id,
            invited_user_id=invitation_harness.user_ids["bob"],
            role="member",
        )
    assert frozen_create.value.code == "project_frozen"
    with pytest.raises(ProjectConflictError) as frozen_accept:
        service.accept_invitation(
            actor_user_id=invitation_harness.user_ids["alice"],
            invitation_id=replacement.invitation_id,
        )
    assert frozen_accept.value.code == "project_frozen"
    declined = service.decline_invitation(
        actor_user_id=invitation_harness.user_ids["alice"],
        invitation_id=replacement.invitation_id,
    )
    assert declined.status == "declined"
    with pytest.raises(ProjectConflictError):
        service.accept_invitation(
            actor_user_id=invitation_harness.user_ids["alice"],
            invitation_id=replacement.invitation_id,
        )
    with invitation_harness.session_factory() as session:
        assert (
            session.get(ProjectMember, (project.id, invitation_harness.user_ids["alice"])) is None
        )


def test_decline_and_revoke_permissions_terminal_states_and_project_cascade(
    invitation_harness: _InvitationHarness,
) -> None:
    service = invitation_harness.app.state.jelica_api_state.project_service
    project = _create_project(invitation_harness)
    _add_member(invitation_harness, project.id, "supervisor", "supervisor")
    _add_member(invitation_harness, project.id, "member", "member")
    declined_invitation = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=invitation_harness.user_ids["alice"],
        role="member",
    )
    with pytest.raises(ProjectInvitationNotFoundError):
        service.decline_invitation(
            actor_user_id=invitation_harness.user_ids["bob"],
            invitation_id=declined_invitation.invitation_id,
        )
    declined = decline_project_invitation(
        declined_invitation.invitation_id,
        _request(
            invitation_harness,
            path=f"/api/invitations/{declined_invitation.invitation_id}/decline",
            user="alice",
        ),
    )
    assert declined.status == "declined"
    assert declined.resolved_at is not None
    with pytest.raises(ProjectConflictError):
        service.accept_invitation(
            actor_user_id=invitation_harness.user_ids["alice"],
            invitation_id=declined_invitation.invitation_id,
        )
    replacement_after_decline = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=invitation_harness.user_ids["alice"],
        role="viewer",
    )
    assert replacement_after_decline.status == "pending"

    revoked_invitation = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=invitation_harness.user_ids["bob"],
        role="viewer",
    )
    with pytest.raises(ProjectPermissionError):
        service.revoke_invitation(
            actor_user_id=invitation_harness.user_ids["member"],
            project_id=project.id,
            invitation_id=revoked_invitation.invitation_id,
        )
    revoked = service.revoke_invitation(
        actor_user_id=invitation_harness.user_ids["supervisor"],
        project_id=project.id,
        invitation_id=revoked_invitation.invitation_id,
    )
    assert revoked.status == "revoked"
    with pytest.raises(ProjectConflictError):
        service.accept_invitation(
            actor_user_id=invitation_harness.user_ids["bob"],
            invitation_id=revoked_invitation.invitation_id,
        )
    with pytest.raises(ProjectConflictError):
        service.decline_invitation(
            actor_user_id=invitation_harness.user_ids["bob"],
            invitation_id=revoked_invitation.invitation_id,
        )
    replacement_after_revoke = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=invitation_harness.user_ids["bob"],
        role="member",
    )
    assert replacement_after_revoke.status == "pending"

    frozen_revoke = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=invitation_harness.user_ids["carol"],
        role="commenter",
    )
    _set_project_status(invitation_harness, project.id, "frozen")
    frozen_revoked = revoke_project_invitation(
        project.id,
        frozen_revoke.invitation_id,
        _request(
            invitation_harness,
            path=(f"/api/projects/{project.id}/invitations/{frozen_revoke.invitation_id}/revoke"),
            user="owner",
        ),
    )
    assert frozen_revoked.status == "revoked"

    with invitation_harness.session_factory() as session:
        assert session.get(ProjectInvitation, declined_invitation.invitation_id) is not None
        assert session.get(ProjectInvitation, revoked_invitation.invitation_id) is not None
        assert session.get(ProjectInvitation, frozen_revoke.invitation_id) is not None
        assert (
            session.get(ProjectMember, (project.id, invitation_harness.user_ids["alice"])) is None
        )
        event_types = {
            event.event_type
            for event in session.execute(
                select(ProjectHistoryEvent).where(ProjectHistoryEvent.project_id == project.id)
            ).scalars()
        }
        assert not any(event_type.startswith("invitation_") for event_type in event_types)

    service.delete_project(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
    )
    with invitation_harness.session_factory() as session:
        assert (
            session.execute(
                select(ProjectInvitation).where(ProjectInvitation.project_id == project.id)
            )
            .scalars()
            .all()
            == []
        )


def test_project_and_received_lists_apply_filters_without_expanding_scope(
    invitation_harness: _InvitationHarness,
) -> None:
    service = invitation_harness.app.state.jelica_api_state.project_service
    first = _create_project(invitation_harness, name="First invitation project")
    second = _create_project(invitation_harness, name="Second invitation project")
    _add_member(invitation_harness, first.id, "member", "member")
    first_alice = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=first.id,
        invited_user_id=invitation_harness.user_ids["alice"],
        role="member",
    )
    first_bob = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=first.id,
        invited_user_id=invitation_harness.user_ids["bob"],
        role="supervisor",
    )
    service.decline_invitation(
        actor_user_id=invitation_harness.user_ids["bob"],
        invitation_id=first_bob.invitation_id,
    )
    service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=first.id,
        invited_user_id=invitation_harness.user_ids["carol"],
        role="viewer",
    )
    second_alice = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=second.id,
        invited_user_id=invitation_harness.user_ids["alice"],
        role="commenter",
    )
    _expire_invitation(invitation_harness, second_alice.invitation_id)
    second_bob = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=second.id,
        invited_user_id=invitation_harness.user_ids["bob"],
        role="member",
    )
    service.revoke_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=second.id,
        invitation_id=second_bob.invitation_id,
    )

    project_filtered = list_project_invitations(
        first.id,
        _request(
            invitation_harness,
            path=f"/api/projects/{first.id}/invitations",
            user="owner",
        ),
        status=["pending", "declined"],
        role=["member", "supervisor"],
        invited_user_id=[
            invitation_harness.user_ids["alice"],
            invitation_harness.user_ids["bob"],
        ],
    )
    assert {item.invitation_id for item in project_filtered.items} == {
        first_alice.invitation_id,
        first_bob.invitation_id,
    }
    with pytest.raises(HTTPException) as member_list:
        list_project_invitations(
            first.id,
            _request(
                invitation_harness,
                path=f"/api/projects/{first.id}/invitations",
                user="member",
            ),
        )
    assert member_list.value.status_code == status.HTTP_403_FORBIDDEN

    alice_received = list_received_invitations(
        _request(invitation_harness, path="/api/invitations", user="alice"),
        status=["pending", "expired"],
        role=["member", "commenter"],
        project_id=[first.id, second.id],
    )
    assert {item.invitation_id for item in alice_received.items} == {
        first_alice.invitation_id,
        second_alice.invitation_id,
    }
    preview_by_id = {item.invitation_id: item for item in alice_received.items}
    assert preview_by_id[first_alice.invitation_id].project_name == first.name
    assert preview_by_id[first_alice.invitation_id].inviter_username == "owner"
    assert preview_by_id[first_alice.invitation_id].status == "pending"
    assert preview_by_id[second_alice.invitation_id].project_name == second.name
    assert preview_by_id[second_alice.invitation_id].status == "expired"

    bob_received = list_received_invitations(
        _request(invitation_harness, path="/api/invitations", user="bob")
    )
    assert {item.invitation_id for item in bob_received.items} == {
        first_bob.invitation_id,
        second_bob.invitation_id,
    }
    assert (
        list_received_invitations(
            _request(invitation_harness, path="/api/invitations", user="outsider"),
            project_id=[first.id, second.id],
        ).items
        == ()
    )
    with pytest.raises(ProjectPermissionError):
        service.get_project(
            actor_user_id=invitation_harness.user_ids["alice"],
            project_id=first.id,
        )


def _create_project(
    invitation_harness: _InvitationHarness,
    *,
    name: str = "Invitation project",
) -> Project:
    record = invitation_harness.app.state.jelica_api_state.project_service.create_project(
        actor_user_id=invitation_harness.user_ids["owner"],
        name=name,
        description=None,
    )
    with invitation_harness.session_factory() as session:
        project = session.get(Project, record.project_id)
        assert project is not None
        session.expunge(project)
        return project


def _add_member(
    invitation_harness: _InvitationHarness,
    project_id: str,
    user: str,
    role: str,
) -> None:
    with invitation_harness.session_factory() as session:
        session.add(
            ProjectMember(
                project_id=project_id,
                user_id=invitation_harness.user_ids[user],
                role=role,
                joined_at=datetime.now(UTC),
            )
        )
        session.commit()


def _expire_invitation(
    invitation_harness: _InvitationHarness,
    invitation_id: str,
) -> None:
    with invitation_harness.session_factory() as session:
        invitation = session.get(ProjectInvitation, invitation_id)
        assert invitation is not None
        invitation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def _set_project_status(
    invitation_harness: _InvitationHarness,
    project_id: str,
    project_status: str,
) -> None:
    with invitation_harness.session_factory() as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.status = project_status
        session.commit()


def _request(
    invitation_harness: _InvitationHarness,
    *,
    path: str,
    user: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if user is not None:
        headers.append(
            (
                b"cookie",
                f"jelica_session={invitation_harness.session_tokens[user]}".encode("ascii"),
            )
        )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": headers,
            "app": invitation_harness.app,
        }
    )
