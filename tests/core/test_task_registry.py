from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jelica_core.config import AnalysisConfigInput, ResolvedAnalysisConfig, resolve_analysis_config
from jelica_core.tasks import (
    TASK_JOB_REGISTRY_EXPECTED_COLUMNS,
    TASK_JOB_REGISTRY_TABLE_NAME,
    TASK_REGISTRY_APPLICATION_ID,
    TASK_REGISTRY_EXPECTED_COLUMNS,
    TASK_REGISTRY_EXPECTED_INDEXES,
    TASK_REGISTRY_SCHEMA_VERSION,
    TASK_REGISTRY_TABLE_NAME,
    AnalyticalTaskAlreadyExistsError,
    AnalyticalTaskInvalidRecordDataError,
    AnalyticalTaskMutationResult,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskRecord,
    AnalyticalTaskRegistry,
    AnalyticalTaskRegistryDatabaseCorruptedError,
    AnalyticalTaskRegistryDatabaseUnavailableError,
    AnalyticalTaskRegistryForeignDatabaseError,
    AnalyticalTaskRegistryIncompatibleSchemaError,
    AnalyticalTaskRegistryMigrationError,
    AnalyticalTaskRegistryService,
    AnalyticalTaskRegistryUnsupportedSchemaVersionError,
    AnalyticalTaskSortOrder,
    AnalyticalTaskState,
    LocalTaskStorage,
    registry_schema,
)


def _build_registry_service(
    tmp_path: Path,
) -> tuple[AnalyticalTaskRegistry, AnalyticalTaskRegistryService, Path, Path]:
    database_path = tmp_path / "data" / "jelica.db"
    tasks_dir = database_path.parent / "tasks"
    registry = AnalyticalTaskRegistry(database_path=database_path)
    service = AnalyticalTaskRegistryService(database_path=database_path, registry=registry)
    return registry, service, database_path, tasks_dir


def _resolved_config(*, sample_name: str, priority: int = 1) -> ResolvedAnalysisConfig:
    resolution = resolve_analysis_config(
        AnalysisConfigInput(samples=[sample_name], priority=priority)
    )
    return resolution.config


def _create_registered_task(
    *,
    service: AnalyticalTaskRegistryService,
    tasks_dir: Path,
    task_id: str,
    priority: int = 1,
) -> AnalyticalTaskRecord:
    storage = LocalTaskStorage(tasks_dir=tasks_dir)
    workspace = storage.create_task_workspace(
        task_id=task_id,
        config=_resolved_config(sample_name=f"{task_id}.fasta", priority=priority),
    )
    return service.register_task(
        task_id=task_id,
        task_dir_relative_path=task_id,
        default_priority=priority,
        current_config_revision=workspace.current_config_revision,
        current_config_relative_path=workspace.current_config_relative_path,
        current_config_hash=workspace.current_config_hash,
    )


def _transition_state(
    *,
    service: AnalyticalTaskRegistryService,
    task_id: str,
    to_state: AnalyticalTaskState,
) -> AnalyticalTaskMutationResult:
    finished_reason: str | None = None
    error_event_code: int | None = None
    if to_state is AnalyticalTaskState.CANCELLED:
        finished_reason = "cancelled"
    elif to_state is AnalyticalTaskState.FAILED:
        finished_reason = "failed"
        error_event_code = 2011
    return service.transition_active_job_state(
        task_id=task_id,
        to_state=to_state,
        finished_reason=finished_reason,
        error_event_code=error_event_code,
    )


def _prepare_task_with_active_job_state(
    *,
    service: AnalyticalTaskRegistryService,
    tasks_dir: Path,
    task_id: str,
    state: AnalyticalTaskState,
) -> AnalyticalTaskMutationResult:
    _create_registered_task(service=service, tasks_dir=tasks_dir, task_id=task_id)
    current = service.start(task_id=task_id)
    assert current.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert current.task is not None
    assert current.job is not None

    path_by_state: dict[AnalyticalTaskState, tuple[AnalyticalTaskState, ...]] = {
        AnalyticalTaskState.QUEUED: (),
        AnalyticalTaskState.RUNNING: (AnalyticalTaskState.RUNNING,),
        AnalyticalTaskState.PAUSE_REQUESTED: (
            AnalyticalTaskState.RUNNING,
            AnalyticalTaskState.PAUSE_REQUESTED,
        ),
        AnalyticalTaskState.PREEMPTION_REQUESTED: (
            AnalyticalTaskState.RUNNING,
            AnalyticalTaskState.PREEMPTION_REQUESTED,
        ),
        AnalyticalTaskState.PAUSED: (AnalyticalTaskState.PAUSED,),
        AnalyticalTaskState.CANCEL_REQUESTED: (
            AnalyticalTaskState.RUNNING,
            AnalyticalTaskState.CANCEL_REQUESTED,
        ),
        AnalyticalTaskState.WAITING: (
            AnalyticalTaskState.RUNNING,
            AnalyticalTaskState.PREEMPTION_REQUESTED,
            AnalyticalTaskState.WAITING,
        ),
    }

    for next_state in path_by_state[state]:
        current = _transition_state(service=service, task_id=task_id, to_state=next_state)
        assert current.result_type is AnalyticalTaskMutationResultType.APPLIED
        assert current.task is not None
        assert current.job is not None

    assert current.job is not None
    assert current.job.state is state
    return current


def _create_v1_task_registry_with_single_task(
    *,
    database_path: Path,
    task_id: str,
    priority: int,
    with_config: bool,
) -> None:
    tasks_dir = database_path.parent / "tasks"
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    if with_config:
        config_path = task_dir / "config.json"
        config_path.write_text(
            json.dumps({"schema_version": 1, "samples": ["legacy.fasta"], "priority": priority}),
            encoding="utf-8",
        )

    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA application_id = {TASK_REGISTRY_APPLICATION_ID}")
        connection.execute("PRAGMA user_version = 1")
        connection.execute(registry_schema.CREATE_ANALYTICAL_TASKS_TABLE_V1_SQL)
        connection.execute(registry_schema.CREATE_STATE_PRIORITY_CREATED_AT_INDEX_V1_SQL)
        connection.execute(registry_schema.CREATE_UPDATED_AT_INDEX_V1_SQL)
        connection.execute(
            f"""
            INSERT INTO {TASK_REGISTRY_TABLE_NAME} (
                task_id,
                state,
                current_stage,
                progress,
                priority,
                created_at,
                updated_at,
                first_started_at,
                last_started_at,
                last_stopped_at,
                finished_at,
                finished_reason,
                error_event_code,
                runtime_state_json,
                task_dir_relative_path,
                record_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                "queued",
                None,
                0,
                priority,
                "2026-07-30T10:00:00.000000Z",
                "2026-07-30T10:00:00.000000Z",
                None,
                None,
                None,
                None,
                None,
                None,
                "{}",
                task_id,
                1,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _create_non_jelica_database(
    *,
    database_path: Path,
    application_id: int,
    user_version: int,
) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(f"PRAGMA application_id = {application_id}")
        connection.execute(f"PRAGMA user_version = {user_version}")
        connection.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel (payload) VALUES ('keep-me')")
        connection.commit()
    finally:
        connection.close()


def _read_database_identity(database_path: Path) -> tuple[int, int, tuple[str, ...]]:
    connection = sqlite3.connect(database_path)
    try:
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name ASC
                """
            ).fetchall()
        )
    finally:
        connection.close()
    return app_id, user_version, tables


