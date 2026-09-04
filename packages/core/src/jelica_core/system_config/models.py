# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

from jelica_contracts import load_notification_catalog
from jelica_core.config.models import AnalysisAlignmentMode

CURRENT_CORE_CONFIG_SCHEMA_VERSION: Final = 1
DEFAULT_DATA_DIRECTORY: Final = "data"
DEFAULT_MAX_PARALLEL_TASKS: Final = 1
DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS: Final = 0.25
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 1.0
DEFAULT_LEASE_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_PROGRESS_FLUSH_INTERVAL_SECONDS: Final = 1.0
DEFAULT_MAX_RECOVERY_ATTEMPTS: Final = 3
DEFAULT_INPUT_DIRECTORY_MAX_DEPTH: Final = 3
DEFAULT_NCBI_API_KEY: Final = ""
DEFAULT_NCBI_MAX_RETRIES: Final = 3
DEFAULT_ALIGNMENT_MODE: Final = AnalysisAlignmentMode.COMPUTE
SUPPORTED_ALIGNMENT_MODES: Final[tuple[str, ...]] = tuple(
    mode.value for mode in AnalysisAlignmentMode
)
DEFAULT_MAX_WORKERS: Final = DEFAULT_MAX_PARALLEL_TASKS  # legacy alias
DEFAULT_LOG_LEVEL: Final = "INFO"
DEFAULT_INCLUDE_DIAGNOSTICS: Final = False
DEFAULT_DIAGNOSTIC_FIELD_LIMIT: Final = 8_192
SUPPORTED_LOG_LEVELS: Final[tuple[str, ...]] = (
    "DEBUG",
    "INFO",
    "SUCCESS",
    "WARNING",
    "ERROR",
    "CRITICAL",
)

CONFIG_FILENAME: Final = "config.toml"
DATA_SECTION_NAME: Final = "data"
EXECUTION_SECTION_NAME: Final = "execution"
LOGGING_SECTION_NAME: Final = "logging"
TOOLS_SECTION_NAME: Final = "tools"


class CoreNotificationChannelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = True
    events: dict[str, StrictBool] = Field(default_factory=dict)

    @field_validator("events")
    @classmethod
    def _local_events_only(cls, value: dict[str, StrictBool]) -> dict[str, StrictBool]:
        catalog = load_notification_catalog()
        allowed = {event.event_id for event in catalog.active_events if event.scope in {"local", "both"}}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown or non-local notification event: {sorted(unknown)[0]}")
        return value


class CoreNotificationSoundConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = True


class CoreNotificationsConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: CoreNotificationChannelConfig | None = None
    sound: CoreNotificationSoundConfig | None = None


class CoreDesktopNotificationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_app: CoreNotificationChannelConfig | None = None


class CoreDesktopConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notifications: CoreDesktopNotificationsInput | None = None

MUTABLE_CORE_CONFIG_PARAMETERS: Final[tuple[str, ...]] = (
    "data.directory",
    "input_directory_max_depth",
    "ncbi_api_key",
    "ncbi_max_retries",
    "default_alignment_mode",
    "execution.max_parallel_tasks",
    "execution.scheduler_poll_interval_seconds",
    "execution.heartbeat_interval_seconds",
    "execution.lease_timeout_seconds",
    "execution.progress_flush_interval_seconds",
    "execution.max_recovery_attempts",
    "logging.level",
    "logging.system_level",
    "logging.task_level",
    "logging.include_diagnostics",
    "logging.diagnostic_field_limit",
    "tools.mafft.executable",
    "notifications.device.enabled",
    "desktop.notifications.in_app.enabled",
    "notifications.sound.enabled",
)
IMMUTABLE_CORE_CONFIG_PARAMETERS: Final[tuple[str, ...]] = ("schema_version",)


class CoreDataConfigInput(BaseModel):
    """Input representation of [data] section."""

    model_config = ConfigDict(extra="forbid")

    directory: StrictStr


class CoreExecutionConfigInput(BaseModel):
    """Input representation of [execution] section."""

    model_config = ConfigDict(extra="forbid")

    max_parallel_tasks: StrictInt = Field(
        validation_alias=AliasChoices("max_parallel_tasks", "max_workers")
    )
    scheduler_poll_interval_seconds: StrictFloat
    heartbeat_interval_seconds: StrictFloat
    lease_timeout_seconds: StrictFloat
    progress_flush_interval_seconds: StrictFloat
    max_recovery_attempts: StrictInt

    @field_validator(
        "scheduler_poll_interval_seconds",
        "heartbeat_interval_seconds",
        "lease_timeout_seconds",
        "progress_flush_interval_seconds",
        mode="before",
    )
    @classmethod
    def _intervals_must_be_floats(cls, value: object) -> object:
        if not isinstance(value, float):
            raise ValueError("value must be a float")
        return value

    @property
    def max_workers(self) -> int:
        """Backward-compatible read-only alias for historical callers."""

        return self.max_parallel_tasks


class CoreLoggingConfigInput(BaseModel):
    """Input representation of [logging] section."""

    model_config = ConfigDict(extra="forbid")

    level: StrictStr
    system_level: StrictStr
    task_level: StrictStr
    include_diagnostics: StrictBool
    diagnostic_field_limit: StrictInt


