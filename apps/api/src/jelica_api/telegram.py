from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from jelica_api.auth import notification_text
from jelica_api.models import (
    Project,
    TelegramAccountLink,
    TelegramLinkToken,
    TelegramMessageContext,
    User,
    WebTask,
)
from jelica_api.notifications import NotificationService
from jelica_api.projects import (
    ProjectDomainError,
    ProjectRecord,
    ProjectService,
    ProjectTaskRecord,
)
from jelica_api.task_discussions import TaskDiscussionService
from jelica_api.telegram_client import TelegramBotApiClient, TelegramDeliveryError
from jelica_api.web_tasks import is_active_task_status

TELEGRAM_LINK_TOKEN_TTL = timedelta(minutes=15)
TELEGRAM_MAX_SELECTORS = 10
TELEGRAM_RESULT_LIMIT = 10
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CALLBACK_PATTERN = re.compile(r"r:([A-Za-z0-9_-]{16,32}):(s|o)")


class TelegramLinkError(ValueError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class TelegramSelectorError(ValueError):
    pass


class ProjectRealtimePublisher(Protocol):
    def comment_created_sync(self, *, record: object) -> None: ...

    def reaction_updated_sync(
        self, *, project_id: str, comment_id: str, summary: object
    ) -> None: ...

    def reaction_deleted_sync(
        self, *, project_id: str, comment_id: str, summary: object
    ) -> None: ...


class TaskRealtimePublisher(Protocol):
    def comment_created_sync(self, *, record: object) -> None: ...

    def reaction_updated_sync(self, *, task_id: str, comment_id: str, summary: object) -> None: ...

    def reaction_deleted_sync(self, *, task_id: str, comment_id: str, summary: object) -> None: ...


@dataclass(frozen=True, slots=True)
class TelegramLinkState:
    integration_available: bool
    linked: bool
    username: str | None = None
    display_name: str | None = None
    linked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TelegramLinkRequest:
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    telegram_user_id: int
    telegram_chat_id: int
    username: str | None
    display_name: str | None


@dataclass(frozen=True, slots=True)
class ProjectSelection:
    projects: tuple[ProjectRecord, ...]
    problems: tuple[str, ...]


@dataclass(slots=True)
class TelegramIntegration:
    session_factory: sessionmaker[Session]
    bot_username: str
    public_web_base_url: str
    configured: bool
    client: TelegramBotApiClient | None
    notification_service: NotificationService
    project_service: ProjectService
    task_discussion_service: TaskDiscussionService
    project_realtime_publisher: ProjectRealtimePublisher
    task_realtime_publisher: TaskRealtimePublisher
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def link_state(self, *, user_id: str) -> TelegramLinkState:
        with self.session_factory() as session:
            link = session.get(TelegramAccountLink, user_id)
            return TelegramLinkState(
                integration_available=self.configured,
                linked=link is not None,
                username=None if link is None else link.username,
                display_name=None if link is None else link.display_name,
                linked_at=None if link is None else link.linked_at,
            )

    def create_link_request(self, *, user_id: str) -> TelegramLinkRequest:
        if not self.configured:
            raise TelegramLinkError(
                code="telegram_unavailable", message="Telegram integration is unavailable."
            )
        raw_token = secrets.token_urlsafe(32)
        if len(raw_token) > 64 or _TOKEN_PATTERN.fullmatch(raw_token) is None:
            raise RuntimeError("generated Telegram token is invalid")
        now = self.clock()
        expires_at = now + TELEGRAM_LINK_TOKEN_TTL
        with self.session_factory() as session, session.begin():
            user = session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None:
                raise TelegramLinkError(
                    code="telegram_user_unavailable",
                    message="Telegram connection cannot be created.",
                )
            session.execute(
                delete(TelegramLinkToken).where(
                    TelegramLinkToken.user_id == user_id,
                    TelegramLinkToken.used_at.is_(None),
                )
            )
            session.add(
                TelegramLinkToken(
                    user_id=user_id,
                    token_hash=_token_hash(raw_token),
                    created_at=now,
                    expires_at=expires_at,
                )
            )
        return TelegramLinkRequest(
            url=f"https://t.me/{self.bot_username}?start={raw_token}",
            expires_at=expires_at,
        )

    def disconnect_user(self, *, user_id: str) -> bool:
        with self.session_factory() as session, session.begin():
            link = session.get(TelegramAccountLink, user_id)
            if link is None:
                return False
            session.delete(link)
            return True

    def handle_update(self, update: dict[str, object]) -> None:
        message = update.get("message")
        if isinstance(message, dict):
            self._handle_message(message)
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback(callback)

    def _handle_message(self, message: dict[str, object]) -> None:
        identity = _message_identity(message)
        if identity is None:
            return
        text = message.get("text")
        reply = message.get("reply_to_message")
        if isinstance(reply, dict) and _is_int64(reply.get("message_id")):
            self._handle_reply(
                identity=identity, message=message, reply_message_id=reply["message_id"]
            )
            return
        if not isinstance(text, str):
            return
        command, argument_text = _parse_command(text, bot_username=self.bot_username)
        if command is None:
            return
        if command == "start" and argument_text:
            result = self._consume_link_token(token=argument_text, identity=identity)
            response = {
                "linked": _bot_text("linked"),
                "invalid": _bot_text("invalid-link"),
                "conflict": _bot_text("link-conflict"),
            }[result]
            self._safe_send(chat_id=identity.telegram_chat_id, text=response)
            return
        link = self._linked_identity(identity)
        if command == "start":
            self._safe_send(
                chat_id=identity.telegram_chat_id,
                text=(
                    _bot_text("connected") if link is not None else _bot_text("connect-in-settings")
                ),
            )
            return
        if link is None:
            self._safe_send(
                chat_id=identity.telegram_chat_id,
                text=_bot_text("not-connected"),
            )
            return
        if command == "help":
            self._safe_send(chat_id=identity.telegram_chat_id, text=_bot_text("help"))
        elif command == "status":
            self._send_status(link=link)
        elif command == "active_tasks":
            if argument_text:
                self._safe_send(
                    chat_id=link.telegram_chat_id,
                    text=_bot_text("usage-active-tasks"),
                )
            else:
                self._send_active_tasks(link=link)
        elif command in {"active_project_tasks", "project_status"}:
            try:
                selectors = parse_project_selectors(argument_text)
            except TelegramSelectorError:
                self._safe_send(
                    chat_id=link.telegram_chat_id,
                    text=_bot_text("usage-project-command").format(command=command),
                )
                return
            selection = self.resolve_projects(user_id=link.user_id, selectors=selectors)
            if command == "active_project_tasks":
                self._send_active_project_tasks(link=link, selection=selection)
            else:
                self._send_project_status(link=link, selection=selection)
        elif command == "disconnect":
            self.disconnect_user(user_id=link.user_id)
            self._safe_send(
                chat_id=identity.telegram_chat_id,
                text=_bot_text("disconnected"),
            )
        else:
            self._safe_send(
                chat_id=identity.telegram_chat_id,
                text=_bot_text("unknown-command"),
            )

    def _consume_link_token(self, *, token: str, identity: TelegramIdentity) -> str:
        if _TOKEN_PATTERN.fullmatch(token) is None:
            return "invalid"
        now = self.clock()
        try:
            with self.session_factory() as session, session.begin():
                token_owner_id = session.scalar(
                    select(TelegramLinkToken.user_id).where(
                        TelegramLinkToken.token_hash == _token_hash(token)
                    )
                )
                if token_owner_id is None:
                    return "invalid"
                if (
                    session.scalar(
                        select(User.id).where(User.id == token_owner_id).with_for_update()
                    )
                    is None
                ):
                    return "invalid"
                token_row = session.scalar(
                    select(TelegramLinkToken)
                    .where(TelegramLinkToken.token_hash == _token_hash(token))
                    .with_for_update()
                )
                if (
                    token_row is None
                    or token_row.used_at is not None
                    or _as_utc(token_row.expires_at) <= _as_utc(now)
                ):
                    return "invalid"
                foreign_link = session.scalars(
                    select(TelegramAccountLink)
                    .where(
                        or_(
                            TelegramAccountLink.telegram_user_id == identity.telegram_user_id,
                            TelegramAccountLink.telegram_chat_id == identity.telegram_chat_id,
                        )
                    )
                    .with_for_update()
                ).first()
                if foreign_link is not None and foreign_link.user_id != token_row.user_id:
                    return "conflict"
                current = session.get(TelegramAccountLink, token_row.user_id)
                if current is None:
                    current = TelegramAccountLink(
                        user_id=token_row.user_id,
                        telegram_user_id=identity.telegram_user_id,
                        telegram_chat_id=identity.telegram_chat_id,
                        username=identity.username,
                        display_name=identity.display_name,
                        linked_at=now,
                    )
                    session.add(current)
                else:
                    current.telegram_user_id = identity.telegram_user_id
                    current.telegram_chat_id = identity.telegram_chat_id
                    current.username = identity.username
                    current.display_name = identity.display_name
                    current.linked_at = now
                token_row.used_at = now
                session.execute(
                    delete(TelegramLinkToken).where(
                        TelegramLinkToken.user_id == token_row.user_id,
                        TelegramLinkToken.used_at.is_(None),
                        TelegramLinkToken.id != token_row.id,
                    )
                )
        except IntegrityError:
            return "conflict"
        return "linked"

    def _linked_identity(self, identity: TelegramIdentity) -> TelegramAccountLink | None:
        with self.session_factory() as session:
            return session.scalar(
                select(TelegramAccountLink).where(
                    TelegramAccountLink.telegram_user_id == identity.telegram_user_id,
                    TelegramAccountLink.telegram_chat_id == identity.telegram_chat_id,
                )
            )

    def _send_status(self, *, link: TelegramAccountLink) -> None:
        projects = self.project_service.list_projects(actor_user_id=link.user_id)
        with self.session_factory() as session:
            own_active = sum(
                1
                for status in session.scalars(
                    select(WebTask.status).where(WebTask.owner_user_id == link.user_id)
                )
                if is_active_task_status(status)
            )
            snapshot = self.notification_service.snapshot(session=session, user_id=link.user_id)
        telegram_enabled = (
            snapshot.enabled
            and snapshot.channels["telegram"] == (True, True)
            and any(effective.get("telegram", False) for _, _, effective in snapshot.events)
        )
        self._safe_send(
            chat_id=link.telegram_chat_id,
            text=_bot_text("status").format(
                notifications=_bot_text("enabled" if telegram_enabled else "disabled"),
                tasks=own_active,
                projects=sum(p.status == "active" for p in projects),
            ),
        )

    def _send_active_tasks(self, *, link: TelegramAccountLink) -> None:
        with self.session_factory() as session:
            rows = session.execute(
                select(WebTask, Project.name)
                .outerjoin(Project, Project.id == WebTask.project_id)
                .where(WebTask.owner_user_id == link.user_id)
                .order_by(WebTask.updated_at.desc(), WebTask.core_task_id.desc())
            ).all()
        active = [
            (task, project_name)
            for task, project_name in rows
            if is_active_task_status(task.status)
        ]
        lines = [_bot_text("own-active-heading").format(count=len(active))]
        keyboard = []
        for task, project_name in active[:TELEGRAM_RESULT_LIMIT]:
            project_suffix = (
                _bot_text("project-suffix").format(project=project_name) if project_name else ""
            )
            lines.append(
                _bot_text("own-task-item").format(
                    name=task.name or task.core_task_id,
                    status=task.status,
                    project=project_suffix,
                )
            )
            keyboard.append(
                [
                    {
                        "text": _bot_text("open-task").format(
                            name=_short_name(task.name) or "task"
                        ),
                        "url": self._task_url(task.core_task_id),
                    }
                ]
            )
        if not active:
            lines.append(_bot_text("no-active-tasks"))
        elif len(active) > TELEGRAM_RESULT_LIMIT:
            lines.append(_bot_text("showing-limit").format(limit=TELEGRAM_RESULT_LIMIT))
            keyboard.append([{"text": _bot_text("open-tasks"), "url": self._web_url("/app/tasks")}])
        self._safe_send(
            chat_id=link.telegram_chat_id,
            text="\n".join(lines),
            reply_markup=_keyboard(keyboard),
        )

    def resolve_projects(self, *, user_id: str, selectors: tuple[str, ...]) -> ProjectSelection:
        accessible = self.project_service.list_projects(actor_user_id=user_id)
        if not selectors:
            return ProjectSelection(projects=accessible, problems=())
        by_id = {project.project_id: project for project in accessible}
        selected: dict[str, ProjectRecord] = {}
        problems: list[str] = []
        for selector in selectors:
            uuid_value = _canonical_uuid(selector)
            if uuid_value is not None:
                project = by_id.get(uuid_value)
                if project is None:
                    problems.append(f"{selector}: {_bot_text('project-unavailable')}")
                else:
                    selected[project.project_id] = project
                continue
            matches = [
                project for project in accessible if project.name.casefold() == selector.casefold()
            ]
            if len(matches) == 1:
                selected[matches[0].project_id] = matches[0]
            elif len(matches) > 1:
                choices = ", ".join(
                    f"{project.name} ({project.project_id[:8]})" for project in matches[:5]
                )
                problems.append(f"{selector}: {_bot_text('ambiguous-project')} ({choices})")
            else:
                problems.append(f"{selector}: {_bot_text('project-unavailable')}")
        return ProjectSelection(projects=tuple(selected.values()), problems=tuple(problems))

    def _send_active_project_tasks(
        self, *, link: TelegramAccountLink, selection: ProjectSelection
    ) -> None:
        active: list[tuple[ProjectTaskRecord, ProjectRecord]] = []
        unavailable = 0
        for selected_project in selection.projects:
            try:
                project = self.project_service.get_project(
                    actor_user_id=link.user_id,
                    project_id=selected_project.project_id,
                )
                tasks = self.project_service.list_tasks(
                    actor_user_id=link.user_id,
                    project_id=project.project_id,
                )
            except ProjectDomainError:
                unavailable += 1
                continue
            active.extend((task, project) for task in tasks if is_active_task_status(task.state))
        lines = [_bot_text("active-project-tasks-heading").format(count=len(active))]
        keyboard = []
        for task, project in active[:TELEGRAM_RESULT_LIMIT]:
            frozen_suffix = _bot_text("frozen-suffix") if project.status == "frozen" else ""
            lines.append(
                _bot_text("project-task-item").format(
                    project=project.name,
                    frozen=frozen_suffix,
                    name=task.name or task.task_id,
                    status=task.state,
                    owner="",
                )
            )
            keyboard.append(
                [
                    {
                        "text": _bot_text("open-task").format(
                            name=_short_name(task.name) or "task"
                        ),
                        "url": self._task_url(task.task_id),
                    }
                ]
            )
        if not active:
            lines.append(_bot_text("no-active-project-tasks"))
        if len(active) > TELEGRAM_RESULT_LIMIT:
            lines.append(_bot_text("showing-limit").format(limit=TELEGRAM_RESULT_LIMIT))
        lines.extend(f"! {_bot_text('project-unavailable')}" for _ in range(unavailable))
        lines.extend(f"! {problem}" for problem in selection.problems)
        self._safe_send(
            chat_id=link.telegram_chat_id,
            text="\n".join(lines),
            reply_markup=_keyboard(keyboard),
        )

    def _send_project_status(
        self, *, link: TelegramAccountLink, selection: ProjectSelection
    ) -> None:
        projects_with_tasks: list[tuple[ProjectRecord, tuple[ProjectTaskRecord, ...]]] = []
        unavailable = 0
        for selected_project in selection.projects:
            try:
                project = self.project_service.get_project(
                    actor_user_id=link.user_id,
                    project_id=selected_project.project_id,
                )
                tasks = self.project_service.list_tasks(
                    actor_user_id=link.user_id,
                    project_id=project.project_id,
                )
            except ProjectDomainError:
                unavailable += 1
                continue
            projects_with_tasks.append((project, tasks))
        lines = [_bot_text("projects-heading").format(count=len(projects_with_tasks))]
        keyboard = []
        for project, tasks in projects_with_tasks[:TELEGRAM_RESULT_LIMIT]:
            active_count = sum(is_active_task_status(task.state) for task in tasks)
            lines.append(
                _bot_text("project-status-item").format(
                    name=project.name,
                    status=project.status,
                    role=project.current_user_role or "member",
                    active=active_count,
                    total=len(tasks),
                )
            )
            keyboard.append(
                [
                    {
                        "text": _bot_text("open-project").format(name=_short_name(project.name)),
                        "url": self._project_url(project.project_id),
                    }
                ]
            )
        if not projects_with_tasks:
            lines.append(_bot_text("no-projects"))
        elif len(projects_with_tasks) > TELEGRAM_RESULT_LIMIT:
            lines.append(_bot_text("showing-limit").format(limit=TELEGRAM_RESULT_LIMIT))
            keyboard.append(
                [{"text": _bot_text("open-projects"), "url": self._web_url("/app/projects")}]
            )
        lines.extend(f"! {_bot_text('project-unavailable')}" for _ in range(unavailable))
        lines.extend(f"! {problem}" for problem in selection.problems)
        self._safe_send(
            chat_id=link.telegram_chat_id,
            text="\n".join(lines),
            reply_markup=_keyboard(keyboard),
        )

    def _handle_reply(
        self,
        *,
        identity: TelegramIdentity,
        message: dict[str, object],
        reply_message_id: int,
    ) -> None:
        link = self._linked_identity(identity)
        if link is None:
            self._safe_send(
                chat_id=identity.telegram_chat_id,
                text=_bot_text("not-connected"),
            )
            return
        with self.session_factory() as session:
            context = session.scalar(
                select(TelegramMessageContext).where(
                    TelegramMessageContext.telegram_chat_id == identity.telegram_chat_id,
                    TelegramMessageContext.telegram_message_id == reply_message_id,
                    TelegramMessageContext.user_id == link.user_id,
                )
            )
        if context is None or context.context_type not in {
            "project_discussion_comment",
            "task_discussion_comment",
        }:
            self._safe_send(
                chat_id=identity.telegram_chat_id,
                text=_bot_text("reply-context-unavailable"),
            )
            return
        body = message.get("text")
        if not isinstance(body, str):
            self._safe_send(
                chat_id=identity.telegram_chat_id,
                text=_bot_text("text-only"),
            )
            return
        try:
            if context.context_type == "project_discussion_comment":
                record = self.project_service.create_comment(
                    actor_user_id=link.user_id,
                    project_id=context.target_id,
                    body=body,
                )
                self.project_realtime_publisher.comment_created_sync(record=record)
            else:
                record = self.task_discussion_service.create_comment(
                    actor_user_id=link.user_id,
                    task_id=context.target_id,
                    body=body,
                )
                self.task_realtime_publisher.comment_created_sync(record=record)
        except (ProjectDomainError, ValueError):
            self._safe_send(
                chat_id=identity.telegram_chat_id,
                text=_bot_text("reply-denied"),
            )
            return
        self._safe_send(
            chat_id=identity.telegram_chat_id,
            text=_bot_text("reply-added"),
        )

    def _handle_callback(self, callback: dict[str, object]) -> None:
        callback_id = callback.get("id")
        message = callback.get("message")
        sender = callback.get("from")
        data = callback.get("data")
        if (
            not isinstance(callback_id, str)
            or not isinstance(message, dict)
            or not isinstance(sender, dict)
            or not isinstance(data, str)
        ):
            return
        identity = _callback_identity(message=message, sender=sender)
        match = _CALLBACK_PATTERN.fullmatch(data)
        message_id = message.get("message_id")
        if identity is None or match is None or not _is_int64(message_id):
            self._safe_answer(callback_id=callback_id, text=_bot_text("action-unavailable"))
            return
        link = self._linked_identity(identity)
        if link is None:
            self._safe_answer(callback_id=callback_id, text=_bot_text("action-connect"))
            return
        callback_token, reaction_code = match.groups()
        with self.session_factory() as session:
            context = session.scalar(
                select(TelegramMessageContext).where(
                    TelegramMessageContext.callback_token == callback_token,
                    TelegramMessageContext.telegram_chat_id == identity.telegram_chat_id,
                    TelegramMessageContext.telegram_message_id == message_id,
                    TelegramMessageContext.user_id == link.user_id,
                )
            )
        if context is None or context.comment_id is None:
            self._safe_answer(callback_id=callback_id, text=_bot_text("action-unavailable"))
            return
        reaction = "support" if reaction_code == "s" else "oppose"
        try:
            self._toggle_reaction(link=link, context=context, reaction=reaction)
        except (ProjectDomainError, ValueError):
            self._safe_answer(callback_id=callback_id, text=_bot_text("action-stale"))
            return
        self._safe_answer(callback_id=callback_id, text=_bot_text("reaction-updated"))

    def _toggle_reaction(
        self,
        *,
        link: TelegramAccountLink,
        context: TelegramMessageContext,
        reaction: str,
    ) -> None:
        if context.context_type == "project_discussion_comment":
            current = self.project_service.get_comment_reactions(
                actor_user_id=link.user_id,
                project_id=context.target_id,
                comment_id=context.comment_id or "",
            )
            if current.current_user_reaction == reaction:
                summary = self.project_service.delete_comment_reaction(
                    actor_user_id=link.user_id,
                    project_id=context.target_id,
                    comment_id=context.comment_id or "",
                )
                self.project_realtime_publisher.reaction_deleted_sync(
                    project_id=context.target_id,
                    comment_id=context.comment_id or "",
                    summary=summary,
                )
            else:
                summary = self.project_service.set_comment_reaction(
                    actor_user_id=link.user_id,
                    project_id=context.target_id,
                    comment_id=context.comment_id or "",
                    reaction=reaction,
                )
                self.project_realtime_publisher.reaction_updated_sync(
                    project_id=context.target_id,
                    comment_id=context.comment_id or "",
                    summary=summary,
                )
            return
        if context.context_type != "task_discussion_comment":
            raise ValueError("unsupported Telegram reaction context")
        current = self.task_discussion_service.get_reactions(
            actor_user_id=link.user_id,
            task_id=context.target_id,
            comment_id=context.comment_id,
        )
        if current.current_user_reaction == reaction:
            summary = self.task_discussion_service.delete_reaction(
                actor_user_id=link.user_id,
                task_id=context.target_id,
                comment_id=context.comment_id,
            )
            self.task_realtime_publisher.reaction_deleted_sync(
                task_id=context.target_id,
                comment_id=context.comment_id,
                summary=summary,
            )
        else:
            summary = self.task_discussion_service.set_reaction(
                actor_user_id=link.user_id,
                task_id=context.target_id,
                comment_id=context.comment_id,
                reaction=reaction,
            )
            self.task_realtime_publisher.reaction_updated_sync(
                task_id=context.target_id,
                comment_id=context.comment_id,
                summary=summary,
            )

    def _safe_send(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        if self.client is None:
            return
        try:
            self.client.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except TelegramDeliveryError:
            return

    def _safe_answer(self, *, callback_id: str, text: str) -> None:
        if self.client is None:
            return
        try:
            self.client.answer_callback_query(callback_query_id=callback_id, text=text)
        except TelegramDeliveryError:
            return

    def _web_url(self, path: str) -> str:
        return f"{self.public_web_base_url.rstrip('/')}{path}"

    def _task_url(self, task_id: str) -> str:
        return self._web_url(f"/app/tasks/{task_id}")

    def _project_url(self, project_id: str) -> str:
        return self._web_url(f"/app/projects/{project_id}")


def parse_project_selectors(argument_text: str) -> tuple[str, ...]:
    selectors: list[str] = []
    index = 0
    length = len(argument_text)
    while index < length:
        while index < length and argument_text[index].isspace():
            index += 1
        if index >= length:
            break
        value: list[str] = []
        if argument_text[index] == '"':
            index += 1
            closed = False
            while index < length:
                character = argument_text[index]
                if character == '"':
                    closed = True
                    index += 1
                    break
                if character == "\\":
                    index += 1
                    if index >= length or argument_text[index] not in {'"', "\\"}:
                        raise TelegramSelectorError("invalid escape")
                    character = argument_text[index]
                value.append(character)
                index += 1
            if not closed or index < length and not argument_text[index].isspace():
                raise TelegramSelectorError("malformed quoted selector")
        else:
            while index < length and not argument_text[index].isspace():
                if argument_text[index] in {'"', "\\"}:
                    raise TelegramSelectorError("quotes and escapes require a quoted selector")
                value.append(argument_text[index])
                index += 1
        selector = "".join(value)
        if not selector:
            raise TelegramSelectorError("empty selector")
        selectors.append(selector)
        if len(selectors) > TELEGRAM_MAX_SELECTORS:
            raise TelegramSelectorError("too many selectors")
    return tuple(selectors)


def _message_identity(message: dict[str, object]) -> TelegramIdentity | None:
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict) or chat.get("type") != "private":
        return None
    user_id = sender.get("id")
    chat_id = chat.get("id")
    if not _is_int64(user_id) or not _is_int64(chat_id):
        return None
    return TelegramIdentity(
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        username=_safe_metadata(sender.get("username"), 64),
        display_name=_display_name(sender),
    )


def _callback_identity(
    *, message: dict[str, object], sender: dict[str, object]
) -> TelegramIdentity | None:
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return None
    synthetic = {"chat": chat, "from": sender}
    return _message_identity(synthetic)


def _parse_command(text: str, *, bot_username: str) -> tuple[str | None, str]:
    if not text.startswith("/") or len(text) > 4096:
        return None, ""
    match = re.fullmatch(
        r"/([a-z_]+)(?:@([A-Za-z0-9_]{5,32}))?(?:\s+(.*))?",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        return None, ""
    command, recipient, remainder = match.groups()
    if recipient is not None and recipient.casefold() != bot_username.casefold():
        return None, ""
    return command, (remainder or "").strip()


def _bot_text(key: str) -> str:
    return notification_text(f"notification.telegram.bot.{key}", "en")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _canonical_uuid(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _display_name(sender: dict[str, object]) -> str | None:
    parts = [
        value
        for key in ("first_name", "last_name")
        if (value := _safe_metadata(sender.get(key), 80)) is not None
    ]
    return _safe_metadata(" ".join(parts), 160) if parts else None


def _safe_metadata(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())[:limit]
    return normalized or None


def _short_name(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())[:32]


def _keyboard(rows: list[list[dict[str, str]]]) -> dict[str, object] | None:
    return {"inline_keyboard": rows} if rows else None


def _is_int64(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and -(2**63) <= value < 2**63


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "ProjectSelection",
    "TELEGRAM_LINK_TOKEN_TTL",
    "TELEGRAM_MAX_SELECTORS",
    "TelegramIdentity",
    "TelegramIntegration",
    "TelegramLinkError",
    "TelegramLinkRequest",
    "TelegramLinkState",
    "TelegramSelectorError",
    "parse_project_selectors",
]
