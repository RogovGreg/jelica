from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .codes import validate_code_namespace
from .enums import CodeNamespace, EventType
from .json_types import JSONValue


class EventDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: int = Field(ge=1000, le=9999)
    name: str = Field(min_length=1)
    namespace: CodeNamespace
    default_type: EventType
    title: str = Field(min_length=1)
    message_template: str = Field(min_length=1)
    category: str | None = None

    @model_validator(mode="after")
    def _validate_namespace_range(self) -> EventDefinition:
        validate_code_namespace(namespace=self.namespace, code=self.code, name=self.name)
        return self

    def render_message(self, *, params: Mapping[str, JSONValue] | None = None) -> str:
        if params is None:
            return self.message_template

        safe_params = {key: _format_template_value(value) for key, value in params.items()}
        try:
            return self.message_template.format(**safe_params)
        except KeyError as error:
            raise ValueError(
                f"Missing message template parameter '{error.args[0]}' for '{self.name}'."
            ) from error


def _format_template_value(value: JSONValue) -> str | int | float:
    if isinstance(value, str | int | float):
        return value
    if isinstance(value, bool) or value is None:
        return str(value).lower() if isinstance(value, bool) else "null"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
