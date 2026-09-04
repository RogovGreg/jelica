from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jelica_core.analysis import AnalysisOrchestrator, InitializeAnalysisTaskRequest
from jelica_core.events import run_initialize_analysis_task_from_inputs
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import (
    AnalyticalTaskAlreadyExistsError,
    AnalyticalTaskInvalidRecordDataError,
    AnalyticalTaskRegistryService,
    LocalTaskStorage,
    generate_automatic_task_name,
    is_uuid_task_reference,
)


def _register_task(
    *,
    service: AnalyticalTaskRegistryService,
    task_id: str,
    name: str | None,
) -> None:
    service.register_task(
        task_id=task_id,
        name=name,
        task_dir_relative_path=task_id,
        current_config_relative_path="configs/000001.json",
        current_config_hash="a" * 64,
    )


@pytest.mark.parametrize(
    "name",
    (
        "a",
        "9",
        "Sample-A",
        "sample_analysis_01",
        "A" * 64,
    ),
)
def test_task_name_accepts_valid_values_and_preserves_case(name: str) -> None:
    request = InitializeAnalysisTaskRequest(name=name)

    assert request.name == name


@pytest.mark.parametrize(
    "name",
    (
        "",
        "_sample",
        "-sample",
        "sample analysis",
        "sample.analysis",
        "sample/analysis",
        "анализ",
        "A" * 65,
    ),
)
def test_task_name_rejects_invalid_values(name: str) -> None:
    with pytest.raises(ValidationError):
        InitializeAnalysisTaskRequest(name=name)


@pytest.mark.parametrize(
    "uuid_like_name",
    (
        "8b1c9d4e-1c33-4ab9-81b6-21408cc92cc4",
        "8b1c9d4e1c334ab981b621408cc92cc4",
    ),
)
def test_task_name_rejects_uuid_like_value(uuid_like_name: str) -> None:
    with pytest.raises(ValidationError, match="must not be a UUID"):
        InitializeAnalysisTaskRequest(name=uuid_like_name)


def test_task_name_is_unique_case_insensitively_and_preserves_original_case(
    tmp_path: Path,
) -> None:
    registry = AnalyticalTaskRegistryService(database_path=tmp_path / "data" / "jelica.db")
    first_id = str(uuid4())
    second_id = str(uuid4())
    _register_task(service=registry, task_id=first_id, name="Sample-A")

    with pytest.raises(AnalyticalTaskAlreadyExistsError) as error:
        _register_task(service=registry, task_id=second_id, name="sample-a")

    assert error.value.field_name == "name"
    assert registry.get_task(task_id=first_id).name == "Sample-A"


def test_task_reference_resolves_uuid_or_case_insensitive_name(tmp_path: Path) -> None:
    registry = AnalyticalTaskRegistryService(database_path=tmp_path / "data" / "jelica.db")
    task_id = str(uuid4())
    _register_task(service=registry, task_id=task_id, name="Sample-A")

    assert registry.resolve_task_id(task_reference=task_id.upper()) == task_id
    assert registry.resolve_task_id(task_reference=task_id.replace("-", "")) == task_id
    assert registry.resolve_task_id(task_reference="sample-a") == task_id


def test_task_reference_falls_back_to_legacy_non_uuid_task_id(tmp_path: Path) -> None:
    registry = AnalyticalTaskRegistryService(database_path=tmp_path / "data" / "jelica.db")
    _register_task(service=registry, task_id="legacy-task-1", name=None)

    assert registry.resolve_task_id(task_reference="legacy-task-1") == "legacy-task-1"


def test_task_reference_rejects_invalid_name_syntax(tmp_path: Path) -> None:
    registry = AnalyticalTaskRegistryService(database_path=tmp_path / "data" / "jelica.db")

    with pytest.raises(AnalyticalTaskInvalidRecordDataError):
        registry.resolve_task_reference(task_reference="invalid name")