class CoreMafftConfigInput(BaseModel):
    """Input representation of [tools.mafft] section."""

    model_config = ConfigDict(extra="forbid")

    executable: StrictStr


class CoreToolsConfigInput(BaseModel):
    """Input representation of [tools] section."""

    model_config = ConfigDict(extra="forbid")

    mafft: CoreMafftConfigInput


class CoreConfigInput(BaseModel):
    """Strictly validated content parsed from config.toml."""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt
    input_directory_max_depth: StrictInt
    ncbi_api_key: StrictStr
    ncbi_max_retries: StrictInt
    default_alignment_mode: AnalysisAlignmentMode
    data: CoreDataConfigInput
    execution: CoreExecutionConfigInput
    logging: CoreLoggingConfigInput
    tools: CoreToolsConfigInput
    _notification_document: dict[str, object] = PrivateAttr(default_factory=dict)

    @field_validator("default_alignment_mode", mode="before")
    @classmethod
    def _alignment_mode_must_be_a_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return value


class ResolvedCoreConfig(BaseModel):
    """Resolved effective system configuration with absolute paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(gt=0)
    data_dir: Path
    tasks_dir: Path
    temp_dir: Path
    database_path: Path
    logs_dir: Path
    max_parallel_tasks: int = Field(gt=0)
    scheduler_poll_interval_seconds: float = Field(gt=0.0)
    heartbeat_interval_seconds: float = Field(gt=0.0)
    lease_timeout_seconds: float = Field(gt=0.0)
    progress_flush_interval_seconds: float = Field(gt=0.0)
    max_recovery_attempts: int = Field(ge=0)
    input_directory_max_depth: int = Field(ge=0)
    ncbi_api_key: str
    ncbi_max_retries: int = Field(ge=0)
    default_alignment_mode: AnalysisAlignmentMode
    log_level: str
    system_log_level: str
    task_log_level: str
    include_diagnostics: bool
    diagnostic_field_limit: int = Field(gt=0)
    mafft_executable: str | None = None

    @field_validator("data_dir", "tasks_dir", "temp_dir", "database_path", "logs_dir")
    @classmethod
    def _paths_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError(f"path must be absolute, got '{value}'")
        return value

    @property
    def max_workers(self) -> int:
        # Backward-compatible alias for historical tests/callers.
        return self.max_parallel_tasks


def core_config_top_level_keys() -> tuple[str, ...]:
    """Return canonical persisted top-level keys derived from the input schema."""

    return tuple(CoreConfigInput.model_fields)


def core_config_field_paths() -> tuple[str, ...]:
    """Return canonical persisted leaf paths derived from the nested input schema."""

    return _model_field_paths(CoreConfigInput)


def build_default_core_config_document(
    *,
    data_directory: str = DEFAULT_DATA_DIRECTORY,
    max_parallel_tasks: int = DEFAULT_MAX_PARALLEL_TASKS,
    log_level: str = DEFAULT_LOG_LEVEL,
) -> dict[str, object]:
    """Build the complete canonical document used when generating config.toml."""

    config = CoreConfigInput.model_validate(
        {
            "schema_version": CURRENT_CORE_CONFIG_SCHEMA_VERSION,
            "input_directory_max_depth": DEFAULT_INPUT_DIRECTORY_MAX_DEPTH,
            "ncbi_api_key": DEFAULT_NCBI_API_KEY,
            "ncbi_max_retries": DEFAULT_NCBI_MAX_RETRIES,
            "default_alignment_mode": DEFAULT_ALIGNMENT_MODE.value,
            DATA_SECTION_NAME: {"directory": data_directory},
            EXECUTION_SECTION_NAME: {
                "max_parallel_tasks": max_parallel_tasks,
                "scheduler_poll_interval_seconds": DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS,
                "heartbeat_interval_seconds": DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                "lease_timeout_seconds": DEFAULT_LEASE_TIMEOUT_SECONDS,
                "progress_flush_interval_seconds": DEFAULT_PROGRESS_FLUSH_INTERVAL_SECONDS,
                "max_recovery_attempts": DEFAULT_MAX_RECOVERY_ATTEMPTS,
            },
            LOGGING_SECTION_NAME: {
                "level": log_level,
                "system_level": "",
                "task_level": "",
                "include_diagnostics": DEFAULT_INCLUDE_DIAGNOSTICS,
                "diagnostic_field_limit": DEFAULT_DIAGNOSTIC_FIELD_LIMIT,
            },
            TOOLS_SECTION_NAME: {"mafft": {"executable": ""}},
        }
    )
    return to_toml_document(config)


def to_toml_document(config: CoreConfigInput) -> dict[str, object]:
    """Convert a complete input model to a full canonical TOML document."""

    document = config.model_dump(mode="json", by_alias=False, exclude_none=True)
    notification_document = getattr(config, "_notification_document", {})
    if notification_document:
        document.update(notification_document)
    return document


def _model_field_paths(model_type: type[BaseModel], *, prefix: str = "") -> tuple[str, ...]:
    paths: list[str] = []
    for field_name, field in model_type.model_fields.items():
        field_path = f"{prefix}.{field_name}" if prefix else field_name
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            paths.extend(_model_field_paths(annotation, prefix=field_path))
        else:
            paths.append(field_path)
    return tuple(paths)
