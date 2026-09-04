from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from jelica_core.analysis import (
    AnalysisOrchestrator,
    InitializeAnalysisTaskRequest,
)
from jelica_core.analysis.errors import AnalysisTaskInitializationError
from jelica_core.analysis.orchestrator import TASK_ID_COLLISION_RETRY_LIMIT
from jelica_core.config import (
    AnalysisConfigInput,
    InvalidConfigJsonSyntaxError,
    MissingSamplesError,
    ResolvedAnalysisConfig,
    parse_cli_overrides,
    resolve_analysis_config,
)
from jelica_core.tasks import (
    LocalTaskStorage,
    TaskConfigSaveError,
    TaskDirectoryAlreadyExistsError,
)


def _resolved_config_samples() -> tuple[AnalysisConfigInput, ResolvedAnalysisConfig]:
    config_input = AnalysisConfigInput(samples=["sample-a", "sample-b"])
    resolution = resolve_analysis_config(config_input)
    return config_input, resolution.config


def test_storage_creates_tasks_directory_and_task_directory(tmp_path: Path) -> None:
    _, resolved_config = _resolved_config_samples()
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    workspace = storage.create_task_workspace(task_id="task-1", config=resolved_config)

    assert (tmp_path / "tasks").is_dir()
    assert workspace.task_dir == tmp_path / "tasks" / "task-1"
    assert workspace.task_dir.is_dir()
    assert workspace.config_path == workspace.task_dir / "config.json"
    assert workspace.config_path.is_file()


def test_storage_creates_unique_task_directories(tmp_path: Path) -> None:
    _, resolved_config = _resolved_config_samples()
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    first_workspace = storage.create_task_workspace(task_id="task-1", config=resolved_config)
    second_workspace = storage.create_task_workspace(task_id="task-2", config=resolved_config)

    assert first_workspace.task_dir != second_workspace.task_dir


def test_storage_config_file_is_utf8_json_with_formatting_and_stable_key_order(
    tmp_path: Path,
) -> None:
    _, resolved_config = _resolved_config_samples()
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")
    workspace = storage.create_task_workspace(task_id="task-1", config=resolved_config)

    raw_bytes = workspace.config_path.read_bytes()
    text = raw_bytes.decode("utf-8")
    parsed_json = json.loads(text)

    assert parsed_json == resolved_config.model_dump(mode="json")
    assert text.endswith("\n")
    assert '\n  "priority": 1,' in text
    assert '\n  "samples": [' in text
    assert '\n  "schema_version": 1' in text
    assert text.index('"priority"') < text.index('"samples"')
    assert text.index('"samples"') < text.index('"schema_version"')


def test_storage_rejects_existing_task_directory(tmp_path: Path) -> None:
    _, resolved_config = _resolved_config_samples()
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")
    existing = tmp_path / "tasks" / "task-1"
    existing.mkdir(parents=True)

    with pytest.raises(TaskDirectoryAlreadyExistsError):
        storage.create_task_workspace(task_id="task-1", config=resolved_config)


def test_storage_cleans_partial_task_directory_when_config_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, resolved_config = _resolved_config_samples()
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    def _failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("jelica_core.tasks.storage.os.replace", _failing_replace)

    with pytest.raises(TaskConfigSaveError):
        storage.create_task_workspace(task_id="task-1", config=resolved_config)

    assert (tmp_path / "tasks").is_dir()
    assert not (tmp_path / "tasks" / "task-1").exists()


def test_orchestrator_does_not_create_directories_on_invalid_json(tmp_path: Path) -> None:
    orchestrator = AnalysisOrchestrator()
    request = InitializeAnalysisTaskRequest(config_json='{"samples":[}')
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    with pytest.raises(InvalidConfigJsonSyntaxError):
        orchestrator.initialize_task(request=request, task_storage=storage)

    assert not (tmp_path / "tasks").exists()


def test_orchestrator_does_not_create_directories_without_samples(tmp_path: Path) -> None:
    orchestrator = AnalysisOrchestrator()
    request = InitializeAnalysisTaskRequest()
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    with pytest.raises(MissingSamplesError):
        orchestrator.initialize_task(request=request, task_storage=storage)

    assert not (tmp_path / "tasks").exists()


def test_orchestrator_retries_when_generated_task_directory_exists(tmp_path: Path) -> None:
    first_id = uuid4()
    second_id = uuid4()
    (tmp_path / "tasks" / str(first_id)).mkdir(parents=True)
    generated_ids = iter([first_id, second_id])

    orchestrator = AnalysisOrchestrator(
        task_id_generator=lambda: next(generated_ids),
        collision_retry_limit=TASK_ID_COLLISION_RETRY_LIMIT,
    )
    request = InitializeAnalysisTaskRequest(positional_sources=("sample-a.fasta",))
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    task = orchestrator.initialize_task(request=request, task_storage=storage)

    assert task.task_id == str(second_id)
    assert task.task_dir == tmp_path / "tasks" / str(second_id)


