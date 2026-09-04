from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from jelica_api.api.routes.comments import (
    create_project_comment,
    delete_project_comment,
    delete_project_comment_reaction,
    edit_project_comment,
    set_project_comment_reaction,
)
from jelica_api.api.routes.invitations import accept_project_invitation
from jelica_api.api.routes.projects import (
    delete_project,
    leave_project,
    remove_project_member,
    transfer_project_ownership,
    update_project,
    update_project_member,
)
from jelica_api.app import create_app
from jelica_api.auth import hash_opaque_token
from jelica_api.contracts.comments import (
    ProjectCommentCreateRequest,
    ProjectCommentReactionUpdateRequest,
    ProjectCommentUpdateRequest,
)
from jelica_api.contracts.projects import (
    ProjectMemberUpdateRequest,
    ProjectTransferOwnershipRequest,
    ProjectUpdateRequest,
)
from jelica_api.models import (
    AuthSession,
    Base,
    ProjectComment,
    ProjectCommentReaction,
    ProjectMember,
    User,
)
from jelica_api.settings import ApiSettings


@dataclass(frozen=True, slots=True)
class _RealtimeHarness:
    app: FastAPI
    session_factory: sessionmaker[Session]
    user_ids: dict[str, str]
    session_tokens: dict[str, str]
    project_id: str


class _AsgiWebSocket:
    def __init__(
        self,
        *,
        app: FastAPI,
        path: str,
        session_token: str | None,
        origin: str = "http://testserver",
    ) -> None:
        self._app = app
        self._path = path
        self._session_token = session_token
        self._origin = origin
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def connect(self) -> dict[str, Any]:
        headers = [(b"host", b"testserver"), (b"origin", self._origin.encode())]
        if self._session_token is not None:
            headers.append((b"cookie", f"jelica_session={self._session_token}".encode()))
        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "ws",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": b"",
            "headers": headers,
            "subprotocols": [],
            "state": {},
            "extensions": {},
        }
        await self._incoming.put({"type": "websocket.connect"})
        self._task = asyncio.create_task(self._app(scope, self._incoming.get, self._outgoing.put))
        return await self.receive_frame()

    async def send_json(self, message: dict[str, Any]) -> None:
        await self._incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(message),
            }
        )

    async def receive_json(self) -> dict[str, Any]:
        frame = await self.receive_frame()
        assert frame["type"] == "websocket.send", frame
        return json.loads(frame["text"])

    async def receive_frame(self) -> dict[str, Any]:
        return await asyncio.wait_for(self._outgoing.get(), timeout=1)

    async def assert_no_frame(self) -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(self._outgoing.get(), timeout=0.02)

    async def disconnect(self) -> None:
        if self._task is None or self._task.done():
            return
        await self._incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self._task, timeout=1)


