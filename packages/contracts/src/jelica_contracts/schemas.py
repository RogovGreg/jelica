from __future__ import annotations

from typing import Any

from .events import Event
from .public_errors import PublicError


def event_json_schema() -> dict[str, Any]:
    return Event.model_json_schema()


def public_error_json_schema() -> dict[str, Any]:
    return PublicError.model_json_schema()


def contract_json_schemas() -> dict[str, dict[str, Any]]:
    return {
        "event": event_json_schema(),
        "public_error": public_error_json_schema(),
    }
