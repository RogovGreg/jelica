from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from jelica_api.api.routes.comments import (
    create_project_comment,
    delete_project_comment,
    edit_project_comment,
    list_project_comments,
)
from jelica_api.app import create_app
from jelica_api.auth import hash_opaque_token
from jelica_api.contracts.comments import (
    ProjectCommentCreateRequest,
    ProjectCommentUpdateRequest,
)
from jelica_api.models import (
    AuthSession,
    Base,
    Project,
    ProjectComment,
    ProjectHistoryEvent,
    ProjectMember,
    User,
)
from jelica_api.settings import ApiSettings


@dataclass(frozen=True, slots=True)
class _CommentHarness:
    app: FastAPI
    session_factory: sessionmaker[Session]
    user_ids: dict[str, str]
    session_tokens: dict[str, str]


@pytest.fixture
def comment_harness() -> Iterator[_CommentHarness]:
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
            "commenter",
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
        yield _CommentHarness(
            app=app,
            session_factory=state.session_factory,
            user_ids=user_ids,
            session_tokens=session_tokens,
        )
    finally:
        state.task_orchestrator.shutdown()
        state.engine.dispose()


def test_comment_routes_auth_creation_roles_validation_and_relationships(
    comment_harness: _CommentHarness,
) -> None:
    paths = comment_harness.app.openapi()["paths"]
    assert {"get", "post"}.issubset(paths["/api/projects/{project_id}/comments"])
    assert {"patch", "delete"}.issubset(paths["/api/projects/{project_id}/comments/{comment_id}"])

    project = _create_project(comment_harness)
    for user, role in (
        ("supervisor", "supervisor"),
        ("commenter", "commenter"),
        ("member", "member"),
        ("viewer", "viewer"),
    ):
        _add_member(comment_harness, project.id, user, role)

    created_ids: list[str] = []
    for actor in ("owner", "supervisor", "commenter", "member"):
        created = create_project_comment(
            project.id,
            ProjectCommentCreateRequest(body=f"  {actor} plain-text @missing  "),
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments",
                user=actor,
            ),
        )
        created_ids.append(created.id)
        assert created.project_id == project.id
        assert created.author_user_id == comment_harness.user_ids[actor]
        assert created.author_username == actor
        assert created.body == f"{actor} plain-text @missing"
        assert created.edited_at is None

    for forbidden_actor in ("viewer", "outsider"):
        with pytest.raises(HTTPException) as forbidden:
            create_project_comment(
                project.id,
                ProjectCommentCreateRequest(body="Not permitted"),
                _request(
                    comment_harness,
                    path=f"/api/projects/{project.id}/comments",
                    user=forbidden_actor,
                ),
            )
        assert forbidden.value.status_code == status.HTTP_403_FORBIDDEN

    with pytest.raises(HTTPException) as guest_create:
        create_project_comment(
            project.id,
            ProjectCommentCreateRequest(body="Guest comment"),
            _request(comment_harness, path=f"/api/projects/{project.id}/comments"),
        )
    assert guest_create.value.status_code == status.HTTP_401_UNAUTHORIZED

    with pytest.raises(HTTPException) as whitespace:
        create_project_comment(
            project.id,
            ProjectCommentCreateRequest(body="   \n\t  "),
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments",
                user="owner",
            ),
        )
    assert whitespace.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    with pytest.raises(ValidationError):
        ProjectCommentCreateRequest(body="")
    with pytest.raises(ValidationError):
        ProjectCommentCreateRequest(body="x" * 10_001)
    with pytest.raises(ValidationError):
        ProjectCommentCreateRequest.model_validate(
            {"body": "Cannot spoof author", "author_user_id": comment_harness.user_ids["bob"]}
        )

    with comment_harness.session_factory() as session:
        persisted_project = session.get(Project, project.id)
        assert persisted_project is not None
        assert {comment.id for comment in persisted_project.comments} == set(created_ids)
        persisted = session.get(ProjectComment, created_ids[0])
        assert persisted is not None
        assert persisted.project.id == project.id
        assert persisted.author.id == comment_harness.user_ids["owner"]
    assert not hasattr(User, "project_comments")
    assert not hasattr(User, "authored_project_comments")


