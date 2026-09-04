from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True, slots=True)
class NotificationEventDefinition:
    event_id: str
    category: str
    scope: str
    default_enabled: bool
    channels: tuple[str, ...]
    supersedes: tuple[str, ...] = ()
    active: bool = True


@dataclass(frozen=True, slots=True)
class NotificationCatalog:
    schema_version: int
    channels: tuple[str, ...]
    events: tuple[NotificationEventDefinition, ...]

    @property
    def active_events(self) -> tuple[NotificationEventDefinition, ...]:
        return tuple(event for event in self.events if event.active)

    def event(self, event_id: str) -> NotificationEventDefinition | None:
        return next((item for item in self.active_events if item.event_id == event_id), None)


def load_notification_catalog() -> NotificationCatalog:
    raw = json.loads(files("jelica_contracts").joinpath("notification_events.json").read_text())
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("unsupported notification catalog schema")
    channels = tuple(raw.get("channels", ()))
    events: list[NotificationEventDefinition] = []
    seen: set[str] = set()
    for item in raw.get("events", ()):
        if not isinstance(item, dict):
            raise ValueError("notification event must be an object")
        event_id = str(item["id"])
        if event_id in seen:
            raise ValueError(f"duplicate notification event: {event_id}")
        seen.add(event_id)
        event_channels = tuple(str(value) for value in item.get("channels", ()))
        if any(channel not in channels for channel in event_channels):
            raise ValueError(f"notification event {event_id} has unknown channel")
        events.append(NotificationEventDefinition(
            event_id=event_id,
            category=str(item["category"]),
            scope=str(item["scope"]),
            default_enabled=bool(item["default_enabled"]),
            channels=event_channels,
            supersedes=tuple(str(value) for value in item.get("supersedes", ())),
            active=bool(item.get("active", True)),
        ))
    return NotificationCatalog(schema_version=1, channels=channels, events=tuple(events))


__all__ = ["NotificationCatalog", "NotificationEventDefinition", "load_notification_catalog"]
