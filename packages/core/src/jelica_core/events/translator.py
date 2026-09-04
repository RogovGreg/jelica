from __future__ import annotations

import traceback
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path

from pydantic import ValidationError

from jelica_contracts import EventDefinition, EventDiagnostics, JSONObject, JSONValue, PublicError
from jelica_core.analysis.errors import (
    AnalysisTaskInitializationError,
    AnalysisTaskWorkspaceCompensationError,
)
from jelica_core.config import AnalysisConfigError
from jelica_core.system_config import (
    CoreConfigAlreadyExistsError,
    CoreConfigInvalidRootTypeError,
    CoreConfigInvalidTomlError,
    CoreConfigInvalidValueError,
    CoreConfigMissingError,
    CoreConfigParameterAlreadyUnsetError,
    CoreConfigParameterNotMutableError,
    CoreConfigParameterNotRemovableError,
    CoreConfigPathResolutionError,
    CoreConfigReadError,
    CoreConfigUnknownFieldError,
    CoreConfigUnknownParameterError,
    CoreConfigValidationError,
    CoreConfigWriteError,
    CoreNotInitializedError,
    CoreWorkingDirectoryCreationError,
    UnsupportedCoreConfigSchemaVersionError,
)
from jelica_core.tasks import (
    AnalyticalTaskAlreadyExistsError,
    AnalyticalTaskConfigRevisionError,
    AnalyticalTaskInvalidRecordDataError,
    AnalyticalTaskInvalidTransitionError,
    AnalyticalTaskJobNotFoundError,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryDatabaseCorruptedError,
    AnalyticalTaskRegistryDatabaseUnavailableError,
    AnalyticalTaskRegistryForeignDatabaseError,
    AnalyticalTaskRegistryIncompatibleSchemaError,
    AnalyticalTaskRegistryMigrationError,
    AnalyticalTaskRegistryUnsupportedSchemaVersionError,
    AnalyticalTaskStateConflictError,
    AnalyticalTaskVersionConflictError,
    TaskConfigSaveError,
    TaskDirectoryAlreadyExistsError,
    TaskDirectoryCreationError,
)

from .context import CoreExecutionContext
from .definitions import CORE_EVENT_CATALOG
from .factory import CoreEventFactory
from .structured_errors import CoreStructuredError


@dataclass(frozen=True, slots=True)
class ExceptionMapping:
    definition_name: str
    expected: bool
    retryable: bool
    can_continue: bool
    message_params: dict[str, JSONValue] | None = None
    safe_details: JSONObject | None = None
    context: JSONObject | None = None


