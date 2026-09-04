from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from jelica_api.api.routes.comments import (
    create_project_comment,
    delete_project_comment,
    delete_project_comment_reaction,
    get_project_comment_reactions,
    list_project_comments,
    set_project_comment_reaction,
)
from jelica_api.app import create_app
from jelica_api.auth import hash_opaque_token
from jelica_api.contracts.comments import (
    ProjectCommentCreateRequest,
    ProjectCommentReactionUpdateRequest,
)
from jelica_api.models import (
    AuthSession,
    Base,
    Project,
    ProjectCommentReaction,
    ProjectHistoryEvent,
    ProjectMember,
    User,
)
from jelica_api.settings import ApiSettings


@dataclass(frozen=True, slots=True)
class _ReactionHarness:
    app: FastAPI
    session_factory: sessionmaker[Session]
    user_ids: dict[str, str]
    session_tokens: dict[str, str]


@pytest.fixture
def reaction_harness() -> Iterator[_ReactionHarness]:
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
            raw_token = f"{label}-session-token"
            session.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=hash_opaque_token(raw_token),
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                    last_used_at=now,
                )
            )
            user_ids[label] = user.id
            session_tokens[label] = raw_token
        session.commit()
    try:
        yield _ReactionHarness(
            app=app,
            session_factory=state.session_factory,
            user_ids=user_ids,
            session_tokens=session_tokens,
        )
    finally:
        state.task_orchestrator.shutdown()
        state.engine.dispose()


def test_reaction_support_replace_and_delete_are_idempotent_without_history(
    reaction_harness: _ReactionHarness,
) -> None:
    paths = reaction_harness.app.openapi()["paths"]
    reaction_path = "/api/projects/{project_id}/comments/{comment_id}/reaction"
    summary_path = "/api/projects/{project_id}/comments/{comment_id}/reactions"
    assert {"put", "delete"}.issubset(paths[reaction_path])
    assert "get" in paths[summary_path]

    project = _create_project(reaction_harness)
    _add_member(reaction_harness, project.id, "alice", "commenter")
    _add_member(reaction_harness, project.id, "bob", "commenter")
    comment_id = _create_comment(reaction_harness, project.id, "alice")
    before_history = _history_snapshot(reaction_harness, project.id)

    initial = _get_summary(reaction_harness, project.id, comment_id, "bob")
    assert (initial.support, initial.oppose, initial.current_user_reaction) == (0, 0, None)

    support = _set_reaction(reaction_harness, project.id, comment_id, "bob", "support")
    assert (support.support, support.oppose, support.current_user_reaction) == (
        1,
        0,
        "support",
    )
    repeated = _set_reaction(reaction_harness, project.id, comment_id, "bob", "support")
    assert repeated == support
    _assert_single_reaction(reaction_harness, comment_id, "bob", "support")

    opposed = _set_reaction(reaction_harness, project.id, comment_id, "bob", "oppose")
    assert (opposed.support, opposed.oppose, opposed.current_user_reaction) == (
        0,
        1,
        "oppose",
    )
    _assert_single_reaction(reaction_harness, comment_id, "bob", "oppose")

    assert _delete_reaction(reaction_harness, project.id, comment_id, "bob") is None
    assert _delete_reaction(reaction_harness, project.id, comment_id, "bob") is None
    after_delete = _get_summary(reaction_harness, project.id, comment_id, "bob")
    assert (after_delete.support, after_delete.oppose, after_delete.current_user_reaction) == (
        0,
        0,
        None,
    )
    with reaction_harness.session_factory() as session:
        assert (
            session.get(
                ProjectCommentReaction,
                (comment_id, reaction_harness.user_ids["bob"]),
            )
            is None
        )
    assert _history_snapshot(reaction_harness, project.id) == before_history


