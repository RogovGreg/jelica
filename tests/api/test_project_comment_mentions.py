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
    edit_project_comment,
    list_project_comments,
)
from jelica_api.app import create_app
from jelica_api.auth import hash_opaque_token
from jelica_api.comment_mentions import parse_mention_usernames
from jelica_api.contracts.comments import (
    ProjectCommentCreateRequest,
    ProjectCommentUpdateRequest,
)
from jelica_api.models import (
    AuthSession,
    Base,
    Project,
    ProjectComment,
    ProjectCommentMention,
    ProjectHistoryEvent,
    ProjectMember,
    User,
)
from jelica_api.settings import ApiSettings


@dataclass(frozen=True, slots=True)
class _MentionHarness:
    app: FastAPI
    session_factory: sessionmaker[Session]
    user_ids: dict[str, str]
    session_tokens: dict[str, str]


@pytest.fixture
def mention_harness() -> Iterator[_MentionHarness]:
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
        for label in ("owner", "alice", "bob", "user_name", "viewer", "outsider"):
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
        yield _MentionHarness(
            app=app,
            session_factory=state.session_factory,
            user_ids=user_ids,
            session_tokens=session_tokens,
        )
    finally:
        state.task_orchestrator.shutdown()
        state.engine.dispose()


def test_mentions_resolve_members_deduplicate_and_preserve_plain_text(
    mention_harness: _MentionHarness,
) -> None:
    project = _create_project(mention_harness)
    _add_member(mention_harness, project.id, "alice", "commenter")
    _add_member(mention_harness, project.id, "bob", "viewer")
    _add_member(mention_harness, project.id, "user_name", "viewer")

    body = "@bob, (@bob) hello @user_name; @does_not_exist @outsider @alice mail@example.com"
    created = create_project_comment(
        project.id,
        ProjectCommentCreateRequest(body=body),
        _request(mention_harness, project.id, "alice"),
    )

    assert created.body == body
    assert [(mention.user_id, mention.username) for mention in created.mentions] == [
        (mention_harness.user_ids["bob"], "bob"),
        (mention_harness.user_ids["user_name"], "user_name"),
    ]
    assert parse_mention_usernames("@bob (@bob) hello @bob mail@example.com") == ("bob",)
    with mention_harness.session_factory() as session:
        rows = (
            session.execute(
                select(ProjectCommentMention).where(
                    ProjectCommentMention.comment_id == created.id,
                )
            )
            .scalars()
            .all()
        )
        assert {row.mentioned_user_id for row in rows} == {
            mention_harness.user_ids["bob"],
            mention_harness.user_ids["user_name"],
        }
        assert len(rows) == 2
        persisted = session.get(ProjectComment, created.id)
        assert persisted is not None
        assert sorted(item.mentioned_user.username for item in persisted.mentions) == [
            "bob",
            "user_name",
        ]
    assert not hasattr(User, "comment_mentions")
    assert not hasattr(User, "mentions")
    assert not hasattr(Project, "mentions")


def test_mentions_edit_uses_set_diff_and_current_membership(
    mention_harness: _MentionHarness,
) -> None:
    project = _create_project(mention_harness)
    for user in ("alice", "bob", "user_name"):
        _add_member(mention_harness, project.id, user, "commenter")
    comment = _create_comment(mention_harness, project.id, "alice", "hello")

    added = edit_project_comment(
        project.id,
        comment.id,
        ProjectCommentUpdateRequest(body="@bob hello"),
        _request(mention_harness, project.id, "alice"),
    )
    assert [(mention.user_id, mention.username) for mention in added.mentions] == [
        (mention_harness.user_ids["bob"], "bob")
    ]
    with mention_harness.session_factory() as session:
        bob_mention = session.get(
            ProjectCommentMention,
            (comment.id, mention_harness.user_ids["bob"]),
        )
        assert bob_mention is not None
        bob_created_at = bob_mention.created_at

    unchanged = edit_project_comment(
        project.id,
        comment.id,
        ProjectCommentUpdateRequest(body="@bob hello again"),
        _request(mention_harness, project.id, "alice"),
    )
    assert [(mention.user_id, mention.username) for mention in unchanged.mentions] == [
        (mention_harness.user_ids["bob"], "bob")
    ]
    with mention_harness.session_factory() as session:
        bob_mention = session.get(
            ProjectCommentMention,
            (comment.id, mention_harness.user_ids["bob"]),
        )
        assert bob_mention is not None
        assert bob_mention.created_at == bob_created_at

    replaced = edit_project_comment(
        project.id,
        comment.id,
        ProjectCommentUpdateRequest(body="@user_name hello"),
        _request(mention_harness, project.id, "alice"),
    )
    assert [(mention.user_id, mention.username) for mention in replaced.mentions] == [
        (mention_harness.user_ids["user_name"], "user_name")
    ]

    removed = edit_project_comment(
        project.id,
        comment.id,
        ProjectCommentUpdateRequest(body="@unknown hello"),
        _request(mention_harness, project.id, "alice"),
    )
    assert removed.mentions == ()