def _register_task_directly(
    *,
    service: AnalyticalTaskRegistryService,
    task_id: str,
    task_dir_relative_path: str,
    default_priority: int = 1,
    current_config_relative_path: str = "configs/000001.json",
    current_config_hash: str = "a" * 64,
) -> AnalyticalTaskRecord:
    return service.register_task(
        task_id=task_id,
        task_dir_relative_path=task_dir_relative_path,
        default_priority=default_priority,
        current_config_revision=1,
        current_config_relative_path=current_config_relative_path,
        current_config_hash=current_config_hash,
    )


def test_ensure_schema_creates_expected_database_identity_and_layout(tmp_path: Path) -> None:
    registry, _, database_path, _ = _build_registry_service(tmp_path)

    registry.ensure_schema()

    connection = sqlite3.connect(database_path)
    try:
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        task_columns = tuple(
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({TASK_REGISTRY_TABLE_NAME})")
        )
        job_columns = tuple(
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({TASK_JOB_REGISTRY_TABLE_NAME})")
        )
        task_indexes = {
            str(row[1])
            for row in connection.execute(f"PRAGMA index_list({TASK_REGISTRY_TABLE_NAME})")
        }
        job_indexes = {
            str(row[1])
            for row in connection.execute(f"PRAGMA index_list({TASK_JOB_REGISTRY_TABLE_NAME})")
        }
    finally:
        connection.close()

    assert app_id == TASK_REGISTRY_APPLICATION_ID
    assert user_version == TASK_REGISTRY_SCHEMA_VERSION
    assert task_columns == TASK_REGISTRY_EXPECTED_COLUMNS
    assert job_columns == TASK_JOB_REGISTRY_EXPECTED_COLUMNS
    assert set(TASK_REGISTRY_EXPECTED_INDEXES).issubset(task_indexes | job_indexes)


def test_ensure_schema_is_idempotent_and_preserves_registered_records(tmp_path: Path) -> None:
    registry, service, _, tasks_dir = _build_registry_service(tmp_path)
    _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1", priority=3)

    registry.ensure_schema()
    reloaded = service.get_task(task_id="task-1")

    assert reloaded.task_id == "task-1"
    assert reloaded.default_priority == 3


def test_ensure_schema_supports_concurrent_initialization(tmp_path: Path) -> None:
    registry, _, _, _ = _build_registry_service(tmp_path)

    failures: list[Exception] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(registry.ensure_schema)
        second = pool.submit(registry.ensure_schema)
        for future in (first, second):
            try:
                future.result()
            except Exception as error:  # pragma: no cover - safety net for thread handoff
                failures.append(error)

    for failure in failures:
        assert isinstance(failure, AnalyticalTaskRegistryDatabaseUnavailableError)
        assert "database is locked" in str(failure)

    registry.validate_schema()


def test_ensure_schema_rejects_foreign_application_id_without_mutation(tmp_path: Path) -> None:
    registry, _, database_path, _ = _build_registry_service(tmp_path)
    _create_non_jelica_database(
        database_path=database_path,
        application_id=99_999,
        user_version=7,
    )
    before_app_id, before_version, before_tables = _read_database_identity(database_path)

    with pytest.raises(AnalyticalTaskRegistryForeignDatabaseError):
        registry.ensure_schema()

    after_app_id, after_version, after_tables = _read_database_identity(database_path)
    assert after_app_id == before_app_id
    assert after_version == before_version
    assert after_tables == before_tables

    connection = sqlite3.connect(database_path)
    try:
        payload = connection.execute("SELECT payload FROM sentinel").fetchone()
    finally:
        connection.close()
    assert payload is not None
    assert str(payload[0]) == "keep-me"


def test_ensure_schema_rejects_newer_schema_version_without_downgrade(tmp_path: Path) -> None:
    registry, _, database_path, _ = _build_registry_service(tmp_path)
    _create_non_jelica_database(
        database_path=database_path,
        application_id=TASK_REGISTRY_APPLICATION_ID,
        user_version=TASK_REGISTRY_SCHEMA_VERSION + 3,
    )
    before_app_id, before_version, before_tables = _read_database_identity(database_path)

    with pytest.raises(AnalyticalTaskRegistryUnsupportedSchemaVersionError):
        registry.ensure_schema()

    after_app_id, after_version, after_tables = _read_database_identity(database_path)
    assert after_app_id == before_app_id
    assert after_version == before_version
    assert after_tables == before_tables

    connection = sqlite3.connect(database_path)
    try:
        payload = connection.execute("SELECT payload FROM sentinel").fetchone()
    finally:
        connection.close()
    assert payload is not None
    assert str(payload[0]) == "keep-me"