def test_analyze_assigns_and_persists_automatic_name(tmp_path: Path) -> None:
    core_service = CoreConfigService(jelica_home=tmp_path / "home")
    core_service.initialize_system_config(force=True)

    result = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=("sample-a.fasta",),
        core_config_service=core_service,
    )

    assert result.ok is True
    assert result.value is not None
    task = result.value
    assert task.name is not None
    assert re.fullmatch(r"analysis-\d{8}T\d{6}", task.name)
    assert len(task.name) <= 64
    assert not is_uuid_task_reference(task.name)

    resolved_config = core_service.require_initialized_config()
    persisted = AnalyticalTaskRegistryService(
        database_path=resolved_config.database_path
    ).get_task(task_id=task.task_id)
    assert persisted.name == task.name


def test_analyze_preserves_explicit_name_in_initialized_and_registry_models(
    tmp_path: Path,
) -> None:
    core_service = CoreConfigService(jelica_home=tmp_path / "home")
    core_service.initialize_system_config(force=True)

    result = run_initialize_analysis_task_from_inputs(
        name="Study-A",
        config_json=None,
        raw_overrides=(),
        positional_sources=("sample-a.fasta",),
        core_config_service=core_service,
    )

    assert result.ok is True
    assert result.value is not None
    assert result.value.name == "Study-A"
    resolved_config = core_service.require_initialized_config()
    persisted = AnalyticalTaskRegistryService(
        database_path=resolved_config.database_path
    ).get_task(task_id=result.value.task_id)
    assert persisted.name == "Study-A"


def test_analyze_returns_registry_resolved_automatic_name_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2026, 8, 21, 0, 15, 32, tzinfo=UTC)
    monkeypatch.setattr("jelica_core.analysis.orchestrator.utc_now", lambda: timestamp)
    core_service = CoreConfigService(jelica_home=tmp_path / "home")
    core_service.initialize_system_config(force=True)

    first = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=("sample-a.fasta",),
        core_config_service=core_service,
    )
    second = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=("sample-b.fasta",),
        core_config_service=core_service,
    )

    assert first.ok is True
    assert first.value is not None
    assert first.value.name == "analysis-20260821T001532"
    assert second.ok is True
    assert second.value is not None
    assert second.value.name == "analysis-20260821T001532-1"


def test_automatic_name_uses_utc_timestamp() -> None:
    source_timezone = timezone(timedelta(hours=2))
    timestamp = datetime(2026, 8, 20, 12, 34, 56, tzinfo=source_timezone)

    assert generate_automatic_task_name(timestamp) == "analysis-20260820T103456"


def test_orchestrator_uses_injected_clock_for_automatic_name(tmp_path: Path) -> None:
    timestamp = datetime(2026, 8, 20, 10, 34, 56, tzinfo=UTC)
    orchestrator = AnalysisOrchestrator(clock=lambda: timestamp)

    task = orchestrator.initialize_task(
        request=InitializeAnalysisTaskRequest(
            positional_sources=("sample-a.fasta",)
        ),
        task_storage=LocalTaskStorage(tasks_dir=tmp_path / "tasks"),
    )

    assert task.name == "analysis-20260820T103456"


def test_automatic_name_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        generate_automatic_task_name(datetime(2026, 8, 20, 10, 34, 56))


def test_registry_suffixes_automatic_name_collisions_case_insensitively(
    tmp_path: Path,
) -> None:
    registry = AnalyticalTaskRegistryService(database_path=tmp_path / "data" / "jelica.db")
    base = generate_automatic_task_name(datetime(2026, 8, 21, 0, 15, 32, tzinfo=UTC))

    assert base == "analysis-20260821T001532"

    first = registry.register_task(
        task_id=str(uuid4()),
        automatic_name_base=base,
        task_dir_relative_path="task-a",
        current_config_relative_path="configs/000001.json",
        current_config_hash="a" * 64,
    )
    second = registry.register_task(
        task_id=str(uuid4()),
        automatic_name_base=base.upper(),
        task_dir_relative_path="task-b",
        current_config_relative_path="configs/000001.json",
        current_config_hash="b" * 64,
    )
    third = registry.register_task(
        task_id=str(uuid4()),
        automatic_name_base=base,
        task_dir_relative_path="task-c",
        current_config_relative_path="configs/000001.json",
        current_config_hash="c" * 64,
    )

    assert first.name == base
    assert second.name == f"{base.upper()}-1"
    assert third.name == f"{base}-2"
