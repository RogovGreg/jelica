from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jelica_api.auth.email import EmailDeliveryError
from jelica_api.email_notifications import EmailNotificationWorker
from jelica_api.models import Base, Notification, NotificationDelivery, User
from jelica_api.notifications import NotificationService


class _CaptureEmailSender:
    def __init__(self, failures: list[EmailDeliveryError] | None = None) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.failures = failures or []

    def send_notification(self, *, email: str, subject: str, body: str) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.messages.append((email, subject, body))


def _setup(*, email_available: bool = True):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    service = NotificationService(session_factory=sessions, email_available=email_available)
    with sessions() as session:
        user = User(username="email-user", email="recipient@example.test", password_hash="x")
        session.add(user)
        session.commit()
        user_id = user.id
    return engine, sessions, service, user_id


def _enable_email(
    sessions, service: NotificationService, user_id: str, *, event_enabled: bool | None = None
) -> None:
    with sessions() as session:
        events = () if event_enabled is None else (("task.completed", "email", event_enabled),)
        service.patch(
            session=session,
            user_id=user_id,
            channels={"email": True},
            events=events,
        )
        session.commit()


def _enqueue(
    sessions,
    service: NotificationService,
    user_id: str,
    *,
    source_id: str,
    payload: dict[str, object] | None = None,
) -> Notification:
    with sessions() as session:
        notification = service.enqueue(
            session=session,
            recipient_user_id=user_id,
            event_id="task.completed",
            source_type="task",
            source_id=source_id,
            payload=payload or {},
        )
        assert notification is not None
        session.commit()
        return notification


def test_email_delivery_uses_current_recipient_and_safe_public_link() -> None:
    engine, sessions, service, user_id = _setup()
    _enable_email(sessions, service, user_id)
    notification = _enqueue(
        sessions,
        service,
        user_id,
        source_id="email-1",
        payload={
            "email_title": "Task completed",
            "email_body": "Analysis is ready.",
            "target_path": "/app/tasks/task-1",
        },
    )
    sender = _CaptureEmailSender()
    worker = EmailNotificationWorker(
        session_factory=sessions,
        sender=sender,
        public_web_base_url="https://jelica.example/",
    )

    report = worker.run_once()

    assert report.selected == 1
    assert report.sent == 1
    assert report.retried == 0
    assert report.failed == 0
    assert sender.messages == [
        (
            "recipient@example.test",
            "JELICA — Task completed",
            "Analysis is ready.\n\nOpen in JELICA:\nhttps://jelica.example/app/tasks/task-1",
        )
    ]
    with sessions() as session:
        delivery = session.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_id == notification.id
            )
        )
        assert delivery is not None and delivery.status == "sent" and delivery.attempts == 1
    engine.dispose()


def test_email_without_deep_link_remains_plaintext_and_valid() -> None:
    engine, sessions, service, user_id = _setup()
    _enable_email(sessions, service, user_id)
    _enqueue(
        sessions,
        service,
        user_id,
        source_id="email-no-link",
        payload={"body": "Body only.", "target_path": "/app/tasks/task-1?token=secret"},
    )
    sender = _CaptureEmailSender()
    worker = EmailNotificationWorker(
        session_factory=sessions,
        sender=sender,
        public_web_base_url="https://jelica.example",
    )

    worker.run_once()

    assert sender.messages[0][2] == "Body only."
    assert "<html" not in sender.messages[0][2].lower()
    assert "secret" not in sender.messages[0][2]
    engine.dispose()


