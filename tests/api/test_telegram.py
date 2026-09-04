from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from jelica_api.api.routes.telegram_webhook import (
    receive_telegram_webhook,
    webhook_secret_matches,
)
from jelica_api.models import (
    Base,
    NotificationDelivery,
    Project,
    ProjectComment,
    ProjectCommentReaction,
    ProjectMember,
    TaskDiscussion,
    TaskDiscussionComment,
    TelegramAccountLink,
    TelegramLinkToken,
    TelegramMessageContext,
    User,
    WebTask,
)
from jelica_api.notifications import NotificationService
from jelica_api.projects import ProjectService
from jelica_api.task_discussions import TaskDiscussionService
from jelica_api.telegram import (
    TELEGRAM_LINK_TOKEN_TTL,
    TelegramIntegration,
    TelegramSelectorError,
    parse_project_selectors,
)
from jelica_api.telegram_client import TelegramDeliveryError
from jelica_api.telegram_notifications import (
    TelegramNotificationWorker,
    build_telegram_notification,
)


class _Bot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict[str, object] | None]] = []
        self.answers: list[tuple[str, str]] = []
        self.failures: list[TelegramDeliveryError] = []

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> int:
        if self.failures:
            raise self.failures.pop(0)
        self.messages.append((chat_id, text, reply_markup))
        return 1000 + len(self.messages)

    def answer_callback_query(self, *, callback_query_id: str, text: str) -> None:
        self.answers.append((callback_query_id, text))


class _Publisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def comment_created_sync(self, *, record: object) -> None:
        self.events.append(("comment", record))

    def reaction_updated_sync(self, **kwargs: object) -> None:
        self.events.append(("reaction-updated", kwargs))

    def reaction_deleted_sync(self, **kwargs: object) -> None:
        self.events.append(("reaction-deleted", kwargs))


def _setup():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    notifications = NotificationService(
        session_factory=sessions,
        telegram_available=True,
    )
    projects = ProjectService(session_factory=sessions, notification_service=notifications)
    task_discussions = TaskDiscussionService(
        session_factory=sessions, notification_service=notifications
    )
    bot = _Bot()
    publisher = _Publisher()
    integration = TelegramIntegration(
        session_factory=sessions,
        bot_username="JelicaTestBot",
        public_web_base_url="https://jelica.example",
        configured=True,
        client=bot,  # type: ignore[arg-type]
        notification_service=notifications,
        project_service=projects,
        task_discussion_service=task_discussions,
        project_realtime_publisher=publisher,
        task_realtime_publisher=publisher,
    )
    with sessions() as session:
        user = User(username="telegram-user", email="tg@example.test", password_hash="x")
        other = User(username="other-user", email="other@example.test", password_hash="x")
        session.add_all([user, other])
        session.commit()
    return engine, sessions, notifications, projects, integration, bot, publisher, user, other


def _private_message(
    *,
    telegram_user_id: int,
    chat_id: int,
    text: str | None = None,
    reply_to: int | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": 20,
        "chat": {"id": chat_id, "type": "private"},
        "from": {
            "id": telegram_user_id,
            "username": "mutable_username",
            "first_name": "Test",
        },
    }
    if text is not None:
        message["text"] = text
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return message


def _link(integration: TelegramIntegration, user_id: str, telegram_id: int = 7001) -> str:
    request = integration.create_link_request(user_id=user_id)
    raw_token = request.url.rsplit("=", 1)[1]
    integration.handle_update(
        {
            "message": _private_message(
                telegram_user_id=telegram_id,
                chat_id=telegram_id,
                text=f"/start {raw_token}",
            )
        }
    )
    return raw_token


def test_linking_token_is_hashed_ttl_one_time_and_identity_owned() -> None:
    engine, sessions, _, _, integration, bot, _, user, other = _setup()
    request = integration.create_link_request(user_id=user.id)
    raw_token = request.url.rsplit("=", 1)[1]
    assert len(raw_token) <= 64
    assert TELEGRAM_LINK_TOKEN_TTL == timedelta(minutes=15)
    with sessions() as session:
        row = session.scalar(select(TelegramLinkToken))
        assert row is not None
        assert row.token_hash == hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        assert raw_token not in row.token_hash

    integration.handle_update(
        {
            "message": _private_message(
                telegram_user_id=7001,
                chat_id=7001,
                text=f"/start {raw_token}",
            )
        }
    )
    with sessions() as session:
        link = session.get(TelegramAccountLink, user.id)
        assert link is not None and link.telegram_user_id == 7001
        assert link.username == "mutable_username"
        assert session.scalar(select(TelegramLinkToken)).used_at is not None  # type: ignore[union-attr]

    integration.handle_update(
        {
            "message": _private_message(
                telegram_user_id=7002,
                chat_id=7002,
                text=f"/start {raw_token}",
            )
        }
    )
    assert "invalid or expired" in bot.messages[-1][1]
    conflicting = integration.create_link_request(user_id=other.id).url.rsplit("=", 1)[1]
    integration.handle_update(
        {
            "message": _private_message(
                telegram_user_id=7001,
                chat_id=7001,
                text=f"/start {conflicting}",
            )
        }
    )
    assert "cannot be connected" in bot.messages[-1][1]
    assert integration.link_state(user_id=other.id).linked is False
    engine.dispose()


