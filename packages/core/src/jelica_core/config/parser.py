from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any

from pydantic import ValidationError

from .errors import (
    EmptyConfigJsonError,
    InvalidConfigJsonRootTypeError,
    InvalidConfigJsonSyntaxError,
    convert_config_validation_error,
)
from .models import AnalysisConfigInput


class ConfigParser:
    """Parse analysis configuration from JSON text."""

    def parse(self, json_text: str) -> AnalysisConfigInput:
        if json_text.strip() == "":
            raise EmptyConfigJsonError()

        try:
            parsed_value = json.loads(json_text)
        except JSONDecodeError as error:
            raise InvalidConfigJsonSyntaxError(
                line=error.lineno,
                column=error.colno,
                detail=error.msg,
            ) from error

        if not isinstance(parsed_value, dict):
            raise InvalidConfigJsonRootTypeError(json_type=_json_type_name(parsed_value))

        try:
            return AnalysisConfigInput.model_validate(parsed_value)
        except ValidationError as error:
            raise convert_config_validation_error(error) from error


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__
