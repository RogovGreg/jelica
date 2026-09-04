from __future__ import annotations

from typing import TypeAlias

from pydantic import JsonValue as PydanticJsonValue

JSONPrimitive: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = PydanticJsonValue
JSONObject: TypeAlias = dict[str, JSONValue]