def test_expired_arbitrary_group_and_disconnect_are_safe() -> None:
    engine, sessions, _, _, integration, bot, _, user, _ = _setup()
    raw_token = integration.create_link_request(user_id=user.id).url.rsplit("=", 1)[1]
    with sessions() as session:
        row = session.scalar(select(TelegramLinkToken))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    integration.handle_update(
        {
            "message": _private_message(
                telegram_user_id=8001, chat_id=8001, text=f"/start {raw_token}"
            )
        }
    )
    assert integration.link_state(user_id=user.id).linked is False
    before = len(bot.messages)
    integration.handle_update(
        {
            "message": {
                "message_id": 21,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 8001},
                "text": "/status",
            }
        }
    )
    assert len(bot.messages) == before
    _link(integration, user.id, 8001)
    integration.handle_update(
        {"message": _private_message(telegram_user_id=8001, chat_id=8001, text="/disconnect")}
    )
    assert integration.link_state(user_id=user.id).linked is False
    engine.dispose()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ()),
        ('"Project Alpha"', ("Project Alpha",)),
        (
            '"Project Alpha" 87d2b9a1-0000-4000-8000-000000000000',
            ("Project Alpha", "87d2b9a1-0000-4000-8000-000000000000"),
        ),
        ('"A \\"quote\\"" "A \\\\ slash"', ('A "quote"', "A \\ slash")),
        ("SingleWord", ("SingleWord",)),
    ],
)
def test_selector_parser(value: str, expected: tuple[str, ...]) -> None:
    assert parse_project_selectors(value) == expected


def test_selector_parser_rejects_malformed_and_more_than_ten() -> None:
    with pytest.raises(TelegramSelectorError):
        parse_project_selectors('"unclosed')
    with pytest.raises(TelegramSelectorError):
        parse_project_selectors(" ".join(f"p{i}" for i in range(11)))


def test_commands_keep_own_attached_tasks_and_show_accessible_foreign_project_tasks() -> None:
    engine, sessions, _, projects, integration, bot, _, user, other = _setup()
    _link(integration, user.id)
    project = projects.create_project(actor_user_id=user.id, name="Project Alpha", description=None)
    duplicate = projects.create_project(
        actor_user_id=user.id, name="project alpha", description=None
    )
    task_ids = [str(uuid4()) for _ in range(4)]
    with sessions() as session:
        session.add_all(
            [
                WebTask(
                    core_task_id=task_ids[0],
                    name="Own free",
                    status="running",
                    owner_user_id=user.id,
                ),
                WebTask(
                    core_task_id=task_ids[1],
                    name="Own attached",
                    status="queued",
                    owner_user_id=user.id,
                    project_id=project.project_id,
                ),
                WebTask(
                    core_task_id=task_ids[2],
                    name="Foreign",
                    status="running",
                    owner_user_id=other.id,
                    project_id=project.project_id,
                ),
                WebTask(
                    core_task_id=task_ids[3], name="Done", status="completed", owner_user_id=user.id
                ),
            ]
        )
        session.commit()
    integration.handle_update(
        {"message": _private_message(telegram_user_id=7001, chat_id=7001, text="/active_tasks")}
    )
    own_text = bot.messages[-1][1]
    assert "Own free" in own_text and "Own attached" in own_text
    assert "Foreign" not in own_text and "Done" not in own_text
    markup = bot.messages[-1][2]
    assert markup is not None
    assert all("/app/tasks/" in row[0]["url"] for row in markup["inline_keyboard"])  # type: ignore[index]

    integration.handle_update(
        {
            "message": _private_message(
                telegram_user_id=7001,
                chat_id=7001,
                text=f"/active_project_tasks {project.project_id}",
            )
        }
    )
    project_text = bot.messages[-1][1]
    assert "Own attached" in project_text and "Foreign" in project_text
    selection = integration.resolve_projects(user_id=user.id, selectors=("PROJECT ALPHA",))
    assert selection.projects == () and "ambiguous" in selection.problems[0]
    selected = integration.resolve_projects(user_id=user.id, selectors=(duplicate.project_id,))
    assert selected.projects[0].project_id == duplicate.project_id
    engine.dispose()


