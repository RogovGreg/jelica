from __future__ import annotations

import sqlite3
import threading
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from jelica_core.runtime import (
    RetentionDeletionOutcome,
    RetentionDeletionResult,
    TaskRetentionMaintenance,
    TaskRetentionWorker,
)
from jelica_core.system_config import CoreConfigService, CoreConfigValidationError
from jelica_core.tasks import (
    TASK_MAINTENANCE_STATE_TABLE_NAME,
    TASK_REGISTRY_TABLE_NAME,
    AnalyticalTaskRegistry,
    AnalyticalTaskRegistryService,
    AnalyticalTaskSortOrder,
    AnalyticalTaskState,
)


def _resolved_config(tmp_path: Path, *, retention_days: int | None):
    service = CoreConfigService(jelica_home=tmp_path / "home")
    service.initialize_system_config()
    return service.load_resolved_config().model_copy(
        update={"tasks_retention_days": retention_days}
    )


class _FakeRegistry:
    def __init__(self, candidates: list[str]) -> None:
        self.candidates = [
            SimpleNamespace(task=SimpleNamespace(task_id=task_id))
            for task_id in candidates
        ]
        self.last_cleanup: datetime | None = None
        self.list_kwargs: dict[str, object] | None = None
        self.write_error: Exception | None = None

    def list_task_snapshots(self, **kwargs: object):
        self.list_kwargs = kwargs
        return self.candidates

    def get_last_tasks_cleanup_at(self) -> datetime | None:
        return self.last_cleanup

    def set_last_tasks_cleanup_at(self, timestamp: datetime | None) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.last_cleanup = timestamp


