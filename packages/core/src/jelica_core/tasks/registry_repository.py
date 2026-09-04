from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from .names import normalize_task_reference, validate_task_name
from .registry_errors import (
    AnalyticalTaskAlreadyExistsError,
    AnalyticalTaskConfigRevisionError,
    AnalyticalTaskInvalidRecordDataError,
    AnalyticalTaskJobNotFoundError,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryDatabaseCorruptedError,
    AnalyticalTaskRegistryDatabaseUnavailableError,
    AnalyticalTaskRegistryForeignDatabaseError,
    AnalyticalTaskRegistryIncompatibleSchemaError,
    AnalyticalTaskRegistryMigrationError,
    AnalyticalTaskRegistryUnsupportedSchemaVersionError,
    AnalyticalTaskVersionConflictError,
)
from .registry_models import (
    TERMINAL_ANALYTICAL_TASK_JOB_STATES,
    AnalyticalTaskJobRecord,
    AnalyticalTaskMutationResult,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskRecord,
    AnalyticalTaskSnapshot,
    AnalyticalTaskSortOrder,
    AnalyticalTaskState,
    ExecutionRuntimeLeaseRecord,
    is_terminal_state,
)
from .registry_schema import (
    MIGRATIONS,
    TASK_JOB_REGISTRY_EXPECTED_COLUMNS,
    TASK_JOB_REGISTRY_TABLE_NAME,
    TASK_REGISTRY_APPLICATION_ID,
    TASK_REGISTRY_EXPECTED_COLUMNS,
    TASK_REGISTRY_EXPECTED_INDEXES,
    TASK_REGISTRY_SCHEMA_VERSION,
    TASK_REGISTRY_TABLE_NAME,
    TASK_RUNTIME_LEASE_REGISTRY_EXPECTED_COLUMNS,
    TASK_RUNTIME_LEASE_REGISTRY_TABLE_NAME,
)
from .state_machine import (
    AnalyticalTaskLifecycleIntent,
    evaluate_job_intent,
    is_reprioritize_allowed,
    is_runtime_transition_allowed,
)
from .storage import (
    TASK_CONFIG_FILENAME,
    compute_config_hash,
    write_task_config_revision,
    write_text_atomically,
)
from .timestamps import parse_utc_datetime, serialize_utc_datetime, utc_now

_BUSY_TIMEOUT_MS = 5_000
_CORRUPTION_MARKERS = (
    "file is not a database",
    "database disk image is malformed",
    "database corruption",
)

_LIVE_WORKER_DELETION_STATES = frozenset(
    {
        AnalyticalTaskState.RUNNING,
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
        AnalyticalTaskState.CANCEL_REQUESTED,
    }
)


def _validate_immutable_trace_id(
    *,
    current_config_document: Mapping[str, object],
    next_config_document: Mapping[str, object],
) -> None:
    current_value = current_config_document.get("trace_id")
    if current_value is None:
        return
    next_value = next_config_document.get("trace_id")
    if next_value is None:
        raise AnalyticalTaskInvalidRecordDataError(
            detail="config trace_id is immutable and must not be removed"
        )
    try:
        current_trace_id = UUID(str(current_value))
        next_trace_id = UUID(str(next_value))
    except ValueError as error:
        raise AnalyticalTaskInvalidRecordDataError(
            detail="config trace_id must be a valid UUID"
        ) from error
    if next_trace_id != current_trace_id:
        raise AnalyticalTaskInvalidRecordDataError(
            detail="config trace_id is immutable and must not be changed"
        )


