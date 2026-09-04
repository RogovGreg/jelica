from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from jelica_api.models import (
    AuthSession,
    Notification,
    NotificationDelivery,
    NotificationDeliveryTarget,
    WebPushSubscription,
)

WEB_PUSH_OUTBOX_POLL_INTERVAL_SECONDS = 1.0
WEB_PUSH_OUTBOX_BATCH_SIZE = 50
WEB_PUSH_MAX_ATTEMPTS = 5
WEB_PUSH_RETRY_BASE_SECONDS = 5
WEB_PUSH_REQUEST_TIMEOUT_SECONDS = 10.0
WEB_PUSH_TTL_SECONDS = 24 * 60 * 60

_PENDING_TARGET_STATUSES = ("pending", "retry")
_PERMANENT_ENDPOINT_STATUS_CODES = frozenset({404, 410})
_TRANSIENT_PROVIDER_STATUS_CODES = frozenset({408, 425, 429})


class WebPushSubscriptionConflictError(ValueError):
    """A browser endpoint is already associated with another account."""


class WebPushSessionUnavailableError(ValueError):
    """The authenticated session disappeared while registering a subscription."""


@dataclass(frozen=True, slots=True)
class WebPushSubscriptionCounts:
    active: int
    current_session: int


@dataclass(frozen=True, slots=True)
class WebPushTarget:
    endpoint: str = field(repr=False)
    p256dh: str = field(repr=False)
    auth: str = field(repr=False)


class WebPushSender(Protocol):
    def send(self, *, target: WebPushTarget, payload: Mapping[str, Any]) -> None: ...


class WebPushDeliveryError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        transient: bool,
        deactivate_subscription: bool = False,
    ) -> None:
        self.code = code
        self.transient = transient
        self.deactivate_subscription = deactivate_subscription
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PyWebPushSender:
    vapid_private_key: str = field(repr=False)
    vapid_subject: str
    timeout_seconds: float = WEB_PUSH_REQUEST_TIMEOUT_SECONDS
    ttl_seconds: int = WEB_PUSH_TTL_SECONDS

    def send(self, *, target: WebPushTarget, payload: Mapping[str, Any]) -> None:
        try:
            from pywebpush import WebPushException, webpush
        except ImportError as error:  # pragma: no cover - dependency is required in production
            raise WebPushDeliveryError(
                code="web_push_dependency_unavailable",
                transient=False,
            ) from error

        try:
            webpush(
                subscription_info={
                    "endpoint": target.endpoint,
                    "keys": {"p256dh": target.p256dh, "auth": target.auth},
                },
                data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                vapid_private_key=self.vapid_private_key,
                vapid_claims={"sub": self.vapid_subject},
                timeout=self.timeout_seconds,
                ttl=self.ttl_seconds,
            )
        except WebPushException as error:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            if status_code in _PERMANENT_ENDPOINT_STATUS_CODES:
                raise WebPushDeliveryError(
                    code="web_push_endpoint_gone",
                    transient=False,
                    deactivate_subscription=True,
                ) from error
            if (
                status_code is None
                or status_code in _TRANSIENT_PROVIDER_STATUS_CODES
                or status_code >= 500
            ):
                raise WebPushDeliveryError(
                    code="web_push_provider_unavailable",
                    transient=True,
                ) from error
            raise WebPushDeliveryError(
                code="web_push_provider_rejected",
                transient=False,
            ) from error
        except Exception as error:
            raise WebPushDeliveryError(
                code="web_push_transport_error",
                transient=True,
            ) from error


