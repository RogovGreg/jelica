from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

from .registry_models import AnalyticalTaskState
from .storage import (
    TASK_CONFIG_FILENAME,
    TASK_CONFIGS_DIRNAME,
    TASK_JOBS_DIRNAME,
    canonicalize_config_document,
    compute_config_hash,
    config_revision_relative_path,
    serialize_config_document,
    write_text_atomically,
)

TASK_REGISTRY_APPLICATION_ID = 0x4A454C49
TASK_REGISTRY_SCHEMA_VERSION = 4
TASK_REGISTRY_TABLE_NAME = "analytical_tasks"
TASK_JOB_REGISTRY_TABLE_NAME = "analytical_task_jobs"
TASK_RUNTIME_LEASE_REGISTRY_TABLE_NAME = "execution_runtime_lease"

TASK_REGISTRY_EXPECTED_COLUMNS: tuple[str, ...] = (
    "task_id",
    "state",
    "default_priority",
    "current_config_revision",
    "current_config_relative_path",
    "current_config_hash",
    "active_job_id",
    "latest_job_id",
    "created_at",
    "updated_at",
    "task_dir_relative_path",
    "record_version",
    "name",
)

TASK_JOB_REGISTRY_EXPECTED_COLUMNS: tuple[str, ...] = (
    "job_id",
    "task_id",
    "config_revision",
    "config_relative_path",
    "config_hash",
    "state",
    "current_stage",
    "progress",
    "priority",
    "created_at",
    "queued_at",
    "first_started_at",
    "last_started_at",
    "last_stopped_at",
    "finished_at",
    "finished_reason",
    "error_event_code",
    "runtime_state_json",
    "worker_instance_id",
    "worker_pid",
    "lease_token",
    "heartbeat_at",
    "lease_expires_at",
    "recovery_count",
    "record_version",
)

TASK_RUNTIME_LEASE_REGISTRY_EXPECTED_COLUMNS: tuple[str, ...] = (
    "singleton_id",
    "runtime_instance_id",
    "owner_pid",
    "lease_token",
    "acquired_at",
    "heartbeat_at",
    "lease_expires_at",
    "record_version",
)

TASK_REGISTRY_EXPECTED_INDEXES: tuple[str, ...] = (
    "idx_analytical_tasks_state_default_priority_created_at",
    "idx_analytical_tasks_updated_at",
    "uq_analytical_tasks_name_nocase",
    "idx_analytical_task_jobs_task_id_created_at",
    "idx_analytical_task_jobs_state_priority_created_at",
    "uq_analytical_task_jobs_single_active",
)

_TASK_STATE_VALUES_SQL = ", ".join(f"'{state.value}'" for state in AnalyticalTaskState)
_JOB_STATE_VALUES_SQL = ", ".join(
    f"'{state.value}'"
    for state in AnalyticalTaskState
    if state is not AnalyticalTaskState.DELETION_REQUESTED
)
_ACTIVE_JOB_STATES_SQL = ", ".join(
    [
        f"'{AnalyticalTaskState.WAITING.value}'",
        f"'{AnalyticalTaskState.QUEUED.value}'",
        f"'{AnalyticalTaskState.RUNNING.value}'",
        f"'{AnalyticalTaskState.PAUSE_REQUESTED.value}'",
        f"'{AnalyticalTaskState.PREEMPTION_REQUESTED.value}'",
        f"'{AnalyticalTaskState.PAUSED.value}'",
        f"'{AnalyticalTaskState.CANCEL_REQUESTED.value}'",
    ]
)

_TASK_NAME_CHECK_SQL = """
name IS NULL OR (
    length(name) BETWEEN 1 AND 64
    AND substr(name, 1, 1) GLOB '[A-Za-z0-9]'
    AND name NOT GLOB '*[^A-Za-z0-9_-]*'
    AND NOT (
        (
            length(name) = 36
            AND substr(name, 9, 1) = '-'
            AND substr(name, 14, 1) = '-'
            AND substr(name, 19, 1) = '-'
            AND substr(name, 24, 1) = '-'
            AND length(replace(name, '-', '')) = 32
            AND replace(name, '-', '') NOT GLOB '*[^A-Fa-f0-9]*'
        )
        OR (
            length(name) = 32
            AND name NOT GLOB '*[^A-Fa-f0-9]*'
        )
    )
)
"""