def test_notification_worker_creates_durable_context_and_required_task_link() -> None:
    engine, sessions, notifications, _, integration, bot, _, user, _ = _setup()
    _link(integration, user.id)
    task_id = str(uuid4())
    with sessions() as session:
        notifications.patch(
            session=session,
            user_id=user.id,
            channels={"telegram": True},
            events=(("task.completed", "telegram", True),),
        )
        notification = notifications.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="task.completed",
            source_type="task",
            source_id=task_id,
            payload={
                "task_id": task_id,
                "resource_kind": "task",
                "target_path": f"/app/tasks/{task_id}",
            },
        )
        session.commit()
        assert notification is not None
    bot.messages.clear()
    worker = TelegramNotificationWorker(
        session_factory=sessions,
        sender=bot,
        public_web_base_url="https://jelica.example",
    )
    report = worker.run_once()
    assert report.sent == 1
    markup = bot.messages[0][2]
    assert markup is not None
    buttons = [button for row in markup["inline_keyboard"] for button in row]  # type: ignore[index]
    assert {button["text"] for button in buttons} == {"Open task"}
    assert buttons[0]["url"] == f"https://jelica.example/app/tasks/{task_id}"
    assert "token=" not in buttons[0]["url"]
    with sessions() as session:
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.channel == "telegram")
        )
        context = session.scalar(select(TelegramMessageContext))
        assert delivery is not None and delivery.status == "sent" and delivery.attempts == 1
        assert context is not None and context.delivery_id == delivery.id
        assert context.telegram_message_id == 1001
    engine.dispose()


def test_telegram_delivery_gates_preferences_and_never_backfills() -> None:
    engine, sessions, notifications, _, integration, _, _, user, _ = _setup()
    with sessions() as session:
        notifications.patch(session=session, user_id=user.id, channels={"telegram": True})
        notifications.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="task.completed",
            source_type="task",
            source_id="before-link",
            payload={"task_id": str(uuid4())},
        )
        session.commit()
    _link(integration, user.id)
    with sessions() as session:
        assert (
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "telegram")
            ).all()
            == []
        )
        notifications.patch(
            session=session,
            user_id=user.id,
            events=(("task.completed", "telegram", False),),
        )
        notifications.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="task.completed",
            source_type="task",
            source_id="event-off",
            payload={"task_id": str(uuid4())},
        )
        session.commit()
        assert (
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "telegram")
            ).all()
            == []
        )
        notifications.patch(
            session=session,
            user_id=user.id,
            events=(("task.completed", "telegram", True),),
        )
        notifications.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="task.completed",
            source_type="task",
            source_id="effective",
            payload={"task_id": str(uuid4())},
        )
        session.commit()
        deliveries = session.scalars(
            select(NotificationDelivery).where(NotificationDelivery.channel == "telegram")
        ).all()
        assert len(deliveries) == 1
    engine.dispose()


def test_project_task_notification_still_opens_canonical_task_page() -> None:
    task_id = str(uuid4())
    project_id = str(uuid4())
    notification = SimpleNamespace(
        id=str(uuid4()),
        event_id="project.task.completed",
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "target_path": f"/app/projects/{project_id}/tasks",
        },
    )
    _, markup, context_type, target_id, _ = build_telegram_notification(
        notification=notification,  # type: ignore[arg-type]
        public_web_base_url="https://jelica.example",
        language="en",
        callback_token="abcdefghijklmnop",
    )
    assert context_type == "task" and target_id == task_id
    assert markup is not None
    buttons = [button for row in markup["inline_keyboard"] for button in row]  # type: ignore[index]
    assert buttons == [{"text": "Open task", "url": f"https://jelica.example/app/tasks/{task_id}"}]


def test_worker_retries_then_deactivates_only_proven_unusable_destination() -> None:
    engine, sessions, notifications, _, integration, bot, _, user, _ = _setup()
    _link(integration, user.id)
    with sessions() as session:
        notifications.patch(session=session, user_id=user.id, channels={"telegram": True})
        notifications.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="task.completed",
            source_type="task",
            source_id="retry",
            payload={"task_id": str(uuid4())},
        )
        session.commit()
    bot.failures.append(TelegramDeliveryError(code="rate", transient=True))
    worker = TelegramNotificationWorker(
        session_factory=sessions,
        sender=bot,
        public_web_base_url="https://jelica.example",
        retry_base_seconds=1,
    )
    assert worker.run_once().retried == 1
    with sessions() as session:
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.channel == "telegram")
        )
        assert delivery is not None
        delivery.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    bot.failures.append(
        TelegramDeliveryError(code="gone", transient=False, destination_unusable=True)
    )
    assert worker.run_once().failed == 1
    assert integration.link_state(user_id=user.id).linked is False
    engine.dispose()


