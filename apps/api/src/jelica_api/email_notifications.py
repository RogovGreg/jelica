from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jelica_api.auth import EmailDeliveryError, NotificationEmailSender, notification_text
from jelica_api.models import Notification, NotificationDelivery, User

EMAIL_OUTBOX_POLL_INTERVAL_SECONDS = 1.0
EMAIL_OUTBOX_BATCH_SIZE = 50
EMAIL_MAX_ATTEMPTS = 5
EMAIL_RETRY_BASE_SECONDS = 5

_PENDING_STATUSES = ("pending", "retry")


@dataclass(frozen=True, slots=True)
class EmailWorkerReport:
    selected: int
    sent: int
    retried: int
    failed: int


@dataclass(frozen=True, slots=True)
class PreparedNotificationEmail:
    delivery_id: str
    recipient: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class EmailNotificationWorker:
    session_factory: sessionmaker[Session]
    sender: NotificationEmailSender
    public_web_base_url: str
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    batch_size: int = EMAIL_OUTBOX_BATCH_SIZE
    max_attempts: int = EMAIL_MAX_ATTEMPTS
    retry_base_seconds: int = EMAIL_RETRY_BASE_SECONDS

    def __post_init__(self) -> None:
        if self.batch_size <= 0 or self.max_attempts <= 0 or self.retry_base_seconds <= 0:
            raise ValueError("email worker limits must be positive")

    def run_once(self) -> EmailWorkerReport:
        now = self.clock()
        delivery_ids = self._due_delivery_ids(now=now)
        sent = 0
        retried = 0
        failed = 0
        for delivery_id in delivery_ids:
            prepared = self._prepare(delivery_id=delivery_id, now=now)
            if prepared is None:
                failed += 1
                continue
            try:
                self.sender.send_notification(
                    email=prepared.recipient,
                    subject=prepared.subject,
                    body=prepared.body,
                )
            except EmailDeliveryError as error:
                outcome = self._record_failure(prepared=prepared, error=error, now=now)
                if outcome == "retry":
                    retried += 1
                else:
                    failed += 1
            except Exception:
                # Sender implementations must not make worker failures leak raw
                # transport details or break unrelated notification channels.
                self._record_failure(
                    prepared=prepared,
                    error=EmailDeliveryError(code="email_delivery_failed", transient=False),
                    now=now,
                )
                failed += 1
            else:
                if self._record_success(prepared=prepared, now=now):
                    sent += 1
        return EmailWorkerReport(
            selected=len(delivery_ids),
            sent=sent,
            retried=retried,
            failed=failed,
        )

    def _due_delivery_ids(self, *, now: datetime) -> tuple[str, ...]:
        with self.session_factory() as session:
            return tuple(
                session.scalars(
                    select(NotificationDelivery.id)
                    .where(
                        NotificationDelivery.channel == "email",
                        NotificationDelivery.status.in_(_PENDING_STATUSES),
                        NotificationDelivery.available_at <= now,
                    )
                    .order_by(
                        NotificationDelivery.available_at.asc(),
                        NotificationDelivery.id.asc(),
                    )
                    .limit(self.batch_size)
                )
            )

    def _prepare(self, *, delivery_id: str, now: datetime) -> PreparedNotificationEmail | None:
        with self.session_factory() as session:
            delivery = session.get(NotificationDelivery, delivery_id)
            notification = (
                session.get(Notification, delivery.notification_id)
                if delivery is not None and delivery.channel == "email"
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
                or user is None
                or delivery.status not in _PENDING_STATUSES
                or _as_utc(delivery.available_at) > _as_utc(now)
                or not user.email.strip()
            ):
                if delivery is not None and delivery.status in _PENDING_STATUSES:
                    delivery.status = "failed"
                    delivery.attempts += 1
                    delivery.available_at = now
                    delivery.last_error_code = "email_recipient_unavailable"
                    session.commit()
                return None
            subject, body = build_notification_email(
                notification=notification,
                public_web_base_url=self.public_web_base_url,
                language=user.language,
            )
            return PreparedNotificationEmail(
                delivery_id=delivery.id,
                recipient=user.email,
                subject=subject,
                body=body,
            )

    def _record_success(self, *, prepared: PreparedNotificationEmail, now: datetime) -> bool:
        with self.session_factory() as session:
            delivery = session.get(NotificationDelivery, prepared.delivery_id)
            if delivery is None or delivery.status not in _PENDING_STATUSES:
                return False
            delivery.status = "sent"
            delivery.attempts += 1
            delivery.sent_at = now
            delivery.available_at = now
            delivery.last_error_code = None
            session.commit()
            return True

    def _record_failure(
        self,
        *,
        prepared: PreparedNotificationEmail,
        error: EmailDeliveryError,
        now: datetime,
    ) -> str:
        with self.session_factory() as session:
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
            session.commit()
            return outcome


