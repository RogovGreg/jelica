from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from jelica_api.auth import notification_text
from jelica_api.email_notifications import build_notification_email
from jelica_api.models import (
    Notification,
    NotificationDelivery,
    TelegramAccountLink,
    TelegramMessageContext,
    User,
)
from jelica_api.telegram_client import TelegramDeliveryError

TELEGRAM_OUTBOX_POLL_INTERVAL_SECONDS = 1.0
TELEGRAM_OUTBOX_BATCH_SIZE = 50
TELEGRAM_MAX_ATTEMPTS = 5
TELEGRAM_RETRY_BASE_SECONDS = 5
TELEGRAM_MESSAGE_CONTEXT_RETENTION_DAYS = 90
TELEGRAM_CONTEXT_CLEANUP_BATCH_SIZE = 500
_PENDING_STATUSES = ("pending", "retry")


class TelegramMessageSender(Protocol):
    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class TelegramWorkerReport:
    selected: int
    sent: int
    retried: int
    failed: int


@dataclass(frozen=True, slots=True)
class PreparedTelegramNotification:
    delivery_id: str
    notification_id: str
    user_id: str
    chat_id: int
    text: str
    reply_markup: dict[str, object] | None
    callback_token: str
    context_type: str
    target_id: str
    comment_id: str | None


@dataclass(frozen=True, slots=True)
class TelegramNotificationWorker:
    session_factory: sessionmaker[Session]
    sender: TelegramMessageSender
    public_web_base_url: str
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    batch_size: int = TELEGRAM_OUTBOX_BATCH_SIZE
    max_attempts: int = TELEGRAM_MAX_ATTEMPTS
    retry_base_seconds: int = TELEGRAM_RETRY_BASE_SECONDS

    def __post_init__(self) -> None:
        if self.batch_size <= 0 or self.max_attempts <= 0 or self.retry_base_seconds <= 0:
            raise ValueError("Telegram worker limits must be positive")

    def run_once(self) -> TelegramWorkerReport:
        now = self.clock()
        self._cleanup_expired_contexts(now=now)
        delivery_ids = self._due_delivery_ids(now=now)
        sent = retried = failed = 0
        for delivery_id in delivery_ids:
            prepared = self._prepare(delivery_id=delivery_id, now=now)
            if prepared is None:
                failed += 1
                continue
            try:
                message_id = self.sender.send_message(
                    chat_id=prepared.chat_id,
                    text=prepared.text,
                    reply_markup=prepared.reply_markup,
                )
            except TelegramDeliveryError as error:
                outcome = self._record_failure(prepared=prepared, error=error, now=now)
                retried += outcome == "retry"
                failed += outcome == "failed"
            except Exception:
                self._record_failure(
                    prepared=prepared,
                    error=TelegramDeliveryError(code="telegram_delivery_failed", transient=False),
                    now=now,
                )
                failed += 1
            else:
                if self._record_success(prepared=prepared, message_id=message_id, now=now):
                    sent += 1
        return TelegramWorkerReport(
            selected=len(delivery_ids), sent=sent, retried=retried, failed=failed
        )

    def _cleanup_expired_contexts(self, *, now: datetime) -> int:
        cutoff = now - timedelta(days=TELEGRAM_MESSAGE_CONTEXT_RETENTION_DAYS)
        with self.session_factory() as session, session.begin():
            expired_ids = tuple(
                session.scalars(
                    select(TelegramMessageContext.id)
                    .where(TelegramMessageContext.created_at < cutoff)
                    .order_by(TelegramMessageContext.created_at)
                    .limit(TELEGRAM_CONTEXT_CLEANUP_BATCH_SIZE)
                )
            )
            if expired_ids:
                session.execute(
                    delete(TelegramMessageContext).where(TelegramMessageContext.id.in_(expired_ids))
                )
            return len(expired_ids)

    def _due_delivery_ids(self, *, now: datetime) -> tuple[str, ...]:
        with self.session_factory() as session:
            return tuple(
                session.scalars(
                    select(NotificationDelivery.id)
                    .where(
                        NotificationDelivery.channel == "telegram",
                        NotificationDelivery.status.in_(_PENDING_STATUSES),
                        NotificationDelivery.available_at <= now,
                    )
                    .order_by(NotificationDelivery.available_at, NotificationDelivery.id)
                    .limit(self.batch_size)
                )
            )

    def _prepare(self, *, delivery_id: str, now: datetime) -> PreparedTelegramNotification | None:
        with self.session_factory() as session:
            delivery = session.get(NotificationDelivery, delivery_id)
            notification = (
                session.get(Notification, delivery.notification_id)
                if delivery is not None and delivery.channel == "telegram"
                else None
            )
            link = (
                session.get(TelegramAccountLink, notification.recipient_user_id)
                if notification is not None
                else None
            )
            user = (
                session.get(User, notification.recipient_user_id)
                if notification is not None
                else None
            )
            if (
                delivery is None
                or notification is None
                or link is None
                or user is None
                or delivery.status not in _PENDING_STATUSES
                or _as_utc(delivery.available_at) > _as_utc(now)
            ):
                if delivery is not None and delivery.status in _PENDING_STATUSES:
                    delivery.status = "failed"
                    delivery.attempts += 1
                    delivery.available_at = now
                    delivery.last_error_code = "telegram_destination_unavailable"
                    session.commit()
                return None
            callback_token = secrets.token_urlsafe(12)
            text, reply_markup, context_type, target_id, comment_id = build_telegram_notification(
                notification=notification,
                public_web_base_url=self.public_web_base_url,
                language=user.language,
                callback_token=callback_token,
            )
            return PreparedTelegramNotification(
                delivery_id=delivery.id,
                notification_id=notification.id,
                user_id=user.id,
                chat_id=link.telegram_chat_id,
                text=text,
                reply_markup=reply_markup,
                callback_token=callback_token,
                context_type=context_type,
                target_id=target_id,
                comment_id=comment_id,
            )

    def _record_success(
        self,
        *,
        prepared: PreparedTelegramNotification,
        message_id: int,
        now: datetime,
    ) -> bool:
        with self.session_factory() as session, session.begin():
            delivery = session.get(NotificationDelivery, prepared.delivery_id)
            if delivery is None or delivery.status not in _PENDING_STATUSES:
                return False
            delivery.status = "sent"
            delivery.attempts += 1
            delivery.sent_at = now
            delivery.available_at = now
            delivery.last_error_code = None
            session.add(
                TelegramMessageContext(
                    user_id=prepared.user_id,
                    notification_id=prepared.notification_id,
                    delivery_id=prepared.delivery_id,
                    telegram_chat_id=prepared.chat_id,
                    telegram_message_id=message_id,
                    callback_token=prepared.callback_token,
                    context_type=prepared.context_type,
                    target_id=prepared.target_id,
                    comment_id=prepared.comment_id,
                    created_at=now,
                )
            )
            return True

    def _record_failure(
        self,
        *,
        prepared: PreparedTelegramNotification,
        error: TelegramDeliveryError,
        now: datetime,
    ) -> str:
        with self.session_factory() as session, session.begin():
            delivery = session.get(NotificationDelivery, prepared.delivery_id)
            if delivery is None or delivery.status not in _PENDING_STATUSES:
                return "failed"
            delivery.attempts += 1
            if error.transient and delivery.attempts < self.max_attempts:
                delivery.status = "retry"
                delivery.available_at = now + timedelta(
                    seconds=self.retry_base_seconds * (2 ** (delivery.attempts - 1))
                )
                outcome = "retry"
            else:
                delivery.status = "failed"
                delivery.available_at = now
                outcome = "failed"
            delivery.last_error_code = _safe_error_code(error.code)
            if error.destination_unusable:
                link = session.get(TelegramAccountLink, prepared.user_id)
                if link is not None and link.telegram_chat_id == prepared.chat_id:
                    session.delete(link)
            return outcome


