# ruff: noqa: E501
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from platformdirs import user_data_path

from jelica_contracts import load_notification_catalog
from jelica_core.tasks import AnalyticalTaskRegistry

from .errors import (
    CoreConfigInvalidValueError,
    CoreConfigMissingError,
    CoreConfigParameterAlreadyUnsetError,
    CoreConfigParameterNotMutableError,
    CoreConfigParameterNotRemovableError,
    CoreConfigPathResolutionError,
    CoreConfigUnknownParameterError,
    CoreNotInitializedError,
    CoreWorkingDirectoryCreationError,
)
from .loader import CoreConfigLoader
from .models import (
    CONFIG_FILENAME,
    DATA_SECTION_NAME,
    DEFAULT_DATA_DIRECTORY,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MAX_PARALLEL_TASKS,
    EXECUTION_SECTION_NAME,
    IMMUTABLE_CORE_CONFIG_PARAMETERS,
    LOGGING_SECTION_NAME,
    MUTABLE_CORE_CONFIG_PARAMETERS,
    SUPPORTED_ALIGNMENT_MODES,
    TOOLS_SECTION_NAME,
    CoreConfigInput,
    ResolvedCoreConfig,
    build_default_core_config_document,
    to_toml_document,
)
from .resolver import CoreConfigResolver
from .writer import CoreConfigWriter

JELICA_HOME_ENV_VAR: Final = "JELICA_HOME"
_CORE_CONFIG_PARAMETER_ALIASES: Final[dict[str, str]] = {
    "data.directory": "data.directory",
    "input_directory_max_depth": "input_directory_max_depth",
    "ncbi_api_key": "ncbi_api_key",
    "ncbi_max_retries": "ncbi_max_retries",
    "default_alignment_mode": "default_alignment_mode",
    "execution.max_parallel_tasks": "execution.max_parallel_tasks",
    "execution.max_workers": "execution.max_parallel_tasks",
    "execution.scheduler_poll_interval_seconds": "execution.scheduler_poll_interval_seconds",
    "execution.heartbeat_interval_seconds": "execution.heartbeat_interval_seconds",
    "execution.lease_timeout_seconds": "execution.lease_timeout_seconds",
    "execution.progress_flush_interval_seconds": "execution.progress_flush_interval_seconds",
    "execution.max_recovery_attempts": "execution.max_recovery_attempts",
    "logging.level": "logging.level",
    "logging.system_level": "logging.system_level",
    "logging.task_level": "logging.task_level",
    "logging.include_diagnostics": "logging.include_diagnostics",
    "logging.diagnostic_field_limit": "logging.diagnostic_field_limit",
    "tools.mafft.executable": "tools.mafft.executable",
    "schema_version": "schema_version",
    "data_directory": "data.directory",
    "data_dir": "data.directory",
    "data-directory": "data.directory",
    "input-directory-max-depth": "input_directory_max_depth",
    "ncbi-api-key": "ncbi_api_key",
    "ncbi-max-retries": "ncbi_max_retries",
    "default-alignment-mode": "default_alignment_mode",
    "max_parallel_tasks": "execution.max_parallel_tasks",
    "max-parallel-tasks": "execution.max_parallel_tasks",
    "max_workers": "execution.max_parallel_tasks",
    "max-workers": "execution.max_parallel_tasks",
    "scheduler_poll_interval_seconds": "execution.scheduler_poll_interval_seconds",
    "scheduler-poll-interval-seconds": "execution.scheduler_poll_interval_seconds",
    "heartbeat_interval_seconds": "execution.heartbeat_interval_seconds",
    "heartbeat-interval-seconds": "execution.heartbeat_interval_seconds",
    "lease_timeout_seconds": "execution.lease_timeout_seconds",
    "lease-timeout-seconds": "execution.lease_timeout_seconds",
    "progress_flush_interval_seconds": "execution.progress_flush_interval_seconds",
    "progress-flush-interval-seconds": "execution.progress_flush_interval_seconds",
    "max_recovery_attempts": "execution.max_recovery_attempts",
    "max-recovery-attempts": "execution.max_recovery_attempts",
    "log_level": "logging.level",
    "log-level": "logging.level",
    "system_log_level": "logging.system_level",
    "system-log-level": "logging.system_level",
    "task_log_level": "logging.task_level",
    "task-log-level": "logging.task_level",
    "include_diagnostics": "logging.include_diagnostics",
    "include-diagnostics": "logging.include_diagnostics",
    "diagnostic_field_limit": "logging.diagnostic_field_limit",
    "diagnostic-field-limit": "logging.diagnostic_field_limit",
    "mafft_executable": "tools.mafft.executable",
    "mafft-executable": "tools.mafft.executable",
    "mafft.executable": "tools.mafft.executable",
    "schema-version": "schema_version",
}