def test_comment_list_is_member_scoped_chronological_and_public_safe(
    comment_harness: _CommentHarness,
) -> None:
    project = _create_project(comment_harness)
    for user, role in (
        ("supervisor", "supervisor"),
        ("commenter", "commenter"),
        ("member", "member"),
        ("viewer", "viewer"),
    ):
        _add_member(comment_harness, project.id, user, role)
    responses = [
        _create_comment(comment_harness, project.id, "owner", "Later"),
        _create_comment(comment_harness, project.id, "commenter", "Same timestamp A"),
        _create_comment(comment_harness, project.id, "member", "Same timestamp B"),
    ]
    first_at = datetime(2026, 8, 25, 11, tzinfo=UTC)
    shared_at = datetime(2026, 8, 25, 10, tzinfo=UTC)
    with comment_harness.session_factory() as session:
        session.get(ProjectComment, responses[0].id).created_at = first_at
        session.get(ProjectComment, responses[1].id).created_at = shared_at
        session.get(ProjectComment, responses[2].id).created_at = shared_at
        session.commit()
    expected_ids = sorted((responses[1].id, responses[2].id)) + [responses[0].id]

    for actor in ("owner", "supervisor", "commenter", "member", "viewer"):
        listed = list_project_comments(
            project.id,
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments",
                user=actor,
            ),
        )
        assert [comment.id for comment in listed.items] == expected_ids
        assert {comment.author_username for comment in listed.items} == {
            "owner",
            "commenter",
            "member",
        }
        assert set(listed.items[0].model_dump()) == {
            "id",
            "project_id",
            "author_user_id",
            "author_username",
            "body",
            "created_at",
            "edited_at",
            "mentions",
            "reaction_summary",
        }

    for actor in ("outsider", None):
        with pytest.raises(HTTPException) as denied:
            list_project_comments(
                project.id,
                _request(
                    comment_harness,
                    path=f"/api/projects/{project.id}/comments",
                    user=actor,
                ),
            )
        assert denied.value.status_code == (
            status.HTTP_401_UNAUTHORIZED if actor is None else status.HTTP_403_FORBIDDEN
        )


def test_comment_edit_is_author_only_and_requires_current_comment_role(
    comment_harness: _CommentHarness,
) -> None:
    project = _create_project(comment_harness)
    other_project = _create_project(comment_harness, name="Other project")
    for project_id in (project.id, other_project.id):
        _add_member(comment_harness, project_id, "alice", "commenter")
        _add_member(comment_harness, project_id, "bob", "commenter")
        _add_member(comment_harness, project_id, "supervisor", "supervisor")
    created = _create_comment(comment_harness, project.id, "alice", "Original")

    edited = edit_project_comment(
        project.id,
        created.id,
        ProjectCommentUpdateRequest(body="  Updated body  "),
        _request(
            comment_harness,
            path=f"/api/projects/{project.id}/comments/{created.id}",
            user="alice",
        ),
    )
    assert edited.body == "Updated body"
    assert edited.edited_at is not None
    assert edited.created_at.replace(tzinfo=None) == created.created_at.replace(tzinfo=None)
    assert edited.author_user_id == created.author_user_id

    for other_actor in ("bob", "supervisor"):
        with pytest.raises(HTTPException) as other_edit:
            edit_project_comment(
                project.id,
                created.id,
                ProjectCommentUpdateRequest(body="Cannot edit another author"),
                _request(
                    comment_harness,
                    path=f"/api/projects/{project.id}/comments/{created.id}",
                    user=other_actor,
                ),
            )
        assert other_edit.value.status_code == status.HTTP_403_FORBIDDEN

    comment_harness.app.state.jelica_api_state.project_service.update_member_role(
        actor_user_id=comment_harness.user_ids["owner"],
        project_id=project.id,
        user_id=comment_harness.user_ids["alice"],
        role="viewer",
    )
    with pytest.raises(HTTPException) as viewer_edit:
        edit_project_comment(
            project.id,
            created.id,
            ProjectCommentUpdateRequest(body="Viewer cannot edit old comment"),
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments/{created.id}",
                user="alice",
            ),
        )
    assert viewer_edit.value.status_code == status.HTTP_403_FORBIDDEN

    with pytest.raises(HTTPException) as cross_project:
        edit_project_comment(
            other_project.id,
            created.id,
            ProjectCommentUpdateRequest(body="Wrong project"),
            _request(
                comment_harness,
                path=f"/api/projects/{other_project.id}/comments/{created.id}",
                user="owner",
            ),
        )
    assert cross_project.value.status_code == status.HTTP_404_NOT_FOUND
    with pytest.raises(HTTPException) as whitespace:
        edit_project_comment(
            project.id,
            created.id,
            ProjectCommentUpdateRequest(body=" \n "),
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments/{created.id}",
                user="alice",
            ),
        )
    assert whitespace.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    with pytest.raises(ValidationError):
        ProjectCommentUpdateRequest(body="x" * 10_001)