def test_mentions_survive_member_removal_and_recompute_on_later_edit(
    mention_harness: _MentionHarness,
) -> None:
    project = _create_project(mention_harness)
    _add_member(mention_harness, project.id, "alice", "commenter")
    _add_member(mention_harness, project.id, "bob", "viewer")
    comment = _create_comment(mention_harness, project.id, "alice", "@bob")
    service = mention_harness.app.state.jelica_api_state.project_service

    service.remove_member(
        actor_user_id=mention_harness.user_ids["owner"],
        project_id=project.id,
        user_id=mention_harness.user_ids["bob"],
    )
    with mention_harness.session_factory() as session:
        assert (
            session.get(
                ProjectCommentMention,
                (comment.id, mention_harness.user_ids["bob"]),
            )
            is not None
        )

    listed = list_project_comments(
        project.id,
        _request(mention_harness, project.id, "owner"),
    )
    assert listed.items[0].mentions[0].username == "bob"
    with pytest.raises(HTTPException) as former_member:
        list_project_comments(
            project.id,
            _request(mention_harness, project.id, "bob"),
        )
    assert former_member.value.status_code == status.HTTP_403_FORBIDDEN

    edited = edit_project_comment(
        project.id,
        comment.id,
        ProjectCommentUpdateRequest(body="@bob still here"),
        _request(mention_harness, project.id, "alice"),
    )
    assert edited.mentions == ()


def test_existing_comments_have_no_backfill_and_delete_cascades_mentions(
    mention_harness: _MentionHarness,
) -> None:
    project = _create_project(mention_harness)
    _add_member(mention_harness, project.id, "alice", "commenter")
    _add_member(mention_harness, project.id, "bob", "viewer")
    with mention_harness.session_factory() as session:
        legacy = ProjectComment(
            project_id=project.id,
            author_user_id=mention_harness.user_ids["alice"],
            body="@bob legacy body",
            created_at=datetime.now(UTC),
        )
        session.add(legacy)
        session.commit()
        legacy_id = legacy.id

    listed = list_project_comments(
        project.id,
        _request(mention_harness, project.id, "bob"),
    )
    assert listed.items[0].mentions == ()

    before_history = _history_snapshot(mention_harness, project.id)
    edited = edit_project_comment(
        project.id,
        legacy_id,
        ProjectCommentUpdateRequest(body="@bob edited"),
        _request(mention_harness, project.id, "alice"),
    )
    assert [(mention.user_id, mention.username) for mention in edited.mentions] == [
        (mention_harness.user_ids["bob"], "bob")
    ]
    with mention_harness.session_factory() as session:
        assert (
            session.get(
                ProjectCommentMention,
                (legacy_id, mention_harness.user_ids["bob"]),
            )
            is not None
        )
    assert _history_snapshot(mention_harness, project.id) == before_history

    service = mention_harness.app.state.jelica_api_state.project_service
    service.delete_project(
        actor_user_id=mention_harness.user_ids["owner"],
        project_id=project.id,
    )
    with mention_harness.session_factory() as session:
        assert (
            session.execute(
                select(ProjectCommentMention).where(
                    ProjectCommentMention.comment_id == legacy_id,
                )
            )
            .scalars()
            .all()
            == []
        )
        assert (
            session.execute(
                select(ProjectHistoryEvent).where(ProjectHistoryEvent.project_id == project.id)
            )
            .scalars()
            .all()
            == []
        )


def _create_project(
    mention_harness: _MentionHarness,
    *,
    name: str = "Mention project",
) -> Project:
    record = mention_harness.app.state.jelica_api_state.project_service.create_project(
        actor_user_id=mention_harness.user_ids["owner"],
        name=name,
        description=None,
    )
    with mention_harness.session_factory() as session:
        project = session.get(Project, record.project_id)
        assert project is not None
        session.expunge(project)
        return project


def _add_member(
    mention_harness: _MentionHarness,
    project_id: str,
    user: str,
    role: str,
) -> None:
    with mention_harness.session_factory() as session:
        session.add(
            ProjectMember(
                project_id=project_id,
                user_id=mention_harness.user_ids[user],
                role=role,
                joined_at=datetime.now(UTC),
            )
        )
        session.commit()


def _create_comment(
    mention_harness: _MentionHarness,
    project_id: str,
    author: str,
    body: str,
):
    return create_project_comment(
        project_id,
        ProjectCommentCreateRequest(body=body),
        _request(mention_harness, project_id, author),
    )


def _history_snapshot(
    mention_harness: _MentionHarness,
    project_id: str,
) -> tuple[tuple[str, str, str | None, object], ...]:
    with mention_harness.session_factory() as session:
        events = session.execute(
            select(ProjectHistoryEvent)
            .where(ProjectHistoryEvent.project_id == project_id)
            .order_by(ProjectHistoryEvent.id)
        ).scalars()
        return tuple(
            (event.id, event.event_type, event.actor_user_id, event.data) for event in events
        )


def _request(
    mention_harness: _MentionHarness,
    project_id: str,
    user: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if user is not None:
        headers.append(
            (
                b"cookie",
                f"jelica_session={mention_harness.session_tokens[user]}".encode("ascii"),
            )
        )
    path = f"/api/projects/{project_id}/comments"
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": headers,
            "app": mention_harness.app,
        }
    )