def _is_notification_parameter(parameter: str) -> bool:
    return parameter in {
        "notifications.device.enabled",
        "desktop.notifications.in_app.enabled",
        "notifications.sound.enabled",
    } or parameter.startswith("notifications.device.events.") or parameter.startswith("desktop.notifications.in_app.events.")


class CoreConfigService:
    """High-level operations for system config path, IO, and mutation."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        jelica_home: Path | None = None,
        platform_home_resolver: Callable[[], Path] | None = None,
        loader: CoreConfigLoader | None = None,
        resolver: CoreConfigResolver | None = None,
        writer: CoreConfigWriter | None = None,
    ) -> None:
        self._environment = environment if environment is not None else os.environ
        self._jelica_home = jelica_home
        self._platform_home_resolver = platform_home_resolver or _default_platform_home
        self._loader = loader or CoreConfigLoader()
        self._resolver = resolver or CoreConfigResolver()
        self._writer = writer or CoreConfigWriter()

    def get_jelica_home(self) -> Path:
        if self._jelica_home is not None:
            return _normalize_home_path(self._jelica_home, source="jelica_home argument")

        raw_home = self._environment.get(JELICA_HOME_ENV_VAR)
        if raw_home is not None:
            stripped_home = raw_home.strip()
            if stripped_home == "":
                raise CoreConfigPathResolutionError(
                    detail=f"environment variable {JELICA_HOME_ENV_VAR} is empty"
                )
            return _normalize_home_path(Path(stripped_home), source=JELICA_HOME_ENV_VAR)

        return _normalize_home_path(self._platform_home_resolver(), source="platform default")

    def get_config_path(self) -> Path:
        return self.get_jelica_home() / CONFIG_FILENAME

    def initialize_system_config(
        self,
        *,
        data_directory: str | None = None,
        max_parallel_tasks: int | None = None,
        max_workers: int | None = None,
        log_level: str | None = None,
        force: bool = False,
    ) -> ResolvedCoreConfig:
        config_path = self.get_config_path()
        _ = force
        self._ensure_directory(config_path.parent)
        if config_path.exists():
            resolved_config = self.load_resolved_config()
            self._ensure_runtime_directories(resolved_config)
            self._initialize_task_registry_database(database_path=resolved_config.database_path)
            return resolved_config

        effective_max_parallel_tasks = (
            max_parallel_tasks if max_parallel_tasks is not None else max_workers
        )
        config_input = self._build_initialized_input(
            data_directory=data_directory,
            max_parallel_tasks=effective_max_parallel_tasks,
            log_level=log_level,
        )
        resolved_config = self._resolver.resolve(config_input=config_input, config_path=config_path)
        self._ensure_runtime_directories(resolved_config)
        self._initialize_task_registry_database(database_path=resolved_config.database_path)
        self._writer.write(config_path=config_path, config_input=config_input)
        return resolved_config

    def load_resolved_config(self) -> ResolvedCoreConfig:
        config_path = self.get_config_path()
        config_input = self._loader.load(config_path=config_path)
        return self._resolver.resolve(config_input=config_input, config_path=config_path)

    def require_initialized_config(self) -> ResolvedCoreConfig:
        try:
            return self.load_resolved_config()
        except CoreConfigMissingError as error:
            raise CoreNotInitializedError(path=error.path) from error

    def validate_current_config(self) -> ResolvedCoreConfig:
        resolved_config = self.load_resolved_config()
        self._validate_task_registry_database(database_path=resolved_config.database_path)
        return resolved_config

    def set_parameter(self, *, parameter: str, value: str) -> ResolvedCoreConfig:
        resolved_parameter = self._resolve_parameter_name(parameter=parameter)

        if resolved_parameter in IMMUTABLE_CORE_CONFIG_PARAMETERS:
            raise CoreConfigParameterNotMutableError(parameter=resolved_parameter)
        if resolved_parameter not in MUTABLE_CORE_CONFIG_PARAMETERS and not _is_notification_parameter(resolved_parameter):
            raise CoreConfigUnknownParameterError(parameter=parameter)

        stripped_value = value.strip()
        if stripped_value == "":
            raise CoreConfigInvalidValueError(
                parameter=resolved_parameter,
                detail="value must not be empty",
            )

        parsed_value = self._coerce_parameter_value(
            parameter=resolved_parameter,
            value=stripped_value,
        )

        config_path = self.get_config_path()
        current_input = self._loader.load(config_path=config_path)
        document = to_toml_document(current_input)
        self._set_parameter_in_document(
            document=document,
            parameter=resolved_parameter,
            value=parsed_value,
        )

        updated_input = self._loader.load_from_mapping(data=document)
        resolved_config = self._resolver.resolve(
            config_input=updated_input,
            config_path=config_path,
        )

        self._writer.write(config_path=config_path, config_input=updated_input)
        self._ensure_runtime_directories(resolved_config)
        return resolved_config

    def unset_parameter(self, *, parameter: str) -> ResolvedCoreConfig:
        resolved_parameter = self._resolve_parameter_name(parameter=parameter)

        if resolved_parameter in IMMUTABLE_CORE_CONFIG_PARAMETERS:
            raise CoreConfigParameterNotRemovableError(parameter=resolved_parameter)
        if resolved_parameter not in MUTABLE_CORE_CONFIG_PARAMETERS and not _is_notification_parameter(resolved_parameter):
            raise CoreConfigUnknownParameterError(parameter=parameter)

        config_path = self.get_config_path()
        current_input = self._loader.load(config_path=config_path)
        document = to_toml_document(current_input)
        self._reset_parameter_in_document(document=document, parameter=resolved_parameter)

        updated_input = self._loader.load_from_mapping(data=document)
        resolved_config = self._resolver.resolve(
            config_input=updated_input,
            config_path=config_path,
        )

        self._writer.write(config_path=config_path, config_input=updated_input)
        self._ensure_runtime_directories(resolved_config)
        return resolved_config

    def _build_initialized_input(
        self,
        *,
        data_directory: str | None,
        max_parallel_tasks: int | None,
        log_level: str | None,
    ) -> CoreConfigInput:
        document = build_default_core_config_document(
            data_directory=(
                data_directory if data_directory is not None else DEFAULT_DATA_DIRECTORY
            ),
            max_parallel_tasks=(
                max_parallel_tasks if max_parallel_tasks is not None else DEFAULT_MAX_PARALLEL_TASKS
            ),
            log_level=log_level if log_level is not None else DEFAULT_LOG_LEVEL,
        )
        return self._loader.load_from_mapping(data=document)

    def _set_parameter_in_document(
        self,
        *,
        document: dict[str, object],
        parameter: str,
        value: str | int | float | bool,
    ) -> None:
        if _is_notification_parameter(parameter):
            parts = parameter.split(".")
            if parameter == "notifications.sound.enabled":
                notifications = _get_or_create_section(document=document, section_name="notifications")
                sound = _get_or_create_section(document=notifications, section_name="sound")
                sound["enabled"] = value
                return
            if parts[0] == "notifications":
                root = _get_or_create_section(document=document, section_name="notifications")
                channel = _get_or_create_section(document=root, section_name="device")
            else:
                desktop = _get_or_create_section(document=document, section_name="desktop")
                notifications = _get_or_create_section(document=desktop, section_name="notifications")
                channel = _get_or_create_section(document=notifications, section_name="in_app")
            if parts[-1] == "enabled":
                channel["enabled"] = value
            else:
                events = _get_or_create_section(document=channel, section_name="events")
                events[parameter.split(".events.", 1)[1]] = value
            return
        if parameter == "input_directory_max_depth":
            document["input_directory_max_depth"] = value
            return
        if parameter == "ncbi_api_key":
            document["ncbi_api_key"] = value
            return
        if parameter == "ncbi_max_retries":
            document["ncbi_max_retries"] = value
            return
        if parameter == "default_alignment_mode":
            document["default_alignment_mode"] = value
            return
        if parameter == "data.directory":
            section = _get_or_create_section(document=document, section_name=DATA_SECTION_NAME)
            section["directory"] = value
            return
        if parameter == "execution.max_parallel_tasks":
            section = _get_or_create_section(document=document, section_name=EXECUTION_SECTION_NAME)
            section["max_parallel_tasks"] = value
            section.pop("max_workers", None)
            return
        if parameter == "execution.scheduler_poll_interval_seconds":
            section = _get_or_create_section(document=document, section_name=EXECUTION_SECTION_NAME)
            section["scheduler_poll_interval_seconds"] = value
            return
        if parameter == "execution.heartbeat_interval_seconds":
            section = _get_or_create_section(document=document, section_name=EXECUTION_SECTION_NAME)
            section["heartbeat_interval_seconds"] = value
            return
        if parameter == "execution.lease_timeout_seconds":
            section = _get_or_create_section(document=document, section_name=EXECUTION_SECTION_NAME)
            section["lease_timeout_seconds"] = value
            return
        if parameter == "execution.progress_flush_interval_seconds":
            section = _get_or_create_section(document=document, section_name=EXECUTION_SECTION_NAME)
            section["progress_flush_interval_seconds"] = value
            return
        if parameter == "execution.max_recovery_attempts":
            section = _get_or_create_section(document=document, section_name=EXECUTION_SECTION_NAME)
            section["max_recovery_attempts"] = value
            return

        if parameter == "tools.mafft.executable":
            tools_section = _get_or_create_section(
                document=document,
                section_name=TOOLS_SECTION_NAME,
            )
            mafft_section = _get_or_create_section(
                document=tools_section,
                section_name="mafft",
            )
            mafft_section["executable"] = value
            return

        section = _get_or_create_section(document=document, section_name=LOGGING_SECTION_NAME)
        if parameter == "logging.level":
            section["level"] = value
            return
        if parameter == "logging.system_level":
            section["system_level"] = value
            return
        if parameter == "logging.task_level":
            section["task_level"] = value
            return
        if parameter == "logging.include_diagnostics":
            section["include_diagnostics"] = value
            return
        section["diagnostic_field_limit"] = value

    def _reset_parameter_in_document(
        self,
        *,
        document: dict[str, object],
        parameter: str,
    ) -> None:
        if _is_notification_parameter(parameter):
            parts = parameter.split(".")
            current: object = document
            for component in parts[:-1]:
                if not isinstance(current, dict) or component not in current:
                    raise CoreConfigParameterAlreadyUnsetError(parameter=parameter)
                current = current[component]
            if not isinstance(current, dict) or parts[-1] not in current:
                raise CoreConfigParameterAlreadyUnsetError(parameter=parameter)
            del current[parts[-1]]
            return
        default_document = build_default_core_config_document()
        current_value = _get_parameter_from_document(document=document, parameter=parameter)
        default_value = _get_parameter_from_document(
            document=default_document,
            parameter=parameter,
        )
        if current_value == default_value:
            raise CoreConfigParameterAlreadyUnsetError(parameter=parameter)
        self._set_parameter_in_document(
            document=document,
            parameter=parameter,
            value=default_value,
        )

    def _coerce_parameter_value(self, *, parameter: str, value: str) -> str | int | float | bool:
        if _is_notification_parameter(parameter):
            return _parse_bool_value(parameter=parameter, value=value)
        if parameter in {
            "input_directory_max_depth",
            "ncbi_max_retries",
        }:
            try:
                int_value = int(value)
            except ValueError as error:
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail="value must be an integer",
                ) from error
            if int_value < 0:
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail="value must be an integer >= 0",
                )
            return int_value

        if parameter == "default_alignment_mode":
            normalized_value = value.strip().lower()
            if normalized_value not in SUPPORTED_ALIGNMENT_MODES:
                allowed_values = ", ".join(SUPPORTED_ALIGNMENT_MODES)
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail=f"value must be one of: {allowed_values}",
                )
            return normalized_value

        if parameter in {
            "execution.max_parallel_tasks",
            "execution.max_recovery_attempts",
        }:
            try:
                int_value = int(value)
            except ValueError as error:
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail="value must be an integer",
                ) from error

            if parameter == "execution.max_parallel_tasks" and int_value <= 0:
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail="value must be an integer >= 1",
                )
            if parameter == "execution.max_recovery_attempts" and int_value < 0:
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail="value must be an integer >= 0",
                )
            return int_value

        if parameter in {
            "execution.scheduler_poll_interval_seconds",
            "execution.heartbeat_interval_seconds",
            "execution.lease_timeout_seconds",
            "execution.progress_flush_interval_seconds",
        }:
            try:
                float_value = float(value)
            except ValueError as error:
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail="value must be a number",
                ) from error
            if float_value <= 0:
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail="value must be > 0",
                )
            return float_value

        if parameter == "logging.include_diagnostics":
            return _parse_bool_value(parameter=parameter, value=value)

        if parameter == "logging.diagnostic_field_limit":
            try:
                parsed_value = int(value)
            except ValueError as error:
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail="value must be an integer",
                ) from error
            if parsed_value <= 0:
                raise CoreConfigInvalidValueError(
                    parameter=parameter,
                    detail="value must be a positive integer",
                )
            return parsed_value

        return value

    def _resolve_parameter_name(self, *, parameter: str) -> str:
        stripped_parameter = parameter.strip()
        resolved_parameter = _CORE_CONFIG_PARAMETER_ALIASES.get(stripped_parameter)
        if resolved_parameter is None and _is_notification_parameter(stripped_parameter):
            event_id = stripped_parameter.split(".events.", 1)[1] if ".events." in stripped_parameter else None
            if event_id is not None:
                event = load_notification_catalog().event(event_id)
                if event is None or event.scope not in {"local", "both"}:
                    raise CoreConfigUnknownParameterError(parameter=parameter)
            resolved_parameter = stripped_parameter
        if resolved_parameter is None:
            raise CoreConfigUnknownParameterError(parameter=parameter)
        return resolved_parameter

    def _ensure_runtime_directories(self, config: ResolvedCoreConfig) -> None:
        self._ensure_directory(config.data_dir)
        self._ensure_directory(config.tasks_dir)
        self._ensure_directory(config.temp_dir)
        self._ensure_directory(config.logs_dir)

    def _ensure_directory(self, directory: Path) -> None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CoreWorkingDirectoryCreationError(path=directory, detail=str(error)) from error

    def _initialize_task_registry_database(self, *, database_path: Path) -> None:
        registry = AnalyticalTaskRegistry(database_path=database_path)
        registry.ensure_schema()

    def _validate_task_registry_database(self, *, database_path: Path) -> None:
        registry = AnalyticalTaskRegistry(database_path=database_path)
        registry.validate_schema()


def _default_platform_home() -> Path:
    return Path(user_data_path(appname="JELICA", appauthor=False))


def _normalize_home_path(path: Path, *, source: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise CoreConfigPathResolutionError(
            detail=f"{source} must be an absolute path, got '{expanded}'"
        )

    try:
        return expanded.resolve(strict=False)
    except OSError as error:
        raise CoreConfigPathResolutionError(detail=str(error)) from error


def _get_or_create_section(
    *,
    document: dict[str, object],
    section_name: str,
) -> dict[str, Any]:
    section = document.get(section_name)
    if section is None:
        created_section: dict[str, Any] = {}
        document[section_name] = created_section
        return created_section
    if not isinstance(section, dict):
        raise CoreConfigPathResolutionError(detail=f"section '{section_name}' is not a TOML table")
    return section


def _get_parameter_from_document(
    *,
    document: dict[str, object],
    parameter: str,
) -> str | int | float | bool:
    current: object = document
    for component in parameter.split("."):
        if not isinstance(current, dict) or component not in current:
            raise CoreConfigParameterAlreadyUnsetError(parameter=parameter)
        current = current[component]
    if isinstance(current, (str, int, float, bool)):
        return current
    raise CoreConfigPathResolutionError(
        detail=f"parameter '{parameter}' does not identify a TOML scalar"
    )


def _parse_bool_value(*, parameter: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise CoreConfigInvalidValueError(
        parameter=parameter,
        detail="value must be a boolean (true/false)",
    )