class AnalyticalTaskRegistry:
    """SQLite repository for analytical task and job persistent state."""

    def __init__(
        self,
        *,
        database_path: Path,
    ) -> None:
        self._database_path = database_path

    @property
    def database_path(self) -> Path:
        return self._database_path

    def ensure_schema(self) -> None:
        self._ensure_database_parent_directory()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                self._ensure_schema_in_transaction(connection)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
        finally:
            connection.close()

    def validate_schema(self) -> None:
        if not self._database_path.exists():
            raise AnalyticalTaskRegistryDatabaseUnavailableError(
                database_path=self._database_path,
                detail="database file does not exist",
            )

        connection = self._open_connection()
        try:
            application_id = self._read_pragma_int(connection, "application_id")
            if application_id != TASK_REGISTRY_APPLICATION_ID:
                if application_id == 0:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="task registry database is not initialized",
                    )
                raise AnalyticalTaskRegistryForeignDatabaseError(
                    database_path=self._database_path,
                    application_id=application_id,
                )

            schema_version = self._read_pragma_int(connection, "user_version")
            if schema_version != TASK_REGISTRY_SCHEMA_VERSION:
                raise AnalyticalTaskRegistryUnsupportedSchemaVersionError(
                    database_path=self._database_path,
                    schema_version=schema_version,
                    supported_version=TASK_REGISTRY_SCHEMA_VERSION,
                )

            self._validate_schema_layout(connection)
            self._run_quick_check(connection)
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def insert(self, *, record: AnalyticalTaskRecord) -> AnalyticalTaskRecord:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                connection.execute(
                    f"""
                    INSERT INTO {TASK_REGISTRY_TABLE_NAME} (
                        task_id,
                        state,
                        default_priority,
                        current_config_revision,
                        current_config_relative_path,
                        current_config_hash,
                        active_job_id,
                        latest_job_id,
                        created_at,
                        updated_at,
                        task_dir_relative_path,
                        record_version,
                        name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.task_id,
                        record.state.value,
                        record.default_priority,
                        record.current_config_revision,
                        record.current_config_relative_path,
                        record.current_config_hash,
                        record.active_job_id,
                        record.latest_job_id,
                        serialize_utc_datetime(record.created_at),
                        serialize_utc_datetime(record.updated_at),
                        record.task_dir_relative_path,
                        record.record_version,
                        record.name,
                    ),
                )
                inserted_row = self._select_task_row(connection, task_id=record.task_id)
                if inserted_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="inserted task record cannot be reloaded",
                    )
            except sqlite3.IntegrityError as error:
                self._rollback(connection)
                raise self._translate_task_integrity_error(
                    error,
                    task_id=record.task_id,
                    task_dir_relative_path=record.task_dir_relative_path,
                    name=record.name,
                ) from error
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return self._row_to_task_record(inserted_row)
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def acquire_execution_runtime_lease(
        self,
        *,
        runtime_instance_id: str,
        owner_pid: int,
        lease_token: str,
        lease_timeout_seconds: float,
        now: datetime | None = None,
    ) -> tuple[ExecutionRuntimeLeaseRecord | None, ExecutionRuntimeLeaseRecord | None]:
        normalized_runtime_instance_id = _normalize_required_text(
            runtime_instance_id, field_name="runtime_instance_id"
        )
        normalized_lease_token = _normalize_required_text(lease_token, field_name="lease_token")
        if owner_pid <= 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="owner_pid must be > 0")
        if lease_timeout_seconds <= 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="lease_timeout_seconds must be > 0")

        effective_now = now or utc_now()
        expires_at = effective_now + timedelta(seconds=lease_timeout_seconds)
        effective_now_serialized = serialize_utc_datetime(effective_now)
        expires_at_serialized = serialize_utc_datetime(expires_at)

        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                lease_row = self._select_runtime_lease_row(connection)
                if lease_row is None:
                    connection.execute(
                        f"""
                        INSERT INTO {TASK_RUNTIME_LEASE_REGISTRY_TABLE_NAME} (
                            singleton_id,
                            runtime_instance_id,
                            owner_pid,
                            lease_token,
                            acquired_at,
                            heartbeat_at,
                            lease_expires_at,
                            record_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            1,
                            normalized_runtime_instance_id,
                            owner_pid,
                            normalized_lease_token,
                            effective_now_serialized,
                            effective_now_serialized,
                            expires_at_serialized,
                            1,
                        ),
                    )
                else:
                    current_lease = self._row_to_runtime_lease_record(lease_row)
                    if current_lease.lease_expires_at > effective_now:
                        self._rollback(connection)
                        return None, current_lease
                    update_result = connection.execute(
                        f"""
                        UPDATE {TASK_RUNTIME_LEASE_REGISTRY_TABLE_NAME}
                        SET
                            runtime_instance_id = ?,
                            owner_pid = ?,
                            lease_token = ?,
                            acquired_at = ?,
                            heartbeat_at = ?,
                            lease_expires_at = ?,
                            record_version = record_version + 1
                        WHERE singleton_id = 1 AND record_version = ?
                        """,
                        (
                            normalized_runtime_instance_id,
                            owner_pid,
                            normalized_lease_token,
                            effective_now_serialized,
                            effective_now_serialized,
                            expires_at_serialized,
                            current_lease.record_version,
                        ),
                    )
                    if update_result.rowcount == 0:
                        latest_row = self._select_runtime_lease_row(connection)
                        self._rollback(connection)
                        if latest_row is None:
                            return None, None
                        return None, self._row_to_runtime_lease_record(latest_row)

                acquired_row = self._select_runtime_lease_row(connection)
                if acquired_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="runtime lease row cannot be reloaded",
                    )
                acquired_lease = self._row_to_runtime_lease_record(acquired_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return acquired_lease, None
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def heartbeat_execution_runtime_lease(
        self,
        *,
        runtime_instance_id: str,
        lease_token: str,
        lease_timeout_seconds: float,
        now: datetime | None = None,
    ) -> ExecutionRuntimeLeaseRecord | None:
        normalized_runtime_instance_id = _normalize_required_text(
            runtime_instance_id, field_name="runtime_instance_id"
        )
        normalized_lease_token = _normalize_required_text(lease_token, field_name="lease_token")
        if lease_timeout_seconds <= 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="lease_timeout_seconds must be > 0")

        effective_now = now or utc_now()
        expires_at = effective_now + timedelta(seconds=lease_timeout_seconds)

        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                update_result = connection.execute(
                    f"""
                    UPDATE {TASK_RUNTIME_LEASE_REGISTRY_TABLE_NAME}
                    SET
                        heartbeat_at = ?,
                        lease_expires_at = ?,
                        record_version = record_version + 1
                    WHERE singleton_id = 1
                      AND runtime_instance_id = ?
                      AND lease_token = ?
                    """,
                    (
                        serialize_utc_datetime(effective_now),
                        serialize_utc_datetime(expires_at),
                        normalized_runtime_instance_id,
                        normalized_lease_token,
                    ),
                )
                if update_result.rowcount == 0:
                    self._rollback(connection)
                    return None
                updated_row = self._select_runtime_lease_row(connection)
                if updated_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="runtime lease row cannot be reloaded after heartbeat",
                    )
                updated_lease = self._row_to_runtime_lease_record(updated_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return updated_lease
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def release_execution_runtime_lease(
        self,
        *,
        runtime_instance_id: str,
        lease_token: str,
    ) -> bool:
        normalized_runtime_instance_id = _normalize_required_text(
            runtime_instance_id, field_name="runtime_instance_id"
        )
        normalized_lease_token = _normalize_required_text(lease_token, field_name="lease_token")
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                delete_result = connection.execute(
                    f"""
                    DELETE FROM {TASK_RUNTIME_LEASE_REGISTRY_TABLE_NAME}
                    WHERE singleton_id = 1
                      AND runtime_instance_id = ?
                      AND lease_token = ?
                    """,
                    (normalized_runtime_instance_id, normalized_lease_token),
                )
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return delete_result.rowcount > 0
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def get_execution_runtime_lease(self) -> ExecutionRuntimeLeaseRecord | None:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            row = self._select_runtime_lease_row(connection)
            if row is None:
                return None
            return self._row_to_runtime_lease_record(row)
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def count_running_jobs(self) -> int:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            row = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM {TASK_JOB_REGISTRY_TABLE_NAME}
                WHERE state = ?
                """,
                (AnalyticalTaskState.RUNNING.value,),
            ).fetchone()
            if row is None:
                return 0
            return int(row[0])
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def count_queued_jobs(self) -> int:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            row = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM {TASK_JOB_REGISTRY_TABLE_NAME}
                WHERE state = ?
                """,
                (AnalyticalTaskState.QUEUED.value,),
            ).fetchone()
            if row is None:
                return 0
            return int(row[0])
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def claim_next_queued_job_for_worker(
        self,
        *,
        worker_instance_id: str,
        lease_token: str,
        lease_timeout_seconds: float,
        now: datetime | None = None,
    ) -> tuple[AnalyticalTaskRecord, AnalyticalTaskJobRecord] | None:
        normalized_worker_instance_id = _normalize_required_text(
            worker_instance_id, field_name="worker_instance_id"
        )
        normalized_lease_token = _normalize_required_text(lease_token, field_name="lease_token")
        if lease_timeout_seconds <= 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="lease_timeout_seconds must be > 0")

        effective_now = now or utc_now()
        effective_now_serialized = serialize_utc_datetime(effective_now)
        expires_at_serialized = serialize_utc_datetime(
            effective_now + timedelta(seconds=lease_timeout_seconds)
        )

        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                candidate_row = connection.execute(
                    f"""
                    SELECT j.*
                    FROM {TASK_JOB_REGISTRY_TABLE_NAME} j
                    JOIN {TASK_REGISTRY_TABLE_NAME} t ON t.task_id = j.task_id
                    WHERE j.state = ?
                      AND t.active_job_id = j.job_id
                    ORDER BY j.priority DESC, j.queued_at ASC, j.created_at ASC, j.job_id ASC
                    LIMIT 1
                    """,
                    (AnalyticalTaskState.QUEUED.value,),
                ).fetchone()
                if candidate_row is None:
                    self._rollback(connection)
                    return None
                candidate_job = self._row_to_job_record(candidate_row)
                candidate_task_row = self._select_task_row(
                    connection,
                    task_id=candidate_job.task_id,
                )
                if candidate_task_row is None:
                    self._rollback(connection)
                    return None
                candidate_task = self._row_to_task_record(candidate_task_row)

                job_update_result = connection.execute(
                    f"""
                    UPDATE {TASK_JOB_REGISTRY_TABLE_NAME}
                    SET
                        state = ?,
                        first_started_at = COALESCE(first_started_at, ?),
                        last_started_at = ?,
                        worker_instance_id = ?,
                        worker_pid = ?,
                        lease_token = ?,
                        heartbeat_at = ?,
                        lease_expires_at = ?,
                        record_version = record_version + 1
                    WHERE job_id = ?
                      AND state = ?
                      AND record_version = ?
                    """,
                    (
                        AnalyticalTaskState.RUNNING.value,
                        effective_now_serialized,
                        effective_now_serialized,
                        normalized_worker_instance_id,
                        None,
                        normalized_lease_token,
                        effective_now_serialized,
                        expires_at_serialized,
                        candidate_job.job_id,
                        AnalyticalTaskState.QUEUED.value,
                        candidate_job.record_version,
                    ),
                )
                if job_update_result.rowcount == 0:
                    self._rollback(connection)
                    return None

                task_update_result = connection.execute(
                    f"""
                    UPDATE {TASK_REGISTRY_TABLE_NAME}
                    SET
                        state = ?,
                        updated_at = ?,
                        record_version = record_version + 1
                    WHERE task_id = ?
                      AND active_job_id = ?
                      AND record_version = ?
                    """,
                    (
                        AnalyticalTaskState.RUNNING.value,
                        effective_now_serialized,
                        candidate_task.task_id,
                        candidate_job.job_id,
                        candidate_task.record_version,
                    ),
                )
                if task_update_result.rowcount == 0:
                    self._rollback(connection)
                    return None

                updated_task_row = self._select_task_row(connection, task_id=candidate_task.task_id)
                updated_job_row = self._select_job_row(connection, job_id=candidate_job.job_id)
                if updated_task_row is None or updated_job_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="claimed records cannot be reloaded",
                    )
                claimed_task = self._row_to_task_record(updated_task_row)
                claimed_job = self._row_to_job_record(updated_job_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return claimed_task, claimed_job
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def attach_worker_pid(
        self,
        *,
        job_id: str,
        worker_instance_id: str,
        lease_token: str,
        worker_pid: int,
    ) -> AnalyticalTaskJobRecord | None:
        normalized_job_id = _normalize_required_text(job_id, field_name="job_id")
        normalized_worker_instance_id = _normalize_required_text(
            worker_instance_id, field_name="worker_instance_id"
        )
        normalized_lease_token = _normalize_required_text(lease_token, field_name="lease_token")
        if worker_pid <= 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="worker_pid must be > 0")

        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                update_result = connection.execute(
                    f"""
                    UPDATE {TASK_JOB_REGISTRY_TABLE_NAME}
                    SET worker_pid = ?, record_version = record_version + 1
                    WHERE job_id = ?
                      AND worker_instance_id = ?
                      AND lease_token = ?
                      AND state = ?
                    """,
                    (
                        worker_pid,
                        normalized_job_id,
                        normalized_worker_instance_id,
                        normalized_lease_token,
                        AnalyticalTaskState.RUNNING.value,
                    ),
                )
                if update_result.rowcount == 0:
                    self._rollback(connection)
                    return None
                updated_row = self._select_job_row(connection, job_id=normalized_job_id)
                if updated_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="worker-pid-updated job cannot be reloaded",
                    )
                updated_job = self._row_to_job_record(updated_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return updated_job
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def heartbeat_job_worker(
        self,
        *,
        job_id: str,
        worker_instance_id: str,
        lease_token: str,
        lease_timeout_seconds: float,
        now: datetime | None = None,
    ) -> AnalyticalTaskJobRecord | None:
        normalized_job_id = _normalize_required_text(job_id, field_name="job_id")
        normalized_worker_instance_id = _normalize_required_text(
            worker_instance_id, field_name="worker_instance_id"
        )
        normalized_lease_token = _normalize_required_text(lease_token, field_name="lease_token")
        if lease_timeout_seconds <= 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="lease_timeout_seconds must be > 0")

        effective_now = now or utc_now()
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                update_result = connection.execute(
                    f"""
                    UPDATE {TASK_JOB_REGISTRY_TABLE_NAME}
                    SET
                        heartbeat_at = ?,
                        lease_expires_at = ?,
                        record_version = record_version + 1
                    WHERE job_id = ?
                      AND worker_instance_id = ?
                      AND lease_token = ?
                      AND state = ?
                    """,
                    (
                        serialize_utc_datetime(effective_now),
                        serialize_utc_datetime(
                            effective_now + timedelta(seconds=lease_timeout_seconds)
                        ),
                        normalized_job_id,
                        normalized_worker_instance_id,
                        normalized_lease_token,
                        AnalyticalTaskState.RUNNING.value,
                    ),
                )
                if update_result.rowcount == 0:
                    self._rollback(connection)
                    return None
                updated_row = self._select_job_row(connection, job_id=normalized_job_id)
                if updated_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="heartbeat-updated job cannot be reloaded",
                    )
                updated_job = self._row_to_job_record(updated_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return updated_job
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def clear_job_worker_lease(
        self,
        *,
        job_id: str,
        worker_instance_id: str,
        lease_token: str,
    ) -> AnalyticalTaskJobRecord | None:
        normalized_job_id = _normalize_required_text(job_id, field_name="job_id")
        normalized_worker_instance_id = _normalize_required_text(
            worker_instance_id, field_name="worker_instance_id"
        )
        normalized_lease_token = _normalize_required_text(lease_token, field_name="lease_token")
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                update_result = connection.execute(
                    f"""
                    UPDATE {TASK_JOB_REGISTRY_TABLE_NAME}
                    SET
                        worker_instance_id = NULL,
                        worker_pid = NULL,
                        lease_token = NULL,
                        heartbeat_at = NULL,
                        lease_expires_at = NULL,
                        record_version = record_version + 1
                    WHERE job_id = ?
                      AND worker_instance_id = ?
                      AND lease_token = ?
                    """,
                    (
                        normalized_job_id,
                        normalized_worker_instance_id,
                        normalized_lease_token,
                    ),
                )
                if update_result.rowcount == 0:
                    self._rollback(connection)
                    return None
                updated_row = self._select_job_row(connection, job_id=normalized_job_id)
                if updated_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="lease-cleared job cannot be reloaded",
                    )
                updated_job = self._row_to_job_record(updated_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return updated_job
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def get(self, *, task_id: str) -> AnalyticalTaskRecord:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            row = self._select_task_row(connection, task_id=task_id)
            if row is None:
                raise AnalyticalTaskNotFoundError(task_id=task_id)
            return self._row_to_task_record(row)
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def get_by_name(self, *, name: str) -> AnalyticalTaskRecord:
        try:
            normalized_name = validate_task_name(name)
        except ValueError as error:
            raise AnalyticalTaskInvalidRecordDataError(detail=str(error)) from error

        self.ensure_schema()
        connection = self._open_connection()
        try:
            row = connection.execute(
                f"""
                SELECT *
                FROM {TASK_REGISTRY_TABLE_NAME}
                WHERE name = ? COLLATE NOCASE
                """,
                (normalized_name,),
            ).fetchone()
            if row is None:
                raise AnalyticalTaskNotFoundError(task_id=normalized_name)
            return self._row_to_task_record(row)
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def resolve_task_reference(self, *, task_reference: str) -> AnalyticalTaskRecord:
        try:
            normalized_reference, is_uuid = normalize_task_reference(task_reference)
        except ValueError as error:
            raise AnalyticalTaskInvalidRecordDataError(detail=str(error)) from error
        if is_uuid:
            return self.get(task_id=normalized_reference)
        try:
            return self.get_by_name(name=normalized_reference)
        except AnalyticalTaskNotFoundError:
            # Registry versions before task names did not require UUID-shaped IDs.
            # Prefer a human name, then preserve lookup for those legacy records.
            return self.get(task_id=normalized_reference)

    def get_job(self, *, job_id: str) -> AnalyticalTaskJobRecord:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            row = self._select_job_row(connection, job_id=job_id)
            if row is None:
                raise AnalyticalTaskJobNotFoundError(job_id=job_id)
            return self._row_to_job_record(row)
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def get_task_snapshot(self, *, task_id: str) -> AnalyticalTaskSnapshot:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            task_row = self._select_task_row(connection, task_id=task_id)
            if task_row is None:
                raise AnalyticalTaskNotFoundError(task_id=task_id)
            task_record = self._row_to_task_record(task_row)
            job_record = self._load_active_or_latest_job(connection, task_record=task_record)
            return AnalyticalTaskSnapshot(task=task_record, active_or_latest_job=job_record)
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def exists(self, *, task_id: str) -> bool:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            row = connection.execute(
                f"SELECT 1 FROM {TASK_REGISTRY_TABLE_NAME} WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return row is not None
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def list(
        self,
        *,
        states: Sequence[AnalyticalTaskState] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: AnalyticalTaskSortOrder = (
            AnalyticalTaskSortOrder.DEFAULT_PRIORITY_DESC_CREATED_AT_ASC
        ),
    ) -> list[AnalyticalTaskRecord]:
        self.ensure_schema()
        if limit is not None and limit <= 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="limit must be > 0")
        if offset < 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="offset must be >= 0")

        params: list[object] = []
        query = f"SELECT * FROM {TASK_REGISTRY_TABLE_NAME}"
        if states is not None and len(states) > 0:
            placeholders = ", ".join("?" for _ in states)
            query += f" WHERE state IN ({placeholders})"
            params.extend(state.value for state in states)
        query += f" ORDER BY {self._build_order_clause(order)}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
            if offset > 0:
                query += " OFFSET ?"
                params.append(offset)
        elif offset > 0:
            query += " LIMIT -1 OFFSET ?"
            params.append(offset)

        connection = self._open_connection()
        try:
            rows = connection.execute(query, params).fetchall()
            return [self._row_to_task_record(row) for row in rows]
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def list_task_snapshots(
        self,
        *,
        states: Sequence[AnalyticalTaskState] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: AnalyticalTaskSortOrder = (
            AnalyticalTaskSortOrder.DEFAULT_PRIORITY_DESC_CREATED_AT_ASC
        ),
    ) -> Sequence[AnalyticalTaskSnapshot]:
        tasks = self.list(states=states, limit=limit, offset=offset, order=order)
        if len(tasks) == 0:
            return []
        connection = self._open_connection()
        try:
            job_ids = [
                task.active_job_id if task.active_job_id is not None else task.latest_job_id
                for task in tasks
            ]
            non_null_job_ids = [job_id for job_id in job_ids if job_id is not None]
            jobs_by_id: dict[str, AnalyticalTaskJobRecord] = {}
            if len(non_null_job_ids) > 0:
                placeholders = ", ".join("?" for _ in non_null_job_ids)
                job_rows = connection.execute(
                    (
                        f"SELECT * FROM {TASK_JOB_REGISTRY_TABLE_NAME} "
                        f"WHERE job_id IN ({placeholders})"
                    ),
                    non_null_job_ids,
                ).fetchall()
                jobs_by_id = {str(row["job_id"]): self._row_to_job_record(row) for row in job_rows}
            snapshots: list[AnalyticalTaskSnapshot] = []
            for task in tasks:
                chosen_job_id = (
                    task.active_job_id if task.active_job_id is not None else task.latest_job_id
                )
                chosen_job = jobs_by_id.get(chosen_job_id) if chosen_job_id is not None else None
                snapshots.append(AnalyticalTaskSnapshot(task=task, active_or_latest_job=chosen_job))
            return snapshots
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def list_jobs(
        self,
        *,
        task_id: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[AnalyticalTaskJobRecord]:
        self.ensure_schema()
        if limit is not None and limit <= 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="limit must be > 0")
        if offset < 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="offset must be >= 0")

        params: list[object] = [task_id]
        query = (
            f"SELECT * FROM {TASK_JOB_REGISTRY_TABLE_NAME} "
            "WHERE task_id = ? ORDER BY created_at DESC, job_id ASC"
        )
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
            if offset > 0:
                query += " OFFSET ?"
                params.append(offset)
        elif offset > 0:
            query += " LIMIT -1 OFFSET ?"
            params.append(offset)

        connection = self._open_connection()
        try:
            rows = connection.execute(query, params).fetchall()
            return [self._row_to_job_record(row) for row in rows]
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def update_default_priority(
        self,
        *,
        task_id: str,
        default_priority: int,
        expected_version: int,
    ) -> AnalyticalTaskRecord:
        if default_priority < 1:
            raise AnalyticalTaskInvalidRecordDataError(detail="default_priority must be >= 1")
        if expected_version < 1:
            raise AnalyticalTaskInvalidRecordDataError(detail="expected_version must be >= 1")

        self.ensure_schema()
        updated_at = serialize_utc_datetime(utc_now())
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                result = connection.execute(
                    f"""
                    UPDATE {TASK_REGISTRY_TABLE_NAME}
                    SET
                        default_priority = ?,
                        updated_at = ?,
                        record_version = record_version + 1
                    WHERE task_id = ? AND record_version = ?
                    """,
                    (
                        default_priority,
                        updated_at,
                        task_id,
                        expected_version,
                    ),
                )
                if result.rowcount == 0:
                    self._raise_version_or_not_found(
                        connection,
                        task_id=task_id,
                        expected_version=expected_version,
                    )
                row = self._select_task_row(connection, task_id=task_id)
                if row is None:
                    raise AnalyticalTaskNotFoundError(task_id=task_id)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return self._row_to_task_record(row)
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def update_priority(
        self,
        *,
        task_id: str,
        priority: int,
        expected_version: int,
    ) -> AnalyticalTaskRecord:
        return self.update_default_priority(
            task_id=task_id,
            default_priority=priority,
            expected_version=expected_version,
        )

    def request_task_deletion(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
        expected_active_job_id: str | None = None,
        expected_worker_instance_id: str | None = None,
        expected_lease_token: str | None = None,
    ) -> AnalyticalTaskMutationResult:
        expected_worker_identity = (
            expected_active_job_id,
            expected_worker_instance_id,
            expected_lease_token,
        )
        if any(value is not None for value in expected_worker_identity) and not all(
            value is not None for value in expected_worker_identity
        ):
            raise AnalyticalTaskInvalidRecordDataError(
                detail="expected active-worker identity must be provided in full"
            )
        normalized_expected_active_job_id = (
            _normalize_required_text(
                expected_active_job_id,
                field_name="expected_active_job_id",
            )
            if expected_active_job_id is not None
            else None
        )
        normalized_expected_worker_instance_id = (
            _normalize_required_text(
                expected_worker_instance_id,
                field_name="expected_worker_instance_id",
            )
            if expected_worker_instance_id is not None
            else None
        )
        normalized_expected_lease_token = (
            _normalize_required_text(
                expected_lease_token,
                field_name="expected_lease_token",
            )
            if expected_lease_token is not None
            else None
        )

        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                task_row = self._select_task_row(connection, task_id=task_id)
                if task_row is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.NOT_FOUND
                    )
                task_record = self._row_to_task_record(task_row)
                active_job = self._load_active_job(connection, task_record=task_record)
                if (
                    expected_task_version is not None
                    and task_record.record_version != expected_task_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )
                if task_record.state is AnalyticalTaskState.DELETION_REQUESTED:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
                        task=task_record,
                        job=active_job,
                    )

                expected_live_worker = normalized_expected_active_job_id is not None
                live_worker_matches = (
                    active_job is not None
                    and task_record.active_job_id == normalized_expected_active_job_id
                    and active_job.job_id == normalized_expected_active_job_id
                    and active_job.worker_instance_id
                    == normalized_expected_worker_instance_id
                    and active_job.lease_token == normalized_expected_lease_token
                    and active_job.state in _LIVE_WORKER_DELETION_STATES
                )
                if expected_live_worker and not live_worker_matches:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONFLICT,
                        task=task_record,
                        job=active_job,
                        details={"reason": "active worker changed before deletion request"},
                    )
                updated_at = serialize_utc_datetime(utc_now())
                update_result = connection.execute(
                    f"""
                    UPDATE {TASK_REGISTRY_TABLE_NAME}
                    SET
                        state = ?,
                        updated_at = ?,
                        record_version = record_version + 1
                    WHERE task_id = ? AND record_version = ?
                    """,
                    (
                        AnalyticalTaskState.DELETION_REQUESTED.value,
                        updated_at,
                        task_record.task_id,
                        task_record.record_version,
                    ),
                )
                if update_result.rowcount == 0:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )

                updated_row = self._select_task_row(connection, task_id=task_record.task_id)
                if updated_row is None:
                    raise AnalyticalTaskNotFoundError(task_id=task_record.task_id)
                updated_task = self._row_to_task_record(updated_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED,
                task=updated_task,
                job=active_job,
            )
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def delete_task_and_jobs(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                task_row = self._select_task_row(connection, task_id=task_id)
                if task_row is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.NOT_FOUND
                    )
                task_record = self._row_to_task_record(task_row)
                active_job = self._load_active_job(connection, task_record=task_record)
                if (
                    expected_task_version is not None
                    and task_record.record_version != expected_task_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )

                delete_result = connection.execute(
                    f"""
                    DELETE FROM {TASK_REGISTRY_TABLE_NAME}
                    WHERE task_id = ? AND record_version = ?
                    """,
                    (task_record.task_id, task_record.record_version),
                )
                if delete_result.rowcount == 0:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED,
                task=task_record,
                job=active_job,
            )
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def start_task(
        self,
        *,
        task_id: str,
        priority: int | None = None,
        expected_task_version: int | None = None,
        requested_job_id: str | None = None,
        runtime_state: Mapping[str, Any] | None = None,
    ) -> AnalyticalTaskMutationResult:
        if priority is not None and priority < 1:
            raise AnalyticalTaskInvalidRecordDataError(detail="priority must be >= 1")
        normalized_requested_job_id: str | None = None
        if requested_job_id is not None:
            normalized_requested_job_id = requested_job_id.strip()
            if normalized_requested_job_id == "":
                raise AnalyticalTaskInvalidRecordDataError(
                    detail="requested_job_id must not be empty"
                )
        runtime_state_payload = dict(runtime_state) if runtime_state is not None else None
        if runtime_state_payload is not None:
            _serialize_runtime_state(runtime_state_payload)
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                task_row = self._select_task_row(connection, task_id=task_id)
                if task_row is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.NOT_FOUND
                    )
                task_record = self._row_to_task_record(task_row)
                active_job = self._load_active_job(connection, task_record=task_record)

                if (
                    expected_task_version is not None
                    and task_record.record_version != expected_task_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )

                if task_record.state is AnalyticalTaskState.DELETION_REQUESTED:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONFLICT,
                        task=task_record,
                        job=active_job,
                        details={"priority": "deletion"},
                    )

                if active_job is not None:
                    decision = evaluate_job_intent(
                        intent=AnalyticalTaskLifecycleIntent.START,
                        current_state=active_job.state,
                    )
                    if decision.result_type is AnalyticalTaskMutationResultType.APPLIED:
                        if decision.target_state is None:
                            self._rollback(connection)
                            return AnalyticalTaskMutationResult(
                                result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                                task=task_record,
                                job=active_job,
                            )
                        updated_task, updated_job = self._apply_job_state_transition(
                            connection,
                            task_record=task_record,
                            job_record=active_job,
                            to_state=decision.target_state,
                            finished_reason=None,
                            error_event_code=None,
                            expected_task_version=task_record.record_version,
                            expected_job_version=active_job.record_version,
                        )
                        self._commit(connection)
                        return AnalyticalTaskMutationResult(
                            result_type=AnalyticalTaskMutationResultType.APPLIED,
                            task=updated_task,
                            job=updated_job,
                        )

                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=decision.result_type,
                        task=task_record,
                        job=active_job,
                        details=decision.details,
                    )

                latest_job = self._load_latest_job(connection, task_record=task_record)
                if latest_job is not None:
                    if latest_job.state is AnalyticalTaskState.COMPLETED:
                        self._rollback(connection)
                        return AnalyticalTaskMutationResult(
                            result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                            task=task_record,
                            job=latest_job,
                            details={"reason": "completed jobs are not restartable"},
                        )
                    if latest_job.state not in {
                        AnalyticalTaskState.FAILED,
                        AnalyticalTaskState.CANCELLED,
                    }:
                        self._rollback(connection)
                        return AnalyticalTaskMutationResult(
                            result_type=AnalyticalTaskMutationResultType.CONFLICT,
                            task=task_record,
                            job=latest_job,
                            details={"reason": "task has a non-terminal latest job"},
                        )

                requested_priority = (
                    priority if priority is not None else task_record.default_priority
                )
                now = utc_now()
                created_job = self._insert_new_job(
                    connection,
                    task_record=task_record,
                    priority=requested_priority,
                    created_at=now,
                    requested_job_id=normalized_requested_job_id,
                    runtime_state=runtime_state_payload,
                )

                updated_task = self._activate_new_job_for_task(
                    connection,
                    task_record=task_record,
                    job_id=created_job.job_id,
                    projected_state=created_job.state,
                    expected_task_version=task_record.record_version,
                )
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED,
                task=updated_task,
                job=created_job,
            )
        except sqlite3.IntegrityError as error:
            self._rollback(connection)
            if "uq_analytical_task_jobs_single_active" in str(error):
                latest_task = self.get(task_id=task_id)
                latest_job = (
                    self.get_job(job_id=latest_task.active_job_id)
                    if latest_task.active_job_id is not None
                    else None
                )
                return AnalyticalTaskMutationResult(
                    result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
                    task=latest_task,
                    job=latest_job,
                )
            raise self._translate_sqlite_error(error) from error
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def request_job_intent(
        self,
        *,
        task_id: str,
        intent: AnalyticalTaskLifecycleIntent,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                task_row = self._select_task_row(connection, task_id=task_id)
                if task_row is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.NOT_FOUND
                    )
                task_record = self._row_to_task_record(task_row)
                active_job = self._load_active_job(connection, task_record=task_record)

                if (
                    expected_task_version is not None
                    and task_record.record_version != expected_task_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )

                if task_record.state is AnalyticalTaskState.DELETION_REQUESTED:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONFLICT,
                        task=task_record,
                        job=active_job,
                        details={"priority": "deletion"},
                    )

                if active_job is None:
                    latest_job = self._load_latest_job(connection, task_record=task_record)
                    if (
                        intent is AnalyticalTaskLifecycleIntent.CANCEL
                        and latest_job is not None
                        and latest_job.state is AnalyticalTaskState.CANCELLED
                    ):
                        self._rollback(connection)
                        return AnalyticalTaskMutationResult(
                            result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
                            task=task_record,
                            job=latest_job,
                        )
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        details={"reason": "task has no active job"},
                    )

                if (
                    expected_job_version is not None
                    and active_job.record_version != expected_job_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )

                decision = evaluate_job_intent(intent=intent, current_state=active_job.state)
                if decision.result_type is not AnalyticalTaskMutationResultType.APPLIED:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=decision.result_type,
                        task=task_record,
                        job=active_job,
                        details=decision.details,
                    )
                if decision.target_state is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        job=active_job,
                    )

                finished_reason: str | None = None
                if (
                    intent is AnalyticalTaskLifecycleIntent.CANCEL
                    and decision.target_state is AnalyticalTaskState.CANCELLED
                ):
                    finished_reason = "cancelled"

                updated_task, updated_job = self._apply_job_state_transition(
                    connection,
                    task_record=task_record,
                    job_record=active_job,
                    to_state=decision.target_state,
                    finished_reason=finished_reason,
                    error_event_code=None,
                    expected_task_version=task_record.record_version,
                    expected_job_version=active_job.record_version,
                )
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED,
                task=updated_task,
                job=updated_job,
            )
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def reprioritize_active_job(
        self,
        *,
        task_id: str,
        priority: int,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        if priority < 1:
            raise AnalyticalTaskInvalidRecordDataError(detail="priority must be >= 1")
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                task_row = self._select_task_row(connection, task_id=task_id)
                if task_row is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.NOT_FOUND
                    )
                task_record = self._row_to_task_record(task_row)
                active_job = self._load_active_job(connection, task_record=task_record)
                if active_job is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        details={"reason": "task has no active job"},
                    )
                if task_record.state is AnalyticalTaskState.DELETION_REQUESTED:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONFLICT,
                        task=task_record,
                        job=active_job,
                        details={"priority": "deletion"},
                    )
                if (
                    expected_task_version is not None
                    and task_record.record_version != expected_task_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )
                if (
                    expected_job_version is not None
                    and active_job.record_version != expected_job_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )
                if not is_reprioritize_allowed(active_job.state):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        job=active_job,
                        details={"state": active_job.state.value},
                    )
                if active_job.priority == priority:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
                        task=task_record,
                        job=active_job,
                    )

                now = utc_now()
                updated_at = serialize_utc_datetime(now)
                job_update_result = connection.execute(
                    f"""
                    UPDATE {TASK_JOB_REGISTRY_TABLE_NAME}
                    SET
                        priority = ?,
                        record_version = record_version + 1
                    WHERE job_id = ? AND record_version = ?
                    """,
                    (priority, active_job.job_id, active_job.record_version),
                )
                if job_update_result.rowcount == 0:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )

                task_update_result = connection.execute(
                    f"""
                    UPDATE {TASK_REGISTRY_TABLE_NAME}
                    SET
                        updated_at = ?,
                        record_version = record_version + 1
                    WHERE task_id = ? AND record_version = ?
                    """,
                    (updated_at, task_record.task_id, task_record.record_version),
                )
                if task_update_result.rowcount == 0:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )

                updated_task_row = self._select_task_row(connection, task_id=task_record.task_id)
                updated_job_row = self._select_job_row(connection, job_id=active_job.job_id)
                if updated_task_row is None or updated_job_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="reprioritized records cannot be reloaded",
                    )
                updated_task = self._row_to_task_record(updated_task_row)
                updated_job = self._row_to_job_record(updated_job_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED,
                task=updated_task,
                job=updated_job,
            )
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def transition_active_job_state(
        self,
        *,
        task_id: str,
        to_state: AnalyticalTaskState,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
        finished_reason: str | None = None,
        error_event_code: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        if to_state is AnalyticalTaskState.DELETION_REQUESTED:
            raise AnalyticalTaskInvalidRecordDataError(
                detail="deletion_requested is not a valid job terminal state"
            )
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                task_row = self._select_task_row(connection, task_id=task_id)
                if task_row is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.NOT_FOUND
                    )
                task_record = self._row_to_task_record(task_row)
                job_record = self._load_active_job(connection, task_record=task_record)
                if job_record is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        details={"reason": "task has no active job"},
                    )
                if task_record.state is AnalyticalTaskState.DELETION_REQUESTED:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONFLICT,
                        task=task_record,
                        job=job_record,
                        details={"priority": "deletion"},
                    )
                if (
                    expected_task_version is not None
                    and task_record.record_version != expected_task_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )
                if (
                    expected_job_version is not None
                    and job_record.record_version != expected_job_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )
                if (
                    task_record.state is AnalyticalTaskState.DELETION_REQUESTED
                    and to_state not in TERMINAL_ANALYTICAL_TASK_JOB_STATES
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONFLICT,
                        task=task_record,
                        job=job_record,
                        details={"priority": "deletion"},
                    )
                if job_record.state is to_state:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
                        task=task_record,
                        job=job_record,
                    )
                if not is_runtime_transition_allowed(
                    from_state=job_record.state,
                    to_state=to_state,
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        job=job_record,
                        details={
                            "from_state": job_record.state.value,
                            "to_state": to_state.value,
                        },
                    )
                updated_task, updated_job = self._apply_job_state_transition(
                    connection,
                    task_record=task_record,
                    job_record=job_record,
                    to_state=to_state,
                    finished_reason=finished_reason,
                    error_event_code=error_event_code,
                    expected_task_version=task_record.record_version,
                    expected_job_version=job_record.record_version,
                )
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED,
                task=updated_task,
                job=updated_job,
            )
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def transition_state(
        self,
        *,
        task_id: str,
        to_state: AnalyticalTaskState,
        expected_version: int,
        finished_reason: str | None = None,
        error_event_code: int | None = None,
    ) -> AnalyticalTaskRecord:
        transition_result = self.transition_active_job_state(
            task_id=task_id,
            to_state=to_state,
            expected_task_version=expected_version,
            expected_job_version=None,
            finished_reason=finished_reason,
            error_event_code=error_event_code,
        )
        if transition_result.task is None:
            if transition_result.result_type is AnalyticalTaskMutationResultType.NOT_FOUND:
                raise AnalyticalTaskNotFoundError(task_id=task_id)
            if transition_result.result_type is AnalyticalTaskMutationResultType.CONCURRENT_UPDATE:
                raise AnalyticalTaskVersionConflictError(
                    task_id=task_id,
                    expected_version=expected_version,
                    actual_version=None,
                )
            raise AnalyticalTaskInvalidRecordDataError(
                detail=(
                    "cannot transition task state via active job transition: "
                    f"{transition_result.result_type.value}"
                )
            )
        return transition_result.task

    def update_active_job_progress(
        self,
        *,
        task_id: str,
        progress: int,
        current_stage: str | None,
        runtime_state: Mapping[str, Any] | None = None,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        if progress < 0 or progress > 100:
            raise AnalyticalTaskInvalidRecordDataError(detail="progress must be in range 0..100")
        normalized_stage = _normalize_optional_text(current_stage, field_name="current_stage")
        runtime_state_payload = dict(runtime_state) if runtime_state is not None else None
        if runtime_state_payload is not None:
            _serialize_runtime_state(runtime_state_payload)

        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                task_row = self._select_task_row(connection, task_id=task_id)
                if task_row is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.NOT_FOUND
                    )
                task_record = self._row_to_task_record(task_row)
                job_record = self._load_active_job(connection, task_record=task_record)
                if job_record is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        details={"reason": "task has no active job"},
                    )
                if (
                    expected_task_version is not None
                    and task_record.record_version != expected_task_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )
                if (
                    expected_job_version is not None
                    and job_record.record_version != expected_job_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )
                if is_terminal_state(job_record.state):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        job=job_record,
                        details={"reason": "terminal jobs are read-only"},
                    )

                effective_progress = max(progress, job_record.progress)
                next_runtime_state = (
                    runtime_state_payload
                    if runtime_state_payload is not None
                    else job_record.runtime_state
                )
                if (
                    job_record.progress == effective_progress
                    and job_record.current_stage == normalized_stage
                    and job_record.runtime_state == next_runtime_state
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
                        task=task_record,
                        job=job_record,
                    )

                now = utc_now()
                updated_at = serialize_utc_datetime(now)
                job_update_result = connection.execute(
                    f"""
                    UPDATE {TASK_JOB_REGISTRY_TABLE_NAME}
                    SET
                        progress = ?,
                        current_stage = ?,
                        runtime_state_json = ?,
                        record_version = record_version + 1
                    WHERE job_id = ? AND record_version = ?
                    """,
                    (
                        effective_progress,
                        normalized_stage,
                        _serialize_runtime_state(next_runtime_state),
                        job_record.job_id,
                        job_record.record_version,
                    ),
                )
                if job_update_result.rowcount == 0:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )

                task_update_result = connection.execute(
                    f"""
                    UPDATE {TASK_REGISTRY_TABLE_NAME}
                    SET
                        updated_at = ?,
                        record_version = record_version + 1
                    WHERE task_id = ? AND record_version = ?
                    """,
                    (updated_at, task_record.task_id, task_record.record_version),
                )
                if task_update_result.rowcount == 0:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )

                updated_task_row = self._select_task_row(connection, task_id=task_record.task_id)
                updated_job_row = self._select_job_row(connection, job_id=job_record.job_id)
                if updated_task_row is None or updated_job_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="updated records cannot be reloaded",
                    )
                updated_task = self._row_to_task_record(updated_task_row)
                updated_job = self._row_to_job_record(updated_job_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED,
                task=updated_task,
                job=updated_job,
            )
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def update_progress_and_stage(
        self,
        *,
        task_id: str,
        progress: int,
        current_stage: str | None,
        expected_version: int,
    ) -> AnalyticalTaskRecord:
        update_result = self.update_active_job_progress(
            task_id=task_id,
            progress=progress,
            current_stage=current_stage,
            expected_task_version=expected_version,
        )
        if update_result.task is None:
            if update_result.result_type is AnalyticalTaskMutationResultType.NOT_FOUND:
                raise AnalyticalTaskNotFoundError(task_id=task_id)
            if update_result.result_type is AnalyticalTaskMutationResultType.CONCURRENT_UPDATE:
                raise AnalyticalTaskVersionConflictError(
                    task_id=task_id,
                    expected_version=expected_version,
                    actual_version=None,
                )
            raise AnalyticalTaskInvalidRecordDataError(
                detail=f"cannot update progress: {update_result.result_type.value}"
            )
        return update_result.task

    def increment_active_job_recovery_count(
        self,
        *,
        task_id: str,
        expected_task_version: int | None = None,
        expected_job_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        self.ensure_schema()
        connection = self._open_connection()
        try:
            self._begin_immediate(connection)
            try:
                task_row = self._select_task_row(connection, task_id=task_id)
                if task_row is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.NOT_FOUND
                    )
                task_record = self._row_to_task_record(task_row)
                job_record = self._load_active_job(connection, task_record=task_record)
                if job_record is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        details={"reason": "task has no active job"},
                    )
                if (
                    expected_task_version is not None
                    and task_record.record_version != expected_task_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )
                if (
                    expected_job_version is not None
                    and job_record.record_version != expected_job_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )
                if is_terminal_state(job_record.state):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        job=job_record,
                        details={"reason": "terminal jobs are read-only"},
                    )

                now_serialized = serialize_utc_datetime(utc_now())
                job_update_result = connection.execute(
                    f"""
                    UPDATE {TASK_JOB_REGISTRY_TABLE_NAME}
                    SET recovery_count = recovery_count + 1, record_version = record_version + 1
                    WHERE job_id = ? AND record_version = ?
                    """,
                    (job_record.job_id, job_record.record_version),
                )
                if job_update_result.rowcount == 0:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )
                task_update_result = connection.execute(
                    f"""
                    UPDATE {TASK_REGISTRY_TABLE_NAME}
                    SET updated_at = ?, record_version = record_version + 1
                    WHERE task_id = ? AND record_version = ?
                    """,
                    (now_serialized, task_record.task_id, task_record.record_version),
                )
                if task_update_result.rowcount == 0:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=job_record,
                    )

                updated_task_row = self._select_task_row(connection, task_id=task_record.task_id)
                updated_job_row = self._select_job_row(connection, job_id=job_record.job_id)
                if updated_task_row is None or updated_job_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="recovery-updated records cannot be reloaded",
                    )
                updated_task = self._row_to_task_record(updated_task_row)
                updated_job = self._row_to_job_record(updated_job_row)
            except Exception:
                self._rollback(connection)
                raise
            self._commit(connection)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED,
                task=updated_task,
                job=updated_job,
            )
        except sqlite3.Error as error:
            self._rollback(connection)
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def update_task_config(
        self,
        *,
        task_id: str,
        config_document: Mapping[str, object],
        expected_task_version: int | None = None,
    ) -> AnalyticalTaskMutationResult:
        if len(config_document) == 0:
            raise AnalyticalTaskInvalidRecordDataError(detail="config document must not be empty")

        self.ensure_schema()
        connection = self._open_connection()
        created_revision_path: Path | None = None
        previous_config_payload: str | None = None
        task_dir: Path | None = None
        try:
            self._begin_immediate(connection)
            try:
                task_row = self._select_task_row(connection, task_id=task_id)
                if task_row is None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.NOT_FOUND
                    )
                task_record = self._row_to_task_record(task_row)
                active_job = self._load_active_job(connection, task_record=task_record)

                if (
                    expected_task_version is not None
                    and task_record.record_version != expected_task_version
                ):
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                        task=task_record,
                        job=active_job,
                    )

                if task_record.state is AnalyticalTaskState.DELETION_REQUESTED:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.CONFLICT,
                        task=task_record,
                        job=active_job,
                        details={"priority": "deletion"},
                    )

                if active_job is not None:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        job=active_job,
                        details={"reason": "cannot update config while active job exists"},
                    )

                latest_job = self._load_latest_job(connection, task_record=task_record)
                if latest_job is not None and latest_job.state is AnalyticalTaskState.COMPLETED:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        job=latest_job,
                        details={"reason": "completed task config is immutable"},
                    )
                if latest_job is not None and latest_job.state not in {
                    AnalyticalTaskState.FAILED,
                    AnalyticalTaskState.CANCELLED,
                }:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
                        task=task_record,
                        job=latest_job,
                        details={
                            "reason": ("config update is allowed only after failed/cancelled jobs")
                        },
                    )

                raw_priority = config_document.get("priority")
                if type(raw_priority) is not int or raw_priority < 1:
                    raise AnalyticalTaskInvalidRecordDataError(
                        detail="config document must include integer priority >= 1"
                    )

                task_dir = self._task_dir_from_relative_path(
                    task_record.task_dir_relative_path
                )
                current_revision_path = task_dir / task_record.current_config_relative_path
                current_config_document = json.loads(
                    current_revision_path.read_text(encoding="utf-8")
                )
                if not isinstance(current_config_document, dict):
                    raise AnalyticalTaskInvalidRecordDataError(
                        detail="current config revision must be a JSON object"
                    )
                _validate_immutable_trace_id(
                    current_config_document=current_config_document,
                    next_config_document=config_document,
                )

                new_hash = compute_config_hash(config_document)
                if new_hash == task_record.current_config_hash:
                    self._rollback(connection)
                    return AnalyticalTaskMutationResult(
                        result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
                        task=task_record,
                    )

                config_path = task_dir / TASK_CONFIG_FILENAME
                previous_config_payload = config_path.read_text(encoding="utf-8")

                next_revision = task_record.current_config_revision + 1
                (
                    created_revision_number,
                    created_revision_relative_path,
                    created_revision_hash,
                    created_revision_path,
                ) = write_task_config_revision(
                    task_dir=task_dir,
                    config_document=config_document,
                    revision=next_revision,
                )

                now = utc_now()
                updated_at = serialize_utc_datetime(now)
                update_result = connection.execute(
                    f"""
                    UPDATE {TASK_REGISTRY_TABLE_NAME}
                    SET
                        current_config_revision = ?,
                        current_config_relative_path = ?,
                        current_config_hash = ?,
                        default_priority = ?,
                        updated_at = ?,
                        record_version = record_version + 1
                    WHERE task_id = ? AND record_version = ?
                    """,
                    (
                        created_revision_number,
                        created_revision_relative_path,
                        created_revision_hash,
                        raw_priority,
                        updated_at,
                        task_record.task_id,
                        task_record.record_version,
                    ),
                )
                if update_result.rowcount == 0:
                    raise AnalyticalTaskVersionConflictError(
                        task_id=task_record.task_id,
                        expected_version=task_record.record_version,
                        actual_version=None,
                    )

                updated_task_row = self._select_task_row(connection, task_id=task_record.task_id)
                if updated_task_row is None:
                    raise AnalyticalTaskRegistryIncompatibleSchemaError(
                        database_path=self._database_path,
                        detail="updated task record cannot be reloaded",
                    )
                updated_task = self._row_to_task_record(updated_task_row)
            except Exception:
                self._rollback(connection)
                if created_revision_path is not None and task_dir is not None:
                    self._compensate_failed_config_update(
                        task_id=task_id,
                        created_revision_path=created_revision_path,
                        task_dir=task_dir,
                        previous_config_payload=previous_config_payload,
                    )
                raise
            self._commit(connection)
            return AnalyticalTaskMutationResult(
                result_type=AnalyticalTaskMutationResultType.APPLIED,
                task=updated_task,
            )
        except AnalyticalTaskVersionConflictError:
            if created_revision_path is not None and task_dir is not None:
                self._compensate_failed_config_update(
                    task_id=task_id,
                    created_revision_path=created_revision_path,
                    task_dir=task_dir,
                    previous_config_payload=previous_config_payload,
                )
            if expected_task_version is not None:
                latest_task = self.get(task_id=task_id)
                return AnalyticalTaskMutationResult(
                    result_type=AnalyticalTaskMutationResultType.CONCURRENT_UPDATE,
                    task=latest_task,
                )
            raise
        except sqlite3.Error as error:
            self._rollback(connection)
            if created_revision_path is not None and task_dir is not None:
                self._compensate_failed_config_update(
                    task_id=task_id,
                    created_revision_path=created_revision_path,
                    task_dir=task_dir,
                    previous_config_payload=previous_config_payload,
                )
            raise self._translate_sqlite_error(error) from error
        finally:
            connection.close()

    def _compensate_failed_config_update(
        self,
        *,
        task_id: str,
        created_revision_path: Path,
        task_dir: Path,
        previous_config_payload: str | None,
    ) -> None:
        config_path = task_dir / TASK_CONFIG_FILENAME
        try:
            if created_revision_path.exists():
                created_revision_path.unlink()
            if previous_config_payload is not None:
                write_text_atomically(path=config_path, payload=previous_config_payload)
        except OSError as error:
            raise AnalyticalTaskConfigRevisionError(task_id=task_id, detail=str(error)) from error

    def _ensure_schema_in_transaction(self, connection: sqlite3.Connection) -> None:
        application_id = self._read_pragma_int(connection, "application_id")
        user_version = self._read_pragma_int(connection, "user_version")
        existing_tables = self._list_user_tables(connection)

        if application_id not in (0, TASK_REGISTRY_APPLICATION_ID):
            raise AnalyticalTaskRegistryForeignDatabaseError(
                database_path=self._database_path,
                application_id=application_id,
            )

        if application_id == 0 and (user_version != 0 or len(existing_tables) > 0):
            raise AnalyticalTaskRegistryForeignDatabaseError(
                database_path=self._database_path,
                application_id=application_id,
            )

        if (
            application_id == TASK_REGISTRY_APPLICATION_ID
            and user_version == 0
            and len(existing_tables) > 0
        ):
            raise AnalyticalTaskRegistryIncompatibleSchemaError(
                database_path=self._database_path,
                detail="version 0 database must not contain user tables",
            )

        if user_version > TASK_REGISTRY_SCHEMA_VERSION:
            raise AnalyticalTaskRegistryUnsupportedSchemaVersionError(
                database_path=self._database_path,
                schema_version=user_version,
                supported_version=TASK_REGISTRY_SCHEMA_VERSION,
            )

        if user_version < TASK_REGISTRY_SCHEMA_VERSION:
            if application_id == 0:
                self._set_pragma_int(connection, "application_id", TASK_REGISTRY_APPLICATION_ID)
            self._apply_migrations(connection, start_version=user_version)

        self._validate_schema_layout(connection)

    def _apply_migrations(self, connection: sqlite3.Connection, *, start_version: int) -> None:
        current_version = start_version
        while current_version < TASK_REGISTRY_SCHEMA_VERSION:
            migration = MIGRATIONS.get(current_version)
            if migration is None:
                raise AnalyticalTaskRegistryUnsupportedSchemaVersionError(
                    database_path=self._database_path,
                    schema_version=current_version,
                    supported_version=TASK_REGISTRY_SCHEMA_VERSION,
                )
            try:
                migration(connection, self._database_path)
            except Exception as error:
                raise AnalyticalTaskRegistryMigrationError(
                    database_path=self._database_path,
                    detail=str(error),
                ) from error
            current_version += 1
            self._set_pragma_int(connection, "user_version", current_version)

    def _validate_schema_layout(self, connection: sqlite3.Connection) -> None:
        self._assert_table_columns(
            connection=connection,
            table_name=TASK_REGISTRY_TABLE_NAME,
            expected_columns=TASK_REGISTRY_EXPECTED_COLUMNS,
        )
        self._assert_table_columns(
            connection=connection,
            table_name=TASK_JOB_REGISTRY_TABLE_NAME,
            expected_columns=TASK_JOB_REGISTRY_EXPECTED_COLUMNS,
        )
        self._assert_table_columns(
            connection=connection,
            table_name=TASK_RUNTIME_LEASE_REGISTRY_TABLE_NAME,
            expected_columns=TASK_RUNTIME_LEASE_REGISTRY_EXPECTED_COLUMNS,
        )

        task_indexes = self._list_indexes(connection, table_name=TASK_REGISTRY_TABLE_NAME)
        job_indexes = self._list_indexes(connection, table_name=TASK_JOB_REGISTRY_TABLE_NAME)
        actual_indexes = task_indexes | job_indexes
        missing_indexes = [
            index_name
            for index_name in TASK_REGISTRY_EXPECTED_INDEXES
            if index_name not in actual_indexes
        ]
        if len(missing_indexes) > 0:
            raise AnalyticalTaskRegistryIncompatibleSchemaError(
                database_path=self._database_path,
                detail=f"missing indexes: {', '.join(missing_indexes)}",
            )

    def _assert_table_columns(
        self,
        *,
        connection: sqlite3.Connection,
        table_name: str,
        expected_columns: tuple[str, ...],
    ) -> None:
        table_exists_row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        if table_exists_row is None:
            raise AnalyticalTaskRegistryIncompatibleSchemaError(
                database_path=self._database_path,
                detail=f"missing table '{table_name}'",
            )

        table_info_rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        actual_columns = tuple(str(row["name"]) for row in table_info_rows)
        if actual_columns != expected_columns:
            raise AnalyticalTaskRegistryIncompatibleSchemaError(
                database_path=self._database_path,
                detail=(
                    f"unexpected columns in {table_name}: "
                    f"expected={expected_columns}, actual={actual_columns}"
                ),
            )

    def _list_indexes(self, connection: sqlite3.Connection, *, table_name: str) -> set[str]:
        index_info_rows = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
        return {str(row["name"]) for row in index_info_rows}

    def _run_quick_check(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("PRAGMA quick_check").fetchall()
        if len(rows) == 0:
            raise AnalyticalTaskRegistryDatabaseCorruptedError(
                database_path=self._database_path,
                detail="quick_check returned no rows",
            )
        failures = [str(row[0]) for row in rows if str(row[0]) != "ok"]
        if len(failures) > 0:
            raise AnalyticalTaskRegistryDatabaseCorruptedError(
                database_path=self._database_path,
                detail=f"quick_check failed: {'; '.join(failures)}",
            )

    def _select_task_row(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT * FROM {TASK_REGISTRY_TABLE_NAME} WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    def _select_job_row(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT * FROM {TASK_JOB_REGISTRY_TABLE_NAME} WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    def _select_runtime_lease_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT * FROM {TASK_RUNTIME_LEASE_REGISTRY_TABLE_NAME} WHERE singleton_id = 1"
        ).fetchone()

    def _load_active_or_latest_job(
        self,
        connection: sqlite3.Connection,
        *,
        task_record: AnalyticalTaskRecord,
    ) -> AnalyticalTaskJobRecord | None:
        chosen_job_id = (
            task_record.active_job_id
            if task_record.active_job_id is not None
            else task_record.latest_job_id
        )
        if chosen_job_id is None:
            return None
        row = self._select_job_row(connection, job_id=chosen_job_id)
        if row is None:
            return None
        return self._row_to_job_record(row)

    def _load_active_job(
        self,
        connection: sqlite3.Connection,
        *,
        task_record: AnalyticalTaskRecord,
    ) -> AnalyticalTaskJobRecord | None:
        if task_record.active_job_id is None:
            return None
        row = self._select_job_row(connection, job_id=task_record.active_job_id)
        if row is None:
            return None
        return self._row_to_job_record(row)

    def _load_latest_job(
        self,
        connection: sqlite3.Connection,
        *,
        task_record: AnalyticalTaskRecord,
    ) -> AnalyticalTaskJobRecord | None:
        if task_record.latest_job_id is None:
            return None
        row = self._select_job_row(connection, job_id=task_record.latest_job_id)
        if row is None:
            return None
        return self._row_to_job_record(row)

    def _insert_new_job(
        self,
        connection: sqlite3.Connection,
        *,
        task_record: AnalyticalTaskRecord,
        priority: int,
        created_at: datetime,
        requested_job_id: str | None = None,
        runtime_state: Mapping[str, Any] | None = None,
    ) -> AnalyticalTaskJobRecord:
        now_serialized = serialize_utc_datetime(created_at)
        job_id = requested_job_id if requested_job_id is not None else str(uuid4())
        runtime_state_payload = dict(runtime_state) if runtime_state is not None else {}
        serialized_runtime_state = _serialize_runtime_state(runtime_state_payload)
        connection.execute(
            f"""
            INSERT INTO {TASK_JOB_REGISTRY_TABLE_NAME} (
                job_id,
                task_id,
                config_revision,
                config_relative_path,
                config_hash,
                state,
                current_stage,
                progress,
                priority,
                created_at,
                queued_at,
                first_started_at,
                last_started_at,
                last_stopped_at,
                finished_at,
                finished_reason,
                error_event_code,
                runtime_state_json,
                worker_instance_id,
                worker_pid,
                lease_token,
                heartbeat_at,
                lease_expires_at,
                recovery_count,
                record_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                task_record.task_id,
                task_record.current_config_revision,
                task_record.current_config_relative_path,
                task_record.current_config_hash,
                AnalyticalTaskState.QUEUED.value,
                None,
                0,
                priority,
                now_serialized,
                now_serialized,
                None,
                None,
                None,
                None,
                None,
                None,
                serialized_runtime_state,
                None,
                None,
                None,
                None,
                None,
                0,
                1,
            ),
        )
        row = self._select_job_row(connection, job_id=job_id)
        if row is None:
            raise AnalyticalTaskRegistryIncompatibleSchemaError(
                database_path=self._database_path,
                detail="inserted job record cannot be reloaded",
            )
        return self._row_to_job_record(row)

    def _activate_new_job_for_task(
        self,
        connection: sqlite3.Connection,
        *,
        task_record: AnalyticalTaskRecord,
        job_id: str,
        projected_state: AnalyticalTaskState,
        expected_task_version: int,
    ) -> AnalyticalTaskRecord:
        now_serialized = serialize_utc_datetime(utc_now())
        result = connection.execute(
            f"""
            UPDATE {TASK_REGISTRY_TABLE_NAME}
            SET
                state = ?,
                active_job_id = ?,
                latest_job_id = ?,
                updated_at = ?,
                record_version = record_version + 1
            WHERE task_id = ? AND record_version = ?
            """,
            (
                projected_state.value,
                job_id,
                job_id,
                now_serialized,
                task_record.task_id,
                expected_task_version,
            ),
        )
        if result.rowcount == 0:
            raise AnalyticalTaskVersionConflictError(
                task_id=task_record.task_id,
                expected_version=expected_task_version,
                actual_version=None,
            )
        row = self._select_task_row(connection, task_id=task_record.task_id)
        if row is None:
            raise AnalyticalTaskNotFoundError(task_id=task_record.task_id)
        return self._row_to_task_record(row)

    def _apply_job_state_transition(
        self,
        connection: sqlite3.Connection,
        *,
        task_record: AnalyticalTaskRecord,
        job_record: AnalyticalTaskJobRecord,
        to_state: AnalyticalTaskState,
        finished_reason: str | None,
        error_event_code: int | None,
        expected_task_version: int,
        expected_job_version: int,
    ) -> tuple[AnalyticalTaskRecord, AnalyticalTaskJobRecord]:
        normalized_finished_reason = _normalize_optional_text(
            finished_reason, field_name="finished_reason"
        )
        _validate_transition_terminal_fields(
            to_state=to_state,
            finished_reason=normalized_finished_reason,
            error_event_code=error_event_code,
        )
        transition_updates = _build_job_transition_updates(
            job_record=job_record,
            to_state=to_state,
            finished_reason=normalized_finished_reason,
            error_event_code=error_event_code,
        )

        job_result = connection.execute(
            f"""
            UPDATE {TASK_JOB_REGISTRY_TABLE_NAME}
            SET
                state = ?,
                queued_at = ?,
                first_started_at = ?,
                last_started_at = ?,
                last_stopped_at = ?,
                finished_at = ?,
                finished_reason = ?,
                error_event_code = ?,
                worker_instance_id = ?,
                worker_pid = ?,
                lease_token = ?,
                heartbeat_at = ?,
                lease_expires_at = ?,
                record_version = record_version + 1
            WHERE job_id = ? AND record_version = ?
            """,
            (
                transition_updates["state"],
                transition_updates["queued_at"],
                transition_updates["first_started_at"],
                transition_updates["last_started_at"],
                transition_updates["last_stopped_at"],
                transition_updates["finished_at"],
                transition_updates["finished_reason"],
                transition_updates["error_event_code"],
                transition_updates["worker_instance_id"],
                transition_updates["worker_pid"],
                transition_updates["lease_token"],
                transition_updates["heartbeat_at"],
                transition_updates["lease_expires_at"],
                job_record.job_id,
                expected_job_version,
            ),
        )
        if job_result.rowcount == 0:
            raise AnalyticalTaskVersionConflictError(
                task_id=task_record.task_id,
                expected_version=expected_task_version,
                actual_version=None,
            )

        projected_task_state = to_state
        next_active_job_id = (
            None if to_state in TERMINAL_ANALYTICAL_TASK_JOB_STATES else task_record.active_job_id
        )
        task_result = connection.execute(
            f"""
            UPDATE {TASK_REGISTRY_TABLE_NAME}
            SET
                state = ?,
                active_job_id = ?,
                latest_job_id = ?,
                updated_at = ?,
                record_version = record_version + 1
            WHERE task_id = ? AND record_version = ?
            """,
            (
                projected_task_state.value,
                next_active_job_id,
                task_record.latest_job_id
                if task_record.latest_job_id is not None
                else job_record.job_id,
                transition_updates["updated_at"],
                task_record.task_id,
                expected_task_version,
            ),
        )
        if task_result.rowcount == 0:
            raise AnalyticalTaskVersionConflictError(
                task_id=task_record.task_id,
                expected_version=expected_task_version,
                actual_version=None,
            )

        updated_task_row = self._select_task_row(connection, task_id=task_record.task_id)
        updated_job_row = self._select_job_row(connection, job_id=job_record.job_id)
        if updated_task_row is None or updated_job_row is None:
            raise AnalyticalTaskRegistryIncompatibleSchemaError(
                database_path=self._database_path,
                detail="transitioned records cannot be reloaded",
            )
        return self._row_to_task_record(updated_task_row), self._row_to_job_record(updated_job_row)

    def _raise_version_or_not_found(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        expected_version: int,
    ) -> None:
        row = self._select_task_row(connection, task_id=task_id)
        if row is None:
            raise AnalyticalTaskNotFoundError(task_id=task_id)
        record = self._row_to_task_record(row)
        raise AnalyticalTaskVersionConflictError(
            task_id=task_id,
            expected_version=expected_version,
            actual_version=record.record_version,
        )

    def _row_to_task_record(self, row: sqlite3.Row) -> AnalyticalTaskRecord:
        try:
            payload: dict[str, Any] = {
                "task_id": str(row["task_id"]),
                "name": row["name"],
                "state": str(row["state"]),
                "default_priority": row["default_priority"],
                "current_config_revision": row["current_config_revision"],
                "current_config_relative_path": row["current_config_relative_path"],
                "current_config_hash": row["current_config_hash"],
                "active_job_id": row["active_job_id"],
                "latest_job_id": row["latest_job_id"],
                "created_at": parse_utc_datetime(str(row["created_at"])),
                "updated_at": parse_utc_datetime(str(row["updated_at"])),
                "task_dir_relative_path": str(row["task_dir_relative_path"]),
                "record_version": row["record_version"],
            }
            return AnalyticalTaskRecord.model_validate(payload)
        except (TypeError, ValueError, ValidationError) as error:
            task_id_value = row["task_id"] if "task_id" in row.keys() else "<unknown>"
            raise AnalyticalTaskInvalidRecordDataError(
                detail=f"task_id={task_id_value}: {error}"
            ) from error

    def _row_to_job_record(self, row: sqlite3.Row) -> AnalyticalTaskJobRecord:
        try:
            runtime_state = _parse_runtime_state(raw_value=str(row["runtime_state_json"]))
            payload: dict[str, Any] = {
                "job_id": str(row["job_id"]),
                "task_id": str(row["task_id"]),
                "config_revision": row["config_revision"],
                "config_relative_path": str(row["config_relative_path"]),
                "config_hash": str(row["config_hash"]),
                "state": str(row["state"]),
                "current_stage": row["current_stage"],
                "progress": row["progress"],
                "priority": row["priority"],
                "created_at": parse_utc_datetime(str(row["created_at"])),
                "queued_at": _parse_optional_timestamp(row["queued_at"]),
                "first_started_at": _parse_optional_timestamp(row["first_started_at"]),
                "last_started_at": _parse_optional_timestamp(row["last_started_at"]),
                "last_stopped_at": _parse_optional_timestamp(row["last_stopped_at"]),
                "finished_at": _parse_optional_timestamp(row["finished_at"]),
                "finished_reason": row["finished_reason"],
                "error_event_code": row["error_event_code"],
                "runtime_state": runtime_state,
                "worker_instance_id": row["worker_instance_id"],
                "worker_pid": row["worker_pid"],
                "lease_token": row["lease_token"],
                "heartbeat_at": _parse_optional_timestamp(row["heartbeat_at"]),
                "lease_expires_at": _parse_optional_timestamp(row["lease_expires_at"]),
                "recovery_count": row["recovery_count"],
                "record_version": row["record_version"],
            }
            return AnalyticalTaskJobRecord.model_validate(payload)
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
            job_id_value = row["job_id"] if "job_id" in row.keys() else "<unknown>"
            raise AnalyticalTaskInvalidRecordDataError(
                detail=f"job_id={job_id_value}: {error}"
            ) from error

    def _row_to_runtime_lease_record(self, row: sqlite3.Row) -> ExecutionRuntimeLeaseRecord:
        try:
            payload: dict[str, Any] = {
                "runtime_instance_id": str(row["runtime_instance_id"]),
                "owner_pid": row["owner_pid"],
                "lease_token": str(row["lease_token"]),
                "acquired_at": parse_utc_datetime(str(row["acquired_at"])),
                "heartbeat_at": parse_utc_datetime(str(row["heartbeat_at"])),
                "lease_expires_at": parse_utc_datetime(str(row["lease_expires_at"])),
                "record_version": row["record_version"],
            }
            return ExecutionRuntimeLeaseRecord.model_validate(payload)
        except (TypeError, ValueError, ValidationError) as error:
            raise AnalyticalTaskInvalidRecordDataError(
                detail=f"invalid runtime lease record: {error}"
            ) from error

    def _ensure_database_parent_directory(self) -> None:
        try:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise AnalyticalTaskRegistryDatabaseUnavailableError(
                database_path=self._database_path,
                detail=str(error),
                sqlite_exception_type=type(error).__name__,
            ) from error

    def _open_connection(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self._database_path,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            self._apply_connection_pragmas(connection)
            return connection
        except sqlite3.Error as error:
            raise self._translate_sqlite_error(error) from error

    def _apply_connection_pragmas(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")

    def _read_pragma_int(self, connection: sqlite3.Connection, pragma_name: str) -> int:
        row = connection.execute(f"PRAGMA {pragma_name}").fetchone()
        if row is None:
            raise AnalyticalTaskRegistryIncompatibleSchemaError(
                database_path=self._database_path,
                detail=f"cannot read PRAGMA {pragma_name}",
            )
        return int(row[0])

    def _set_pragma_int(self, connection: sqlite3.Connection, pragma_name: str, value: int) -> None:
        connection.execute(f"PRAGMA {pragma_name} = {value}")

    def _list_user_tables(self, connection: sqlite3.Connection) -> Sequence[str]:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name ASC
            """
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _begin_immediate(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def _commit(self, connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("COMMIT")

    def _rollback(self, connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

    def _build_order_clause(self, order: AnalyticalTaskSortOrder) -> str:
        if order is AnalyticalTaskSortOrder.UPDATED_AT_DESC:
            return "updated_at DESC, task_id ASC"
        return "default_priority DESC, created_at ASC, task_id ASC"

    def _translate_task_integrity_error(
        self,
        error: sqlite3.IntegrityError,
        *,
        task_id: str,
        task_dir_relative_path: str,
        name: str | None,
    ) -> Exception:
        error_text = str(error)
        if "analytical_tasks.name" in error_text:
            return AnalyticalTaskAlreadyExistsError(
                field_name="name",
                field_value=name or "",
            )
        if "analytical_tasks.task_id" in error_text or "PRIMARY KEY" in error_text:
            return AnalyticalTaskAlreadyExistsError(field_name="task_id", field_value=task_id)
        if "analytical_tasks.task_dir_relative_path" in error_text:
            return AnalyticalTaskAlreadyExistsError(
                field_name="task_dir_relative_path",
                field_value=task_dir_relative_path,
            )
        return AnalyticalTaskInvalidRecordDataError(detail=error_text)

    def _translate_sqlite_error(self, error: sqlite3.Error) -> Exception:
        error_text = str(error)
        error_text_lower = error_text.lower()
        sqlite_exception_type = type(error).__name__
        if any(marker in error_text_lower for marker in _CORRUPTION_MARKERS):
            return AnalyticalTaskRegistryDatabaseCorruptedError(
                database_path=self._database_path,
                detail=error_text,
                sqlite_exception_type=sqlite_exception_type,
            )
        return AnalyticalTaskRegistryDatabaseUnavailableError(
            database_path=self._database_path,
            detail=error_text,
            sqlite_exception_type=sqlite_exception_type,
        )

    def _task_dir_from_relative_path(self, task_dir_relative_path: str) -> Path:
        return self._database_path.parent / "tasks" / task_dir_relative_path


def _normalize_optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        raise AnalyticalTaskInvalidRecordDataError(
            detail=f"{field_name} must not be empty when provided"
        )
    return normalized


def _normalize_required_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise AnalyticalTaskInvalidRecordDataError(detail=f"{field_name} must not be empty")
    return normalized


def _validate_transition_terminal_fields(
    *,
    to_state: AnalyticalTaskState,
    finished_reason: str | None,
    error_event_code: int | None,
) -> None:
    if to_state in TERMINAL_ANALYTICAL_TASK_JOB_STATES:
        if to_state in {AnalyticalTaskState.FAILED, AnalyticalTaskState.CANCELLED}:
            if finished_reason is None:
                raise AnalyticalTaskInvalidRecordDataError(
                    detail=f"finished_reason is required for state '{to_state.value}'"
                )
        if to_state is AnalyticalTaskState.FAILED:
            if error_event_code is not None and error_event_code <= 0:
                raise AnalyticalTaskInvalidRecordDataError(
                    detail="error_event_code must be a positive integer when provided"
                )
            return
        if error_event_code is not None:
            raise AnalyticalTaskInvalidRecordDataError(
                detail=f"error_event_code must be null for state '{to_state.value}'"
            )
        return

    if finished_reason is not None:
        raise AnalyticalTaskInvalidRecordDataError(
            detail=f"finished_reason is allowed only for terminal states, got '{to_state.value}'"
        )
    if error_event_code is not None:
        raise AnalyticalTaskInvalidRecordDataError(
            detail=f"error_event_code is allowed only for failed state, got '{to_state.value}'"
        )


def _build_job_transition_updates(
    *,
    job_record: AnalyticalTaskJobRecord,
    to_state: AnalyticalTaskState,
    finished_reason: str | None,
    error_event_code: int | None,
) -> dict[str, object]:
    now = utc_now()
    now_serialized = serialize_utc_datetime(now)

    queued_at = job_record.queued_at
    first_started_at = job_record.first_started_at
    last_started_at = job_record.last_started_at
    last_stopped_at = job_record.last_stopped_at
    finished_at: datetime | None = None
    stored_finished_reason: str | None = None
    stored_error_event_code: int | None = None
    worker_instance_id = job_record.worker_instance_id
    worker_pid = job_record.worker_pid
    lease_token = job_record.lease_token
    heartbeat_at = job_record.heartbeat_at
    lease_expires_at = job_record.lease_expires_at

    if to_state is AnalyticalTaskState.QUEUED:
        if queued_at is None:
            queued_at = now
    elif to_state is AnalyticalTaskState.RUNNING:
        if first_started_at is None:
            first_started_at = now
        last_started_at = now
    elif to_state in {AnalyticalTaskState.PAUSED, AnalyticalTaskState.WAITING}:
        if first_started_at is not None:
            last_stopped_at = now
        worker_instance_id = None
        worker_pid = None
        lease_token = None
        heartbeat_at = None
        lease_expires_at = None
    elif is_terminal_state(to_state):
        finished_at = now
        stored_finished_reason = finished_reason
        if first_started_at is not None:
            last_stopped_at = now
        if to_state is AnalyticalTaskState.FAILED:
            stored_error_event_code = error_event_code
        worker_instance_id = None
        worker_pid = None
        lease_token = None
        heartbeat_at = None
        lease_expires_at = None

    return {
        "state": to_state.value,
        "updated_at": now_serialized,
        "queued_at": _serialize_optional_timestamp(queued_at),
        "first_started_at": _serialize_optional_timestamp(first_started_at),
        "last_started_at": _serialize_optional_timestamp(last_started_at),
        "last_stopped_at": _serialize_optional_timestamp(last_stopped_at),
        "finished_at": _serialize_optional_timestamp(finished_at),
        "finished_reason": stored_finished_reason,
        "error_event_code": stored_error_event_code,
        "worker_instance_id": worker_instance_id,
        "worker_pid": worker_pid,
        "lease_token": lease_token,
        "heartbeat_at": _serialize_optional_timestamp(heartbeat_at),
        "lease_expires_at": _serialize_optional_timestamp(lease_expires_at),
    }


def _serialize_optional_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return serialize_utc_datetime(value)


def _parse_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return parse_utc_datetime(str(value))


def _serialize_runtime_state(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise AnalyticalTaskInvalidRecordDataError(
            detail=f"runtime_state serialization failed: {error}"
        ) from error


def _parse_runtime_state(*, raw_value: str) -> dict[str, Any]:
    parsed = json.loads(raw_value)
    if not isinstance(parsed, dict):
        raise ValueError("runtime_state_json must decode to an object")
    return parsed
