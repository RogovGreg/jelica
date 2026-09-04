from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from jelica_api.models import (
    AuthSession,
    Base,
    NotificationDelivery,
    NotificationDeliveryTarget,
    User,
    WebPushSubscription,
)
from jelica_api.notifications import NotificationService
from jelica_api.web_push import (
    WebPushDeliveryError,
    WebPushDeliveryWorker,
    WebPushSubscriptionService,
    WebPushTarget,
    add_web_push_delivery_targets,
)


@dataclass
class _Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class _RetryOneSender:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def send(self, *, target: WebPushTarget, payload) -> None:
        assert payload["target_path"].startswith("/app/")
        self.calls[target.endpoint] = self.calls.get(target.endpoint, 0) + 1
        if target.endpoint.endswith("/retry") and self.calls[target.endpoint] == 1:
            raise WebPushDeliveryError(code="provider_unavailable", transient=True)


class _GoneSender:
    def send(self, *, target: WebPushTarget, payload) -> None:
        _ = (target, payload)
        raise WebPushDeliveryError(
            code="endpoint_gone",
            transient=False,
            deactivate_subscription=True,
        )


def _database():
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def _user_and_session(sessions, *, label: str, now: datetime) -> tuple[User, AuthSession]:
    with sessions() as session:
        user = User(
            username=label,
            email=f"{label}@example.test",
            password_hash="x",
        )
        session.add(user)
        session.flush()
        auth_session = AuthSession(
            user_id=user.id,
            token_hash=(label.encode().hex() + "0" * 64)[:64],
            expires_at=now + timedelta(days=1),
        )
        session.add(auth_session)
        session.commit()
        return user, auth_session


def test_subscription_is_bound_to_auth_session_and_cascades_on_revoke() -> None:
    engine, sessions = _database()
    now = datetime.now(UTC)
    user, auth_session = _user_and_session(sessions, label="push-cascade", now=now)
    service = WebPushSubscriptionService(session_factory=sessions, clock=lambda: now)
    subscription_id = service.upsert(
        user_id=user.id,
        auth_session_id=auth_session.id,
        endpoint="https://push.example.test/cascade",
        expiration_time=None,
        p256dh="p256dh",
        auth="auth",
    )
    assert service.counts(user_id=user.id, auth_session_id=auth_session.id).active == 1
    with sessions() as session:
        session.delete(session.get(AuthSession, auth_session.id))
        session.commit()
        assert session.get(WebPushSubscription, subscription_id) is None
    engine.dispose()


def test_per_subscription_retry_does_not_repeat_successful_target() -> None:
    engine, sessions = _database()
    now = datetime.now(UTC)
    clock = _Clock(now)
    user, first_session = _user_and_session(sessions, label="push-fanout", now=now)
    with sessions() as session:
        second_session = AuthSession(
            user_id=user.id,
            token_hash="f" * 64,
            expires_at=now + timedelta(days=1),
        )
        session.add(second_session)
        session.commit()
    subscriptions = WebPushSubscriptionService(session_factory=sessions, clock=clock)
    subscriptions.upsert(
        user_id=user.id,
        auth_session_id=first_session.id,
        endpoint="https://push.example.test/success",
        expiration_time=None,
        p256dh="p256dh-a",
        auth="auth-a",
    )
    subscriptions.upsert(
        user_id=user.id,
        auth_session_id=second_session.id,
        endpoint="https://push.example.test/retry",
        expiration_time=None,
        p256dh="p256dh-b",
        auth="auth-b",
    )
    notifications = NotificationService(
        session_factory=sessions,
        device_available=True,
        device_target_writer=lambda session, delivery, recipient: add_web_push_delivery_targets(
            session=session,
            delivery_id=delivery.id,
            subscriptions=subscriptions.active_subscriptions(
                session=session, user_id=recipient, now=clock()
            ),
        ),
    )
    with sessions() as session:
        notifications.patch(
            session=session,
            user_id=user.id,
            channels={"device": True},
        )
        notifications.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="task.completed",
            source_type="task",
            source_id="push-task",
            payload={"target_path": "/app/tasks/push-task"},
        )
        session.commit()
        assert len(session.scalars(select(NotificationDeliveryTarget)).all()) == 2

    sender = _RetryOneSender()
    worker = WebPushDeliveryWorker(
        session_factory=sessions,
        sender=sender,
        clock=clock,
        retry_base_seconds=5,
    )
    first = worker.run_once()
    assert (first.sent, first.retried) == (1, 1)
    clock.value += timedelta(seconds=5)
    second = worker.run_once()
    assert second.sent == 1
    assert sender.calls["https://push.example.test/success"] == 1
    assert sender.calls["https://push.example.test/retry"] == 2
    with sessions() as session:
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.channel == "device")
        )
        assert delivery is not None and delivery.status == "sent"
    engine.dispose()


def test_permanent_push_error_disables_only_expired_subscription() -> None:
    engine, sessions = _database()
    now = datetime.now(UTC)
    user, auth_session = _user_and_session(sessions, label="push-gone", now=now)
    subscriptions = WebPushSubscriptionService(session_factory=sessions, clock=lambda: now)
    subscription_id = subscriptions.upsert(
        user_id=user.id,
        auth_session_id=auth_session.id,
        endpoint="https://push.example.test/gone",
        expiration_time=None,
        p256dh="p256dh",
        auth="auth",
    )
    notifications = NotificationService(
        session_factory=sessions,
        device_available=True,
        device_target_writer=lambda session, delivery, recipient: add_web_push_delivery_targets(
            session=session,
            delivery_id=delivery.id,
            subscriptions=subscriptions.active_subscriptions(
                session=session, user_id=recipient, now=now
            ),
        ),
    )
    with sessions() as session:
        notifications.patch(
            session=session,
            user_id=user.id,
            channels={"device": True},
        )
        notifications.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="task.completed",
            source_type="task",
            source_id="gone-task",
            payload={"target_path": "/app/tasks/gone-task"},
        )
        session.commit()
    report = WebPushDeliveryWorker(
        session_factory=sessions,
        sender=_GoneSender(),
        clock=lambda: now,
    ).run_once()
    assert report.failed == 1
    assert report.deactivated_subscriptions == 1
    with sessions() as session:
        subscription = session.get(WebPushSubscription, subscription_id)
        assert subscription is not None and subscription.active is False
        target = session.scalar(select(NotificationDeliveryTarget))
        assert target is not None and target.status == "failed"
    engine.dispose()