@pytest.fixture
def realtime_harness(tmp_path: Path) -> Iterator[_RealtimeHarness]:
    database_path = tmp_path / "realtime.sqlite3"
    app = create_app(
        settings=ApiSettings(
            app_name="JELICA Web Backend",
            api_host="127.0.0.1",
            api_port=8000,
            database_url=f"sqlite+pysqlite:///{database_path}",
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
        for label in ("owner", "supervisor", "alice", "bob", "viewer", "outsider"):
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
    project = state.project_service.create_project(
        actor_user_id=user_ids["owner"],
        name="Realtime Project",
        description=None,
    )
    with state.session_factory() as session:
        for label, role in (
            ("supervisor", "supervisor"),
            ("alice", "commenter"),
            ("bob", "commenter"),
            ("viewer", "viewer"),
        ):
            session.add(
                ProjectMember(
                    project_id=project.project_id,
                    user_id=user_ids[label],
                    role=role,
                    joined_at=now,
                )
            )
        session.commit()
    try:
        yield _RealtimeHarness(
            app=app,
            session_factory=state.session_factory,
            user_ids=user_ids,
            session_tokens=session_tokens,
            project_id=project.project_id,
        )
    finally:
        state.task_orchestrator.shutdown()
        state.engine.dispose()


def test_realtime_handshake_is_authenticated_same_origin_and_non_disclosing(
    realtime_harness: _RealtimeHarness,
) -> None:
    async def scenario() -> None:
        path = _realtime_path(realtime_harness.project_id)
        unauthenticated = _AsgiWebSocket(
            app=realtime_harness.app,
            path=path,
            session_token=None,
        )
        assert await unauthenticated.connect() == {
            "type": "websocket.close",
            "code": 4401,
            "reason": "",
        }
        wrong_origin = _AsgiWebSocket(
            app=realtime_harness.app,
            path=path,
            session_token=realtime_harness.session_tokens["viewer"],
            origin="https://attacker.example",
        )
        assert (await wrong_origin.connect())["code"] == 4400

        outsider_codes = []
        for target_project in (realtime_harness.project_id, "missing-project"):
            socket = _AsgiWebSocket(
                app=realtime_harness.app,
                path=_realtime_path(target_project),
                session_token=realtime_harness.session_tokens["outsider"],
            )
            outsider_codes.append((await socket.connect())["code"])
        assert outsider_codes == [4404, 4404]

        viewer = await _connect(realtime_harness, user="viewer")
        snapshot = await viewer.receive_json()
        assert snapshot["type"] == "presence.snapshot"
        assert snapshot["users"] == [
            {
                "user_id": realtime_harness.user_ids["viewer"],
                "username": "viewer",
            }
        ]
        assert (await viewer.receive_json())["type"] == "presence.joined"
        await viewer.disconnect()

        await asyncio.to_thread(
            realtime_harness.app.state.jelica_api_state.project_service.update_project,
            actor_user_id=realtime_harness.user_ids["owner"],
            project_id=realtime_harness.project_id,
            changes={"status": "frozen"},
        )
        frozen_viewer = await _connect(realtime_harness, user="viewer")
        assert (await frozen_viewer.receive_json())["type"] == "presence.snapshot"
        await frozen_viewer.disconnect()

    _run(scenario())


def test_presence_is_unique_per_user_across_tabs(realtime_harness: _RealtimeHarness) -> None:
    async def scenario() -> None:
        alice_one = await _connect(realtime_harness, user="alice")
        await alice_one.receive_json()
        assert (await alice_one.receive_json())["type"] == "presence.joined"

        alice_two = await _connect(realtime_harness, user="alice")
        snapshot = await alice_two.receive_json()
        assert [user["username"] for user in snapshot["users"]] == ["alice"]
        await alice_one.assert_no_frame()
        await alice_two.assert_no_frame()

        bob = await _connect(realtime_harness, user="bob")
        bob_snapshot = await bob.receive_json()
        assert {user["username"] for user in bob_snapshot["users"]} == {"alice", "bob"}
        assert (await bob.receive_json())["type"] == "presence.joined"
        assert (await alice_one.receive_json())["user"]["username"] == "bob"
        assert (await alice_two.receive_json())["user"]["username"] == "bob"

        await alice_one.disconnect()
        await bob.assert_no_frame()
        await alice_two.disconnect()
        left = await bob.receive_json()
        assert left == {
            "type": "presence.left",
            "user": {
                "user_id": realtime_harness.user_ids["alice"],
                "username": "alice",
            },
        }
        await bob.disconnect()

    _run(scenario())


def test_comment_commands_ack_after_persistence_reuse_mentions_and_keep_errors_open(
    realtime_harness: _RealtimeHarness,
) -> None:
    async def scenario() -> None:
        alice = await _connect_ready(realtime_harness, user="alice")
        await alice.send_json(
            {
                "type": "command",
                "id": "create-1",
                "command": "comment.create",
                "payload": {"body": "@viewer please review"},
            }
        )
        ack = await alice.receive_json()
        event = await alice.receive_json()
        assert ack["type"] == "command.ack"
        assert ack["id"] == "create-1"
        assert ack["result"]["mentions"] == [
            {
                "user_id": realtime_harness.user_ids["viewer"],
                "username": "viewer",
            }
        ]
        assert event["type"] == "comment.created"
        assert event["command_id"] == "create-1"
        assert event["comment"] == ack["result"]
        comment_id = ack["result"]["id"]
        with realtime_harness.session_factory() as session:
            assert session.get(ProjectComment, comment_id) is not None
        await alice.assert_no_frame()

        await alice.send_json(
            {
                "type": "command",
                "id": "invalid-1",
                "command": "comment.update",
                "payload": {"comment_id": comment_id, "body": "   "},
            }
        )
        error = await alice.receive_json()
        assert error["type"] == "command.error"
        assert error["error"]["code"] == "validation_error"
        await alice.assert_no_frame()

        await alice.send_json(
            {
                "type": "command",
                "id": "update-1",
                "command": "comment.update",
                "payload": {"comment_id": comment_id, "body": "updated"},
            }
        )
        updated_ack = await alice.receive_json()
        updated_event = await alice.receive_json()
        assert updated_ack["result"]["body"] == "updated"
        assert updated_event["type"] == "comment.updated"

        await alice.send_json(
            {
                "type": "command",
                "id": "delete-own",
                "command": "comment.delete",
                "payload": {"comment_id": comment_id},
            }
        )
        assert (await alice.receive_json())["type"] == "command.ack"
        assert (await alice.receive_json())["type"] == "comment.deleted"
        with realtime_harness.session_factory() as session:
            assert session.get(ProjectComment, comment_id) is None

        await alice.send_json(
            {
                "type": "command",
                "id": "create-for-moderation",
                "command": "comment.create",
                "payload": {"body": "moderate me"},
            }
        )
        moderation_ack = await alice.receive_json()
        await alice.receive_json()
        moderation_comment_id = moderation_ack["result"]["id"]
        bob = await _connect_ready(realtime_harness, user="bob")
        assert (await alice.receive_json())["type"] == "presence.joined"
        for command_name, payload in (
            (
                "comment.update",
                {"comment_id": moderation_comment_id, "body": "not mine"},
            ),
            ("comment.delete", {"comment_id": moderation_comment_id}),
        ):
            await bob.send_json(
                {
                    "type": "command",
                    "id": f"bob-{command_name}",
                    "command": command_name,
                    "payload": payload,
                }
            )
            assert (await bob.receive_json())["error"]["code"] == "forbidden"

        supervisor = await _connect_ready(realtime_harness, user="supervisor")
        assert (await alice.receive_json())["type"] == "presence.joined"
        assert (await bob.receive_json())["type"] == "presence.joined"
        await supervisor.send_json(
            {
                "type": "command",
                "id": "moderation-delete",
                "command": "comment.delete",
                "payload": {"comment_id": moderation_comment_id},
            }
        )
        assert (await supervisor.receive_json())["type"] == "command.ack"
        assert (await supervisor.receive_json())["type"] == "comment.deleted"
        assert (await alice.receive_json())["type"] == "comment.deleted"
        assert (await bob.receive_json())["type"] == "comment.deleted"

        viewer = await _connect_ready(realtime_harness, user="viewer")
        assert (await alice.receive_json())["type"] == "presence.joined"
        assert (await bob.receive_json())["type"] == "presence.joined"
        assert (await supervisor.receive_json())["type"] == "presence.joined"
        await viewer.send_json(
            {
                "type": "command",
                "id": "viewer-create",
                "command": "comment.create",
                "payload": {"body": "forbidden"},
            }
        )
        assert (await viewer.receive_json())["error"]["code"] == "forbidden"
        await viewer.send_json({"type": "typing.start"})
        await viewer.assert_no_frame()
        await viewer.send_json({"type": "unsupported"})
        assert (await viewer.receive_json())["type"] == "protocol.error"

        await viewer.disconnect()
        await supervisor.disconnect()
        await bob.disconnect()
        await alice.disconnect()

    _run(scenario())


def test_reactions_rest_broadcast_typing_ttl_and_freeze_role_changes(
    realtime_harness: _RealtimeHarness,
) -> None:
    async def scenario() -> None:
        state = realtime_harness.app.state.jelica_api_state
        state.realtime_hub._typing_ttl_seconds = 0.02
        comment = await asyncio.to_thread(
            state.project_service.create_comment,
            actor_user_id=realtime_harness.user_ids["alice"],
            project_id=realtime_harness.project_id,
            body="Hypothesis",
        )
        bob = await _connect_ready(realtime_harness, user="bob")
        await bob.send_json({"type": "typing.start"})
        assert (await bob.receive_json())["type"] == "typing.started"
        assert (await bob.receive_json())["type"] == "typing.stopped"
        state.realtime_hub._typing_ttl_seconds = 5.0

        await bob.send_json(
            {
                "type": "command",
                "id": "react-1",
                "command": "reaction.set",
                "payload": {"comment_id": comment.comment_id, "reaction": "support"},
            }
        )
        reaction_ack = await bob.receive_json()
        reaction_event = await bob.receive_json()
        assert reaction_ack["result"] == {
            "support": 1,
            "oppose": 0,
            "current_user_reaction": "support",
        }
        assert reaction_event == {
            "type": "reaction.updated",
            "command_id": "react-1",
            "comment_id": comment.comment_id,
            "support": 1,
            "oppose": 0,
        }
        assert "user_id" not in reaction_event

        await asyncio.to_thread(
            set_project_comment_reaction,
            realtime_harness.project_id,
            comment.comment_id,
            ProjectCommentReactionUpdateRequest(reaction="oppose"),
            _request(realtime_harness, user="supervisor", method="PUT"),
        )
        rest_event = await bob.receive_json()
        assert rest_event["type"] == "reaction.updated"
        assert rest_event["command_id"] is None
        await bob.assert_no_frame()

        await asyncio.to_thread(
            update_project_member,
            realtime_harness.project_id,
            realtime_harness.user_ids["bob"],
            ProjectMemberUpdateRequest(role="viewer"),
            _request(realtime_harness, user="owner", method="PATCH"),
        )
        assert (await bob.receive_json())["type"] == "member.role_changed"
        await bob.send_json(
            {
                "type": "command",
                "id": "denied-after-role",
                "command": "comment.create",
                "payload": {"body": "not permitted"},
            }
        )
        assert (await bob.receive_json())["error"]["code"] == "forbidden"

        await asyncio.to_thread(
            update_project_member,
            realtime_harness.project_id,
            realtime_harness.user_ids["bob"],
            ProjectMemberUpdateRequest(role="commenter"),
            _request(realtime_harness, user="owner", method="PATCH"),
        )
        assert (await bob.receive_json())["role"] == "commenter"
        await bob.send_json({"type": "typing.start"})
        assert (await bob.receive_json())["type"] == "typing.started"
        await asyncio.to_thread(
            update_project,
            realtime_harness.project_id,
            ProjectUpdateRequest(status="frozen"),
            _request(realtime_harness, user="owner", method="PATCH"),
        )
        assert (await bob.receive_json())["type"] == "project.frozen"
        assert (await bob.receive_json())["type"] == "typing.stopped"
        await bob.send_json({"type": "typing.start"})
        await bob.send_json(
            {
                "type": "command",
                "id": "frozen-reaction",
                "command": "reaction.set",
                "payload": {"comment_id": comment.comment_id, "reaction": "support"},
            }
        )
        assert (await bob.receive_json())["error"]["code"] == "project_frozen"
        await bob.send_json(
            {
                "type": "command",
                "id": "frozen-create",
                "command": "comment.create",
                "payload": {"body": "blocked while frozen"},
            }
        )
        assert (await bob.receive_json())["error"]["code"] == "project_frozen"
        await asyncio.to_thread(
            update_project,
            realtime_harness.project_id,
            ProjectUpdateRequest(status="active"),
            _request(realtime_harness, user="owner", method="PATCH"),
        )
        assert (await bob.receive_json())["type"] == "project.unfrozen"
        await bob.send_json(
            {
                "type": "command",
                "id": "after-unfreeze",
                "command": "comment.create",
                "payload": {"body": "works again"},
            }
        )
        assert (await bob.receive_json())["type"] == "command.ack"
        assert (await bob.receive_json())["type"] == "comment.created"
        await bob.disconnect()

    _run(scenario())


def test_reaction_replace_delete_self_and_viewer_errors_do_not_close_socket(
    realtime_harness: _RealtimeHarness,
) -> None:
    async def scenario() -> None:
        state = realtime_harness.app.state.jelica_api_state
        comment = await asyncio.to_thread(
            state.project_service.create_comment,
            actor_user_id=realtime_harness.user_ids["alice"],
            project_id=realtime_harness.project_id,
            body="Proposal",
        )
        bob = await _connect_ready(realtime_harness, user="bob")
        for command_id, reaction, support, oppose in (
            ("support", "support", 1, 0),
            ("oppose", "oppose", 0, 1),
        ):
            await bob.send_json(
                {
                    "type": "command",
                    "id": command_id,
                    "command": "reaction.set",
                    "payload": {
                        "comment_id": comment.comment_id,
                        "reaction": reaction,
                    },
                }
            )
            ack = await bob.receive_json()
            event = await bob.receive_json()
            assert (ack["result"]["support"], ack["result"]["oppose"]) == (
                support,
                oppose,
            )
            assert (event["support"], event["oppose"]) == (support, oppose)
            with realtime_harness.session_factory() as session:
                reactions = session.execute(
                    select(ProjectCommentReaction).where(
                        ProjectCommentReaction.comment_id == comment.comment_id,
                        ProjectCommentReaction.user_id == realtime_harness.user_ids["bob"],
                    )
                ).scalars()
                assert len(tuple(reactions)) == 1

        await bob.send_json(
            {
                "type": "command",
                "id": "delete-reaction",
                "command": "reaction.delete",
                "payload": {"comment_id": comment.comment_id},
            }
        )
        assert (await bob.receive_json())["result"]["current_user_reaction"] is None
        deleted_event = await bob.receive_json()
        assert deleted_event["type"] == "reaction.deleted"
        assert (deleted_event["support"], deleted_event["oppose"]) == (0, 0)

        alice = await _connect_ready(realtime_harness, user="alice")
        await _drain_join(bob)
        await alice.send_json(
            {
                "type": "command",
                "id": "self-reaction",
                "command": "reaction.set",
                "payload": {"comment_id": comment.comment_id, "reaction": "support"},
            }
        )
        assert (await alice.receive_json())["error"]["code"] == "reaction_not_allowed"
        await alice.send_json(
            {
                "type": "command",
                "id": "still-open",
                "command": "comment.create",
                "payload": {"body": "socket remains usable"},
            }
        )
        assert (await alice.receive_json())["type"] == "command.ack"
        assert (await alice.receive_json())["type"] == "comment.created"
        assert (await bob.receive_json())["type"] == "comment.created"

        viewer = await _connect_ready(realtime_harness, user="viewer")
        await _drain_join(bob)
        await _drain_join(alice)
        await viewer.send_json(
            {
                "type": "command",
                "id": "viewer-reaction",
                "command": "reaction.delete",
                "payload": {"comment_id": comment.comment_id},
            }
        )
        assert (await viewer.receive_json())["error"]["code"] == "forbidden"
        await viewer.send_json({"type": "typing.start"})
        await viewer.assert_no_frame()
        await viewer.send_json({"type": "unsupported"})
        assert (await viewer.receive_json())["type"] == "protocol.error"

        await viewer.disconnect()
        await alice.disconnect()
        await bob.disconnect()

    _run(scenario())


def test_rest_comment_and_reaction_mutations_publish_exactly_once(
    realtime_harness: _RealtimeHarness,
) -> None:
    async def scenario() -> None:
        observer = await _connect_ready(realtime_harness, user="bob")
        created = await asyncio.to_thread(
            create_project_comment,
            realtime_harness.project_id,
            ProjectCommentCreateRequest(body="REST comment"),
            _request(realtime_harness, user="alice", method="POST"),
        )
        event = await observer.receive_json()
        assert event["type"] == "comment.created"
        assert event["comment"]["id"] == created.id
        await observer.assert_no_frame()

        await asyncio.to_thread(
            edit_project_comment,
            realtime_harness.project_id,
            created.id,
            ProjectCommentUpdateRequest(body="REST updated"),
            _request(realtime_harness, user="alice", method="PATCH"),
        )
        assert (await observer.receive_json())["type"] == "comment.updated"
        await observer.assert_no_frame()

        await asyncio.to_thread(
            set_project_comment_reaction,
            realtime_harness.project_id,
            created.id,
            ProjectCommentReactionUpdateRequest(reaction="support"),
            _request(realtime_harness, user="bob", method="PUT"),
        )
        assert (await observer.receive_json())["type"] == "reaction.updated"
        await observer.assert_no_frame()
        await asyncio.to_thread(
            delete_project_comment_reaction,
            realtime_harness.project_id,
            created.id,
            _request(realtime_harness, user="bob", method="DELETE"),
        )
        assert (await observer.receive_json())["type"] == "reaction.deleted"
        await observer.assert_no_frame()

        await asyncio.to_thread(
            delete_project_comment,
            realtime_harness.project_id,
            created.id,
            _request(realtime_harness, user="alice", method="DELETE"),
        )
        deleted = await observer.receive_json()
        assert deleted == {
            "type": "comment.deleted",
            "command_id": None,
            "comment_id": created.id,
        }
        await observer.assert_no_frame()
        await observer.disconnect()

    _run(scenario())


def test_invitation_accept_and_voluntary_leave_publish_membership_lifecycle(
    realtime_harness: _RealtimeHarness,
) -> None:
    async def scenario() -> None:
        state = realtime_harness.app.state.jelica_api_state
        owner = await _connect_ready(realtime_harness, user="owner")
        invitation = await asyncio.to_thread(
            state.project_service.create_invitation,
            actor_user_id=realtime_harness.user_ids["owner"],
            project_id=realtime_harness.project_id,
            invited_user_id=realtime_harness.user_ids["outsider"],
            role="commenter",
        )
        await asyncio.to_thread(
            accept_project_invitation,
            invitation.invitation_id,
            _request(realtime_harness, user="outsider", method="POST"),
        )
        joined = await owner.receive_json()
        assert joined == {
            "type": "member.joined",
            "user_id": realtime_harness.user_ids["outsider"],
            "username": "outsider",
            "role": "commenter",
        }

        outsider_one = await _connect_ready(realtime_harness, user="outsider")
        assert (await owner.receive_json())["type"] == "presence.joined"
        outsider_two = await _connect(realtime_harness, user="outsider")
        assert (await outsider_two.receive_json())["type"] == "presence.snapshot"
        await asyncio.to_thread(
            leave_project,
            realtime_harness.project_id,
            _request(realtime_harness, user="outsider", method="POST"),
        )
        assert (await owner.receive_json())["type"] == "member.removed"
        for socket in (outsider_one, outsider_two):
            assert (await socket.receive_json())["type"] == "access.revoked"
            assert (await socket.receive_frame())["code"] == 4403
        assert (await owner.receive_json())["type"] == "presence.left"
        await owner.disconnect()

    _run(scenario())


def test_remove_transfer_and_delete_publish_after_domain_commit(
    realtime_harness: _RealtimeHarness,
) -> None:
    async def scenario() -> None:
        owner = await _connect_ready(realtime_harness, user="owner")
        bob_one = await _connect_ready(realtime_harness, user="bob")
        bob_two = await _connect(realtime_harness, user="bob")
        assert (await bob_two.receive_json())["type"] == "presence.snapshot"
        await _drain_join(owner)
        await _drain_join(bob_one)

        await asyncio.to_thread(
            transfer_project_ownership,
            realtime_harness.project_id,
            ProjectTransferOwnershipRequest(
                new_owner_user_id=realtime_harness.user_ids["supervisor"]
            ),
            _request(realtime_harness, user="owner", method="POST"),
        )
        transfer_event = await owner.receive_json()
        assert transfer_event == {
            "type": "project.ownership_transferred",
            "previous_owner_user_id": realtime_harness.user_ids["owner"],
            "new_owner_user_id": realtime_harness.user_ids["supervisor"],
        }
        assert (await bob_one.receive_json())["type"] == "project.ownership_transferred"
        assert (await bob_two.receive_json())["type"] == "project.ownership_transferred"

        await asyncio.to_thread(
            remove_project_member,
            realtime_harness.project_id,
            realtime_harness.user_ids["bob"],
            _request(realtime_harness, user="supervisor", method="DELETE"),
        )
        assert (await owner.receive_json())["type"] == "member.removed"
        for socket in (bob_one, bob_two):
            assert (await socket.receive_json())["type"] == "access.revoked"
            assert (await socket.receive_frame())["code"] == 4403
        assert (await owner.receive_json())["type"] == "presence.left"

        denied = _AsgiWebSocket(
            app=realtime_harness.app,
            path=_realtime_path(realtime_harness.project_id),
            session_token=realtime_harness.session_tokens["bob"],
        )
        assert (await denied.connect())["code"] == 4404

        await asyncio.to_thread(
            delete_project,
            realtime_harness.project_id,
            _request(realtime_harness, user="supervisor", method="DELETE"),
        )
        assert (await owner.receive_json())["type"] == "project.deleted"
        assert (await owner.receive_frame())["code"] == 4404

    _run(scenario())


async def _connect(harness: _RealtimeHarness, *, user: str) -> _AsgiWebSocket:
    socket = _AsgiWebSocket(
        app=harness.app,
        path=_realtime_path(harness.project_id),
        session_token=harness.session_tokens[user],
    )
    assert (await socket.connect())["type"] == "websocket.accept"
    return socket


async def _connect_ready(harness: _RealtimeHarness, *, user: str) -> _AsgiWebSocket:
    socket = await _connect(harness, user=user)
    assert (await socket.receive_json())["type"] == "presence.snapshot"
    first_event = await socket.receive_json()
    assert first_event["type"] == "presence.joined"
    return socket


async def _drain_join(socket: _AsgiWebSocket) -> None:
    while True:
        try:
            event = await asyncio.wait_for(socket.receive_json(), timeout=0.02)
        except TimeoutError:
            return
        assert event["type"] == "presence.joined"


def _request(
    harness: _RealtimeHarness,
    *,
    user: str,
    method: str,
) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": f"/api/projects/{harness.project_id}",
            "headers": [
                (
                    b"cookie",
                    f"jelica_session={harness.session_tokens[user]}".encode(),
                )
            ],
            "app": harness.app,
        }
    )


def _realtime_path(project_id: str) -> str:
    return f"/api/projects/{project_id}/realtime"


def _run(coroutine: Coroutine[Any, Any, None]) -> None:
    asyncio.run(coroutine)
