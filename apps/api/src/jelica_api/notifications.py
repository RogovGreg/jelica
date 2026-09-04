from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from jelica_contracts import (
    NotificationCatalog,
    NotificationEventDefinition,
    load_notification_catalog,
)

from .models import (
    Notification,
    NotificationDelivery,
    TelegramAccountLink,
    User,
    UserNotificationChannelSetting,
    UserNotificationEventSetting,
    UserNotificationSettings,
)

WEB_NOTIFICATION_RETENTION_DAYS = 30
WEB_NOTIFICATION_CLEANUP_BATCH_SIZE = 500
WEB_CHANNEL_DEFAULTS = {
    "in_app": True,
    "device": False,
    "email": False,
    "telegram": False,
}

_REALTIME_QUEUE_KEY = "jelica_notification_realtime_queue"
_LOGGER = logging.getLogger(__name__)
_RESOURCE_KINDS = {
    "task",
    "project",
    "project_tasks",
    "project_discussion",
    "task_discussion",
    "invitation",
}


class NotificationPreferenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NotificationPreferenceSnapshot:
    enabled: bool
    sound_enabled: bool
    channels: dict[str, tuple[bool, bool]]
    events: tuple[tuple[NotificationEventDefinition, dict[str, bool], dict[str, bool]], ...]


@dataclass(frozen=True, slots=True)
class NotificationItem:
    id: str
    event_id: str
    category: str
    actor_username: str | None
    resource: dict[str, str | None] | None
    created_at: datetime
    read_at: datetime | None
    target_path: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "category": self.category,
            "actor_username": self.actor_username,
            "resource": self.resource,
            "created_at": self.created_at.isoformat(),
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "target_path": self.target_path,
        }


DeviceTargetWriter = Callable[[Session, NotificationDelivery, str], int]
RealtimePublisher = Callable[[str, dict[str, Any]], None]