def test_project_and_task_native_replies_and_reaction_toggle_recheck_domain_rules() -> None:
    engine, sessions, _, projects, integration, bot, publisher, user, other = _setup()
    _link(integration, user.id)
    project = projects.create_project(actor_user_id=other.id, name="Bridge", description=None)
    with sessions() as session:
        session.add(ProjectMember(project_id=project.project_id, user_id=user.id, role="member"))
        session.commit()
    original = projects.create_comment(
        actor_user_id=other.id, project_id=project.project_id, body="Original"
    )
    callback_token = "abcdefghijklmnop"
    with sessions() as session:
        session.add(
            TelegramMessageContext(
                user_id=user.id,
                telegram_chat_id=7001,
                telegram_message_id=501,
                callback_token=callback_token,
                context_type="project_discussion_comment",
                target_id=project.project_id,
                comment_id=original.comment_id,
            )
        )
        task_id = str(uuid4())
        task = WebTask(
            core_task_id=task_id,
            name="Discuss task",
            status="running",
            owner_user_id=other.id,
            project_id=project.project_id,
        )
        session.add(task)
        session.flush()
        session.add(TaskDiscussion(task_id=task.id))
        session.flush()
        task_comment = TaskDiscussionComment(
            task_id=task.id, author_user_id=other.id, body="Task original"
        )
        session.add(task_comment)
        session.flush()
        session.add(
            TelegramMessageContext(
                user_id=user.id,
                telegram_chat_id=7001,
                telegram_message_id=502,
                callback_token="qrstuvwxyzABCDEF",
                context_type="task_discussion_comment",
                target_id=task_id,
                comment_id=task_comment.id,
            )
        )
        session.commit()

    integration.handle_update(
        {
            "message": _private_message(
                telegram_user_id=7001,
                chat_id=7001,
                text="Project reply",
                reply_to=501,
            )
        }
    )
    integration.handle_update(
        {
            "message": _private_message(
                telegram_user_id=7001,
                chat_id=7001,
                text="Task reply",
                reply_to=502,
            )
        }
    )
    with sessions() as session:
        assert (
            session.scalar(select(ProjectComment).where(ProjectComment.body == "Project reply"))
            is not None
        )
        assert (
            session.scalar(
                select(TaskDiscussionComment).where(TaskDiscussionComment.body == "Task reply")
            )
            is not None
        )
    assert [event[0] for event in publisher.events].count("comment") == 2

    callback = {
        "id": "callback-1",
        "from": {"id": 7001, "first_name": "Test"},
        "message": {"message_id": 501, "chat": {"id": 7001, "type": "private"}},
        "data": f"r:{callback_token}:s",
    }
    integration.handle_update({"callback_query": callback})
    with sessions() as session:
        reaction = session.get(ProjectCommentReaction, (original.comment_id, user.id))
        assert reaction is not None and reaction.reaction == "support"
    integration.handle_update({"callback_query": {**callback, "id": "callback-2"}})
    with sessions() as session:
        assert session.get(ProjectCommentReaction, (original.comment_id, user.id)) is None

    with sessions() as session:
        session.get(Project, project.project_id).status = "frozen"  # type: ignore[union-attr]
        session.commit()
    integration.handle_update(
        {
            "message": _private_message(
                telegram_user_id=7001,
                chat_id=7001,
                text="Denied reply",
                reply_to=501,
            )
        }
    )
    with sessions() as session:
        assert (
            session.scalar(select(ProjectComment).where(ProjectComment.body == "Denied reply"))
            is None
        )
    assert "no longer available" in bot.messages[-1][1]
    engine.dispose()


def _request(*, body: bytes, secret: str, content_type: str = "application/json") -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/integrations/telegram/webhook",
            "headers": [
                (b"content-type", content_type.encode()),
                (b"x-telegram-bot-api-secret-token", secret.encode()),
            ],
        },
        receive,
    )


def test_webhook_secret_json_validation_and_unsupported_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    fake_state = SimpleNamespace(
        settings=SimpleNamespace(telegram_webhook_secret="expected-secret"),
        telegram_integration=SimpleNamespace(handle_update=updates.append),
    )
    monkeypatch.setattr(
        "jelica_api.api.routes.telegram_webhook.get_app_state", lambda request: fake_state
    )
    assert webhook_secret_matches(provided="expected-secret", expected="expected-secret")
    assert not webhook_secret_matches(provided="wrong", expected="expected-secret")
    payload = asyncio.run(
        receive_telegram_webhook(
            _request(body=json.dumps({"update_id": 1}).encode(), secret="expected-secret")
        )
    )
    assert payload == {"ok": True} and updates == [{"update_id": 1}]
    with pytest.raises(HTTPException) as wrong:
        asyncio.run(receive_telegram_webhook(_request(body=b"{}", secret="wrong")))
    assert wrong.value.status_code == 403
    with pytest.raises(HTTPException) as malformed:
        asyncio.run(receive_telegram_webhook(_request(body=b"not-json", secret="expected-secret")))
    assert malformed.value.status_code == 400