def build_telegram_notification(
    *,
    notification: Notification,
    public_web_base_url: str,
    language: str,
    callback_token: str,
) -> tuple[str, dict[str, object] | None, str, str, str | None]:
    subject, body = build_notification_email(
        notification=notification,
        public_web_base_url=public_web_base_url,
        language=language,
    )
    title = subject.removeprefix("JELICA — ")
    text = f"{title}\n\n{body}"[:4096]
    payload = notification.payload if isinstance(notification.payload, dict) else {}
    task_id = _safe_uuid(payload.get("task_id"))
    project_id = _safe_uuid(payload.get("project_id"))
    comment_id = _safe_uuid(payload.get("comment_id"))
    rows: list[list[dict[str, str]]] = []
    context_type = "task" if task_id else "project"
    target_id = task_id or project_id or notification.id
    is_project_discussion = notification.event_id.startswith("project_discussion.")
    is_task_discussion = notification.event_id.startswith("task_discussion.")
    if comment_id is not None and is_project_discussion and project_id is not None:
        context_type = "project_discussion_comment"
        target_id = project_id
    elif comment_id is not None and is_task_discussion and task_id is not None:
        context_type = "task_discussion_comment"
        target_id = task_id
    if context_type in {"project_discussion_comment", "task_discussion_comment"}:
        rows.append(
            [
                {
                    "text": _bot_text("support", language),
                    "callback_data": f"r:{callback_token}:s",
                },
                {
                    "text": _bot_text("oppose", language),
                    "callback_data": f"r:{callback_token}:o",
                },
            ]
        )
        discussion_path = (
            f"/app/projects/{project_id}/discussion"
            if context_type == "project_discussion_comment"
            else f"/app/tasks/{task_id}/discussion"
        )
        rows.append(
            [
                {
                    "text": _bot_text("open-discussion", language),
                    "url": _web_url(public_web_base_url, discussion_path),
                }
            ]
        )
    if task_id is not None:
        rows.append(
            [
                {
                    "text": _bot_text("open-task", language).format(name="task"),
                    "url": _web_url(public_web_base_url, f"/app/tasks/{task_id}"),
                }
            ]
        )
    elif project_id is not None:
        rows.append(
            [
                {
                    "text": _bot_text("open-project", language).format(name="project"),
                    "url": _web_url(public_web_base_url, f"/app/projects/{project_id}"),
                }
            ]
        )
    return text, ({"inline_keyboard": rows} if rows else None), context_type, target_id, comment_id


def _safe_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _bot_text(key: str, language: str) -> str:
    return notification_text(f"notification.telegram.bot.{key}", language)


def _web_url(base: str, path: str) -> str:
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("public Web base URL is invalid")
    return f"{base.rstrip('/')}{path}"


def _safe_error_code(value: object) -> str:
    if not isinstance(value, str):
        return "telegram_delivery_failed"
    normalized = "".join(
        character for character in value if character.isalnum() or character in "_-"
    )
    return normalized[:64] or "telegram_delivery_failed"


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "TELEGRAM_MAX_ATTEMPTS",
    "TELEGRAM_MESSAGE_CONTEXT_RETENTION_DAYS",
    "TELEGRAM_OUTBOX_BATCH_SIZE",
    "TELEGRAM_OUTBOX_POLL_INTERVAL_SECONDS",
    "TELEGRAM_RETRY_BASE_SECONDS",
    "PreparedTelegramNotification",
    "TelegramNotificationWorker",
    "TelegramWorkerReport",
    "build_telegram_notification",
]