def test_comment_hard_delete_allows_author_and_supervisor_moderation_only(
    comment_harness: _CommentHarness,
) -> None:
    project = _create_project(comment_harness)
    other_project = _create_project(comment_harness, name="Delete other project")
    for project_id in (project.id, other_project.id):
        for user, role in (
            ("supervisor", "supervisor"),
            ("alice", "commenter"),
            ("bob", "commenter"),
            ("member", "member"),
            ("viewer", "viewer"),
        ):
            _add_member(comment_harness, project_id, user, role)

    own = _create_comment(comment_harness, project.id, "alice", "Author deletes")
    assert (
        delete_project_comment(
            project.id,
            own.id,
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments/{own.id}",
                user="alice",
            ),
        )
        is None
    )
    with comment_harness.session_factory() as session:
        assert session.get(ProjectComment, own.id) is None
    with pytest.raises(HTTPException) as repeated:
        delete_project_comment(
            project.id,
            own.id,
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments/{own.id}",
                user="alice",
            ),
        )
    assert repeated.value.status_code == status.HTTP_404_NOT_FOUND

    moderated = _create_comment(comment_harness, project.id, "alice", "Moderated")
    delete_project_comment(
        project.id,
        moderated.id,
        _request(
            comment_harness,
            path=f"/api/projects/{project.id}/comments/{moderated.id}",
            user="supervisor",
        ),
    )
    owner_moderated = _create_comment(comment_harness, project.id, "alice", "Owner moderated")
    delete_project_comment(
        project.id,
        owner_moderated.id,
        _request(
            comment_harness,
            path=f"/api/projects/{project.id}/comments/{owner_moderated.id}",
            user="owner",
        ),
    )

    protected = _create_comment(comment_harness, project.id, "alice", "Protected")
    demoted_owned = _create_comment(
        comment_harness,
        project.id,
        "alice",
        "Viewer cannot delete an older own comment",
    )
    for ordinary_actor in ("bob", "member", "viewer"):
        with pytest.raises(HTTPException) as forbidden:
            delete_project_comment(
                project.id,
                protected.id,
                _request(
                    comment_harness,
                    path=f"/api/projects/{project.id}/comments/{protected.id}",
                    user=ordinary_actor,
                ),
            )
        assert forbidden.value.status_code == status.HTTP_403_FORBIDDEN

    comment_harness.app.state.jelica_api_state.project_service.update_member_role(
        actor_user_id=comment_harness.user_ids["owner"],
        project_id=project.id,
        user_id=comment_harness.user_ids["alice"],
        role="viewer",
    )
    with pytest.raises(HTTPException) as demoted_author:
        delete_project_comment(
            project.id,
            demoted_owned.id,
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments/{demoted_owned.id}",
                user="alice",
            ),
        )
    assert demoted_author.value.status_code == status.HTTP_403_FORBIDDEN

    with pytest.raises(HTTPException) as cross_project:
        delete_project_comment(
            other_project.id,
            protected.id,
            _request(
                comment_harness,
                path=f"/api/projects/{other_project.id}/comments/{protected.id}",
                user="owner",
            ),
        )
    assert cross_project.value.status_code == status.HTTP_404_NOT_FOUND
    listed = list_project_comments(
        project.id,
        _request(
            comment_harness,
            path=f"/api/projects/{project.id}/comments",
            user="owner",
        ),
    )
    assert {comment.id for comment in listed.items} == {protected.id, demoted_owned.id}


def test_frozen_project_comments_are_read_only_for_owner_and_supervisor(
    comment_harness: _CommentHarness,
) -> None:
    project = _create_project(comment_harness)
    _add_member(comment_harness, project.id, "supervisor", "supervisor")
    existing = _create_comment(comment_harness, project.id, "owner", "Freeze me")
    _set_project_status(comment_harness, project.id, "frozen")

    for actor in ("owner", "supervisor"):
        listed = list_project_comments(
            project.id,
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments",
                user=actor,
            ),
        )
        assert [comment.id for comment in listed.items] == [existing.id]

        with pytest.raises(HTTPException) as create_error:
            create_project_comment(
                project.id,
                ProjectCommentCreateRequest(body="Frozen creation"),
                _request(
                    comment_harness,
                    path=f"/api/projects/{project.id}/comments",
                    user=actor,
                ),
            )
        assert create_error.value.status_code == status.HTTP_409_CONFLICT
        with pytest.raises(HTTPException) as edit_error:
            edit_project_comment(
                project.id,
                existing.id,
                ProjectCommentUpdateRequest(body="Frozen edit"),
                _request(
                    comment_harness,
                    path=f"/api/projects/{project.id}/comments/{existing.id}",
                    user=actor,
                ),
            )
        assert edit_error.value.status_code == status.HTTP_409_CONFLICT
        with pytest.raises(HTTPException) as delete_error:
            delete_project_comment(
                project.id,
                existing.id,
                _request(
                    comment_harness,
                    path=f"/api/projects/{project.id}/comments/{existing.id}",
                    user=actor,
                ),
            )
        assert delete_error.value.status_code == status.HTTP_409_CONFLICT