def test_comment_list_bulk_reaction_summary_is_personalized_and_private(
    reaction_harness: _ReactionHarness,
) -> None:
    project = _create_project(reaction_harness)
    for user in ("alice", "bob", "carol"):
        _add_member(reaction_harness, project.id, user, "commenter")
    first_comment_id = _create_comment(reaction_harness, project.id, "alice")
    second_comment_id = _create_comment(reaction_harness, project.id, "alice")
    _set_reaction(reaction_harness, project.id, first_comment_id, "bob", "support")
    _set_reaction(reaction_harness, project.id, first_comment_id, "carol", "oppose")

    bob_list = list_project_comments(
        project.id,
        _request(
            reaction_harness,
            path=f"/api/projects/{project.id}/comments",
            user="bob",
        ),
    )
    carol_list = list_project_comments(
        project.id,
        _request(
            reaction_harness,
            path=f"/api/projects/{project.id}/comments",
            user="carol",
        ),
    )
    assert [item.id for item in bob_list.items] == [first_comment_id, second_comment_id]
    assert bob_list.items[0].reaction_summary.model_dump() == {
        "support": 1,
        "oppose": 1,
        "current_user_reaction": "support",
    }
    assert carol_list.items[0].reaction_summary.current_user_reaction == "oppose"
    assert bob_list.items[1].reaction_summary.model_dump() == {
        "support": 0,
        "oppose": 0,
        "current_user_reaction": None,
    }
    assert set(bob_list.items[0].reaction_summary.model_dump()) == {
        "support",
        "oppose",
        "current_user_reaction",
    }


def test_comment_authors_cannot_react_to_their_own_comments_at_any_role(
    reaction_harness: _ReactionHarness,
) -> None:
    project = _create_project(reaction_harness)
    _add_member(reaction_harness, project.id, "alice", "commenter")
    _add_member(reaction_harness, project.id, "supervisor", "supervisor")

    for author in ("alice", "supervisor", "owner"):
        comment_id = _create_comment(reaction_harness, project.id, author)
        with pytest.raises(HTTPException) as self_reaction:
            _set_reaction(reaction_harness, project.id, comment_id, author, "support")
        assert self_reaction.value.status_code == status.HTTP_403_FORBIDDEN
        summary = _get_summary(reaction_harness, project.id, comment_id, author)
        assert (summary.support, summary.oppose, summary.current_user_reaction) == (0, 0, None)


def test_viewer_reads_but_cannot_mutate_while_commenter_can(
    reaction_harness: _ReactionHarness,
) -> None:
    project = _create_project(reaction_harness)
    for user, role in (
        ("alice", "commenter"),
        ("bob", "commenter"),
        ("carol", "commenter"),
        ("viewer", "viewer"),
    ):
        _add_member(reaction_harness, project.id, user, role)
    comment_id = _create_comment(reaction_harness, project.id, "alice")
    _set_reaction(reaction_harness, project.id, comment_id, "bob", "support")

    viewer_summary = _get_summary(reaction_harness, project.id, comment_id, "viewer")
    assert (viewer_summary.support, viewer_summary.oppose) == (1, 0)
    assert viewer_summary.current_user_reaction is None
    for operation in (
        lambda: _set_reaction(
            reaction_harness,
            project.id,
            comment_id,
            "viewer",
            "oppose",
        ),
        lambda: _delete_reaction(reaction_harness, project.id, comment_id, "viewer"),
    ):
        with pytest.raises(HTTPException) as forbidden:
            operation()
        assert forbidden.value.status_code == status.HTTP_403_FORBIDDEN

    commenter_summary = _set_reaction(
        reaction_harness,
        project.id,
        comment_id,
        "carol",
        "oppose",
    )
    assert (commenter_summary.support, commenter_summary.oppose) == (1, 1)

    reaction_harness.app.state.jelica_api_state.project_service.update_member_role(
        actor_user_id=reaction_harness.user_ids["owner"],
        project_id=project.id,
        user_id=reaction_harness.user_ids["bob"],
        role="viewer",
    )
    with pytest.raises(HTTPException) as demoted_delete:
        _delete_reaction(reaction_harness, project.id, comment_id, "bob")
    assert demoted_delete.value.status_code == status.HTTP_403_FORBIDDEN
    _assert_single_reaction(reaction_harness, comment_id, "bob", "support")