def build_notification_email(
    *,
    notification: Notification,
    public_web_base_url: str,
    language: str = "en",
) -> tuple[str, str]:
    payload = notification.payload if isinstance(notification.payload, dict) else {}
    title = _safe_text(
        _first_text(payload, "email_title", "title", "push_title")
        or _catalog_event_title(event_id=notification.event_id, language=language),
        fallback=_event_title(notification.event_id),
        max_length=160,
    )
    body = _safe_body(
        _first_text(payload, "email_body", "body", "push_body", "message"),
        fallback=notification_text("notification.email.body-fallback", language).format(
            title=title
        ),
        max_length=4_000,
    )
    deep_link = _safe_deep_link(
        public_web_base_url=public_web_base_url,
        target_path=payload.get("target_path"),
    )
    if deep_link is not None:
        body = (
            f"{body.rstrip()}\n\n"
            f"{notification_text('notification.email.open', language)}\n{deep_link}"
        )
    return notification_text("notification.email.subject", language).format(title=title), body


def _first_text(payload: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _event_title(event_id: str) -> str:
    return " ".join(part.replace("_", " ") for part in event_id.split(".")).title()


def _catalog_event_title(*, event_id: str, language: str) -> str:
    value = notification_text(f"notification.{event_id}.title", language)
    return value if not value.startswith("notification.") else _event_title(event_id)


def _safe_text(value: str | None, *, fallback: str, max_length: int) -> str:
    if not value:
        return fallback
    normalized = " ".join(value.split())
    return normalized[:max_length] or fallback


def _safe_body(value: str | None, *, fallback: str, max_length: int) -> str:
    if not value:
        return fallback
    lines = [" ".join(line.split()) for line in value.splitlines()]
    normalized = "\n".join(line for line in lines if line).strip()
    return normalized[:max_length] or fallback


def _safe_deep_link(*, public_web_base_url: str, target_path: object) -> str | None:
    if not isinstance(target_path, str) or not target_path.startswith("/app/"):
        return None
    parsed_path = urlsplit(target_path)
    if (
        target_path.startswith("//")
        or "\\" in target_path
        or any(ord(character) < 32 for character in target_path)
        or parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.query
        or parsed_path.fragment
        or any(segment in {".", ".."} for segment in parsed_path.path.split("/"))
    ):
        return None
    parsed_base = urlsplit(public_web_base_url)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
        return None
    return f"{public_web_base_url.rstrip('/')}{target_path}"


def _safe_error_code(value: object) -> str:
    if not isinstance(value, str):
        return "email_delivery_failed"
    normalized = "".join(
        character for character in value if character.isalnum() or character in "_-"
    )
    return normalized[:64] or "email_delivery_failed"


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "EMAIL_MAX_ATTEMPTS",
    "EMAIL_OUTBOX_BATCH_SIZE",
    "EMAIL_OUTBOX_POLL_INTERVAL_SECONDS",
    "EMAIL_RETRY_BASE_SECONDS",
    "EmailNotificationWorker",
    "EmailWorkerReport",
    "PreparedNotificationEmail",
    "build_notification_email",
]
