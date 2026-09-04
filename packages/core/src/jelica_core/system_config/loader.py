from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import (
    CoreConfigInvalidRootTypeError,
    CoreConfigInvalidTomlError,
    CoreConfigMissingError,
    CoreConfigMissingFieldError,
    CoreConfigReadError,
    CoreConfigUnknownFieldError,
    CoreConfigValidationError,
)
from .models import CoreConfigInput, CoreDesktopConfigInput, CoreNotificationsConfigInput


class CoreConfigLoader:
    """Read and validate raw system config TOML."""

    def load(self, *, config_path: Path) -> CoreConfigInput:
        text = self._read_text(config_path=config_path)
        parsed_data = self._parse_toml(text=text, config_path=config_path)
        return self.load_from_mapping(data=parsed_data)

    def load_from_mapping(self, *, data: object) -> CoreConfigInput:
        if not isinstance(data, dict):
            raise CoreConfigInvalidRootTypeError(root_type=_python_type_name(data))

        notification_document = {
            key: data[key] for key in ("notifications", "desktop") if key in data
        }
        core_data = {key: value for key, value in data.items() if key not in notification_document}
        try:
            result = CoreConfigInput.model_validate(core_data)
            if "notifications" in notification_document:
                CoreNotificationsConfigInput.model_validate(notification_document["notifications"])
            if "desktop" in notification_document:
                CoreDesktopConfigInput.model_validate(notification_document["desktop"])
            result._notification_document = notification_document
            return result
        except ValidationError as error:
            raise _convert_validation_error(error) from error

    def _read_text(self, *, config_path: Path) -> str:
        try:
            raw_bytes = config_path.read_bytes()
        except FileNotFoundError as error:
            raise CoreConfigMissingError(path=config_path) from error
        except OSError as error:
            raise CoreConfigReadError(path=config_path, detail=str(error)) from error

        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CoreConfigReadError(path=config_path, detail=f"invalid UTF-8: {error}") from error

    def _parse_toml(self, *, text: str, config_path: Path) -> dict[str, Any]:
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise CoreConfigInvalidTomlError(
                path=config_path,
                line=getattr(error, "lineno", None),
                column=getattr(error, "colno", None),
                detail=str(error),
            ) from error

        if not isinstance(parsed, dict):
            raise CoreConfigInvalidRootTypeError(root_type=_python_type_name(parsed))

        return parsed


def _convert_validation_error(error: ValidationError) -> CoreConfigValidationError:
    details = error.errors(include_url=False)
    if len(details) == 0:
        return CoreConfigValidationError(detail="unknown validation error")

    for detail in details:
        if str(detail.get("type", "")) == "missing":
            return CoreConfigMissingFieldError(field_path=_format_location(detail.get("loc", ())))

    first_detail = details[0]
    location = _format_location(first_detail.get("loc", ()))
    error_type = str(first_detail.get("type", ""))

    if error_type == "extra_forbidden":
        return CoreConfigUnknownFieldError(field_path=location)

    message = str(first_detail.get("msg", "invalid value"))
    return CoreConfigValidationError(detail=f"{location}: {message}")


def _format_location(raw_location: tuple[Any, ...]) -> str:
    if len(raw_location) == 0:
        return "<root>"
    return ".".join(str(part) for part in raw_location)


def _python_type_name(value: object) -> str:
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
