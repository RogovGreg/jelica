from __future__ import annotations

from pathlib import Path

from .registry_models import AnalyticalTaskState


class AnalyticalTaskRegistryError(RuntimeError):
    """Base error for analytical task registry operations."""


class AnalyticalTaskRegistryDatabaseError(AnalyticalTaskRegistryError):
    def __init__(
        self,
        *,
        database_path: Path,
        detail: str,
        sqlite_exception_type: str | None = None,
    ) -> None:
        self.database_path = database_path
        self.detail = detail
        self.sqlite_exception_type = sqlite_exception_type
        super().__init__(detail)


class AnalyticalTaskRegistryDatabaseUnavailableError(AnalyticalTaskRegistryDatabaseError):
    def __init__(
        self,
        *,
        database_path: Path,
        detail: str,
        sqlite_exception_type: str | None = None,
    ) -> None:
        sqlite_type_suffix = (
            ""
            if sqlite_exception_type is None
            else f" (sqlite_exception_type={sqlite_exception_type})"
        )
        super().__init__(
            database_path=database_path,
            detail=(
                f"Task registry database is unavailable at '{database_path}': "
                f"{detail}{sqlite_type_suffix}."
            ),
            sqlite_exception_type=sqlite_exception_type,
        )


class AnalyticalTaskRegistryDatabaseCorruptedError(AnalyticalTaskRegistryDatabaseError):
    def __init__(
        self,
        *,
        database_path: Path,
        detail: str,
        sqlite_exception_type: str | None = None,
    ) -> None:
        sqlite_type_suffix = (
            ""
            if sqlite_exception_type is None
            else f" (sqlite_exception_type={sqlite_exception_type})"
        )
        super().__init__(
            database_path=database_path,
            detail=(
                f"Task registry database is corrupted at '{database_path}': "
                f"{detail}{sqlite_type_suffix}."
            ),
            sqlite_exception_type=sqlite_exception_type,
        )


class AnalyticalTaskRegistryForeignDatabaseError(AnalyticalTaskRegistryError):
    def __init__(self, *, database_path: Path, application_id: int) -> None:
        self.database_path = database_path
        self.application_id = application_id
        super().__init__(
            "Task registry database belongs to another application: "
            f"path='{database_path}', application_id={application_id}."
        )


class AnalyticalTaskRegistryUnsupportedSchemaVersionError(AnalyticalTaskRegistryError):
    def __init__(
        self,
        *,
        database_path: Path,
        schema_version: int,
        supported_version: int,
    ) -> None:
        self.database_path = database_path
        self.schema_version = schema_version
        self.supported_version = supported_version
        super().__init__(
            "Unsupported task registry schema version "
            f"{schema_version} at '{database_path}'. "
            f"Supported version is {supported_version}."
        )


class AnalyticalTaskRegistryIncompatibleSchemaError(AnalyticalTaskRegistryError):
    def __init__(self, *, database_path: Path, detail: str) -> None:
        self.database_path = database_path
        self.detail = detail
        super().__init__(f"Incompatible task registry schema at '{database_path}': {detail}.")


class AnalyticalTaskAlreadyExistsError(AnalyticalTaskRegistryError):
    def __init__(self, *, field_name: str, field_value: str) -> None:
        self.field_name = field_name
        self.field_value = field_value
        super().__init__(f"Analytical task already exists for {field_name}='{field_value}'.")


class AnalyticalTaskNotFoundError(AnalyticalTaskRegistryError):
    def __init__(self, *, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"Analytical task '{task_id}' was not found.")


class AnalyticalTaskInvalidTransitionError(AnalyticalTaskRegistryError):
    def __init__(
        self,
        *,
        task_id: str,
        from_state: AnalyticalTaskState,
        to_state: AnalyticalTaskState,
    ) -> None:
        self.task_id = task_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            "Invalid state transition for task "
            f"'{task_id}': {from_state.value} -> {to_state.value}."
        )


class AnalyticalTaskVersionConflictError(AnalyticalTaskRegistryError):
    def __init__(
        self,
        *,
        task_id: str,
        expected_version: int,
        actual_version: int | None,
    ) -> None:
        self.task_id = task_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        actual_text = "unknown" if actual_version is None else str(actual_version)
        super().__init__(
            "Optimistic concurrency conflict for task "
            f"'{task_id}': expected record_version={expected_version}, actual={actual_text}."
        )


class AnalyticalTaskStateConflictError(AnalyticalTaskRegistryError):
    def __init__(
        self,
        *,
        task_id: str,
        expected_state: AnalyticalTaskState,
        actual_state: AnalyticalTaskState,
    ) -> None:
        self.task_id = task_id
        self.expected_state = expected_state
        self.actual_state = actual_state
        super().__init__(
            f"Task '{task_id}' state changed concurrently: "
            f"expected {expected_state.value}, actual {actual_state.value}."
        )


class AnalyticalTaskInvalidRecordDataError(AnalyticalTaskRegistryError):
    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Invalid analytical task data: {detail}.")


class AnalyticalTaskRegistryMigrationError(AnalyticalTaskRegistryError):
    def __init__(self, *, database_path: Path, detail: str) -> None:
        self.database_path = database_path
        self.detail = detail
        super().__init__(f"Task registry migration failed at '{database_path}': {detail}.")


class AnalyticalTaskConfigRevisionError(AnalyticalTaskRegistryError):
    def __init__(self, *, task_id: str, detail: str) -> None:
        self.task_id = task_id
        self.detail = detail
        super().__init__(f"Task config revision operation failed for '{task_id}': {detail}.")


class AnalyticalTaskJobNotFoundError(AnalyticalTaskRegistryError):
    def __init__(self, *, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Analytical task job '{job_id}' was not found.")
