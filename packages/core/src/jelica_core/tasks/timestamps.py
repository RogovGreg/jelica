from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def serialize_utc_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime value must be timezone-aware")
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc_datetime(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("timestamp must use UTC 'Z' suffix")

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid timestamp format: {value}") from error

    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")

    normalized = parsed.astimezone(UTC)
    if serialize_utc_datetime(normalized) != value:
        raise ValueError("timestamp must be in canonical UTC format with microseconds")
    return normalized