CREATE_ANALYTICAL_TASKS_TABLE_SQL = f"""
CREATE TABLE {TASK_REGISTRY_TABLE_NAME} (
    task_id TEXT PRIMARY KEY CHECK (length(trim(task_id)) > 0),
    state TEXT NOT NULL DEFAULT '{AnalyticalTaskState.WAITING.value}' CHECK (
        state IN ({_TASK_STATE_VALUES_SQL})
    ),
    default_priority INTEGER NOT NULL DEFAULT 1 CHECK (default_priority >= 1),
    current_config_revision INTEGER NOT NULL CHECK (current_config_revision >= 1),
    current_config_relative_path TEXT NOT NULL CHECK (
        length(trim(current_config_relative_path)) > 0
    ),
    current_config_hash TEXT NOT NULL CHECK (length(trim(current_config_hash)) > 0),
    active_job_id TEXT,
    latest_job_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    task_dir_relative_path TEXT NOT NULL UNIQUE CHECK (
        length(trim(task_dir_relative_path)) > 0
    ),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
    name TEXT CHECK ({_TASK_NAME_CHECK_SQL}),
    CHECK (active_job_id IS NULL OR length(trim(active_job_id)) > 0),
    CHECK (latest_job_id IS NULL OR length(trim(latest_job_id)) > 0)
)
"""

CREATE_ANALYTICAL_TASK_JOBS_TABLE_SQL = f"""
CREATE TABLE {TASK_JOB_REGISTRY_TABLE_NAME} (
    job_id TEXT PRIMARY KEY CHECK (length(trim(job_id)) > 0),
    task_id TEXT NOT NULL CHECK (length(trim(task_id)) > 0),

    config_revision INTEGER NOT NULL CHECK (config_revision >= 1),
    config_relative_path TEXT NOT NULL CHECK (length(trim(config_relative_path)) > 0),
    config_hash TEXT NOT NULL CHECK (length(trim(config_hash)) > 0),

    state TEXT NOT NULL CHECK (
        state IN ({_JOB_STATE_VALUES_SQL})
    ),
    current_stage TEXT CHECK (current_stage IS NULL OR length(trim(current_stage)) > 0),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    priority INTEGER NOT NULL DEFAULT 1 CHECK (priority >= 1),

    created_at TEXT NOT NULL,
    queued_at TEXT,
    first_started_at TEXT,
    last_started_at TEXT,
    last_stopped_at TEXT,
    finished_at TEXT,

    finished_reason TEXT CHECK (finished_reason IS NULL OR length(trim(finished_reason)) > 0),
    error_event_code INTEGER CHECK (error_event_code IS NULL OR error_event_code > 0),

    runtime_state_json TEXT NOT NULL DEFAULT '{{}}' CHECK (
        json_valid(runtime_state_json) AND json_type(runtime_state_json) = 'object'
    ),

    worker_instance_id TEXT CHECK (
        worker_instance_id IS NULL OR length(trim(worker_instance_id)) > 0
    ),
    worker_pid INTEGER CHECK (worker_pid IS NULL OR worker_pid > 0),
    lease_token TEXT CHECK (lease_token IS NULL OR length(trim(lease_token)) > 0),
    heartbeat_at TEXT,
    lease_expires_at TEXT,

    recovery_count INTEGER NOT NULL DEFAULT 0 CHECK (recovery_count >= 0),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),

    FOREIGN KEY (task_id) REFERENCES {TASK_REGISTRY_TABLE_NAME}(task_id) ON DELETE CASCADE
)
"""

CREATE_STATE_PRIORITY_CREATED_AT_INDEX_SQL = f"""
CREATE INDEX idx_analytical_tasks_state_default_priority_created_at
ON {TASK_REGISTRY_TABLE_NAME} (state, default_priority DESC, created_at ASC)
"""

CREATE_UPDATED_AT_INDEX_SQL = f"""
CREATE INDEX idx_analytical_tasks_updated_at
ON {TASK_REGISTRY_TABLE_NAME} (updated_at DESC)
"""

CREATE_UNIQUE_TASK_NAME_INDEX_SQL = f"""
CREATE UNIQUE INDEX uq_analytical_tasks_name_nocase
ON {TASK_REGISTRY_TABLE_NAME} (name COLLATE NOCASE)
WHERE name IS NOT NULL
"""