def test_frozen_project_reactions_are_read_only(
    reaction_harness: _ReactionHarness,
) -> None:
    project = _create_project(reaction_harness)
    for user, role in (
        ("alice", "commenter"),
        ("bob", "commenter"),
        ("carol", "commenter"),
        ("viewer", "viewer"),
    ):
        _add_member(reaction_harness, project.id, user, role)
    comment_id = _create_comment(reaction_harness, project.id, "alice")
    _set_reaction(reaction_harness, project.id, comment_id, "bob", "support")
    _set_project_status(reaction_harness, project.id, "frozen")

    summary = _get_summary(reaction_harness, project.id, comment_id, "viewer")
    assert (summary.support, summary.oppose, summary.current_user_reaction) == (1, 0, None)
    for operation in (
        lambda: _set_reaction(
            reaction_harness,
            project.id,
            comment_id,
            "carol",
            "oppose",
        ),
        lambda: _set_reaction(
            reaction_harness,
            project.id,
            comment_id,
            "bob",
            "support",
        ),
        lambda: _delete_reaction(reaction_harness, project.id, comment_id, "bob"),
    ):
        with pytest.raises(HTTPException) as frozen:
            operation()
        assert frozen.value.status_code == status.HTTP_409_CONFLICT
    _assert_single_reaction(reaction_harness, comment_id, "bob", "support")


def test_reaction_access_is_project_scoped_and_authenticated(
    reaction_harness: _ReactionHarness,
) -> None:
    project = _create_project(reaction_harness)
    other_project = _create_project(reaction_harness, name="Other project")
    for project_id in (project.id, other_project.id):
        _add_member(reaction_harness, project_id, "alice", "commenter")
        _add_member(reaction_harness, project_id, "bob", "commenter")
    comment_id = _create_comment(reaction_harness, project.id, "alice")

    for operation in (
        lambda: _get_summary(reaction_harness, project.id, comment_id, "outsider"),
        lambda: _set_reaction(
            reaction_harness,
            project.id,
            comment_id,
            "outsider",
            "support",
        ),
        lambda: _delete_reaction(reaction_harness, project.id, comment_id, "outsider"),
    ):
        with pytest.raises(HTTPException) as outsider:
            operation()
        assert outsider.value.status_code == status.HTTP_403_FORBIDDEN

    for operation in (
        lambda: _get_summary(reaction_harness, other_project.id, comment_id, "bob"),
        lambda: _set_reaction(
            reaction_harness,
            other_project.id,
            comment_id,
            "bob",
            "support",
        ),
        lambda: _delete_reaction(reaction_harness, other_project.id, comment_id, "bob"),
    ):
        with pytest.raises(HTTPException) as wrong_project:
            operation()
        assert wrong_project.value.status_code == status.HTTP_404_NOT_FOUND

    with pytest.raises(HTTPException) as guest:
        get_project_comment_reactions(
            project.id,
            comment_id,
            _request(
                reaction_harness,
                path=f"/api/projects/{project.id}/comments/{comment_id}/reactions",
            ),
        )
    assert guest.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_reaction_survives_member_removal_and_cascades_with_comment(
    reaction_harness: _ReactionHarness,
) -> None:
    project = _create_project(reaction_harness)
    _add_member(reaction_harness, project.id, "alice", "commenter")
    _add_member(reaction_harness, project.id, "bob", "commenter")
    comment_id = _create_comment(reaction_harness, project.id, "alice")
    _set_reaction(reaction_harness, project.id, comment_id, "bob", "support")

    service = reaction_harness.app.state.jelica_api_state.project_service
    service.remove_member(
        actor_user_id=reaction_harness.user_ids["owner"],
        project_id=project.id,
        user_id=reaction_harness.user_ids["bob"],
    )
    _assert_single_reaction(reaction_harness, comment_id, "bob", "support")
    owner_summary = _get_summary(reaction_harness, project.id, comment_id, "owner")
    assert (owner_summary.support, owner_summary.oppose) == (1, 0)
    with pytest.raises(HTTPException) as former_member:
        _get_summary(reaction_harness, project.id, comment_id, "bob")
    assert former_member.value.status_code == status.HTTP_403_FORBIDDEN

    assert (
        delete_project_comment(
            project.id,
            comment_id,
            _request(
                reaction_harness,
                path=f"/api/projects/{project.id}/comments/{comment_id}",
                user="owner",
            ),
        )
        is None
    )
    with reaction_harness.session_factory() as session:
        assert (
            session.execute(
                select(ProjectCommentReaction).where(
                    ProjectCommentReaction.comment_id == comment_id
                )
            )
            .scalars()
            .all()
            == []
        )