class CoreExceptionTranslator:
    """Convert Core/internal exceptions into a stable PublicError contract."""

    def __init__(
        self,
        *,
        factory: CoreEventFactory | None = None,
        include_diagnostics: bool,
        diagnostic_field_limit: int,
    ) -> None:
        self._factory = factory or CoreEventFactory()
        self._include_diagnostics = include_diagnostics
        self._diagnostic_field_limit = diagnostic_field_limit

    def to_public_error(
        self,
        exception: Exception,
        *,
        execution_context: CoreExecutionContext | None = None,
        context: JSONObject | None = None,
    ) -> PublicError:
        mapping = self._map_exception(exception)
        diagnostics = self._build_diagnostics(exception=exception, expected=mapping.expected)
        merged_context = _merge_context(mapping.context, context)
        event = self._factory.create(
            definition=self._resolve_definition(mapping.definition_name),
            execution_context=execution_context,
            message_params=mapping.message_params,
            context=merged_context,
            diagnostics=diagnostics,
        )
        return PublicError(
            event=event,
            expected=mapping.expected,
            retryable=mapping.retryable,
            can_continue=mapping.can_continue,
            safe_details=mapping.safe_details,
        )

    def _resolve_definition(self, definition_name: str) -> EventDefinition:
        return CORE_EVENT_CATALOG.get(definition_name)

    def _map_exception(self, exception: Exception) -> ExceptionMapping:
        if isinstance(exception, CoreStructuredError):
            return ExceptionMapping(
                definition_name=exception.definition.name,
                expected=exception.expected,
                retryable=exception.retryable,
                can_continue=exception.can_continue,
                message_params=exception.message_params,
                safe_details=exception.safe_details,
                context=exception.context,
            )

        if isinstance(exception, (CoreNotInitializedError, CoreConfigMissingError)):
            return ExceptionMapping(
                definition_name="CORE_SYSTEM_CONFIG_NOT_FOUND",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={"config_path": str(exception.path)},
                safe_details={"config_path": str(exception.path)},
            )

        if isinstance(exception, CoreConfigAlreadyExistsError):
            return ExceptionMapping(
                definition_name="CORE_SYSTEM_CONFIG_ALREADY_EXISTS",
                expected=True,
                retryable=False,
                can_continue=True,
                message_params={"config_path": str(exception.path)},
                safe_details={"config_path": str(exception.path)},
            )

        if isinstance(exception, CoreConfigReadError):
            return ExceptionMapping(
                definition_name="CORE_SYSTEM_CONFIG_READ_ERROR",
                expected=True,
                retryable=True,
                can_continue=False,
                message_params={
                    "config_path": str(exception.path),
                    "detail": exception.detail,
                },
                safe_details={"config_path": str(exception.path)},
            )

        if isinstance(exception, CoreConfigWriteError):
            return ExceptionMapping(
                definition_name="CORE_SYSTEM_CONFIG_WRITE_ATOMIC_ERROR",
                expected=True,
                retryable=True,
                can_continue=False,
                message_params={
                    "config_path": str(exception.path),
                    "detail": exception.detail,
                },
                safe_details={"config_path": str(exception.path)},
            )

        if isinstance(exception, AnalyticalTaskRegistryDatabaseUnavailableError):
            safe_details: JSONObject = {"database_path": str(exception.database_path)}
            if exception.sqlite_exception_type is not None:
                safe_details["sqlite_exception_type"] = exception.sqlite_exception_type
            return ExceptionMapping(
                definition_name="CORE_TASK_REGISTRY_DATABASE_UNAVAILABLE",
                expected=True,
                retryable=True,
                can_continue=False,
                message_params={"detail": str(exception)},
                safe_details=safe_details,
            )

        if isinstance(exception, AnalyticalTaskRegistryDatabaseCorruptedError):
            safe_details = {"database_path": str(exception.database_path)}
            if exception.sqlite_exception_type is not None:
                safe_details["sqlite_exception_type"] = exception.sqlite_exception_type
            return ExceptionMapping(
                definition_name="CORE_TASK_REGISTRY_DATABASE_CORRUPTED",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={"detail": str(exception)},
                safe_details=safe_details,
            )

        if isinstance(exception, AnalyticalTaskRegistryForeignDatabaseError):
            return ExceptionMapping(
                definition_name="CORE_TASK_REGISTRY_FOREIGN_DATABASE",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={
                    "database_path": str(exception.database_path),
                    "application_id": exception.application_id,
                },
                safe_details={"database_path": str(exception.database_path)},
            )

        if isinstance(exception, AnalyticalTaskRegistryUnsupportedSchemaVersionError):
            return ExceptionMapping(
                definition_name="CORE_TASK_REGISTRY_SCHEMA_VERSION_UNSUPPORTED",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={
                    "schema_version": exception.schema_version,
                    "supported_version": exception.supported_version,
                },
                safe_details={"database_path": str(exception.database_path)},
            )

        if isinstance(exception, AnalyticalTaskRegistryIncompatibleSchemaError):
            return ExceptionMapping(
                definition_name="CORE_TASK_REGISTRY_SCHEMA_INCOMPATIBLE",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={"detail": str(exception)},
                safe_details={"database_path": str(exception.database_path)},
            )

        if isinstance(exception, AnalyticalTaskRegistryMigrationError):
            return ExceptionMapping(
                definition_name="CORE_TASK_REGISTRY_MIGRATION_FAILED",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={"detail": str(exception)},
                safe_details={"database_path": str(exception.database_path)},
            )

        if isinstance(exception, AnalyticalTaskConfigRevisionError):
            return ExceptionMapping(
                definition_name="CORE_TASK_CONFIG_COMPENSATION_FAILED",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={
                    "task_id": exception.task_id,
                    "detail": exception.detail,
                },
                safe_details={"task_id": exception.task_id},
            )

        if isinstance(exception, AnalysisTaskWorkspaceCompensationError):
            compensation_safe_details: JSONObject = {
                "task_id": exception.task_id,
                "task_dir": str(exception.task_dir),
                "original_exception_type": exception.original_exception_type,
                "original_message": exception.original_message,
                "cleanup_exception_type": exception.cleanup_exception_type,
                "cleanup_message": exception.cleanup_message,
            }
            return ExceptionMapping(
                definition_name="CORE_ANALYZE_TASK_WORKSPACE_COMPENSATION_FAILED",
                expected=True,
                retryable=False,
                can_continue=True,
                message_params={
                    "task_dir": str(exception.task_dir),
                    "detail": exception.cleanup_message,
                },
                safe_details=compensation_safe_details,
                context=compensation_safe_details,
            )

        if isinstance(exception, AnalyticalTaskNotFoundError):
            return ExceptionMapping(
                definition_name="CORE_ANALYTICAL_TASK_NOT_FOUND",
                expected=True,
                retryable=False,
                can_continue=True,
                message_params={"task_id": exception.task_id},
                safe_details={"task_id": exception.task_id},
            )

        if isinstance(exception, AnalyticalTaskJobNotFoundError):
            return ExceptionMapping(
                definition_name="CORE_ANALYTICAL_TASK_NOT_FOUND",
                expected=True,
                retryable=False,
                can_continue=True,
                message_params={"task_id": exception.job_id},
                safe_details={"job_id": exception.job_id},
            )

        if isinstance(exception, AnalyticalTaskAlreadyExistsError):
            return ExceptionMapping(
                definition_name="CORE_ANALYTICAL_TASK_ALREADY_EXISTS",
                expected=True,
                retryable=False,
                can_continue=True,
                message_params={"detail": str(exception)},
                safe_details={
                    "field_name": exception.field_name,
                    "field_value": exception.field_value,
                },
            )

        if isinstance(
            exception,
            (
                AnalyticalTaskInvalidRecordDataError,
                AnalyticalTaskInvalidTransitionError,
                AnalyticalTaskVersionConflictError,
                AnalyticalTaskStateConflictError,
            ),
        ):
            return ExceptionMapping(
                definition_name="CORE_ANALYTICAL_TASK_REQUEST_INVALID",
                expected=True,
                retryable=False,
                can_continue=True,
                message_params={"detail": str(exception)},
            )

        if isinstance(
            exception,
            (
                CoreConfigInvalidTomlError,
                CoreConfigInvalidRootTypeError,
                CoreConfigValidationError,
                CoreConfigUnknownFieldError,
                CoreConfigInvalidValueError,
                UnsupportedCoreConfigSchemaVersionError,
                CoreConfigPathResolutionError,
                CoreConfigUnknownParameterError,
                CoreConfigParameterNotMutableError,
                CoreConfigParameterNotRemovableError,
                CoreConfigParameterAlreadyUnsetError,
                CoreWorkingDirectoryCreationError,
            ),
        ):
            return ExceptionMapping(
                definition_name="CORE_SYSTEM_CONFIG_INVALID",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={"detail": str(exception)},
            )

        if (
            isinstance(exception, AnalysisTaskInitializationError)
            and exception.__cause__ is not None
        ):
            cause = exception.__cause__
            if isinstance(cause, Exception):
                return self._map_exception(cause)

        if isinstance(exception, (AnalysisConfigError, JSONDecodeError, ValidationError)):
            return ExceptionMapping(
                definition_name="CORE_ANALYZE_TASK_CONFIG_INVALID",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={"detail": str(exception)},
            )

        if isinstance(exception, (TaskDirectoryCreationError, TaskDirectoryAlreadyExistsError)):
            return ExceptionMapping(
                definition_name="CORE_ANALYZE_TASK_DIRECTORY_CREATE_FAILED",
                expected=True,
                retryable=True,
                can_continue=False,
                message_params={"detail": str(exception)},
            )

        if isinstance(exception, TaskConfigSaveError):
            return ExceptionMapping(
                definition_name="CORE_ANALYZE_TASK_CONFIG_SAVE_FAILED",
                expected=True,
                retryable=True,
                can_continue=False,
                message_params={"detail": str(exception)},
            )

        if isinstance(exception, FileNotFoundError):
            source = _error_path(exception) or "<unknown>"
            return ExceptionMapping(
                definition_name="CORE_ANALYZE_SOURCE_NOT_FOUND",
                expected=True,
                retryable=False,
                can_continue=False,
                message_params={"source": source},
                safe_details={"source": source},
            )

        if isinstance(exception, PermissionError):
            source = _error_path(exception) or "<unknown>"
            return ExceptionMapping(
                definition_name="CORE_ANALYZE_SOURCE_UNAVAILABLE",
                expected=True,
                retryable=True,
                can_continue=False,
                message_params={"source": source},
                safe_details={"source": source},
            )

        if isinstance(exception, OSError):
            source = _error_path(exception) or "<unknown>"
            return ExceptionMapping(
                definition_name="CORE_ANALYZE_SOURCE_UNAVAILABLE",
                expected=True,
                retryable=True,
                can_continue=False,
                message_params={"source": source},
                safe_details={"source": source},
            )

        return ExceptionMapping(
            definition_name="CORE_INTERNAL_UNEXPECTED_ERROR",
            expected=False,
            retryable=False,
            can_continue=False,
            safe_details={"exception_type": type(exception).__name__},
        )

    def _build_diagnostics(
        self,
        *,
        exception: Exception,
        expected: bool,
    ) -> EventDiagnostics | None:
        if expected and not self._include_diagnostics:
            return None

        traceback_text: str | None = None
        if exception.__traceback__ is not None:
            traceback_text = "".join(
                traceback.format_exception(type(exception), exception, exception.__traceback__)
            )

        stdout_text = _normalize_text(getattr(exception, "stdout", None))
        stderr_text = _normalize_text(getattr(exception, "stderr", None))

        return EventDiagnostics(
            diagnostic_message=_truncate_text(str(exception), self._diagnostic_field_limit),
            source_exception_type=type(exception).__name__,
            traceback=_truncate_text(traceback_text, self._diagnostic_field_limit),
            stdout=_truncate_text(stdout_text, self._diagnostic_field_limit),
            stderr=_truncate_text(stderr_text, self._diagnostic_field_limit),
        )


def _truncate_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated>"


def _error_path(error: OSError) -> str | None:
    if error.filename is None:
        return None
    return str(Path(error.filename))


def _merge_context(
    left: JSONObject | None,
    right: JSONObject | None,
) -> JSONObject | None:
    merged: dict[str, JSONValue] = {}
    if left is not None:
        merged.update(left)
    if right is not None:
        merged.update(right)
    return merged or None


def _normalize_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)