def test_ensure_schema_rejects_unavailable_parent_path_without_partial_file(
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not-a-directory", encoding="utf-8")
    database_path = blocked_parent / "jelica.db"
    registry = AnalyticalTaskRegistry(database_path=database_path)

    with pytest.raises(AnalyticalTaskRegistryDatabaseUnavailableError):
        registry.ensure_schema()

    assert blocked_parent.is_file()
    assert not database_path.exists()


def test_validate_schema_rejects_wrong_application_id(tmp_path: Path) -> None:
    registry, _, database_path, _ = _build_registry_service(tmp_path)
    _create_non_jelica_database(
        database_path=database_path,
        application_id=54_321,
        user_version=TASK_REGISTRY_SCHEMA_VERSION,
    )

    with pytest.raises(AnalyticalTaskRegistryForeignDatabaseError):
        registry.validate_schema()


def test_validate_schema_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    registry, _, database_path, _ = _build_registry_service(tmp_path)
    _create_non_jelica_database(
        database_path=database_path,
        application_id=TASK_REGISTRY_APPLICATION_ID,
        user_version=TASK_REGISTRY_SCHEMA_VERSION + 1,
    )

    with pytest.raises(AnalyticalTaskRegistryUnsupportedSchemaVersionError):
        registry.validate_schema()


def test_validate_schema_rejects_missing_required_table(tmp_path: Path) -> None:
    registry, _, database_path, _ = _build_registry_service(tmp_path)
    registry.ensure_schema()

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(f"DROP TABLE {TASK_JOB_REGISTRY_TABLE_NAME}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AnalyticalTaskRegistryIncompatibleSchemaError):
        registry.validate_schema()


def test_validate_schema_rejects_missing_required_column(tmp_path: Path) -> None:
    registry, _, database_path, _ = _build_registry_service(tmp_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(f"PRAGMA application_id = {TASK_REGISTRY_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {TASK_REGISTRY_SCHEMA_VERSION}")
        connection.execute(
            f"""
            CREATE TABLE {TASK_REGISTRY_TABLE_NAME} (
                task_id TEXT PRIMARY KEY
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE {TASK_JOB_REGISTRY_TABLE_NAME} (
                job_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AnalyticalTaskRegistryIncompatibleSchemaError):
        registry.validate_schema()


def test_validate_schema_rejects_missing_required_index(tmp_path: Path) -> None:
    registry, _, database_path, _ = _build_registry_service(tmp_path)
    registry.ensure_schema()

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("DROP INDEX uq_analytical_task_jobs_single_active")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AnalyticalTaskRegistryIncompatibleSchemaError):
        registry.validate_schema()


def test_validate_schema_detects_quick_check_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _, _, _ = _build_registry_service(tmp_path)
    registry.ensure_schema()

    def _failing_quick_check(connection: sqlite3.Connection) -> None:
        raise AnalyticalTaskRegistryDatabaseCorruptedError(
            database_path=registry.database_path,
            detail="quick_check failed: malformed",
        )

    monkeypatch.setattr(registry, "_run_quick_check", _failing_quick_check)

    with pytest.raises(AnalyticalTaskRegistryDatabaseCorruptedError):
        registry.validate_schema()


def test_migration_from_v1_preserves_task_and_bootstraps_revision(tmp_path: Path) -> None:
    _, service, database_path, tasks_dir = _build_registry_service(tmp_path)
    _create_v1_task_registry_with_single_task(
        database_path=database_path,
        task_id="legacy-task",
        priority=7,
        with_config=True,
    )

    service.registry.ensure_schema()
    migrated = service.get_task(task_id="legacy-task")
    migrated_task_dir = tasks_dir / "legacy-task"
    revision_path = migrated_task_dir / "configs" / "000001.json"

    assert migrated.state is AnalyticalTaskState.WAITING
    assert migrated.name is None
    assert migrated.default_priority == 7
    assert migrated.active_job_id is None
    assert migrated.latest_job_id is None
    assert migrated.current_config_revision == 1
    assert migrated.current_config_relative_path == "configs/000001.json"
    assert len(migrated.current_config_hash) == 64
    assert revision_path.is_file()


def test_migration_from_v3_adds_nullable_task_name_without_losing_tasks(
    tmp_path: Path,
) -> None:
    registry, service, database_path, _ = _build_registry_service(tmp_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    current_sql = registry_schema.CREATE_ANALYTICAL_TASKS_TABLE_SQL
    name_column_start = current_sql.index("\n    name TEXT CHECK (")
    following_column_start = current_sql.index(
        "\n    CHECK (active_job_id",
        name_column_start,
    )
    legacy_task_sql = (
        current_sql[:name_column_start] + current_sql[following_column_start:]
    )

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(f"PRAGMA application_id = {TASK_REGISTRY_APPLICATION_ID}")
        connection.execute("PRAGMA user_version = 3")
        for statement in (
            legacy_task_sql,
            registry_schema.CREATE_ANALYTICAL_TASK_JOBS_TABLE_SQL,
            registry_schema.CREATE_STATE_PRIORITY_CREATED_AT_INDEX_SQL,
            registry_schema.CREATE_UPDATED_AT_INDEX_SQL,
            registry_schema.CREATE_JOB_TASK_CREATED_AT_INDEX_SQL,
            registry_schema.CREATE_JOB_STATE_PRIORITY_CREATED_AT_INDEX_SQL,
            registry_schema.CREATE_SINGLE_ACTIVE_JOB_INDEX_SQL,
            registry_schema.CREATE_RUNTIME_LEASE_TABLE_SQL,
        ):
            connection.execute(statement)
        connection.execute(
            f"""
            INSERT INTO {TASK_REGISTRY_TABLE_NAME} (
                task_id, state, default_priority, current_config_revision,
                current_config_relative_path, current_config_hash,
                active_job_id, latest_job_id, created_at, updated_at,
                task_dir_relative_path, record_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-v3-task",
                AnalyticalTaskState.WAITING.value,
                1,
                1,
                "configs/000001.json",
                "a" * 64,
                None,
                None,
                "2026-08-20T10:00:00.000000Z",
                "2026-08-20T10:00:00.000000Z",
                "legacy-v3-task",
                1,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    registry.ensure_schema()

    migrated = service.get_task(task_id="legacy-v3-task")
    assert migrated.name is None
    connection = sqlite3.connect(database_path)
    try:
        indexes = {
            str(row[1])
            for row in connection.execute(f"PRAGMA index_list({TASK_REGISTRY_TABLE_NAME})")
        }
    finally:
        connection.close()
    assert "uq_analytical_tasks_name_nocase" in indexes


def test_migration_rolls_back_when_config_file_missing(tmp_path: Path) -> None:
    registry, _, database_path, _ = _build_registry_service(tmp_path)
    _create_v1_task_registry_with_single_task(
        database_path=database_path,
        task_id="broken-legacy-task",
        priority=3,
        with_config=False,
    )

    with pytest.raises(AnalyticalTaskRegistryMigrationError):
        registry.ensure_schema()

    connection = sqlite3.connect(database_path)
    try:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        columns = tuple(
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({TASK_REGISTRY_TABLE_NAME})")
        )
    finally:
        connection.close()

    assert user_version == 1
    assert "priority" in columns
    assert "default_priority" not in columns


def test_execution_runtime_lease_acquire_heartbeat_and_release(tmp_path: Path) -> None:
    _, service, _, _ = _build_registry_service(tmp_path)

    acquired, conflict = service.acquire_execution_runtime_lease(
        runtime_instance_id="runtime-a",
        owner_pid=101,
        lease_token="token-a",
        lease_timeout_seconds=5.0,
    )

    assert acquired is not None
    assert conflict is None
    assert acquired.runtime_instance_id == "runtime-a"

    second_acquired, second_conflict = service.acquire_execution_runtime_lease(
        runtime_instance_id="runtime-b",
        owner_pid=202,
        lease_token="token-b",
        lease_timeout_seconds=5.0,
    )
    assert second_acquired is None
    assert second_conflict is not None
    assert second_conflict.runtime_instance_id == "runtime-a"

    heartbeat = service.heartbeat_execution_runtime_lease(
        runtime_instance_id="runtime-a",
        lease_token="token-a",
        lease_timeout_seconds=5.0,
    )
    assert heartbeat is not None
    assert heartbeat.record_version > acquired.record_version

    stale_heartbeat = service.heartbeat_execution_runtime_lease(
        runtime_instance_id="runtime-a",
        lease_token="stale-token",
        lease_timeout_seconds=5.0,
    )
    assert stale_heartbeat is None

    released = service.release_execution_runtime_lease(
        runtime_instance_id="runtime-a",
        lease_token="token-a",
    )
    assert released is True
    assert service.get_execution_runtime_lease() is None


def test_execution_runtime_lease_takeover_after_expiry(tmp_path: Path) -> None:
    registry, service, _, _ = _build_registry_service(tmp_path)
    base_time = datetime(2026, 7, 30, 15, 0, 0, tzinfo=UTC)

    acquired, conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id="runtime-a",
        owner_pid=101,
        lease_token="token-a",
        lease_timeout_seconds=5.0,
        now=base_time,
    )
    assert acquired is not None
    assert conflict is None

    still_locked, current_owner = registry.acquire_execution_runtime_lease(
        runtime_instance_id="runtime-b",
        owner_pid=202,
        lease_token="token-b",
        lease_timeout_seconds=5.0,
        now=base_time + timedelta(seconds=1),
    )
    assert still_locked is None
    assert current_owner is not None
    assert current_owner.runtime_instance_id == "runtime-a"

    takeover, takeover_conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id="runtime-b",
        owner_pid=202,
        lease_token="token-b",
        lease_timeout_seconds=5.0,
        now=base_time + timedelta(seconds=6),
    )
    assert takeover is not None
    assert takeover_conflict is None
    assert takeover.runtime_instance_id == "runtime-b"

    stale_release = service.release_execution_runtime_lease(
        runtime_instance_id="runtime-a",
        lease_token="token-a",
    )
    assert stale_release is False


def test_claim_next_queued_job_uses_priority_order_and_sets_worker_lease(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    _create_registered_task(
        service=service,
        tasks_dir=tasks_dir,
        task_id="task-low-priority",
        priority=1,
    )
    _create_registered_task(
        service=service,
        tasks_dir=tasks_dir,
        task_id="task-high-priority",
        priority=1,
    )
    service.start(task_id="task-low-priority", priority=2)
    service.start(task_id="task-high-priority", priority=5)

    first_claim = service.claim_next_queued_job_for_worker(
        worker_instance_id="worker-a",
        lease_token="lease-a",
        lease_timeout_seconds=5.0,
    )
    assert first_claim is not None
    first_task, first_job = first_claim
    assert first_task.task_id == "task-high-priority"
    assert first_job.state is AnalyticalTaskState.RUNNING
    assert first_job.worker_instance_id == "worker-a"
    assert first_job.lease_token == "lease-a"
    assert first_job.first_started_at is not None
    assert first_job.last_started_at is not None

    assert service.count_running_jobs() == 1
    assert service.count_queued_jobs() == 1

    second_claim = service.claim_next_queued_job_for_worker(
        worker_instance_id="worker-b",
        lease_token="lease-b",
        lease_timeout_seconds=5.0,
    )
    assert second_claim is not None
    second_task, second_job = second_claim
    assert second_task.task_id == "task-low-priority"
    assert second_job.state is AnalyticalTaskState.RUNNING
    assert second_job.worker_instance_id == "worker-b"
    assert second_job.lease_token == "lease-b"


def test_create_task_without_job_initializes_waiting_projection(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(
        service=service,
        tasks_dir=tasks_dir,
        task_id="task-1",
        priority=4,
    )

    assert created.state is AnalyticalTaskState.WAITING
    assert created.default_priority == 4
    assert created.active_job_id is None
    assert created.latest_job_id is None
    assert created.current_config_revision == 1
    assert created.current_config_relative_path == "configs/000001.json"


def test_register_task_rejects_invalid_identifiers_and_priority(tmp_path: Path) -> None:
    _, service, _, _ = _build_registry_service(tmp_path)

    with pytest.raises(AnalyticalTaskInvalidRecordDataError):
        _register_task_directly(
            service=service,
            task_id=" ",
            task_dir_relative_path="task-1",
        )
    with pytest.raises(AnalyticalTaskInvalidRecordDataError):
        _register_task_directly(
            service=service,
            task_id="task-1",
            task_dir_relative_path="task-1",
            default_priority=0,
        )


@pytest.mark.parametrize(
    "invalid_relative_path",
    ["/abs/task-1", r"C:\\tasks\\task-1", " ", "../task-1"],
)
def test_register_task_rejects_invalid_task_dir_relative_path(
    tmp_path: Path,
    invalid_relative_path: str,
) -> None:
    _, service, _, _ = _build_registry_service(tmp_path)

    with pytest.raises(AnalyticalTaskInvalidRecordDataError):
        _register_task_directly(
            service=service,
            task_id="task-1",
            task_dir_relative_path=invalid_relative_path,
        )


@pytest.mark.parametrize(
    "invalid_config_relative_path",
    ["/abs/config.json", r"C:\\configs\\000001.json", " ", "../configs/000001.json"],
)
def test_register_task_rejects_invalid_current_config_relative_path(
    tmp_path: Path,
    invalid_config_relative_path: str,
) -> None:
    _, service, _, _ = _build_registry_service(tmp_path)

    with pytest.raises(AnalyticalTaskInvalidRecordDataError):
        _register_task_directly(
            service=service,
            task_id="task-1",
            task_dir_relative_path="task-1",
            current_config_relative_path=invalid_config_relative_path,
        )


def test_register_task_rejects_duplicate_task_id_and_task_directory(tmp_path: Path) -> None:
    _, service, _, _ = _build_registry_service(tmp_path)
    _register_task_directly(
        service=service,
        task_id="task-1",
        task_dir_relative_path="task-1",
    )

    with pytest.raises(AnalyticalTaskAlreadyExistsError):
        _register_task_directly(
            service=service,
            task_id="task-1",
            task_dir_relative_path="task-2",
        )
    with pytest.raises(AnalyticalTaskAlreadyExistsError):
        _register_task_directly(
            service=service,
            task_id="task-2",
            task_dir_relative_path="task-1",
        )


def test_start_rejects_invalid_job_priority_value(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")

    with pytest.raises(AnalyticalTaskInvalidRecordDataError):
        service.start(task_id=created.task_id, priority=0)


def test_job_identifier_constraints_reject_empty_and_duplicate_job_id(tmp_path: Path) -> None:
    _, service, database_path, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    started = service.start(task_id=created.task_id)
    assert started.job is not None

    connection = sqlite3.connect(database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"""
                INSERT INTO {TASK_JOB_REGISTRY_TABLE_NAME}
                SELECT * FROM {TASK_JOB_REGISTRY_TABLE_NAME}
                WHERE job_id = ?
                """,
                (started.job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
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
                    "",
                    created.task_id,
                    1,
                    "configs/000001.json",
                    "a" * 64,
                    AnalyticalTaskState.COMPLETED.value,
                    None,
                    0,
                    1,
                    "2026-07-30T10:00:00.000000Z",
                    "2026-07-30T10:00:00.000000Z",
                    None,
                    None,
                    None,
                    "2026-07-30T10:00:01.000000Z",
                    None,
                    None,
                    "{}",
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    1,
                ),
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "invalid_config_relative_path",
    ["/abs/config.json", r"C:\\configs\\000001.json", "../configs/000001.json"],
)
def test_get_job_rejects_invalid_persisted_config_relative_path(
    tmp_path: Path,
    invalid_config_relative_path: str,
) -> None:
    _, service, database_path, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    started = service.start(task_id=created.task_id)
    assert started.job is not None

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            f"""
            UPDATE {TASK_JOB_REGISTRY_TABLE_NAME}
            SET config_relative_path = ?
            WHERE job_id = ?
            """,
            (invalid_config_relative_path, started.job.job_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AnalyticalTaskInvalidRecordDataError):
        service.get_job(job_id=started.job.job_id)


def test_start_creates_first_job_and_is_idempotent_for_active_job(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")

    first_start = service.start(task_id=created.task_id)
    second_start = service.start(task_id=created.task_id)

    assert first_start.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert first_start.task is not None
    assert first_start.job is not None
    assert first_start.task.state is AnalyticalTaskState.QUEUED
    assert first_start.task.active_job_id == first_start.job.job_id
    assert first_start.task.latest_job_id == first_start.job.job_id
    assert first_start.job.priority == created.default_priority
    assert first_start.job.config_revision == created.current_config_revision
    assert first_start.job.config_relative_path == created.current_config_relative_path
    assert first_start.job.config_hash == created.current_config_hash

    assert second_start.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED
    assert second_start.job is not None
    assert second_start.job.job_id == first_start.job.job_id


def test_start_seeds_new_job_with_requested_id_and_runtime_state(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    runtime_state = {
        "checkpoint": {
            "pipeline_version": "2026.08.21",
            "completed_stages": {
                "alignment": {
                    "artifacts": ["alignment/alignment_manifest.json"],
                }
            },
        }
    }

    started = service.start(
        task_id=created.task_id,
        requested_job_id="seeded-job-id",
        runtime_state=runtime_state,
    )
    assert started.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert started.job is not None
    assert started.job.job_id == "seeded-job-id"
    assert started.job.runtime_state == runtime_state

    reloaded = service.get_job(job_id="seeded-job-id")
    assert reloaded.runtime_state == runtime_state


def test_start_rejects_empty_requested_job_id(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")

    with pytest.raises(AnalyticalTaskInvalidRecordDataError, match="requested_job_id"):
        service.start(task_id=created.task_id, requested_job_id="   ")


def test_start_creates_new_job_after_failed_and_cancelled(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    first = service.start(task_id="task-1")
    assert first.job is not None

    running = _transition_state(
        service=service,
        task_id="task-1",
        to_state=AnalyticalTaskState.RUNNING,
    )
    assert running.job is not None
    failed = _transition_state(
        service=service,
        task_id="task-1",
        to_state=AnalyticalTaskState.FAILED,
    )
    assert failed.job is not None
    restart_after_failed = service.start(task_id="task-1")

    assert restart_after_failed.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert restart_after_failed.job is not None
    assert restart_after_failed.job.job_id != failed.job.job_id

    cancelled = service.cancel(task_id="task-1")
    assert cancelled.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert cancelled.job is not None
    assert cancelled.job.state is AnalyticalTaskState.CANCELLED
    restart_after_cancelled = service.start(task_id="task-1")

    assert restart_after_cancelled.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert restart_after_cancelled.job is not None
    assert restart_after_cancelled.job.job_id != cancelled.job.job_id


def test_start_after_completed_is_blocked(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    service.start(task_id="task-1")
    _transition_state(service=service, task_id="task-1", to_state=AnalyticalTaskState.RUNNING)
    completed = _transition_state(
        service=service,
        task_id="task-1",
        to_state=AnalyticalTaskState.COMPLETED,
    )
    assert completed.result_type is AnalyticalTaskMutationResultType.APPLIED

    restart = service.start(task_id="task-1")
    assert restart.result_type is AnalyticalTaskMutationResultType.INVALID_TRANSITION


def test_concurrent_start_creates_single_active_job(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(service.start, task_id="task-1")
        second_future = pool.submit(service.start, task_id="task-1")
        first = first_future.result()
        second = second_future.result()

    result_types = {first.result_type, second.result_type}
    assert result_types == {
        AnalyticalTaskMutationResultType.APPLIED,
        AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
    }
    assert first.job is not None
    assert second.job is not None
    assert first.job.job_id == second.job.job_id

    snapshots = service.list_task_snapshots(limit=10, order=AnalyticalTaskSortOrder.UPDATED_AT_DESC)
    assert len(snapshots) == 1
    assert snapshots[0].task.active_job_id == first.job.job_id


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (AnalyticalTaskState.WAITING, AnalyticalTaskState.QUEUED),
        (AnalyticalTaskState.WAITING, AnalyticalTaskState.PAUSED),
        (AnalyticalTaskState.WAITING, AnalyticalTaskState.CANCELLED),
        (AnalyticalTaskState.QUEUED, AnalyticalTaskState.PAUSED),
        (AnalyticalTaskState.QUEUED, AnalyticalTaskState.CANCELLED),
        (AnalyticalTaskState.RUNNING, AnalyticalTaskState.PAUSE_REQUESTED),
        (AnalyticalTaskState.RUNNING, AnalyticalTaskState.PREEMPTION_REQUESTED),
        (AnalyticalTaskState.RUNNING, AnalyticalTaskState.CANCEL_REQUESTED),
        (AnalyticalTaskState.RUNNING, AnalyticalTaskState.COMPLETED),
        (AnalyticalTaskState.RUNNING, AnalyticalTaskState.FAILED),
        (AnalyticalTaskState.PAUSE_REQUESTED, AnalyticalTaskState.PAUSED),
        (AnalyticalTaskState.PAUSE_REQUESTED, AnalyticalTaskState.CANCEL_REQUESTED),
        (AnalyticalTaskState.PAUSE_REQUESTED, AnalyticalTaskState.COMPLETED),
        (AnalyticalTaskState.PAUSE_REQUESTED, AnalyticalTaskState.FAILED),
        (AnalyticalTaskState.PREEMPTION_REQUESTED, AnalyticalTaskState.WAITING),
        (AnalyticalTaskState.PREEMPTION_REQUESTED, AnalyticalTaskState.PAUSE_REQUESTED),
        (AnalyticalTaskState.PREEMPTION_REQUESTED, AnalyticalTaskState.CANCEL_REQUESTED),
        (AnalyticalTaskState.PREEMPTION_REQUESTED, AnalyticalTaskState.COMPLETED),
        (AnalyticalTaskState.PREEMPTION_REQUESTED, AnalyticalTaskState.FAILED),
        (AnalyticalTaskState.PAUSED, AnalyticalTaskState.QUEUED),
        (AnalyticalTaskState.PAUSED, AnalyticalTaskState.CANCELLED),
        (AnalyticalTaskState.CANCEL_REQUESTED, AnalyticalTaskState.CANCELLED),
    ],
)
def test_state_machine_transition_matrix_is_supported(
    tmp_path: Path,
    from_state: AnalyticalTaskState,
    to_state: AnalyticalTaskState,
) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    prepared = _prepare_task_with_active_job_state(
        service=service,
        tasks_dir=tasks_dir,
        task_id=f"task-{from_state.value}-{to_state.value}",
        state=from_state,
    )
    assert prepared.task is not None
    assert prepared.job is not None

    transitioned = _transition_state(
        service=service,
        task_id=prepared.task.task_id,
        to_state=to_state,
    )

    assert transitioned.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert transitioned.task is not None
    assert transitioned.job is not None
    assert transitioned.job.state is to_state
    assert transitioned.task.state is to_state
    if to_state in {
        AnalyticalTaskState.COMPLETED,
        AnalyticalTaskState.FAILED,
        AnalyticalTaskState.CANCELLED,
    }:
        assert transitioned.task.active_job_id is None
    else:
        assert transitioned.task.active_job_id == transitioned.job.job_id


def test_intent_priority_and_idempotency_rules(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    prepared = _prepare_task_with_active_job_state(
        service=service,
        tasks_dir=tasks_dir,
        task_id="task-1",
        state=AnalyticalTaskState.RUNNING,
    )
    assert prepared.task is not None

    cancel = service.cancel(task_id=prepared.task.task_id)
    pause_after_cancel = service.pause(task_id=prepared.task.task_id)
    preempt_after_cancel = service.scheduler_preempt(task_id=prepared.task.task_id)
    cancel_repeat = service.cancel(task_id=prepared.task.task_id)

    assert cancel.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert pause_after_cancel.result_type is AnalyticalTaskMutationResultType.CONFLICT
    assert preempt_after_cancel.result_type is AnalyticalTaskMutationResultType.CONFLICT
    assert cancel_repeat.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED

    cancelled = _transition_state(
        service=service,
        task_id=prepared.task.task_id,
        to_state=AnalyticalTaskState.CANCELLED,
    )
    assert cancelled.result_type is AnalyticalTaskMutationResultType.APPLIED
    cancel_terminal_repeat = service.cancel(task_id=prepared.task.task_id)
    assert cancel_terminal_repeat.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED

    prepared_for_pause = _prepare_task_with_active_job_state(
        service=service,
        tasks_dir=tasks_dir,
        task_id="task-2",
        state=AnalyticalTaskState.RUNNING,
    )
    assert prepared_for_pause.task is not None
    pause = service.pause(task_id=prepared_for_pause.task.task_id)
    preempt_after_pause = service.scheduler_preempt(task_id=prepared_for_pause.task.task_id)
    assert pause.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert preempt_after_pause.result_type is AnalyticalTaskMutationResultType.CONFLICT


def test_deletion_requested_blocks_mutations(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    service.start(task_id=created.task_id)

    deletion = service.request_deletion(task_id=created.task_id)
    start_after_deletion = service.start(task_id=created.task_id)
    pause_after_deletion = service.pause(task_id=created.task_id)
    cancel_after_deletion = service.cancel(task_id=created.task_id)
    config_update_after_deletion = service.update_task_config(
        task_id=created.task_id,
        config_document={"schema_version": 1, "samples": ["changed.fasta"], "priority": 2},
    )

    assert deletion.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert start_after_deletion.result_type is AnalyticalTaskMutationResultType.CONFLICT
    assert pause_after_deletion.result_type is AnalyticalTaskMutationResultType.CONFLICT
    assert cancel_after_deletion.result_type is AnalyticalTaskMutationResultType.CONFLICT
    assert config_update_after_deletion.result_type is AnalyticalTaskMutationResultType.CONFLICT


def test_reprioritize_changes_only_active_job_priority(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(
        service=service,
        tasks_dir=tasks_dir,
        task_id="task-1",
        priority=3,
    )
    started = service.start(task_id=created.task_id)
    assert started.job is not None

    reprioritized = service.reprioritize_active_job(task_id=created.task_id, priority=9)
    assert reprioritized.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert reprioritized.task is not None
    assert reprioritized.job is not None
    assert reprioritized.task.default_priority == 3
    assert reprioritized.task.current_config_revision == created.current_config_revision
    assert reprioritized.job.priority == 9


def test_reprioritize_running_job_is_idempotent_and_preserves_state(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(
        service=service,
        tasks_dir=tasks_dir,
        task_id="task-running",
        priority=2,
    )
    service.start(task_id=created.task_id)
    _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.RUNNING,
    )

    reprioritized = service.reprioritize_active_job(task_id=created.task_id, priority=8)
    assert reprioritized.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert reprioritized.task is not None
    assert reprioritized.job is not None
    assert reprioritized.task.state is AnalyticalTaskState.RUNNING
    assert reprioritized.job.state is AnalyticalTaskState.RUNNING
    assert reprioritized.task.default_priority == 2
    assert reprioritized.job.priority == 8

    unchanged = service.reprioritize_active_job(task_id=created.task_id, priority=8)
    assert unchanged.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED
    assert unchanged.job is not None
    assert unchanged.job.state is AnalyticalTaskState.RUNNING


def test_reprioritize_rejects_invalid_priority_and_no_active_job(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")

    no_active = service.reprioritize_active_job(task_id=created.task_id, priority=2)
    assert no_active.result_type is AnalyticalTaskMutationResultType.INVALID_TRANSITION

    with pytest.raises(AnalyticalTaskInvalidRecordDataError):
        service.reprioritize_active_job(task_id=created.task_id, priority=0)

    service.start(task_id=created.task_id)
    _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.RUNNING,
    )
    _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.COMPLETED,
    )
    terminal = service.reprioritize_active_job(task_id=created.task_id, priority=5)
    assert terminal.result_type is AnalyticalTaskMutationResultType.INVALID_TRANSITION


def test_job_timestamps_follow_lifecycle_and_idempotency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)

    def _sequenced_now() -> datetime:
        nonlocal now
        current = now
        now = now + timedelta(seconds=1)
        return current

    monkeypatch.setattr("jelica_core.tasks.registry_repository.utc_now", _sequenced_now)

    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")

    started = service.start(task_id=created.task_id)
    assert started.job is not None
    queued_job = started.job
    assert queued_job.created_at is not None
    assert queued_job.queued_at is not None
    assert queued_job.first_started_at is None
    assert queued_job.last_started_at is None
    assert queued_job.last_stopped_at is None
    assert queued_job.finished_at is None

    repeated_start = service.start(task_id=created.task_id)
    assert repeated_start.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED
    assert repeated_start.job is not None
    assert repeated_start.job.created_at == queued_job.created_at
    assert repeated_start.job.queued_at == queued_job.queued_at

    running_once = _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.RUNNING,
    )
    assert running_once.job is not None
    assert running_once.job.first_started_at is not None
    assert running_once.job.last_started_at is not None
    assert running_once.job.first_started_at == running_once.job.last_started_at

    running_repeat = service.transition_active_job_state(
        task_id=created.task_id,
        to_state=AnalyticalTaskState.RUNNING,
    )
    assert running_repeat.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED
    assert running_repeat.job is not None
    assert running_repeat.job.first_started_at == running_once.job.first_started_at
    assert running_repeat.job.last_started_at == running_once.job.last_started_at

    pause_requested = service.pause(task_id=created.task_id)
    assert pause_requested.result_type is AnalyticalTaskMutationResultType.APPLIED
    paused = _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.PAUSED,
    )
    assert paused.job is not None
    assert paused.job.last_stopped_at is not None

    resumed = service.resume(task_id=created.task_id)
    assert resumed.result_type is AnalyticalTaskMutationResultType.APPLIED
    running_twice = _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.RUNNING,
    )
    assert running_twice.job is not None
    assert running_twice.job.first_started_at == running_once.job.first_started_at
    assert running_twice.job.last_started_at is not None
    assert running_once.job.last_started_at is not None
    assert running_twice.job.last_started_at > running_once.job.last_started_at

    preempt_requested = service.scheduler_preempt(task_id=created.task_id)
    assert preempt_requested.result_type is AnalyticalTaskMutationResultType.APPLIED
    waiting = _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.WAITING,
    )
    assert waiting.job is not None
    assert waiting.job.last_stopped_at is not None
    assert paused.job.last_stopped_at is not None
    assert waiting.job.last_stopped_at > paused.job.last_stopped_at

    cancelled = service.cancel(task_id=created.task_id)
    assert cancelled.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert cancelled.job is not None
    assert cancelled.job.finished_at is not None

    cancelled_repeat = service.cancel(task_id=created.task_id)
    assert cancelled_repeat.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED
    assert cancelled_repeat.job is not None
    assert cancelled_repeat.job.finished_at == cancelled.job.finished_at


def test_terminal_job_is_read_only_for_runtime_progress_mutation(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    service.start(task_id=created.task_id)
    _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.RUNNING,
    )
    _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.COMPLETED,
    )
    before = service.get_task_snapshot(task_id=created.task_id)
    assert before.active_or_latest_job is not None

    mutation = service.update_active_job_progress(
        task_id=created.task_id,
        progress=55,
        current_stage="alignment",
    )
    assert mutation.result_type is AnalyticalTaskMutationResultType.INVALID_TRANSITION
    after = service.get_task_snapshot(task_id=created.task_id)
    assert after.active_or_latest_job is not None
    assert after.active_or_latest_job.finished_at == before.active_or_latest_job.finished_at
    assert after.active_or_latest_job.last_stopped_at == before.active_or_latest_job.last_stopped_at
    assert after.active_or_latest_job.last_started_at == before.active_or_latest_job.last_started_at


def test_runtime_state_json_is_stable_roundtrip_and_order_independent(tmp_path: Path) -> None:
    _, service, database_path, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    started = service.start(task_id=created.task_id)
    assert started.job is not None

    initial_job = service.get_job(job_id=started.job.job_id)
    assert initial_job.runtime_state == {}

    runtime_state = {
        "schema_version": 1,
        "completed_stages": ["validation"],
        "active_stage": "alignment",
    }
    first_update = service.update_active_job_progress(
        task_id=created.task_id,
        progress=10,
        current_stage="alignment",
        runtime_state=runtime_state,
    )
    assert first_update.result_type is AnalyticalTaskMutationResultType.APPLIED

    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            f"""
            SELECT runtime_state_json
            FROM {TASK_JOB_REGISTRY_TABLE_NAME}
            WHERE job_id = ?
            """,
            (started.job.job_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert str(row[0]) == (
        '{"active_stage":"alignment","completed_stages":["validation"],"schema_version":1}'
    )

    same_state_different_order = {
        "active_stage": "alignment",
        "schema_version": 1,
        "completed_stages": ["validation"],
    }
    unchanged = service.update_active_job_progress(
        task_id=created.task_id,
        progress=10,
        current_stage="alignment",
        runtime_state=same_state_different_order,
    )
    assert unchanged.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED

    second_update = service.update_active_job_progress(
        task_id=created.task_id,
        progress=20,
        current_stage="tree",
        runtime_state=None,
    )
    assert second_update.result_type is AnalyticalTaskMutationResultType.APPLIED

    reloaded_job = service.get_job(job_id=started.job.job_id)
    assert reloaded_job.runtime_state == runtime_state


def test_runtime_state_json_rejects_non_json_values(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    service.start(task_id=created.task_id)

    with pytest.raises(AnalyticalTaskInvalidRecordDataError):
        service.update_active_job_progress(
            task_id=created.task_id,
            progress=15,
            current_stage="alignment",
            runtime_state={
                "schema_version": 1,
                "completed_stages": ["validation"],
                "active_stage": {"not", "json"},
            },
        )


def test_config_revision_update_creates_new_revision_and_is_idempotent(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    task_dir = tasks_dir / created.task_id
    revision_1 = task_dir / "configs" / "000001.json"
    revision_1_content = revision_1.read_text(encoding="utf-8")

    updated = service.update_task_config(
        task_id=created.task_id,
        config_document={"schema_version": 1, "samples": ["new.fasta"], "priority": 5},
    )
    assert updated.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert updated.task is not None
    assert updated.task.current_config_revision == 2
    assert updated.task.current_config_relative_path == "configs/000002.json"
    assert updated.task.default_priority == 5
    revision_2 = task_dir / "configs" / "000002.json"
    assert revision_2.is_file()
    assert revision_1.read_text(encoding="utf-8") == revision_1_content
    assert (task_dir / "config.json").read_text(encoding="utf-8") == revision_2.read_text(
        encoding="utf-8"
    )

    unchanged = service.update_task_config(
        task_id=created.task_id,
        config_document={"schema_version": 1, "samples": ["new.fasta"], "priority": 5},
    )
    assert unchanged.result_type is AnalyticalTaskMutationResultType.ALREADY_SATISFIED
    assert unchanged.task is not None
    assert unchanged.task.default_priority == 5
    assert not (task_dir / "configs" / "000003.json").exists()


def test_config_update_policy_for_active_failed_cancelled_and_completed(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)

    active_task = _create_registered_task(
        service=service,
        tasks_dir=tasks_dir,
        task_id="active-task",
    )
    service.start(task_id=active_task.task_id)
    active_update = service.update_task_config(
        task_id=active_task.task_id,
        config_document={"schema_version": 1, "samples": ["a.fasta"], "priority": 2},
    )
    assert active_update.result_type is AnalyticalTaskMutationResultType.INVALID_TRANSITION

    failed_task = _create_registered_task(
        service=service,
        tasks_dir=tasks_dir,
        task_id="failed-task",
    )
    service.start(task_id=failed_task.task_id)
    _transition_state(
        service=service,
        task_id=failed_task.task_id,
        to_state=AnalyticalTaskState.RUNNING,
    )
    _transition_state(
        service=service,
        task_id=failed_task.task_id,
        to_state=AnalyticalTaskState.FAILED,
    )
    failed_update = service.update_task_config(
        task_id=failed_task.task_id,
        config_document={"schema_version": 1, "samples": ["b.fasta"], "priority": 2},
    )
    assert failed_update.result_type is AnalyticalTaskMutationResultType.APPLIED

    cancelled_task = _create_registered_task(
        service=service, tasks_dir=tasks_dir, task_id="cancelled-task"
    )
    service.start(task_id=cancelled_task.task_id)
    service.cancel(task_id=cancelled_task.task_id)
    cancelled_update = service.update_task_config(
        task_id=cancelled_task.task_id,
        config_document={"schema_version": 1, "samples": ["c.fasta"], "priority": 2},
    )
    assert cancelled_update.result_type is AnalyticalTaskMutationResultType.APPLIED

    completed_task = _create_registered_task(
        service=service,
        tasks_dir=tasks_dir,
        task_id="completed-task",
    )
    service.start(task_id=completed_task.task_id)
    _transition_state(
        service=service, task_id=completed_task.task_id, to_state=AnalyticalTaskState.RUNNING
    )
    _transition_state(
        service=service, task_id=completed_task.task_id, to_state=AnalyticalTaskState.COMPLETED
    )
    completed_update = service.update_task_config(
        task_id=completed_task.task_id,
        config_document={"schema_version": 1, "samples": ["d.fasta"], "priority": 2},
    )
    assert completed_update.result_type is AnalyticalTaskMutationResultType.INVALID_TRANSITION


def test_config_update_preserves_old_job_revision_and_new_start_uses_latest(
    tmp_path: Path,
) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    first_start = service.start(task_id=created.task_id)
    assert first_start.job is not None
    failed_job_id = first_start.job.job_id

    _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.RUNNING,
    )
    _transition_state(
        service=service,
        task_id=created.task_id,
        to_state=AnalyticalTaskState.FAILED,
    )

    updated = service.update_task_config(
        task_id=created.task_id,
        config_document={"schema_version": 1, "samples": ["updated.fasta"], "priority": 6},
    )
    assert updated.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert updated.task is not None
    assert updated.task.current_config_revision == 2
    assert updated.task.default_priority == 6

    restart = service.start(task_id=created.task_id)
    assert restart.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert restart.job is not None
    assert restart.job.config_revision == 2
    assert restart.job.priority == 6

    previous_job = service.get_job(job_id=failed_job_id)
    assert previous_job.config_revision == 1


def test_start_and_update_race_keep_consistent_task_and_job_config_revision(
    tmp_path: Path,
) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    task_dir = tasks_dir / created.task_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        start_future = pool.submit(service.start, task_id=created.task_id)
        update_future = pool.submit(
            service.update_task_config,
            task_id=created.task_id,
            config_document={"schema_version": 1, "samples": ["race.fasta"], "priority": 7},
        )
        start_result = start_future.result()
        update_result = update_future.result()

    assert start_result.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert start_result.job is not None
    assert update_result.result_type in {
        AnalyticalTaskMutationResultType.APPLIED,
        AnalyticalTaskMutationResultType.INVALID_TRANSITION,
    }

    snapshot = service.get_task_snapshot(task_id=created.task_id)
    assert snapshot.active_or_latest_job is not None
    assert len(service.list_task_jobs(task_id=created.task_id, limit=None, offset=0)) == 1

    if update_result.result_type is AnalyticalTaskMutationResultType.APPLIED:
        assert snapshot.task.current_config_revision == 2
        assert snapshot.task.default_priority == 7
        assert snapshot.active_or_latest_job.config_revision == 2
        assert snapshot.active_or_latest_job.priority == 7
        assert (task_dir / "configs" / "000002.json").is_file()
    else:
        assert snapshot.task.current_config_revision == 1
        assert snapshot.task.default_priority == 1
        assert snapshot.active_or_latest_job.config_revision == 1
        assert snapshot.active_or_latest_job.priority == 1

    assert not (task_dir / "configs" / "000003.json").exists()


def test_config_update_compensates_created_revision_when_sql_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    task_dir = tasks_dir / created.task_id
    revision_1_payload = (task_dir / "config.json").read_text(encoding="utf-8")
    original_select = service.registry._select_task_row
    call_counter = {"count": 0}

    def _failing_select_task_row(connection: sqlite3.Connection, *, task_id: str):  # type: ignore[no-untyped-def]
        call_counter["count"] += 1
        if call_counter["count"] >= 2:
            return None
        return original_select(connection, task_id=task_id)

    monkeypatch.setattr(service.registry, "_select_task_row", _failing_select_task_row)

    with pytest.raises(Exception):
        service.update_task_config(
            task_id=created.task_id,
            config_document={"schema_version": 1, "samples": ["rollback.fasta"], "priority": 3},
        )

    assert not (task_dir / "configs" / "000002.json").exists()
    assert (task_dir / "config.json").read_text(encoding="utf-8") == revision_1_payload
    monkeypatch.setattr(service.registry, "_select_task_row", original_select)
    reloaded = service.get_task(task_id=created.task_id)
    assert reloaded.current_config_revision == 1


def test_read_only_listing_snapshot_and_jobs_history(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    task_a = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-a")
    task_b = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-b")

    service.start(task_id=task_b.task_id)
    service.pause(task_id=task_b.task_id)
    service.resume(task_id=task_b.task_id)
    _transition_state(service=service, task_id=task_b.task_id, to_state=AnalyticalTaskState.RUNNING)
    _transition_state(service=service, task_id=task_b.task_id, to_state=AnalyticalTaskState.FAILED)
    service.start(task_id=task_b.task_id)

    snapshots = service.list_task_snapshots(
        states=(AnalyticalTaskState.WAITING, AnalyticalTaskState.QUEUED),
        limit=2,
        offset=0,
        order=AnalyticalTaskSortOrder.UPDATED_AT_DESC,
    )
    assert len(snapshots) == 2
    snapshot_a = service.get_task_snapshot(task_id=task_a.task_id)
    snapshot_b = service.get_task_snapshot(task_id=task_b.task_id)
    assert snapshot_a.active_or_latest_job is None
    assert snapshot_b.active_or_latest_job is not None

    jobs = service.list_task_jobs(task_id=task_b.task_id, limit=10, offset=0)
    assert len(jobs) == 2
    serialized = [job.model_dump(mode="json") for job in jobs]
    assert serialized[0]["task_id"] == task_b.task_id
    assert serialized[1]["task_id"] == task_b.task_id


def test_lifecycle_returns_concurrent_update_for_stale_expected_version(tmp_path: Path) -> None:
    _, service, _, tasks_dir = _build_registry_service(tmp_path)
    created = _create_registered_task(service=service, tasks_dir=tasks_dir, task_id="task-1")
    started = service.start(task_id=created.task_id, expected_task_version=created.record_version)
    assert started.result_type is AnalyticalTaskMutationResultType.APPLIED

    stale_start = service.start(
        task_id=created.task_id,
        expected_task_version=created.record_version,
    )
    assert stale_start.result_type is AnalyticalTaskMutationResultType.CONCURRENT_UPDATE