def _create_project(
    reaction_harness: _ReactionHarness,
    *,
    name: str = "Reaction project",
) -> Project:
    record = reaction_harness.app.state.jelica_api_state.project_service.create_project(
        actor_user_id=reaction_harness.user_ids["owner"],
        name=name,
        description=None,
    )
    with reaction_harness.session_factory() as session:
        project = session.get(Project, record.project_id)
        assert project is not None
        session.expunge(project)
        return project


def _add_member(
    reaction_harness: _ReactionHarness,
    project_id: str,
    user: str,
    role: str,
) -> None:
    with reaction_harness.session_factory() as session:
        session.add(
            ProjectMember(
                project_id=project_id,
                user_id=reaction_harness.user_ids[user],
                role=role,
                joined_at=datetime.now(UTC),
            )
        )
        session.commit()


def _create_comment(
    reaction_harness: _ReactionHarness,
    project_id: str,
    author: str,
) -> str:
    response = create_project_comment(
        project_id,
        ProjectCommentCreateRequest(body=f"Comment by {author}"),
        _request(
            reaction_harness,
            path=f"/api/projects/{project_id}/comments",
            user=author,
        ),
    )
    return response.id


def _get_summary(
    reaction_harness: _ReactionHarness,
    project_id: str,
    comment_id: str,
    actor: str,
):
    return get_project_comment_reactions(
        project_id,
        comment_id,
        _request(
            reaction_harness,
            path=f"/api/projects/{project_id}/comments/{comment_id}/reactions",
            user=actor,
        ),
    )


def _set_reaction(
    reaction_harness: _ReactionHarness,
    project_id: str,
    comment_id: str,
    actor: str,
    reaction: str,
):
    return set_project_comment_reaction(
        project_id,
        comment_id,
        ProjectCommentReactionUpdateRequest(reaction=reaction),
        _request(
            reaction_harness,
            path=f"/api/projects/{project_id}/comments/{comment_id}/reaction",
            user=actor,
        ),
    )


def _delete_reaction(
    reaction_harness: _ReactionHarness,
    project_id: str,
    comment_id: str,
    actor: str,
) -> None:
    return delete_project_comment_reaction(
        project_id,
        comment_id,
        _request(
            reaction_harness,
            path=f"/api/projects/{project_id}/comments/{comment_id}/reaction",
            user=actor,
        ),
    )


def _assert_single_reaction(
    reaction_harness: _ReactionHarness,
    comment_id: str,
    user: str,
    reaction: str,
) -> None:
    with reaction_harness.session_factory() as session:
        rows = (
            session.execute(
                select(ProjectCommentReaction).where(
                    ProjectCommentReaction.comment_id == comment_id,
                    ProjectCommentReaction.user_id == reaction_harness.user_ids[user],
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].reaction == reaction


def _history_snapshot(
    reaction_harness: _ReactionHarness,
    project_id: str,
) -> tuple[tuple[str, str, str | None, object], ...]:
    with reaction_harness.session_factory() as session:
        events = session.execute(
            select(ProjectHistoryEvent)
            .where(ProjectHistoryEvent.project_id == project_id)
            .order_by(ProjectHistoryEvent.id)
        ).scalars()
        return tuple(
            (event.id, event.event_type, event.actor_user_id, event.data) for event in events
        )


def _set_project_status(
    reaction_harness: _ReactionHarness,
    project_id: str,
    project_status: str,
) -> None:
    with reaction_harness.session_factory() as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.status = project_status
        session.commit()


def _request(
    reaction_harness: _ReactionHarness,
    *,
    path: str,
    user: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if user is not None:
        headers.append(
            (
                b"cookie",
                f"jelica_session={reaction_harness.session_tokens[user]}".encode("ascii"),
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
            "app": reaction_harness.app,
        }
    )