def test_comment_survives_author_removal_then_cascades_with_project(
    comment_harness: _CommentHarness,
) -> None:
    project = _create_project(comment_harness)
    _add_member(comment_harness, project.id, "alice", "commenter")
    _add_member(comment_harness, project.id, "bob", "member")
    comment = _create_comment(comment_harness, project.id, "alice", "Keep discussion context")

    service = comment_harness.app.state.jelica_api_state.project_service
    service.remove_member(
        actor_user_id=comment_harness.user_ids["owner"],
        project_id=project.id,
        user_id=comment_harness.user_ids["alice"],
    )
    with comment_harness.session_factory() as session:
        persisted = session.get(ProjectComment, comment.id)
        assert persisted is not None
        assert persisted.author_user_id == comment_harness.user_ids["alice"]

    bob_view = list_project_comments(
        project.id,
        _request(
            comment_harness,
            path=f"/api/projects/{project.id}/comments",
            user="bob",
        ),
    )
    assert [item.id for item in bob_view.items] == [comment.id]
    with pytest.raises(HTTPException) as former_member:
        list_project_comments(
            project.id,
            _request(
                comment_harness,
                path=f"/api/projects/{project.id}/comments",
                user="alice",
            ),
        )
    assert former_member.value.status_code == status.HTTP_403_FORBIDDEN

    service.delete_project(
        actor_user_id=comment_harness.user_ids["owner"],
        project_id=project.id,
    )
    with comment_harness.session_factory() as session:
        assert session.get(ProjectComment, comment.id) is None


def test_comment_lifecycle_does_not_write_project_history(
    comment_harness: _CommentHarness,
) -> None:
    project = _create_project(comment_harness)
    with comment_harness.session_factory() as session:
        before = tuple(
            session.execute(
                select(ProjectHistoryEvent)
                .where(ProjectHistoryEvent.project_id == project.id)
                .order_by(ProjectHistoryEvent.id)
            ).scalars()
        )
        before_snapshot = tuple(
            (event.id, event.event_type, event.actor_user_id, event.data) for event in before
        )

    created = _create_comment(comment_harness, project.id, "owner", "No history event")
    edit_project_comment(
        project.id,
        created.id,
        ProjectCommentUpdateRequest(body="Still no history event"),
        _request(
            comment_harness,
            path=f"/api/projects/{project.id}/comments/{created.id}",
            user="owner",
        ),
    )
    delete_project_comment(
        project.id,
        created.id,
        _request(
            comment_harness,
            path=f"/api/projects/{project.id}/comments/{created.id}",
            user="owner",
        ),
    )

    with comment_harness.session_factory() as session:
        after = tuple(
            session.execute(
                select(ProjectHistoryEvent)
                .where(ProjectHistoryEvent.project_id == project.id)
                .order_by(ProjectHistoryEvent.id)
            ).scalars()
        )
        assert (
            tuple((event.id, event.event_type, event.actor_user_id, event.data) for event in after)
            == before_snapshot
        )
        assert not any(event.event_type.startswith("comment_") for event in after)


def _create_project(
    comment_harness: _CommentHarness,
    *,
    name: str = "Comment project",
) -> Project:
    record = comment_harness.app.state.jelica_api_state.project_service.create_project(
        actor_user_id=comment_harness.user_ids["owner"],
        name=name,
        description=None,
    )
    with comment_harness.session_factory() as session:
        project = session.get(Project, record.project_id)
        assert project is not None
        session.expunge(project)
        return project


def _add_member(
    comment_harness: _CommentHarness,
    project_id: str,
    user: str,
    role: str,
) -> None:
    with comment_harness.session_factory() as session:
        session.add(
            ProjectMember(
                project_id=project_id,
                user_id=comment_harness.user_ids[user],
                role=role,
                joined_at=datetime.now(UTC),
            )
        )
        session.commit()


def _create_comment(
    comment_harness: _CommentHarness,
    project_id: str,
    actor: str,
    body: str,
):
    return create_project_comment(
        project_id,
        ProjectCommentCreateRequest(body=body),
        _request(
            comment_harness,
            path=f"/api/projects/{project_id}/comments",
            user=actor,
        ),
    )


def _set_project_status(
    comment_harness: _CommentHarness,
    project_id: str,
    project_status: str,
) -> None:
    with comment_harness.session_factory() as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.status = project_status
        session.commit()


def _request(
    comment_harness: _CommentHarness,
    *,
    path: str,
    user: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if user is not None:
        headers.append(
            (
                b"cookie",
                f"jelica_session={comment_harness.session_tokens[user]}".encode("ascii"),
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
            "app": comment_harness.app,
        }
    )
