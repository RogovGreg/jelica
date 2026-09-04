from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from json import JSONDecodeError
from typing import Any

from pydantic import ValidationError

from .errors import (
    ConfigOverrideApplicationError,
    InvalidConfigOverridePathError,
    convert_config_validation_error,
)
from .models import (
    AnalysisConfigInput,
    ConfigArrayIndexSegment,
    ConfigObjectKeySegment,
    ConfigOverride,
    ConfigPathSegment,
)

MAX_CONFIG_OVERRIDE_ARRAY_INDEX = 100_000


def parse_cli_overrides(
    raw_overrides: Sequence[str],
    *,
    max_array_index: int = MAX_CONFIG_OVERRIDE_ARRAY_INDEX,
) -> list[ConfigOverride]:
    parsed_overrides: list[ConfigOverride] = []
    for order, raw_override in enumerate(raw_overrides):
        parsed_overrides.append(
            parse_cli_override(
                raw_override=raw_override,
                order=order,
                max_array_index=max_array_index,
            )
        )
    return parsed_overrides


def parse_cli_override(
    *,
    raw_override: str,
    order: int,
    max_array_index: int = MAX_CONFIG_OVERRIDE_ARRAY_INDEX,
) -> ConfigOverride:
    if not raw_override.startswith("--"):
        raise InvalidConfigOverridePathError(
            raw_parameter=raw_override,
            reason="dynamic parameter must start with '--'",
        )

    parameter_text = raw_override[2:]
    if "=" not in parameter_text:
        raise InvalidConfigOverridePathError(
            raw_parameter=raw_override,
            reason="dynamic parameter must use '--parameter=value' syntax",
        )

    raw_parameter, raw_value = parameter_text.split("=", 1)
    if raw_parameter == "":
        raise InvalidConfigOverridePathError(
            raw_parameter=raw_override,
            reason="parameter path must not be empty",
        )

    parsed_path = _parse_override_path(
        raw_parameter=raw_parameter,
        raw_override=raw_override,
        max_array_index=max_array_index,
    )
    parsed_value = _parse_override_value(raw_value)

    return ConfigOverride(
        raw_parameter=raw_parameter,
        path=parsed_path,
        value=parsed_value,
        order=order,
    )


def apply_config_overrides(
    *,
    base_config: AnalysisConfigInput,
    overrides: Sequence[ConfigOverride],
) -> AnalysisConfigInput:
    mutable_config = base_config.model_dump(mode="python")
    for override in overrides:
        _apply_single_override(mutable_config, override)

    try:
        return AnalysisConfigInput.model_validate(mutable_config)
    except ValidationError as error:
        raise convert_config_validation_error(error) from error


def _parse_override_path(
    *,
    raw_parameter: str,
    raw_override: str,
    max_array_index: int,
) -> tuple[ConfigPathSegment, ...]:
    if raw_parameter == "":
        raise InvalidConfigOverridePathError(
            raw_parameter=raw_override,
            reason="parameter path must not be empty",
        )

    segments: list[ConfigPathSegment] = []
    for raw_segment in raw_parameter.split("."):
        if raw_segment == "":
            raise InvalidConfigOverridePathError(
                raw_parameter=raw_override,
                reason="path contains an empty segment",
            )

        if raw_segment.startswith("-") and raw_segment[1:].isdigit():
            raise InvalidConfigOverridePathError(
                raw_parameter=raw_override,
                reason="negative array indices are not supported",
            )

        if raw_segment.isdigit():
            index = int(raw_segment)
            if index > max_array_index:
                raise InvalidConfigOverridePathError(
                    raw_parameter=raw_override,
                    reason=(
                        f"array index {index} exceeds maximum supported index {max_array_index}"
                    ),
                )
            segments.append(ConfigArrayIndexSegment(index=index))
            continue

        segments.append(ConfigObjectKeySegment(key=raw_segment))

    return tuple(segments)