CREATE_JOB_TASK_CREATED_AT_INDEX_SQL = f"""
CREATE INDEX idx_analytical_task_jobs_task_id_created_at
ON {TASK_JOB_REGISTRY_TABLE_NAME} (task_id, created_at DESC, job_id ASC)
"""

CREATE_JOB_STATE_PRIORITY_CREATED_AT_INDEX_SQL = f"""
CREATE INDEX idx_analytical_task_jobs_state_priority_created_at
ON {TASK_JOB_REGISTRY_TABLE_NAME} (state, priority DESC, created_at ASC, job_id ASC)
"""

CREATE_SINGLE_ACTIVE_JOB_INDEX_SQL = f"""
CREATE UNIQUE INDEX uq_analytical_task_jobs_single_active
ON {TASK_JOB_REGISTRY_TABLE_NAME}(task_id)
WHERE state IN ({_ACTIVE_JOB_STATES_SQL})
"""

CREATE_RUNTIME_LEASE_TABLE_SQL = f"""
CREATE TABLE {TASK_RUNTIME_LEASE_REGISTRY_TABLE_NAME} (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    runtime_instance_id TEXT NOT NULL CHECK (length(trim(runtime_instance_id)) > 0),
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    lease_token TEXT NOT NULL CHECK (length(trim(lease_token)) > 0),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1)
)
"""

_LEGACY_STATE_VALUES_SQL = ", ".join(
    f"'{state}'"
    for state in [
        "queued",
        "running",
        "pause_requested",
        "paused",
        "cancel_requested",
        "completed",
        "failed",
        "cancelled",
    ]
)

CREATE_ANALYTICAL_TASKS_TABLE_V1_SQL = f"""
CREATE TABLE {TASK_REGISTRY_TABLE_NAME} (
    task_id TEXT PRIMARY KEY CHECK (length(trim(task_id)) > 0),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (
        state IN ({_LEGACY_STATE_VALUES_SQL})
    ),
    current_stage TEXT CHECK (current_stage IS NULL OR length(trim(current_stage)) > 0),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    priority INTEGER NOT NULL DEFAULT 1 CHECK (priority >= 1),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    first_started_at TEXT,
    last_started_at TEXT,
    last_stopped_at TEXT,
    finished_at TEXT,

    finished_reason TEXT,
    error_event_code INTEGER,

    runtime_state_json TEXT NOT NULL DEFAULT '{{}}' CHECK (
        json_valid(runtime_state_json) AND json_type(runtime_state_json) = 'object'
    ),
    task_dir_relative_path TEXT NOT NULL UNIQUE CHECK (length(trim(task_dir_relative_path)) > 0),
    record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1)
)
"""

CREATE_STATE_PRIORITY_CREATED_AT_INDEX_V1_SQL = f"""
CREATE INDEX idx_analytical_tasks_state_priority_created_at
ON {TASK_REGISTRY_TABLE_NAME} (state, priority DESC, created_at ASC)
"""

CREATE_UPDATED_AT_INDEX_V1_SQL = f"""
CREATE INDEX idx_analytical_tasks_updated_at
ON {TASK_REGISTRY_TABLE_NAME} (updated_at DESC)
"""


def _create_schema_v2(connection: sqlite3.Connection) -> None:
    connection.execute(CREATE_ANALYTICAL_TASKS_TABLE_SQL)
    connection.execute(CREATE_ANALYTICAL_TASK_JOBS_TABLE_SQL)
    connection.execute(CREATE_STATE_PRIORITY_CREATED_AT_INDEX_SQL)
    connection.execute(CREATE_UPDATED_AT_INDEX_SQL)
    connection.execute(CREATE_JOB_TASK_CREATED_AT_INDEX_SQL)
    connection.execute(CREATE_JOB_STATE_PRIORITY_CREATED_AT_INDEX_SQL)
    connection.execute(CREATE_SINGLE_ACTIVE_JOB_INDEX_SQL)


def _create_schema_v3(connection: sqlite3.Connection) -> None:
    _create_schema_v2(connection)
    connection.execute(CREATE_RUNTIME_LEASE_TABLE_SQL)


