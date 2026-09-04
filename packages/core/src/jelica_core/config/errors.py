from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import ValidationError


class AnalysisConfigValidationCode(StrEnum):
    SCHEMA_VALIDATION = "CONFIG_SCHEMA_VALIDATION"
    COMPARATIVE_ANALYSIS_EMPTY = "COMPARATIVE_ANALYSIS_EMPTY"
    SEQUENCE_DIFFERENCES_EMPTY = "SEQUENCE_DIFFERENCES_EMPTY"
    SEQUENCE_DIFFERENCES_REQUIRES_ALIGNMENT = (
        "SEQUENCE_DIFFERENCES_REQUIRES_ALIGNMENT"
    )
    DISTANCE_MATRIX_REQUIRES_ALIGNMENT = "DISTANCE_MATRIX_REQUIRES_ALIGNMENT"
    PHYLOGENETIC_TREE_REQUIRES_DISTANCE_MATRIX = (
        "PHYLOGENETIC_TREE_REQUIRES_DISTANCE_MATRIX"
    )
    CLADE_DETECTION_REQUIRES_DISTANCE_MATRIX = (
        "CLADE_DETECTION_REQUIRES_DISTANCE_MATRIX"
    )
    CLADE_DETECTION_REQUIRES_PHYLOGENETIC_TREE = (
        "CLADE_DETECTION_REQUIRES_PHYLOGENETIC_TREE"
    )
    CLADE_DETECTION_THRESHOLD_REQUIRED = "CLADE_DETECTION_THRESHOLD_REQUIRED"
    COMPARATIVE_REFERENCE_REQUIRED = "COMPARATIVE_REFERENCE_REQUIRED"
    PAIRWISE_SELECTION_EMPTY = "PAIRWISE_SELECTION_EMPTY"
    PAIRWISE_ALL_WITH_EXPLICIT_SELECTION = "PAIRWISE_ALL_WITH_EXPLICIT_SELECTION"
    PAIRWISE_PAIR_INVALID = "PAIRWISE_PAIR_INVALID"
    PAIRWISE_SELF_PAIR = "PAIRWISE_SELF_PAIR"
    PAIRWISE_GROUP_TOO_SMALL = "PAIRWISE_GROUP_TOO_SMALL"
    PAIRWISE_SELECTOR_INVALID = "PAIRWISE_SELECTOR_INVALID"


class AnalysisConfigError(ValueError):
    """Base error for analysis configuration failures."""


class EmptyConfigJsonError(AnalysisConfigError):
    """Raised when configuration JSON text is empty."""

    def __init__(self) -> None:
        super().__init__("Configuration JSON text is empty.")


class InvalidConfigJsonSyntaxError(AnalysisConfigError):
    """Raised when configuration JSON has invalid syntax."""

    def __init__(self, *, line: int, column: int, detail: str) -> None:
        self.line = line
        self.column = column
        self.detail = detail
        super().__init__(
            f"Invalid configuration JSON syntax at line {line}, column {column}: {detail}."
        )


class InvalidConfigJsonRootTypeError(AnalysisConfigError):
    """Raised when configuration JSON root value is not an object."""

    def __init__(self, *, json_type: str) -> None:
        self.json_type = json_type
        super().__init__(f"Configuration JSON root must be an object, got {json_type}.")


class ConfigSchemaValidationError(AnalysisConfigError):
    """Raised when configuration data violates the Pydantic schema."""

    def __init__(
        self,
        detail: str,
        *,
        code: AnalysisConfigValidationCode = (
            AnalysisConfigValidationCode.SCHEMA_VALIDATION
        ),
        field_path: str | None = None,
    ) -> None:
        self.detail = detail
        self.code = code
        self.field_path = field_path
        if (
            code is AnalysisConfigValidationCode.SCHEMA_VALIDATION
            and field_path is None
        ):
            message = f"Configuration does not match schema: {detail}"
        elif code is AnalysisConfigValidationCode.SCHEMA_VALIDATION:
            message = (
                f"Configuration does not match schema at '{field_path}': {detail}"
            )
        elif field_path is None:
            message = f"Configuration does not match schema [{code.value}]: {detail}"
        else:
            message = (
                f"Configuration does not match schema [{code.value}] at "
                f"'{field_path}': {detail}"
            )
        super().__init__(message)


def convert_config_validation_error(
    error: ValidationError,
) -> ConfigSchemaValidationError:
    """Convert Pydantic errors without exposing rejected input values."""

    details = error.errors(include_url=False, include_input=False)
    if len(details) == 0:
        return ConfigSchemaValidationError("Unknown validation error.")

    first_detail = details[0]
    return ConfigSchemaValidationError(
        str(first_detail.get("msg", "Invalid value.")),
        field_path=_format_validation_location(first_detail.get("loc", ())),
    )


def _format_validation_location(raw_location: tuple[Any, ...]) -> str:
    if len(raw_location) == 0:
        return "<root>"
    parts: list[str] = []
    for segment in raw_location:
        if isinstance(segment, int):
            if len(parts) == 0:
                parts.append(f"[{segment}]")
            else:
                parts[-1] = f"{parts[-1]}[{segment}]"
            continue
        parts.append(str(segment))
    return ".".join(parts)


class UnsupportedConfigSchemaVersionError(AnalysisConfigError):
    """Raised when schema_version is unsupported."""

    def __init__(self, *, schema_version: int, supported_version: int) -> None:
        self.schema_version = schema_version
        self.supported_version = supported_version
        super().__init__(
            "Unsupported analysis config schema_version "
            f"{schema_version}. Supported schema_version is {supported_version}."
        )


class MissingSamplesError(AnalysisConfigError):
    """Raised when final resolved samples are missing or empty."""

    def __init__(self, *, empty_list: bool) -> None:
        self.empty_list = empty_list
        if empty_list:
            message = "Resolved analysis configuration has an empty 'samples' list."
        else:
            message = "Resolved analysis configuration is missing required 'samples'."
        super().__init__(message)


class InvalidConfigOverridePathError(AnalysisConfigError):
    """Raised when a CLI override path is malformed."""

    def __init__(self, *, raw_parameter: str, reason: str) -> None:
        self.raw_parameter = raw_parameter
        self.reason = reason
        super().__init__(f"Invalid CLI override '{raw_parameter}': {reason}.")


class ConfigOverrideApplicationError(AnalysisConfigError):
    """Raised when a CLI override cannot be applied to config data."""

    def __init__(self, *, raw_parameter: str, order: int, reason: str) -> None:
        self.raw_parameter = raw_parameter
        self.order = order
        self.reason = reason
        super().__init__(
            f"Failed to apply CLI override '{raw_parameter}' (operation #{order}): {reason}."
        )