def _parse_override_value(raw_value: str) -> Any:
    if raw_value == "":
        return ""

    try:
        return json.loads(raw_value)
    except JSONDecodeError:
        return raw_value


def _apply_single_override(
    root_config: dict[str, Any],
    override: ConfigOverride,
) -> None:
    if len(override.path) == 0:
        raise ConfigOverrideApplicationError(
            raw_parameter=override.raw_parameter,
            order=override.order,
            reason="override path must not be empty",
        )

    first_segment = override.path[0]
    if isinstance(first_segment, ConfigArrayIndexSegment):
        raise ConfigOverrideApplicationError(
            raw_parameter=override.raw_parameter,
            order=override.order,
            reason="root-level array index is not supported",
        )

    current: Any = root_config
    parent: dict[str, Any] | list[Any] | None = None
    parent_segment: ConfigPathSegment | None = None

    for segment_index, segment in enumerate(override.path):
        is_last_segment = segment_index == len(override.path) - 1
        next_segment = None if is_last_segment else override.path[segment_index + 1]

        if isinstance(segment, ConfigObjectKeySegment):
            if not isinstance(current, dict):
                replacement: dict[str, Any] = {}
                _replace_in_parent(
                    parent=parent,
                    parent_segment=parent_segment,
                    replacement=replacement,
                    override=override,
                )
                current = replacement

            if is_last_segment:
                current[segment.key] = copy.deepcopy(override.value)
                return

            child_value = current.get(segment.key)
            if child_value is None:
                child_value = _new_container(next_segment)
                current[segment.key] = child_value
            elif not _container_matches(next_segment, child_value):
                child_value = _new_container(next_segment)
                current[segment.key] = child_value

            parent = current
            parent_segment = segment
            current = child_value
            continue

        if not isinstance(current, list):
            replacement_list: list[Any] = []
            _replace_in_parent(
                parent=parent,
                parent_segment=parent_segment,
                replacement=replacement_list,
                override=override,
            )
            current = replacement_list

        _ensure_list_index(current, segment.index)

        if is_last_segment:
            current[segment.index] = copy.deepcopy(override.value)
            return

        child_value = current[segment.index]
        if child_value is None:
            child_value = _new_container(next_segment)
            current[segment.index] = child_value
        elif not _container_matches(next_segment, child_value):
            child_value = _new_container(next_segment)
            current[segment.index] = child_value

        parent = current
        parent_segment = segment
        current = child_value


def _replace_in_parent(
    *,
    parent: dict[str, Any] | list[Any] | None,
    parent_segment: ConfigPathSegment | None,
    replacement: dict[str, Any] | list[Any],
    override: ConfigOverride,
) -> None:
    if parent is None or parent_segment is None:
        raise ConfigOverrideApplicationError(
            raw_parameter=override.raw_parameter,
            order=override.order,
            reason="unable to replace root container",
        )

    if isinstance(parent_segment, ConfigObjectKeySegment):
        if not isinstance(parent, dict):
            raise ConfigOverrideApplicationError(
                raw_parameter=override.raw_parameter,
                order=override.order,
                reason="parent container type mismatch for object key segment",
            )
        parent[parent_segment.key] = replacement
        return

    if not isinstance(parent, list):
        raise ConfigOverrideApplicationError(
            raw_parameter=override.raw_parameter,
            order=override.order,
            reason="parent container type mismatch for array index segment",
        )
    _ensure_list_index(parent, parent_segment.index)
    parent[parent_segment.index] = replacement


def _new_container(next_segment: ConfigPathSegment | None) -> dict[str, Any] | list[Any]:
    if isinstance(next_segment, ConfigArrayIndexSegment):
        return []
    return {}


def _container_matches(next_segment: ConfigPathSegment | None, value: Any) -> bool:
    if isinstance(next_segment, ConfigArrayIndexSegment):
        return isinstance(value, list)
    return isinstance(value, dict)


def _ensure_list_index(values: list[Any], index: int) -> None:
    if index < len(values):
        return
    values.extend([None] * (index + 1 - len(values)))