def migrate_0_to_1(connection: sqlite3.Connection, _: Path) -> None:
    connection.execute(CREATE_ANALYTICAL_TASKS_TABLE_V1_SQL)
    connection.execute(CREATE_STATE_PRIORITY_CREATED_AT_INDEX_V1_SQL)
    connection.execute(CREATE_UPDATED_AT_INDEX_V1_SQL)


def migrate_1_to_2(connection: sqlite3.Connection, database_path: Path) -> None:
    legacy_rows = connection.execute(
        f"""
        SELECT
            task_id,
            priority,
            created_at,
            updated_at,
            task_dir_relative_path,
            record_version
        FROM {TASK_REGISTRY_TABLE_NAME}
        ORDER BY created_at ASC, task_id ASC
        """
    ).fetchall()

    connection.execute(f"ALTER TABLE {TASK_REGISTRY_TABLE_NAME} RENAME TO analytical_tasks_v1")
    connection.execute("DROP INDEX IF EXISTS idx_analytical_tasks_state_priority_created_at")
    connection.execute("DROP INDEX IF EXISTS idx_analytical_tasks_updated_at")
    _create_schema_v2(connection)

    tasks_dir = database_path.parent / "tasks"
    for legacy_row in legacy_rows:
        task_id = str(legacy_row["task_id"]).strip()
        task_dir_relative_path = str(legacy_row["task_dir_relative_path"]).strip()
        task_dir = tasks_dir / task_dir_relative_path
        revision_relative_path, config_hash = _ensure_initial_revision(
            task_id=task_id,
            task_dir=task_dir,
        )

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
                record_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                AnalyticalTaskState.WAITING.value,
                max(1, int(legacy_row["priority"])),
                1,
                revision_relative_path,
                config_hash,
                None,
                None,
                str(legacy_row["created_at"]),
                str(legacy_row["updated_at"]),
                task_dir_relative_path,
                max(1, int(legacy_row["record_version"])),
            ),
        )

    connection.execute("DROP TABLE analytical_tasks_v1")


def migrate_2_to_3(connection: sqlite3.Connection, _: Path) -> None:
    connection.execute(CREATE_RUNTIME_LEASE_TABLE_SQL)


def migrate_3_to_4(connection: sqlite3.Connection, _: Path) -> None:
    task_columns = {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({TASK_REGISTRY_TABLE_NAME})"
        ).fetchall()
    }
    if "name" not in task_columns:
        connection.execute(
            f"""
            ALTER TABLE {TASK_REGISTRY_TABLE_NAME}
            ADD COLUMN name TEXT CHECK ({_TASK_NAME_CHECK_SQL})
            """
        )
    connection.execute(CREATE_UNIQUE_TASK_NAME_INDEX_SQL)


def _ensure_initial_revision(*, task_id: str, task_dir: Path) -> tuple[str, str]:
    config_path = task_dir / TASK_CONFIG_FILENAME
    if not config_path.is_file():
        raise RuntimeError(f"missing normalized config for task '{task_id}' at '{config_path}'.")

    config_document = _load_json_document(path=config_path)
    revision_relative_path = config_revision_relative_path(1)
    revision_path = task_dir / Path(revision_relative_path)
    jobs_dir = task_dir / TASK_JOBS_DIRNAME
    configs_dir = task_dir / TASK_CONFIGS_DIRNAME

    configs_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    payload = serialize_config_document(config_document)
    if revision_path.exists():
        existing_document = _load_json_document(path=revision_path)
        if canonicalize_config_document(existing_document) != canonicalize_config_document(
            config_document
        ):
            raise RuntimeError(
                f"existing revision file '{revision_path}' is incompatible with config.json."
            )
    else:
        write_text_atomically(path=revision_path, payload=payload)

    write_text_atomically(path=config_path, payload=payload)
    return revision_relative_path, compute_config_hash(config_document)


def _load_json_document(*, path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot load JSON config '{path}': {error}") from error
    if not isinstance(loaded, dict):
        raise RuntimeError(f"config '{path}' must be a JSON object")
    return _normalize_mapping(loaded)


def _normalize_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}


MigrationCallable = Callable[[sqlite3.Connection, Path], None]

MIGRATIONS: dict[int, MigrationCallable] = {
    0: migrate_0_to_1,
    1: migrate_1_to_2,
    2: migrate_2_to_3,
    3: migrate_3_to_4,
}