class NotificationService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        catalog: NotificationCatalog | None = None,
        device_available: bool = False,
        email_available: bool = False,
        telegram_available: bool = False,
        device_target_writer: DeviceTargetWriter | None = None,
        realtime_publisher: RealtimePublisher | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.catalog = catalog or load_notification_catalog()
        self._device_available = device_available
        self._email_available = email_available
        self._telegram_available = telegram_available
        self._device_target_writer = device_target_writer
        self._realtime_publisher = realtime_publisher
        event.listen(session_factory, "after_commit", self._publish_after_commit)
        event.listen(session_factory, "after_rollback", self._discard_after_rollback)

    def configure_device_delivery(
        self, *, available: bool, target_writer: DeviceTargetWriter | None
    ) -> None:
        self._device_available = available
        self._device_target_writer = target_writer

    def configure_email_delivery(self, *, available: bool) -> None:
        self._email_available = available

    def configure_telegram_delivery(self, *, available: bool) -> None:
        self._telegram_available = available

    def configure_realtime(self, *, publisher: RealtimePublisher | None) -> None:
        self._realtime_publisher = publisher

    def snapshot(self, *, session: Session, user_id: str) -> NotificationPreferenceSnapshot:
        global_row = session.get(UserNotificationSettings, user_id)
        enabled = True if global_row is None else bool(global_row.enabled)
        sound_enabled = True if global_row is None else bool(global_row.sound_enabled)
        channel_rows = {
            row.channel: row
            for row in session.scalars(
                select(UserNotificationChannelSetting).where(
                    UserNotificationChannelSetting.user_id == user_id
                )
            )
        }
        event_rows = {
            (row.event_id, row.channel): row
            for row in session.scalars(
                select(UserNotificationEventSetting).where(
                    UserNotificationEventSetting.user_id == user_id
                )
            )
        }
        availability = {
            "in_app": True,
            "device": self._device_available,
            "email": self._email_available,
            "telegram": self._telegram_available
            and session.get(TelegramAccountLink, user_id) is not None,
        }
        channels = {
            channel: (
                bool(channel_rows[channel].enabled) if channel in channel_rows else default,
                availability[channel],
            )
            for channel, default in WEB_CHANNEL_DEFAULTS.items()
        }
        events = []
        for definition in self.catalog.active_events:
            configured: dict[str, bool] = {}
            effective: dict[str, bool] = {}
            for channel in definition.channels:
                if channel in {"desktop_in_app", "whatsapp"}:
                    continue
                override = event_rows.get((definition.event_id, channel))
                configured[channel] = (
                    bool(override.enabled) if override else bool(definition.default_enabled)
                )
                channel_enabled, channel_available = channels.get(channel, (False, False))
                effective[channel] = (
                    configured[channel] and enabled and channel_enabled and channel_available
                )
            events.append((definition, configured, effective))
        return NotificationPreferenceSnapshot(
            enabled=enabled,
            sound_enabled=sound_enabled,
            channels=channels,
            events=tuple(events),
        )

    def patch(
        self,
        *,
        session: Session,
        user_id: str,
        enabled: bool | None = None,
        sound_enabled: bool | None = None,
        channels: Mapping[str, bool] | None = None,
        events: tuple[tuple[str, str, bool], ...] | None = None,
    ) -> NotificationPreferenceSnapshot:
        catalog_channels = set(WEB_CHANNEL_DEFAULTS)
        for channel in channels or {}:
            if channel not in catalog_channels:
                raise NotificationPreferenceError(f"unknown notification channel: {channel}")
        for event_id, channel, _ in events or ():
            definition = self.catalog.event(event_id)
            if (
                definition is None
                or definition not in self.catalog.active_events
                or definition.scope == "local"
                or channel not in definition.channels
                or channel not in WEB_CHANNEL_DEFAULTS
                or channel == "desktop_in_app"
            ):
                raise NotificationPreferenceError(
                    f"notification event/channel is not available: {event_id}/{channel}"
                )

        global_row = session.get(UserNotificationSettings, user_id)
        next_enabled = (
            (bool(global_row.enabled) if global_row is not None else True)
            if enabled is None
            else enabled
        )
        next_sound = (
            (bool(global_row.sound_enabled) if global_row is not None else True)
            if sound_enabled is None
            else sound_enabled
        )
        if next_enabled and next_sound:
            if global_row is not None:
                session.delete(global_row)
        elif global_row is None:
            session.add(
                UserNotificationSettings(
                    user_id=user_id,
                    enabled=next_enabled,
                    sound_enabled=next_sound,
                )
            )
        else:
            global_row.enabled = next_enabled
            global_row.sound_enabled = next_sound

        for channel, value in (channels or {}).items():
            default = WEB_CHANNEL_DEFAULTS[channel]
            row = session.scalar(
                select(UserNotificationChannelSetting).where(
                    UserNotificationChannelSetting.user_id == user_id,
                    UserNotificationChannelSetting.channel == channel,
                )
            )
            if value == default:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(
                    UserNotificationChannelSetting(user_id=user_id, channel=channel, enabled=value)
                )
            else:
                row.enabled = value
        for event_id, channel, value in events or ():
            definition = self.catalog.event(event_id)
            assert definition is not None
            row = session.scalar(
                select(UserNotificationEventSetting).where(
                    UserNotificationEventSetting.user_id == user_id,
                    UserNotificationEventSetting.event_id == event_id,
                    UserNotificationEventSetting.channel == channel,
                )
            )
            if value == definition.default_enabled:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(
                    UserNotificationEventSetting(
                        user_id=user_id,
                        event_id=event_id,
                        channel=channel,
                        enabled=value,
                    )
                )
            else:
                row.enabled = value
        session.flush()
        return self.snapshot(session=session, user_id=user_id)

    def enqueue(
        self,
        *,
        session: Session,
        recipient_user_id: str,
        event_id: str,
        source_type: str,
        source_id: str,
        payload: dict[str, Any],
        actor_user_id: str | None = None,
    ) -> Notification | None:
        definition = self.catalog.event(event_id)
        if definition is None or definition not in self.catalog.active_events:
            raise NotificationPreferenceError(f"unknown or deferred notification event: {event_id}")
        if actor_user_id == recipient_user_id:
            return None

        for superseded_id in definition.supersedes:
            superseded = tuple(
                session.scalars(
                    select(Notification).where(
                        Notification.recipient_user_id == recipient_user_id,
                        Notification.event_id == superseded_id,
                        Notification.source_type == source_type,
                        Notification.source_id == source_id,
                    )
                )
            )
            if superseded:
                pending = session.info.get(_REALTIME_QUEUE_KEY, {})
                for existing_notification in superseded:
                    pending.pop(existing_notification.id, None)
                    session.execute(
                        delete(NotificationDelivery).where(
                            NotificationDelivery.notification_id == existing_notification.id
                        )
                    )
                    session.delete(existing_notification)
                session.flush()
        for candidate in self.catalog.active_events:
            if event_id not in candidate.supersedes:
                continue
            existing_superseding = session.scalar(
                select(Notification).where(
                    Notification.recipient_user_id == recipient_user_id,
                    Notification.event_id == candidate.event_id,
                    Notification.source_type == source_type,
                    Notification.source_id == source_id,
                )
            )
            if existing_superseding is not None:
                return existing_superseding
        existing = session.scalar(
            select(Notification).where(
                Notification.recipient_user_id == recipient_user_id,
                Notification.event_id == event_id,
                Notification.source_type == source_type,
                Notification.source_id == source_id,
            )
        )
        if existing is not None:
            return existing

        notification = Notification(
            recipient_user_id=recipient_user_id,
            actor_user_id=actor_user_id,
            event_id=event_id,
            source_type=source_type,
            source_id=source_id,
            payload=dict(payload),
        )
        session.add(notification)
        session.flush()
        snapshot = self.snapshot(session=session, user_id=recipient_user_id)
        effective = dict(
            next(
                (
                    values
                    for event_definition, _, values in snapshot.events
                    if event_definition.event_id == event_id
                ),
                {},
            )
        )
        in_app_eligible = False
        for channel, is_enabled in effective.items():
            if not is_enabled:
                continue
            if channel == "in_app":
                now = datetime.now(timezone.utc)
                session.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        channel=channel,
                        status="sent",
                        attempts=1,
                        sent_at=now,
                    )
                )
                in_app_eligible = True
            elif channel == "device" and self._device_target_writer is not None:
                delivery = NotificationDelivery(
                    notification_id=notification.id,
                    channel=channel,
                )
                session.add(delivery)
                session.flush()
                if self._device_target_writer(session, delivery, recipient_user_id) == 0:
                    session.delete(delivery)
            elif channel == "email" and self._email_available:
                session.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        channel=channel,
                    )
                )
            elif channel == "telegram" and self._telegram_available:
                session.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        channel=channel,
                    )
                )
        session.flush()
        if in_app_eligible:
            item = self.to_item(session=session, notification=notification)
            self._queue_realtime(
                session=session,
                recipient_user_id=recipient_user_id,
                key=notification.id,
                message={"type": "notification.created", "notification": item.as_dict()},
            )
        return notification

    def list_inbox(self, *, session: Session, user_id: str) -> tuple[NotificationItem, ...]:
        cutoff = self.retention_cutoff()
        rows = tuple(
            session.scalars(
                select(Notification)
                .join(
                    NotificationDelivery,
                    NotificationDelivery.notification_id == Notification.id,
                )
                .where(
                    Notification.recipient_user_id == user_id,
                    Notification.created_at >= cutoff,
                    NotificationDelivery.channel == "in_app",
                )
                .order_by(Notification.created_at.desc(), Notification.id.desc())
            )
        )
        return tuple(self.to_item(session=session, notification=row) for row in rows)

    def unread_count(self, *, session: Session, user_id: str) -> int:
        return int(
            session.scalar(
                select(func.count(Notification.id))
                .join(
                    NotificationDelivery,
                    NotificationDelivery.notification_id == Notification.id,
                )
                .where(
                    Notification.recipient_user_id == user_id,
                    Notification.created_at >= self.retention_cutoff(),
                    Notification.read_at.is_(None),
                    NotificationDelivery.channel == "in_app",
                )
            )
            or 0
        )

    def set_read(
        self, *, session: Session, user_id: str, notification_id: str, read: bool
    ) -> NotificationItem | None:
        notification = session.scalar(
            select(Notification)
            .join(
                NotificationDelivery,
                NotificationDelivery.notification_id == Notification.id,
            )
            .where(
                Notification.id == notification_id,
                Notification.recipient_user_id == user_id,
                Notification.created_at >= self.retention_cutoff(),
                NotificationDelivery.channel == "in_app",
            )
        )
        if notification is None:
            return None
        notification.read_at = datetime.now(timezone.utc) if read else None
        session.flush()
        item = self.to_item(session=session, notification=notification)
        self._queue_realtime(
            session=session,
            recipient_user_id=user_id,
            key=f"read:{notification.id}",
            message={
                "type": "notification.read_changed",
                "notification_id": notification.id,
                "read_at": item.read_at.isoformat() if item.read_at else None,
            },
        )
        return item

    def mark_all_read(self, *, session: Session, user_id: str) -> tuple[int, datetime]:
        now = datetime.now(timezone.utc)
        notifications = tuple(
            session.scalars(
                select(Notification)
                .join(
                    NotificationDelivery,
                    NotificationDelivery.notification_id == Notification.id,
                )
                .where(
                    Notification.recipient_user_id == user_id,
                    Notification.created_at >= self.retention_cutoff(now=now),
                    Notification.read_at.is_(None),
                    NotificationDelivery.channel == "in_app",
                )
            )
        )
        for notification in notifications:
            notification.read_at = now
        session.flush()
        self._queue_realtime(
            session=session,
            recipient_user_id=user_id,
            key="all-read",
            message={"type": "notifications.all_read", "read_at": now.isoformat()},
        )
        return len(notifications), now

    def cleanup_expired(self, *, session: Session) -> int:
        expired_ids = tuple(
            session.scalars(
                select(Notification.id)
                .where(Notification.created_at < self.retention_cutoff())
                .order_by(Notification.created_at)
                .limit(WEB_NOTIFICATION_CLEANUP_BATCH_SIZE)
            )
        )
        if expired_ids:
            session.execute(delete(Notification).where(Notification.id.in_(expired_ids)))
        return len(expired_ids)

    def to_item(self, *, session: Session, notification: Notification) -> NotificationItem:
        definition = self.catalog.event(notification.event_id)
        category = definition.category if definition is not None else "unknown"
        actor_username = None
        if notification.actor_user_id:
            actor = session.get(User, notification.actor_user_id)
            actor_username = actor.username if actor is not None else None
        payload = notification.payload if isinstance(notification.payload, dict) else {}
        resource_kind = payload.get("resource_kind")
        resource = None
        if isinstance(resource_kind, str) and resource_kind in _RESOURCE_KINDS:
            resource = {
                "kind": resource_kind,
                "project_id": _safe_identifier(payload.get("project_id")),
                "task_id": _safe_identifier(payload.get("task_id")),
                "display_name": _safe_display_name(payload.get("display_name")),
            }
        return NotificationItem(
            id=notification.id,
            event_id=notification.event_id,
            category=category,
            actor_username=actor_username,
            resource=resource,
            created_at=notification.created_at,
            read_at=notification.read_at,
            target_path=_safe_target_path(payload.get("target_path")),
        )

    @staticmethod
    def retention_cutoff(*, now: datetime | None = None) -> datetime:
        reference = now or datetime.now(timezone.utc)
        return reference - timedelta(days=WEB_NOTIFICATION_RETENTION_DAYS)

    def _queue_realtime(
        self,
        *,
        session: Session,
        recipient_user_id: str,
        key: str,
        message: dict[str, Any],
    ) -> None:
        queued = session.info.setdefault(_REALTIME_QUEUE_KEY, {})
        queued[key] = (recipient_user_id, message)

    def _publish_after_commit(self, session: Session) -> None:
        queued = session.info.pop(_REALTIME_QUEUE_KEY, {})
        publisher = self._realtime_publisher
        if publisher is None:
            return
        for recipient_user_id, message in queued.values():
            try:
                publisher(recipient_user_id, message)
            except Exception:
                _LOGGER.exception("notification realtime publication failed after commit")

    @staticmethod
    def _discard_after_rollback(session: Session) -> None:
        session.info.pop(_REALTIME_QUEUE_KEY, None)


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 160:
        return None
    if any(ord(character) < 32 for character in value):
        return None
    return value


def _safe_display_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())[:160]
    return normalized or None


def _safe_target_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("/app/"):
        return None
    if value.startswith("//") or "\\" in value or len(value) > 500:
        return None
    if any(ord(character) < 32 for character in value):
        return None
    return value


__all__ = [
    "NotificationItem",
    "NotificationPreferenceError",
    "NotificationPreferenceSnapshot",
    "NotificationService",
    "WEB_CHANNEL_DEFAULTS",
    "WEB_NOTIFICATION_CLEANUP_BATCH_SIZE",
    "WEB_NOTIFICATION_RETENTION_DAYS",
]