def test_email_preference_masters_and_unavailable_transport_gate_outbox() -> None:
    engine, sessions, service, user_id = _setup(email_available=False)
    _enable_email(sessions, service, user_id)
    with sessions() as session:
        snapshot = service.snapshot(session=session, user_id=user_id)
        assert snapshot.channels["email"] == (True, False)
    _enqueue(sessions, service, user_id, source_id="email-unavailable")
    with sessions() as session:
        assert (
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "email")
            ).all()
            == []
        )
    engine.dispose()

    engine, sessions, service, user_id = _setup()
    with sessions() as session:
        service.patch(session=session, user_id=user_id, enabled=False, channels={"email": True})
        session.commit()
    _enqueue(sessions, service, user_id, source_id="email-global-off")
    with sessions() as session:
        assert (
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "email")
            ).all()
            == []
        )
    engine.dispose()

    engine, sessions, service, user_id = _setup()
    with sessions() as session:
        service.patch(session=session, user_id=user_id, channels={"email": False})
        session.commit()
    _enqueue(sessions, service, user_id, source_id="email-channel-off")
    with sessions() as session:
        assert (
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "email")
            ).all()
            == []
        )
    engine.dispose()

    engine, sessions, service, user_id = _setup()
    _enable_email(sessions, service, user_id, event_enabled=False)
    _enqueue(sessions, service, user_id, source_id="email-event-off")
    with sessions() as session:
        assert (
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "email")
            ).all()
            == []
        )
    engine.dispose()


def test_email_delivery_is_deduplicated_and_not_backfilled() -> None:
    engine, sessions, service, user_id = _setup(email_available=False)
    _enable_email(sessions, service, user_id)
    _enqueue(sessions, service, user_id, source_id="historical")
    service.configure_email_delivery(available=True)
    with sessions() as session:
        assert (
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "email")
            ).all()
            == []
        )
    _enqueue(sessions, service, user_id, source_id="new")
    _enqueue(sessions, service, user_id, source_id="new")
    with sessions() as session:
        deliveries = session.scalars(
            select(NotificationDelivery).where(NotificationDelivery.channel == "email")
        ).all()
        assert len(deliveries) == 1
    engine.dispose()


def test_email_transient_failure_retries_then_terminal_failure() -> None:
    engine, sessions, service, user_id = _setup()
    _enable_email(sessions, service, user_id)
    _enqueue(sessions, service, user_id, source_id="retry")
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    with sessions() as session:
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.channel == "email")
        )
        assert delivery is not None
        delivery.available_at = now[0]
        session.commit()
    sender = _CaptureEmailSender(
        failures=[
            EmailDeliveryError(code="smtp_transport_error", transient=True),
            EmailDeliveryError(code="smtp_transport_error", transient=True),
        ]
    )
    worker = EmailNotificationWorker(
        session_factory=sessions,
        sender=sender,
        public_web_base_url="https://jelica.example",
        clock=lambda: now[0],
        max_attempts=2,
        retry_base_seconds=5,
    )

    first = worker.run_once()
    with sessions() as session:
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.channel == "email")
        )
        assert delivery is not None and delivery.status == "retry" and delivery.attempts == 1
        assert delivery.last_error_code == "smtp_transport_error"
    now[0] += timedelta(seconds=5)
    second = worker.run_once()

    assert first.retried == 1 and first.failed == 0
    assert second.retried == 0 and second.failed == 1
    with sessions() as session:
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.channel == "email")
        )
        assert delivery is not None and delivery.status == "failed" and delivery.attempts == 2
    engine.dispose()


def test_email_failure_does_not_change_in_app_delivery() -> None:
    engine, sessions, service, user_id = _setup()
    _enable_email(sessions, service, user_id)
    notification = _enqueue(sessions, service, user_id, source_id="independent")
    sender = _CaptureEmailSender(
        failures=[EmailDeliveryError(code="smtp_rejected", transient=False)]
    )
    worker = EmailNotificationWorker(
        session_factory=sessions,
        sender=sender,
        public_web_base_url="https://jelica.example",
    )

    worker.run_once()

    with sessions() as session:
        deliveries = tuple(
            session.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.notification_id == notification.id
                )
            )
        )
        assert {delivery.channel: delivery.status for delivery in deliveries} == {
            "in_app": "sent",
            "email": "failed",
        }
    engine.dispose()
