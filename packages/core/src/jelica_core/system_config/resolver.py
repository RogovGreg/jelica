from __future__ import annotations

from pathlib import Path

from .errors import (
    CoreConfigInvalidValueError,
    CoreConfigPathResolutionError,
    UnsupportedCoreConfigSchemaVersionError,
)
from .models import (
    CURRENT_CORE_CONFIG_SCHEMA_VERSION,
    SUPPORTED_LOG_LEVELS,
    AnalysisAlignmentMode,
    CoreConfigInput,
    ResolvedCoreConfig,
)
from .resources import detect_available_logical_cpu_count


class CoreConfigResolver:
    """Resolve effective config values and all derived absolute paths."""

    def resolve(self, *, config_input: CoreConfigInput, config_path: Path) -> ResolvedCoreConfig:
        self._validate_schema_version(config_input.schema_version)

        data_directory = self._resolve_data_directory(config_input)
        max_parallel_tasks = self._resolve_max_parallel_tasks(config_input)
        scheduler_poll_interval_seconds = self._validate_positive_float(
            parameter="execution.scheduler_poll_interval_seconds",
            value=config_input.execution.scheduler_poll_interval_seconds,
        )
        heartbeat_interval_seconds = self._validate_positive_float(
            parameter="execution.heartbeat_interval_seconds",
            value=config_input.execution.heartbeat_interval_seconds,
        )
        lease_timeout_seconds = self._validate_positive_float(
            parameter="execution.lease_timeout_seconds",
            value=config_input.execution.lease_timeout_seconds,
        )
        progress_flush_interval_seconds = self._validate_positive_float(
            parameter="execution.progress_flush_interval_seconds",
            value=config_input.execution.progress_flush_interval_seconds,
        )
        max_recovery_attempts = self._resolve_max_recovery_attempts(config_input)
        input_directory_max_depth = self._resolve_input_directory_max_depth(config_input)
        ncbi_api_key = self._resolve_ncbi_api_key(config_input)
        ncbi_max_retries = self._resolve_ncbi_max_retries(config_input)
        default_alignment_mode = self._resolve_default_alignment_mode(config_input)
        log_level = self._resolve_log_level(config_input)
        system_log_level = self._resolve_system_log_level(config_input, fallback=log_level)
        task_log_level = self._resolve_task_log_level(config_input, fallback=log_level)
        include_diagnostics = self._resolve_include_diagnostics(config_input)
        diagnostic_field_limit = self._resolve_diagnostic_field_limit(config_input)
        mafft_executable = self._resolve_mafft_executable(config_input)
        if lease_timeout_seconds <= heartbeat_interval_seconds:
            raise CoreConfigInvalidValueError(
                parameter="execution.lease_timeout_seconds",
                detail=(
                    "value must be greater than execution.heartbeat_interval_seconds "
                    f"({heartbeat_interval_seconds})"
                ),
            )

        data_dir = self._resolve_data_dir_path(
            data_directory=data_directory,
            config_path=config_path,
        )
        tasks_dir = data_dir / "tasks"
        temp_dir = data_dir / "temp"
        logs_dir = data_dir / "logs"
        database_path = data_dir / "jelica.db"

        return ResolvedCoreConfig(
            schema_version=config_input.schema_version,
            data_dir=data_dir,
            tasks_dir=tasks_dir,
            temp_dir=temp_dir,
            database_path=database_path,
            logs_dir=logs_dir,
            max_parallel_tasks=max_parallel_tasks,
            scheduler_poll_interval_seconds=scheduler_poll_interval_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            lease_timeout_seconds=lease_timeout_seconds,
            progress_flush_interval_seconds=progress_flush_interval_seconds,
            max_recovery_attempts=max_recovery_attempts,
            input_directory_max_depth=input_directory_max_depth,
            ncbi_api_key=ncbi_api_key,
            ncbi_max_retries=ncbi_max_retries,
            default_alignment_mode=default_alignment_mode,
            log_level=log_level,
            system_log_level=system_log_level,
            task_log_level=task_log_level,
            include_diagnostics=include_diagnostics,
            diagnostic_field_limit=diagnostic_field_limit,
            mafft_executable=mafft_executable,
        )

    def _validate_schema_version(self, schema_version: int) -> None:
        if schema_version != CURRENT_CORE_CONFIG_SCHEMA_VERSION:
            raise UnsupportedCoreConfigSchemaVersionError(
                schema_version=schema_version,
                supported_version=CURRENT_CORE_CONFIG_SCHEMA_VERSION,
            )

    def _resolve_data_directory(self, config_input: CoreConfigInput) -> str:
        normalized_value = config_input.data.directory.strip()
        if normalized_value == "":
            raise CoreConfigInvalidValueError(
                parameter="data.directory",
                detail="path must not be empty",
            )
        return normalized_value

    def _resolve_max_parallel_tasks(self, config_input: CoreConfigInput) -> int:
        resolved_value = config_input.execution.max_parallel_tasks

        if resolved_value < 1:
            raise CoreConfigInvalidValueError(
                parameter="execution.max_parallel_tasks",
                detail="value must be an integer >= 1",
            )
        available_cpu_count = detect_available_logical_cpu_count()
        if resolved_value > available_cpu_count:
            raise CoreConfigInvalidValueError(
                parameter="execution.max_parallel_tasks",
                detail=(
                    "value must not exceed the detected available logical CPU count "
                    f"({available_cpu_count})"
                ),
            )
        return resolved_value

    def _validate_positive_float(
        self,
        *,
        parameter: str,
        value: float,
    ) -> float:
        if value <= 0:
            raise CoreConfigInvalidValueError(
                parameter=parameter,
                detail="value must be > 0",
            )
        return value

    def _resolve_max_recovery_attempts(self, config_input: CoreConfigInput) -> int:
        resolved_value = config_input.execution.max_recovery_attempts
        if resolved_value < 0:
            raise CoreConfigInvalidValueError(
                parameter="execution.max_recovery_attempts",
                detail="value must be an integer >= 0",
            )
        return resolved_value

    def _resolve_input_directory_max_depth(self, config_input: CoreConfigInput) -> int:
        resolved_value = config_input.input_directory_max_depth
        if resolved_value < 0:
            raise CoreConfigInvalidValueError(
                parameter="input_directory_max_depth",
                detail="value must be an integer >= 0",
            )
        return resolved_value

    def _resolve_ncbi_api_key(self, config_input: CoreConfigInput) -> str:
        return config_input.ncbi_api_key.strip()

    def _resolve_ncbi_max_retries(self, config_input: CoreConfigInput) -> int:
        resolved_value = config_input.ncbi_max_retries
        if resolved_value < 0:
            raise CoreConfigInvalidValueError(
                parameter="ncbi_max_retries",
                detail="value must be an integer >= 0",
            )
        return resolved_value

    def _resolve_default_alignment_mode(
        self, config_input: CoreConfigInput
    ) -> AnalysisAlignmentMode:
        return config_input.default_alignment_mode

    def _resolve_log_level(self, config_input: CoreConfigInput) -> str:
        normalized_value = config_input.logging.level.strip().upper()
        if normalized_value not in SUPPORTED_LOG_LEVELS:
            allowed_values = ", ".join(SUPPORTED_LOG_LEVELS)
            raise CoreConfigInvalidValueError(
                parameter="logging.level",
                detail=f"expected one of: {allowed_values}",
            )
        return normalized_value

    def _resolve_system_log_level(self, config_input: CoreConfigInput, *, fallback: str) -> str:
        if config_input.logging.system_level.strip() == "":
            return fallback
        return self._normalize_log_level(
            raw_value=config_input.logging.system_level,
            parameter="logging.system_level",
        )

    def _resolve_task_log_level(self, config_input: CoreConfigInput, *, fallback: str) -> str:
        if config_input.logging.task_level.strip() == "":
            return fallback
        return self._normalize_log_level(
            raw_value=config_input.logging.task_level,
            parameter="logging.task_level",
        )

    def _resolve_include_diagnostics(self, config_input: CoreConfigInput) -> bool:
        return config_input.logging.include_diagnostics

    def _resolve_diagnostic_field_limit(self, config_input: CoreConfigInput) -> int:
        value = config_input.logging.diagnostic_field_limit
        if value <= 0:
            raise CoreConfigInvalidValueError(
                parameter="logging.diagnostic_field_limit",
                detail="value must be a positive integer",
            )
        return value

    def _resolve_mafft_executable(self, config_input: CoreConfigInput) -> str | None:
        executable = config_input.tools.mafft.executable.strip()
        if executable == "":
            return None
        return executable

    def _normalize_log_level(self, *, raw_value: str, parameter: str) -> str:
        normalized_value = raw_value.strip().upper()
        if normalized_value not in SUPPORTED_LOG_LEVELS:
            allowed_values = ", ".join(SUPPORTED_LOG_LEVELS)
            raise CoreConfigInvalidValueError(
                parameter=parameter,
                detail=f"expected one of: {allowed_values}",
            )
        return normalized_value

    def _resolve_data_dir_path(self, *, data_directory: str, config_path: Path) -> Path:
        config_home = config_path.parent
        data_dir_candidate = Path(data_directory).expanduser()
        if not data_dir_candidate.is_absolute():
            data_dir_candidate = config_home / data_dir_candidate

        try:
            return data_dir_candidate.resolve(strict=False)
        except OSError as error:
            raise CoreConfigPathResolutionError(detail=str(error)) from error
