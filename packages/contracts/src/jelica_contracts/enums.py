from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class EventComponent(StrEnum):
    SYSTEM = "system"
    CORE = "core"
    CLI = "cli"
    SERVER = "server"
    WEB = "web"
    DESKTOP = "desktop"


class CodeNamespace(StrEnum):
    SYSTEM = "SYSTEM"
    CORE = "CORE"
    CLI = "CLI"
    SERVER = "SERVER"
    WEB = "WEB"
    DESKTOP = "DESKTOP"
    RESERVED = "RESERVED"


_EVENT_TYPE_RANK: dict[EventType, int] = {
    EventType.DEBUG: 10,
    EventType.INFO: 20,
    EventType.SUCCESS: 25,
    EventType.WARNING: 30,
    EventType.ERROR: 40,
    EventType.CRITICAL: 50,
}


def event_type_rank(event_type: EventType) -> int:
    return _EVENT_TYPE_RANK[event_type]
