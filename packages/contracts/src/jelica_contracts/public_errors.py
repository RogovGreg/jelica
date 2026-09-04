from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .constants import CONTRACT_SCHEMA_VERSION
from .enums import EventType
from .events import Event
from .json_types import JSONObject


class PublicError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = CONTRACT_SCHEMA_VERSION
    event: Event
    expected: bool = True
    retryable: bool = False
    can_continue: bool = False
    safe_details: JSONObject | None = None

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != CONTRACT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version '{value}'. Expected '{CONTRACT_SCHEMA_VERSION}'."
            )
        return value

    @model_validator(mode="after")
    def _validate_error_event_type(self) -> PublicError:
        if self.event.type not in {EventType.ERROR, EventType.CRITICAL}:
            raise ValueError(
                f"PublicError.event.type must be ERROR or CRITICAL, got {self.event.type.value}."
            )
        return self

    def to_dict(self, *, include_diagnostics: bool = False) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        if not include_diagnostics:
            event_payload = payload.get("event")
            if isinstance(event_payload, dict):
                event_payload.pop("diagnostics", None)
        return payload

    def to_json(self, *, include_diagnostics: bool = False) -> str:
        return json.dumps(
            self.to_dict(include_diagnostics=include_diagnostics),
            ensure_ascii=False,
            sort_keys=True,
        )
