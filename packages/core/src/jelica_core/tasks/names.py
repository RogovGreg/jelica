from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

TASK_NAME_MAX_LENGTH = 64

_TASK_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CANONICAL_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_COMPACT_UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
_HUMAN_NAME_WHITESPACE_PATTERN = re.compile(r"\s+")
_HUMAN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def normalize_human_readable_name(value: str) -> str:
    """Normalize the shared human-readable task/result name representation."""

    return _HUMAN_NAME_WHITESPACE_PATTERN.sub("_", value.strip())


def validate_human_readable_name(value: str, *, max_length: int | None = 64) -> str:
    """Normalize and validate the shared task/result human-readable name rules."""

    normalized = normalize_human_readable_name(value)

    if normalized == "" or _HUMAN_NAME_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            "name must be 1..64 characters, start with an ASCII letter or digit, "
            "and contain only ASCII letters, digits, '_' or '-'"
        )
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(
            "name must be 1..64 characters, start with an ASCII letter or digit, "
            "and contain only ASCII letters, digits, '_' or '-'"
        )
    if is_uuid_task_reference(normalized):
        raise ValueError("name must not be a UUID")
    return normalized


def validate_task_name(value: str) -> str:
    """Normalize and validate a task name without changing its original case."""

    return validate_human_readable_name(value, max_length=TASK_NAME_MAX_LENGTH)


def is_uuid_task_reference(value: str) -> bool:
    """Return whether a reference is an unambiguous UUID string."""

    if not (
        _CANONICAL_UUID_PATTERN.fullmatch(value)
        or _COMPACT_UUID_PATTERN.fullmatch(value)
    ):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def normalize_task_reference(value: str) -> tuple[str, bool]:
    """Normalize a human task reference and report whether it is a UUID."""

    normalized = normalize_human_readable_name(value)
    if normalized == "":
        raise ValueError("task reference must not be empty")
    if is_uuid_task_reference(normalized):
        return str(UUID(normalized)), True
    return validate_task_name(normalized), False


def generate_automatic_task_name(timestamp: datetime) -> str:
    """Build a reproducible UTC-based automatic task name."""

    if timestamp.tzinfo is None:
        raise ValueError("automatic task name timestamp must be timezone-aware")
    return timestamp.astimezone(UTC).strftime("analysis-%Y%m%dT%H%M%S")