@dataclass(frozen=True, slots=True)
class WebPushSubscriptionService:
    session_factory: sessionmaker[Session]
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def counts(self, *, user_id: str, auth_session_id: str) -> WebPushSubscriptionCounts:
        now = self.clock()
        now_ms = _milliseconds_since_epoch(now)
        with self.session_factory() as session:
            base = (
                select(func.count(WebPushSubscription.id))
                .join(
                    AuthSession,
                    AuthSession.id == WebPushSubscription.auth_session_id,
                )
                .where(
                    WebPushSubscription.user_id == user_id,
                    WebPushSubscription.active.is_(True),
                    AuthSession.user_id == user_id,
                    AuthSession.expires_at > now,
                    or_(
                        WebPushSubscription.expiration_time.is_(None),
                        WebPushSubscription.expiration_time > now_ms,
                    ),
                )
            )
            active = int(session.scalar(base) or 0)
            current = int(
                session.scalar(base.where(WebPushSubscription.auth_session_id == auth_session_id))
                or 0
            )
        return WebPushSubscriptionCounts(active=active, current_session=current)

    def upsert(
        self,
        *,
        user_id: str,
        auth_session_id: str,
        endpoint: str,
        expiration_time: int | None,
        p256dh: str,
        auth: str,
    ) -> str:
        now = self.clock()
        fingerprint = endpoint_fingerprint(endpoint=endpoint)
        with self.session_factory() as session:
            auth_session = session.scalar(
                select(AuthSession).where(
                    AuthSession.id == auth_session_id,
                    AuthSession.user_id == user_id,
                    AuthSession.expires_at > now,
                )
            )
            if auth_session is None:
                raise WebPushSessionUnavailableError("authentication is required")

            current = session.scalar(
                select(WebPushSubscription).where(
                    WebPushSubscription.auth_session_id == auth_session_id
                )
            )
            matching_endpoint = session.scalar(
                select(WebPushSubscription).where(
                    WebPushSubscription.endpoint_fingerprint == fingerprint
                )
            )
            if matching_endpoint is not None and matching_endpoint.user_id != user_id:
                raise WebPushSubscriptionConflictError("push subscription is unavailable")

            subscription = current or matching_endpoint
            if (
                current is not None
                and matching_endpoint is not None
                and current.id != matching_endpoint.id
            ):
                session.delete(matching_endpoint)
                session.flush()
                subscription = current
            if subscription is None:
                subscription = WebPushSubscription(
                    user_id=user_id,
                    auth_session_id=auth_session_id,
                    endpoint=endpoint,
                    endpoint_fingerprint=fingerprint,
                    p256dh_key=p256dh,
                    auth_key=auth,
                    expiration_time=expiration_time,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(subscription)
            else:
                subscription.user_id = user_id
                subscription.auth_session_id = auth_session_id
                subscription.endpoint = endpoint
                subscription.endpoint_fingerprint = fingerprint
                subscription.p256dh_key = p256dh
                subscription.auth_key = auth
                subscription.expiration_time = expiration_time
                subscription.active = True
                subscription.disabled_at = None
                subscription.last_error_code = None
                subscription.updated_at = now
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise WebPushSubscriptionConflictError(
                    "push subscription is unavailable"
                ) from error
            return subscription.id

    def delete_current(
        self,
        *,
        user_id: str,
        auth_session_id: str,
        endpoint: str,
    ) -> bool:
        fingerprint = endpoint_fingerprint(endpoint=endpoint)
        with self.session_factory() as session:
            subscription = session.scalar(
                select(WebPushSubscription).where(
                    WebPushSubscription.user_id == user_id,
                    WebPushSubscription.auth_session_id == auth_session_id,
                    WebPushSubscription.endpoint_fingerprint == fingerprint,
                )
            )
            if subscription is None:
                return False
            session.delete(subscription)
            session.commit()
            return True

    def active_subscriptions(
        self,
        *,
        session: Session,
        user_id: str,
        now: datetime | None = None,
    ) -> tuple[WebPushSubscription, ...]:
        resolved_now = self.clock() if now is None else now
        now_ms = _milliseconds_since_epoch(resolved_now)
        return tuple(
            session.scalars(
                select(WebPushSubscription)
                .join(
                    AuthSession,
                    AuthSession.id == WebPushSubscription.auth_session_id,
                )
                .where(
                    WebPushSubscription.user_id == user_id,
                    WebPushSubscription.active.is_(True),
                    AuthSession.user_id == user_id,
                    AuthSession.expires_at > resolved_now,
                    or_(
                        WebPushSubscription.expiration_time.is_(None),
                        WebPushSubscription.expiration_time > now_ms,
                    ),
                )
                .order_by(WebPushSubscription.id.asc())
            )
        )


def add_web_push_delivery_targets(
    *,
    session: Session,
    delivery_id: str,
    subscriptions: Iterable[WebPushSubscription],
) -> int:
    existing = set(
        session.scalars(
            select(NotificationDeliveryTarget.target_fingerprint).where(
                NotificationDeliveryTarget.delivery_id == delivery_id
            )
        )
    )
    added = 0
    for subscription in sorted(subscriptions, key=lambda item: item.id):
        if not subscription.active or subscription.endpoint_fingerprint in existing:
            continue
        session.add(
            NotificationDeliveryTarget(
                delivery_id=delivery_id,
                subscription_id=subscription.id,
                target_fingerprint=subscription.endpoint_fingerprint,
            )
        )
        existing.add(subscription.endpoint_fingerprint)
        added += 1
    if added:
        session.flush()
    return added


@dataclass(frozen=True, slots=True)
class WebPushWorkerReport:
    selected: int
    sent: int
    retried: int
    failed: int
    deactivated_subscriptions: int


@dataclass(frozen=True, slots=True)
class _PreparedTarget:
    target_id: str
    subscription_id: str
    target: WebPushTarget
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PreparationResult:
    prepared: _PreparedTarget | None
    failed: bool = False
    deactivated: bool = False


@dataclass(frozen=True, slots=True)
class WebPushDeliveryWorker:
    session_factory: sessionmaker[Session]
    sender: WebPushSender
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    batch_size: int = WEB_PUSH_OUTBOX_BATCH_SIZE
    max_attempts: int = WEB_PUSH_MAX_ATTEMPTS
    retry_base_seconds: int = WEB_PUSH_RETRY_BASE_SECONDS

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("batch_size must be > 0")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be > 0")
        if self.retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be > 0")

    def run_once(self) -> WebPushWorkerReport:
        now = self.clock()
        target_ids = self._due_target_ids(now=now)
        sent = 0
        retried = 0
        failed = 0
        deactivated = 0
        for target_id in target_ids:
            preparation = self._prepare(target_id=target_id, now=now)
            if preparation.failed:
                failed += 1
            if preparation.deactivated:
                deactivated += 1
            prepared = preparation.prepared
            if prepared is None:
                continue
            try:
                self.sender.send(target=prepared.target, payload=prepared.payload)
            except WebPushDeliveryError as error:
                outcome, did_deactivate = self._record_failure(
                    prepared=prepared,
                    error=error,
                    now=now,
                )
                if outcome == "retry":
                    retried += 1
                elif outcome == "failed":
                    failed += 1
                if did_deactivate:
                    deactivated += 1
            else:
                if self._record_success(prepared=prepared, now=now):
                    sent += 1
        return WebPushWorkerReport(
            selected=len(target_ids),
            sent=sent,
            retried=retried,
            failed=failed,
            deactivated_subscriptions=deactivated,
        )

    def _due_target_ids(self, *, now: datetime) -> tuple[str, ...]:
        with self.session_factory() as session:
            statement = (
                select(NotificationDeliveryTarget.id)
                .join(
                    NotificationDelivery,
                    NotificationDelivery.id == NotificationDeliveryTarget.delivery_id,
                )
                .where(
                    NotificationDelivery.channel == "device",
                    NotificationDeliveryTarget.status.in_(_PENDING_TARGET_STATUSES),
                    NotificationDeliveryTarget.available_at <= now,
                )
                .order_by(
                    NotificationDeliveryTarget.available_at.asc(),
                    NotificationDeliveryTarget.id.asc(),
                )
                .limit(self.batch_size)
            )
            return tuple(session.scalars(statement))

    def _prepare(self, *, target_id: str, now: datetime) -> _PreparationResult:
        with self.session_factory() as session:
            target = session.get(NotificationDeliveryTarget, target_id)
            if (
                target is None
                or target.status not in _PENDING_TARGET_STATUSES
                or _as_utc(target.available_at) > _as_utc(now)
            ):
                return _PreparationResult(prepared=None)
            delivery = session.get(NotificationDelivery, target.delivery_id)
            notification = (
                session.get(Notification, delivery.notification_id)
                if delivery is not None and delivery.channel == "device"
                else None
            )
            subscription = (
                session.get(WebPushSubscription, target.subscription_id)
                if target.subscription_id is not None
                else None
            )
            auth_session = (
                session.get(AuthSession, subscription.auth_session_id)
                if subscription is not None
                else None
            )
            valid = bool(
                delivery is not None
                and notification is not None
                and subscription is not None
                and subscription.active
                and subscription.user_id == notification.recipient_user_id
                and auth_session is not None
                and auth_session.user_id == subscription.user_id
                and _as_utc(auth_session.expires_at) > _as_utc(now)
                and (
                    subscription.expiration_time is None
                    or subscription.expiration_time > _milliseconds_since_epoch(now)
                )
            )
            if not valid:
                did_deactivate = False
                if subscription is not None and subscription.active:
                    subscription.active = False
                    subscription.disabled_at = now
                    subscription.last_error_code = "web_push_subscription_ineligible"
                    did_deactivate = True
                target.status = "failed"
                target.attempts += 1
                target.last_error_code = "web_push_subscription_ineligible"
                target.available_at = now
                self._refresh_parent(session=session, delivery=delivery, now=now)
                session.commit()
                return _PreparationResult(
                    prepared=None,
                    failed=True,
                    deactivated=did_deactivate,
                )
            assert notification is not None
            assert subscription is not None
            return _PreparationResult(
                prepared=_PreparedTarget(
                    target_id=target.id,
                    subscription_id=subscription.id,
                    target=WebPushTarget(
                        endpoint=subscription.endpoint,
                        p256dh=subscription.p256dh_key,
                        auth=subscription.auth_key,
                    ),
                    payload=build_web_push_payload(notification=notification),
                )
            )

    def _record_success(self, *, prepared: _PreparedTarget, now: datetime) -> bool:
        with self.session_factory() as session:
            target = session.get(NotificationDeliveryTarget, prepared.target_id)
            if target is None or target.status not in _PENDING_TARGET_STATUSES:
                return False
            target.status = "sent"
            target.attempts += 1
            target.sent_at = now
            target.last_error_code = None
            subscription = session.get(WebPushSubscription, prepared.subscription_id)
            if subscription is not None:
                subscription.last_success_at = now
                subscription.last_error_code = None
            delivery = session.get(NotificationDelivery, target.delivery_id)
            self._refresh_parent(session=session, delivery=delivery, now=now)
            session.commit()
            return True

    def _record_failure(
        self,
        *,
        prepared: _PreparedTarget,
        error: WebPushDeliveryError,
        now: datetime,
    ) -> tuple[str | None, bool]:
        with self.session_factory() as session:
            target = session.get(NotificationDeliveryTarget, prepared.target_id)
            if target is None or target.status not in _PENDING_TARGET_STATUSES:
                return None, False
            target.attempts += 1
            did_deactivate = False
            if error.transient and target.attempts < self.max_attempts:
                target.status = "retry"
                target.available_at = now + timedelta(
                    seconds=self.retry_base_seconds * (2 ** (target.attempts - 1))
                )
                outcome = "retry"
            else:
                target.status = "failed"
                target.available_at = now
                outcome = "failed"
            target.last_error_code = error.code
            subscription = session.get(WebPushSubscription, prepared.subscription_id)
            if subscription is not None:
                subscription.last_error_code = error.code
                if error.deactivate_subscription and subscription.active:
                    subscription.active = False
                    subscription.disabled_at = now
                    did_deactivate = True
            delivery = session.get(NotificationDelivery, target.delivery_id)
            self._refresh_parent(session=session, delivery=delivery, now=now)
            session.commit()
            return outcome, did_deactivate

    @staticmethod
    def _refresh_parent(
        *,
        session: Session,
        delivery: NotificationDelivery | None,
        now: datetime,
    ) -> None:
        if delivery is None:
            return
        targets = tuple(
            session.scalars(
                select(NotificationDeliveryTarget).where(
                    NotificationDeliveryTarget.delivery_id == delivery.id
                )
            )
        )
        if not targets:
            delivery.status = "failed"
            delivery.last_error_code = "web_push_no_targets"
            delivery.available_at = now
            return
        delivery.attempts = max(target.attempts for target in targets)
        pending = tuple(target for target in targets if target.status in _PENDING_TARGET_STATUSES)
        if pending:
            delivery.status = (
                "retry" if any(target.status == "retry" for target in pending) else "pending"
            )
            delivery.available_at = min(
                pending,
                key=lambda target: _as_utc(target.available_at),
            ).available_at
            delivery.sent_at = None
            delivery.last_error_code = (
                "web_push_target_retry" if delivery.status == "retry" else None
            )
            return
        if all(target.status == "sent" for target in targets):
            delivery.status = "sent"
            delivery.sent_at = (
                max(
                    targets,
                    key=lambda target: _as_utc(target.sent_at or now),
                ).sent_at
                or now
            )
            delivery.available_at = now
            delivery.last_error_code = None
            return
        delivery.status = "failed"
        delivery.sent_at = None
        delivery.available_at = now
        delivery.last_error_code = (
            "web_push_partial_failure"
            if any(target.status == "sent" for target in targets)
            else "web_push_delivery_failed"
        )


def endpoint_fingerprint(*, endpoint: str) -> str:
    return hashlib.sha256(endpoint.strip().encode("utf-8")).hexdigest()


def build_web_push_payload(*, notification: Notification) -> dict[str, Any]:
    payload = notification.payload if isinstance(notification.payload, dict) else {}
    return {
        "notification_id": notification.id,
        "event_id": notification.event_id,
        "title": _safe_push_text(
            payload.get("push_title"),
            fallback="JELICA",
            max_length=120,
        ),
        "body": _safe_push_text(
            payload.get("push_body"),
            fallback="You have a new JELICA notification.",
            max_length=240,
        ),
        "target_path": _safe_relative_deep_link(payload.get("target_path")),
        "tag": f"jelica-notification-{notification.id}",
    }


def _safe_push_text(value: object, *, fallback: str, max_length: int) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = " ".join(value.split())
    if not normalized:
        return fallback
    return normalized[:max_length]


def _safe_relative_deep_link(value: object) -> str:
    if not isinstance(value, str) or len(value) > 1024:
        return "/app/notifications"
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith("/app/")
        or parsed.path.startswith("//")
    ):
        return "/app/notifications"
    return value


def _milliseconds_since_epoch(value: datetime) -> int:
    return int(_as_utc(value).timestamp() * 1000)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "PyWebPushSender",
    "WEB_PUSH_MAX_ATTEMPTS",
    "WEB_PUSH_OUTBOX_BATCH_SIZE",
    "WEB_PUSH_OUTBOX_POLL_INTERVAL_SECONDS",
    "WEB_PUSH_REQUEST_TIMEOUT_SECONDS",
    "WEB_PUSH_RETRY_BASE_SECONDS",
    "WEB_PUSH_TTL_SECONDS",
    "WebPushDeliveryError",
    "WebPushDeliveryWorker",
    "WebPushSessionUnavailableError",
    "WebPushSubscriptionConflictError",
    "WebPushSubscriptionCounts",
    "WebPushSubscriptionService",
    "WebPushTarget",
    "WebPushWorkerReport",
    "add_web_push_delivery_targets",
    "build_web_push_payload",
    "endpoint_fingerprint",
]
