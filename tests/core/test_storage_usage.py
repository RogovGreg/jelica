from __future__ import annotations

import os
from pathlib import Path

import pytest

from jelica_core.storage import StorageCategory, analyze_storage_usage
from jelica_core.system_config import CoreConfigService


def _initialized_service(home: Path, *, data_directory: str | None = None) -> CoreConfigService:
    service = CoreConfigService(jelica_home=home)
    service.initialize_system_config(data_directory=data_directory)
    return service


def test_storage_usage_reports_categories_and_database_artifacts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    service = _initialized_service(home)
    resolved = service.load_resolved_config()
    (resolved.tasks_dir / "task-a").mkdir()
    (resolved.tasks_dir / "task-a" / "artifact.bin").write_bytes(b"task")
    (home / "result_packages").mkdir()
    (home / "result_packages" / "incoming.tmp").write_bytes(b"result")
    resolved.logs_dir.joinpath("log.txt").write_bytes(b"log")
    Path(f"{resolved.database_path}-wal").write_bytes(b"wal")

    report = analyze_storage_usage(core_config_service=service)

    assert report.tasks.bytes >= 4
    assert report.tasks.workspaces == 1
    assert report.results.bytes >= 6
    assert report.results.packages == 0
    assert report.database.main_bytes > 0
    assert report.database.wal_bytes == 3
    assert report.other.bytes >= 3
    assert report.total.bytes == (
        report.tasks.bytes
        + report.results.bytes
        + report.database.bytes
        + report.other.bytes
    )
    assert report.complete is True


def test_storage_usage_external_data_directory_is_in_total_without_home_double_count(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    external = tmp_path / "external-data"
    service = _initialized_service(home, data_directory=str(external))
    (home / "result_packages").mkdir()
    (home / "result_packages" / "temporary.import").write_bytes(b"home-result")
    (external / "tasks" / "task-a").mkdir(parents=True)
    (external / "tasks" / "task-a" / "payload").write_bytes(b"external-task")
    (external / "logs" / "system.log").write_bytes(b"external-log")

    report = analyze_storage_usage(core_config_service=service)

    assert report.tasks.bytes >= len(b"external-task")
    assert report.results.bytes >= len(b"home-result")
    assert report.other.bytes >= len(b"external-log")
    assert str(home) in report.total.managed_roots
    assert str(external) in report.total.managed_roots


def test_storage_usage_nested_data_directory_is_scanned_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    nested = home / "custom-data"
    service = _initialized_service(home, data_directory=str(nested))
    (nested / "payload.bin").write_bytes(b"payload")

    report = analyze_storage_usage(core_config_service=service)

    assert report.total.bytes == (
        report.tasks.bytes
        + report.results.bytes
        + report.database.bytes
        + report.other.bytes
    )
    assert report.other.bytes >= len(b"payload")


def test_storage_usage_does_not_follow_symlink_and_deduplicates_hardlink(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    service = _initialized_service(home)
    resolved = service.load_resolved_config()
    payload = resolved.tasks_dir / "payload.bin"
    payload.write_bytes(b"123456")
    hardlink = resolved.logs_dir / "payload-hardlink.bin"
    os.link(payload, hardlink)

    external = tmp_path / "external"
    external.mkdir()
    (external / "large.bin").write_bytes(b"x" * 4096)
    link = resolved.tasks_dir / "external-link"
    try:
        link.symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    report = analyze_storage_usage(core_config_service=service)

    assert report.total.bytes < report.database.bytes + report.other.bytes + 4096
    assert report.tasks.bytes >= 6


def test_storage_usage_task_workspace_consistency_metrics(tmp_path: Path) -> None:
    home = tmp_path / "home"
    service = _initialized_service(home)
    resolved = service.load_resolved_config()
    (resolved.tasks_dir / "orphan-task").mkdir()

    report = analyze_storage_usage(core_config_service=service)

    assert report.tasks.workspaces == 1
    assert report.tasks.registry_tasks == 0
    assert report.tasks.missing_workspaces == 0
    assert report.tasks.orphan_workspaces == 1


def test_storage_usage_machine_payload_can_select_categories(tmp_path: Path) -> None:
    service = _initialized_service(tmp_path / "home")

    report = analyze_storage_usage(core_config_service=service)
    payload = report.to_dict(
        selected_categories=(StorageCategory.RESULTS, StorageCategory.TOTAL)
    )

    assert set(payload) == {"complete", "diagnostics", "selected_categories", "results", "total"}
    assert payload["results"]["bytes"] == 0  # type: ignore[index]
    assert isinstance(payload["total"]["bytes"], int)  # type: ignore[index]
