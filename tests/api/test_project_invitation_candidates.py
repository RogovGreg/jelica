from __future__ import annotations

import pytest
from fastapi import HTTPException, status
from test_project_invitations import (
    _add_member,
    _create_project,
    _expire_invitation,
    _request,
)

from jelica_api.api.routes.invitations import list_project_invitation_candidates
from jelica_api.contracts import ProjectInvitationCandidateListResponse
from jelica_api.models import User
from jelica_api.projects import ProjectValidationError

pytest_plugins = ("test_project_invitations",)


def test_candidates_search_exclusions_order_and_safe_response(invitation_harness) -> None:
    service = invitation_harness.app.state.jelica_api_state.project_service
    project = _create_project(invitation_harness)
    _add_member(invitation_harness, project.id, "member", "member")
    users = {
        name: _add_user(invitation_harness, name)
        for name in (
            "greg",
            "greg_dev",
            "gregory",
            "term_declined",
            "term_revoked",
            "term_pending",
            "under_score",
            "underX",
        )
    }
    active = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=users["gregory"],
        role="member",
    )
    result = list_project_invitation_candidates(
        project.id,
        _request(
            invitation_harness,
            path=f"/api/projects/{project.id}/invitation-candidates",
            user="owner",
        ),
        q=" GRe ",
    )
    assert isinstance(result, ProjectInvitationCandidateListResponse)
    assert [item.username for item in result.items] == ["greg", "greg_dev"]
    assert all(set(item.model_dump()) == {"user_id", "username"} for item in result.items)
    assert active.invitation_id

    declined = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=users["term_declined"],
        role="member",
    )
    service.decline_invitation(
        actor_user_id=users["term_declined"],
        invitation_id=declined.invitation_id,
    )
    revoked = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=users["term_revoked"],
        role="member",
    )
    service.revoke_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invitation_id=revoked.invitation_id,
    )
    pending = service.create_invitation(
        actor_user_id=invitation_harness.user_ids["owner"],
        project_id=project.id,
        invited_user_id=users["term_pending"],
        role="member",
    )
    terminal_result = list_project_invitation_candidates(
        project.id,
        _request(
            invitation_harness,
            path=f"/api/projects/{project.id}/invitation-candidates",
            user="owner",
        ),
        q="term",
    )
    assert [item.username for item in terminal_result.items] == [
        "term_declined",
        "term_revoked",
    ]
    assert pending.invitation_id

    wildcard_result = list_project_invitation_candidates(
        project.id,
        _request(
            invitation_harness,
            path=f"/api/projects/{project.id}/invitation-candidates",
            user="owner",
        ),
        q="under_",
    )
    assert [item.username for item in wildcard_result.items] == ["under_score"]

    _expire_invitation(invitation_harness, active.invitation_id)
    eligible = list_project_invitation_candidates(
        project.id,
        _request(
            invitation_harness,
            path=f"/api/projects/{project.id}/invitation-candidates",
            user="owner",
        ),
        q="greg",
    )
    assert "gregory" in [item.username for item in eligible.items]


def test_candidates_authorization_frozen_and_limits(invitation_harness) -> None:
    project = _create_project(invitation_harness)
    _add_member(invitation_harness, project.id, "supervisor", "supervisor")
    _add_member(invitation_harness, project.id, "member", "member")
    _add_member(invitation_harness, project.id, "viewer", "viewer")
    path = f"/api/projects/{project.id}/invitation-candidates"

    for actor in ("owner", "supervisor"):
        response = list_project_invitation_candidates(
            project.id,
            _request(invitation_harness, path=path, user=actor),
            q="ali",
        )
        assert [item.username for item in response.items] == ["alice"]

    with invitation_harness.session_factory() as session:
        project_row = session.get(type(project), project.id)
        assert project_row is not None
        project_row.status = "frozen"
        session.commit()
    assert [
        item.username
        for item in list_project_invitation_candidates(
            project.id,
            _request(invitation_harness, path=path, user="owner"),
            q="ali",
        ).items
    ] == ["alice"]

    for actor in ("member", "viewer", "outsider"):
        with pytest.raises(HTTPException) as error:
            list_project_invitation_candidates(
                project.id,
                _request(invitation_harness, path=path, user=actor),
                q="ali",
            )
        assert error.value.status_code == status.HTTP_403_FORBIDDEN

    with pytest.raises(HTTPException) as guest_error:
        list_project_invitation_candidates(
            project.id,
            _request(invitation_harness, path=path),
            q="ali",
        )
    assert guest_error.value.status_code == status.HTTP_401_UNAUTHORIZED

    service = invitation_harness.app.state.jelica_api_state.project_service
    with pytest.raises(ProjectValidationError):
        service.list_invitation_candidates(
            actor_user_id=invitation_harness.user_ids["owner"],
            project_id=project.id,
            query="   ",
        )
    with pytest.raises(ProjectValidationError):
        service.list_invitation_candidates(
            actor_user_id=invitation_harness.user_ids["owner"],
            project_id=project.id,
            query="a",
            limit=21,
        )


def _add_user(invitation_harness, username: str) -> str:
    with invitation_harness.session_factory() as session:
        user = User(
            username=username,
            email=f"{username.replace('%', 'percent')}@example.org",
            password_hash="test-password-hash",
            email_verified=True,
            language="en",
        )
        session.add(user)
        session.commit()
        return user.id