def test_orchestrator_fails_after_collision_retry_limit(tmp_path: Path) -> None:
    fixed_id = uuid4()
    (tmp_path / "tasks" / str(fixed_id)).mkdir(parents=True)
    orchestrator = AnalysisOrchestrator(
        task_id_generator=lambda: fixed_id,
        collision_retry_limit=2,
    )
    request = InitializeAnalysisTaskRequest(positional_sources=("sample-a.fasta",))
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    with pytest.raises(AnalysisTaskInitializationError, match="unique task_id"):
        orchestrator.initialize_task(request=request, task_storage=storage)


def test_orchestrator_returns_paths_and_uuid4_identifier(tmp_path: Path) -> None:
    orchestrator = AnalysisOrchestrator()
    request = InitializeAnalysisTaskRequest(positional_sources=("sample-a.fasta",))
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    task = orchestrator.initialize_task(request=request, task_storage=storage)

    assert task.task_dir == tmp_path / "tasks" / task.task_id
    assert task.config_path == task.task_dir / "config.json"
    assert task.config_path.exists()
    assert UUID(task.task_id).version == 4


def test_saved_config_contains_only_resolved_config_fields(
    tmp_path: Path,
    default_resolved_alignment_block: dict[str, object],
    default_resolved_comparative_analysis_block: dict[str, object],
    default_resolved_distance_matrix_block: dict[str, object],
    default_resolved_phylogenetic_tree_block: dict[str, object],
    default_resolved_clade_detection_block: dict[str, object],
) -> None:
    orchestrator = AnalysisOrchestrator()
    request = InitializeAnalysisTaskRequest(
        config_json='{"samples":["json-a"],"unknown":"value"}',
    )
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    task = orchestrator.initialize_task(request=request, task_storage=storage)
    saved_config = json.loads(task.config_path.read_text(encoding="utf-8"))
    trace_id = saved_config.pop("trace_id")

    assert UUID(str(trace_id)).version == 4
    assert saved_config == {
        "alignment": default_resolved_alignment_block,
        "comparative_analysis": default_resolved_comparative_analysis_block,
        "distance_matrix": default_resolved_distance_matrix_block,
        "execution": {"from_phase": "auto", "target": "full_analysis"},
        "phylogenetic_tree": default_resolved_phylogenetic_tree_block,
        "clade_detection": default_resolved_clade_detection_block,
        "priority": 1,
        "reference": None,
        "samples": ["json-a"],
        "schema_version": 1,
        "statistics": {"kmer_strand": "forward", "kmers": []},
    }
    assert "unknown" not in saved_config


def test_orchestrator_saves_sparse_samples_with_null_in_config_json(tmp_path: Path) -> None:
    orchestrator = AnalysisOrchestrator()
    request = InitializeAnalysisTaskRequest(
        config_json='{"samples":["Sample_5.fasta"]}',
        overrides=tuple(parse_cli_overrides(["--samples.2=Sample_6.fasta"])),
    )
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    task = orchestrator.initialize_task(request=request, task_storage=storage)
    saved_config = json.loads(task.config_path.read_text(encoding="utf-8"))

    assert saved_config["samples"] == ["Sample_5.fasta", None, "Sample_6.fasta"]


def test_initialized_task_model_is_immutable(tmp_path: Path) -> None:
    orchestrator = AnalysisOrchestrator()
    request = InitializeAnalysisTaskRequest(positional_sources=("sample-a.fasta",))
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")
    task = orchestrator.initialize_task(request=request, task_storage=storage)

    with pytest.raises((ValidationError, TypeError)):
        task.task_id = "changed"  # type: ignore[misc]


def test_initialized_task_contains_only_initialization_state_fields(tmp_path: Path) -> None:
    orchestrator = AnalysisOrchestrator()
    request = InitializeAnalysisTaskRequest(positional_sources=("sample-a.fasta",))
    storage = LocalTaskStorage(tasks_dir=tmp_path / "tasks")

    task = orchestrator.initialize_task(request=request, task_storage=storage)

    assert set(task.model_dump().keys()) == {
        "task_id",
        "task_dir",
        "config_path",
        "config",
        "current_config_revision",
        "current_config_relative_path",
        "current_config_hash",
        "warnings",
    }