def test_retention_selects_terminal_tasks_with_strict_cutoff_and_updates_timestamp(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    registry = _FakeRegistry(["old-terminal"])
    deleted: list[str] = []
    maintenance = TaskRetentionMaintenance(
        resolved_config=_resolved_config(tmp_path, retention_days=30),
        registry_service=registry,  # type: ignore[arg-type]
        delete_task=lambda task_id: (
            deleted.append(task_id)
            or RetentionDeletionOutcome(result=RetentionDeletionResult.DELETED)
        ),
        clock=lambda: now,
    )

    result = maintenance.run_once()

    assert result.success is True
    assert result.enabled is True
    assert result.candidate_count == 1
    assert result.deleted_count == 1
    assert result.already_satisfied_count == 0
    assert result.failed_count == 0
    assert deleted == ["old-terminal"]
    assert registry.last_cleanup == now
    assert registry.list_kwargs is not None
    assert set(registry.list_kwargs["states"]) == {
        AnalyticalTaskState.COMPLETED,
        AnalyticalTaskState.FAILED,
        AnalyticalTaskState.CANCELLED,
    }
    assert registry.list_kwargs["updated_before"] == now - timedelta(days=30)
    assert registry.list_kwargs["order"] is AnalyticalTaskSortOrder.UPDATED_AT_ASC


def test_retention_zero_candidates_is_success_and_updates_timestamp(tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    registry = _FakeRegistry([])
    maintenance = TaskRetentionMaintenance(
        resolved_config=_resolved_config(tmp_path, retention_days=1),
        registry_service=registry,  # type: ignore[arg-type]
        delete_task=lambda _: pytest.fail("no deletion expected"),
        clock=lambda: now,
    )

    result = maintenance.run_once()

    assert result.success is True
    assert result.candidate_count == 0
    assert result.attempted == 0
    assert registry.last_cleanup == now


def test_retention_partial_failure_does_not_update_timestamp(tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    registry = _FakeRegistry(["a", "b"])

    def delete_task(task_id: str) -> RetentionDeletionOutcome:
        return RetentionDeletionOutcome(
            result=(
                RetentionDeletionResult.DELETED
                if task_id == "a"
                else RetentionDeletionResult.FAILED
            ),
            detail="unsafe workspace" if task_id == "b" else None,
        )

    maintenance = TaskRetentionMaintenance(
        resolved_config=_resolved_config(tmp_path, retention_days=1),
        registry_service=registry,  # type: ignore[arg-type]
        delete_task=delete_task,
        clock=lambda: now,
    )

    result = maintenance.run_once()

    assert result.success is False
    assert result.deleted_count == 1
    assert result.failed_count == 1
    assert result.failed_task_ids == ("b",)
    assert registry.last_cleanup is None


def test_retention_maintenance_state_write_failure_fails_pass(tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    registry = _FakeRegistry([])
    registry.write_error = OSError("database is read-only")
    maintenance = TaskRetentionMaintenance(
        resolved_config=_resolved_config(tmp_path, retention_days=1),
        registry_service=registry,  # type: ignore[arg-type]
        delete_task=lambda _: pytest.fail("no deletion expected"),
        clock=lambda: now,
    )

    result = maintenance.run_once()

    assert result.success is False
    assert result.failed_count == 1
    assert "maintenance state write failed" in result.errors[0]
    assert registry.last_cleanup is None


def test_retention_already_satisfied_race_is_success(tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    registry = _FakeRegistry(["gone-task"])
    maintenance = TaskRetentionMaintenance(
        resolved_config=_resolved_config(tmp_path, retention_days=1),
        registry_service=registry,  # type: ignore[arg-type]
        delete_task=lambda _: RetentionDeletionOutcome(
            result=RetentionDeletionResult.ALREADY_SATISFIED
        ),
        clock=lambda: now,
    )

    result = maintenance.run_once()

    assert result.success is True
    assert result.deleted_count == 0
    assert result.already_satisfied_count == 1
    assert registry.last_cleanup == now


def test_disabled_retention_does_not_scan_or_write_state(tmp_path: Path) -> None:
    registry = _FakeRegistry(["must-not-be-read"])
    maintenance = TaskRetentionMaintenance(
        resolved_config=_resolved_config(tmp_path, retention_days=None),
        registry_service=registry,  # type: ignore[arg-type]
        delete_task=lambda _: pytest.fail("disabled retention must not delete"),
    )

    result = maintenance.run_once()

    assert result.enabled is False
    assert result.success is True
    assert registry.list_kwargs is None
    assert registry.last_cleanup is None


def test_maintenance_state_round_trips_timestamp_and_exists_on_fresh_database(
    tmp_path: Path,
) -> None:
    service = CoreConfigService(jelica_home=tmp_path / "home")
    resolved = service.initialize_system_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    timestamp = datetime(2026, 9, 27, 12, 0, 0, 123456, tzinfo=UTC)

    assert registry.get_last_tasks_cleanup_at() is None
    registry.set_last_tasks_cleanup_at(timestamp)
    assert registry.get_last_tasks_cleanup_at() == timestamp

    connection = sqlite3.connect(resolved.database_path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
                (TASK_MAINTENANCE_STATE_TABLE_NAME,),
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_schema_v4_to_v5_migration_preserves_tasks_and_creates_state(
    tmp_path: Path,
) -> None:
    service = CoreConfigService(jelica_home=tmp_path / "home")
    resolved = service.initialize_system_config()
    connection = sqlite3.connect(resolved.database_path)
    try:
        connection.execute(
            f"""
            INSERT INTO {TASK_REGISTRY_TABLE_NAME} (
                task_id, state, default_priority, current_config_revision,
                current_config_relative_path, current_config_hash,
                active_job_id, latest_job_id, created_at, updated_at,
                task_dir_relative_path, record_version, name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-v4-task",
                "completed",
                1,
                1,
                "configs/000001.json",
                "a" * 64,
                None,
                None,
                "2026-08-20T10:00:00.000000Z",
                "2026-08-20T10:00:00.000000Z",
                "legacy-v4-task",
                1,
                None,
            ),
        )
        connection.execute(f"DROP TABLE {TASK_MAINTENANCE_STATE_TABLE_NAME}")
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
    finally:
        connection.close()

    registry = AnalyticalTaskRegistry(database_path=resolved.database_path)
    registry.ensure_schema()

    assert registry.get(task_id="legacy-v4-task").state.value == "completed"
    assert registry.get_last_tasks_cleanup_at() is None


def test_config_requires_retention_key_and_rejects_invalid_values(tmp_path: Path) -> None:
    service = CoreConfigService(jelica_home=tmp_path / "home")
    service.initialize_system_config()
    document = service.get_config_path().read_text(encoding="utf-8")
    missing = document.replace('tasks_retention_days = ""\n', "")
    service.get_config_path().write_text(missing, encoding="utf-8")

    with pytest.raises(CoreConfigValidationError):
        service.load_resolved_config()

    for invalid in (0, -1, 1.5, True, False, "30"):
        with pytest.raises(CoreConfigValidationError):
            service._loader.load_from_mapping(
                data={
                    "schema_version": 1,
                    "input_directory_max_depth": 3,
                    "ncbi_api_key": "",
                    "ncbi_max_retries": 3,
                    "tasks_retention_days": invalid,
                    "default_alignment_mode": "compute",
                    "data": {"directory": "data"},
                    "execution": {
                        "max_parallel_tasks": 1,
                        "scheduler_poll_interval_seconds": 0.25,
                        "heartbeat_interval_seconds": 1.0,
                        "lease_timeout_seconds": 5.0,
                        "progress_flush_interval_seconds": 1.0,
                        "max_recovery_attempts": 3,
                    },
                    "logging": {
                        "level": "INFO",
                        "system_level": "",
                        "task_level": "",
                        "include_diagnostics": False,
                        "diagnostic_field_limit": 8192,
                    },
                    "tools": {"mafft": {"executable": ""}},
                }
            )


def test_config_set_and_unset_retention_preserves_required_key(tmp_path: Path) -> None:
    service = CoreConfigService(jelica_home=tmp_path / "home")
    service.initialize_system_config()

    enabled = service.set_parameter(parameter="tasks_retention_days", value="30")
    assert enabled.tasks_retention_days == 30
    assert tomllib.loads(service.get_config_path().read_text(encoding="utf-8"))[
        "tasks_retention_days"
    ] == 30

    disabled = service.unset_parameter(parameter="tasks_retention_days")
    assert disabled.tasks_retention_days is None
    document = tomllib.loads(service.get_config_path().read_text(encoding="utf-8"))
    assert document["tasks_retention_days"] == ""


def test_retention_worker_startup_due_and_shutdown_are_interruptible(tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    registry = _FakeRegistry(["old-task"])
    deletion_started = threading.Event()
    maintenance = TaskRetentionMaintenance(
        resolved_config=_resolved_config(tmp_path, retention_days=1),
        registry_service=registry,  # type: ignore[arg-type]
        delete_task=lambda task_id: (
            deletion_started.set()
            or RetentionDeletionOutcome(result=RetentionDeletionResult.DELETED)
        ),
        clock=lambda: now,
    )
    worker = TaskRetentionWorker(
        maintenance=maintenance,
        registry_service=registry,  # type: ignore[arg-type]
        clock=lambda: now,
    )

    worker.start()
    try:
        assert deletion_started.wait(timeout=2.0)
    finally:
        worker.stop()
        worker.join()

    assert registry.last_cleanup == now
