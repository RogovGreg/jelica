from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from jelica_core.config import AnalysisConfigInput, ResolvedAnalysisConfig, resolve_analysis_config
from jelica_core.events import (
    run_add_analytical_task_samples,
    run_config_init,
    run_config_validate,
    run_delete_analytical_tasks,
    run_get_analytical_task,
    run_initialize_analysis_task_from_inputs,
    run_list_analytical_task_jobs,
    run_list_analytical_task_samples,
    run_list_analytical_tasks,
    run_remove_analytical_task_samples,
    run_reprioritize_analytical_task,
    run_runtime_continue,
    run_start_analytical_task,
    run_update_analytical_task,
    run_watch_analytical_task,
)
from jelica_core.events.sinks import TASK_EVENTS_LOG_FILENAME, EventSinkError, JsonlFileEventSink
from jelica_core.result_package import RESULT_PACKAGE_LINK_FILENAME
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import (
    TASK_REGISTRY_APPLICATION_ID,
    TASK_REGISTRY_SCHEMA_VERSION,
    AnalyticalTaskAlreadyExistsError,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryDatabaseUnavailableError,
    AnalyticalTaskRegistryService,
    AnalyticalTaskState,
    LocalTaskStorage,
)


def _initialize_core(jelica_home: Path) -> CoreConfigService:
    service = CoreConfigService(jelica_home=jelica_home)
    service.initialize_system_config(force=True)
    return service


def _write_sample(path: Path, *, sample_id: str) -> None:
    path.write_text(f">{sample_id}\nACGT\n", encoding="utf-8")


def _task_directories(tasks_dir: Path) -> list[Path]:
    if not tasks_dir.exists():
        return []
    return sorted(path for path in tasks_dir.iterdir() if path.is_dir())


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _create_registered_task_for_listing(
    *,
    service: CoreConfigService,
    task_id: str,
    sample_path: str,
    priority: int = 1,
) -> None:
    resolved = service.load_resolved_config()
    registry_service = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    storage = LocalTaskStorage(tasks_dir=resolved.tasks_dir)
    workspace = storage.create_task_workspace(
        task_id=task_id,
        config=json_to_resolved_config({"samples": [sample_path], "priority": priority}),
    )
    registry_service.register_task(
        task_id=task_id,
        task_dir_relative_path=task_id,
        default_priority=priority,
        current_config_revision=workspace.current_config_revision,
        current_config_relative_path=workspace.current_config_relative_path,
        current_config_hash=workspace.current_config_hash,
    )


def _initialize_task_for_runtime(
    *,
    service: CoreConfigService,
    sample_paths: tuple[Path, ...],
    raw_overrides: tuple[str, ...] = (),
) -> str:
    result = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=raw_overrides,
        positional_sources=tuple(str(path) for path in sample_paths),
        core_config_service=service,
    )
    assert result.ok is True
    assert result.value is not None
    return result.value.task_id


def json_to_resolved_config(payload: dict[str, object]) -> ResolvedAnalysisConfig:
    return resolve_analysis_config(AnalysisConfigInput.model_validate(payload)).config


def _create_custom_registry_database(
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


def test_analyze_registers_waiting_task_and_workspace_revision_consistently(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    sample_a = tmp_path / "sample-a.fasta"
    sample_b = tmp_path / "sample-b.fasta"
    _write_sample(sample_a, sample_id="a")
    _write_sample(sample_b, sample_id="b")
    service = _initialize_core(jelica_home)

    result = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample_a), str(sample_b)),
        core_config_service=service,
    )

    assert result.ok is True
    assert result.value is not None
    assert result.task_log_path is not None
    task = result.value
    resolved_config = service.load_resolved_config()
    registry_service = AnalyticalTaskRegistryService(database_path=resolved_config.database_path)
    record = registry_service.get_task(task_id=task.task_id)
    saved_config = json.loads(task.config_path.read_text(encoding="utf-8"))

    assert task.task_dir.is_dir()
    assert task.config_path.is_file()
    assert result.task_log_path.is_file()
    assert result.task_log_path.name == TASK_EVENTS_LOG_FILENAME
    assert (task.task_dir / "configs" / "000001.json").is_file()

    assert saved_config["priority"] == 1
    assert saved_config["samples"] == [str(sample_a), str(sample_b)]

    assert record.task_id == task.task_id
    assert record.state is AnalyticalTaskState.WAITING
    assert record.default_priority == 1
    assert record.active_job_id is None
    assert record.latest_job_id is None
    assert record.current_config_revision == 1
    assert record.current_config_relative_path == "configs/000001.json"
    assert record.current_config_hash == task.current_config_hash
    assert record.record_version == 1


def test_analyze_priority_consistency_between_config_and_default_task_priority(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")
    service = _initialize_core(jelica_home)
    config_json = json.dumps({"samples": [str(sample)], "priority": 4})

    result = run_initialize_analysis_task_from_inputs(
        config_json=config_json,
        raw_overrides=("--priority=7",),
        positional_sources=(),
        core_config_service=service,
    )

    assert result.ok is True
    assert result.value is not None
    task = result.value
    resolved_config = service.load_resolved_config()
    record = AnalyticalTaskRegistryService(database_path=resolved_config.database_path).get_task(
        task_id=task.task_id
    )
    saved_config = json.loads(task.config_path.read_text(encoding="utf-8"))

    assert saved_config["priority"] == 7
    assert record.default_priority == 7


def test_run_update_analytical_task_applies_overrides_and_is_semantically_idempotent(
    tmp_path: Path,
    default_resolved_alignment_block: dict[str, object],
) -> None:
    service = _initialize_core(tmp_path / "home")
    sample_a = tmp_path / "sample-a.fasta"
    sample_b = tmp_path / "sample-b.fasta"
    _write_sample(sample_a, sample_id="a")
    _write_sample(sample_b, sample_id="b")

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample_a),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id

    update = run_update_analytical_task(
        task_id=task_id,
        config_json=json.dumps(
            {"schema_version": 1, "samples": [str(sample_b)], "priority": 3},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        raw_overrides=(f'--samples=["{sample_a}"]', "--priority=6"),
        core_config_service=service,
    )
    assert update.ok is True
    assert update.value is not None
    assert update.value.result.value == "applied"
    assert update.value.current_config_revision == 2
    assert update.value.default_priority == 6

    resolved = service.load_resolved_config()
    task_dir = resolved.tasks_dir / task_id
    working_config = json.loads((task_dir / "config.json").read_text(encoding="utf-8"))
    immutable_revision = json.loads(
        (task_dir / "configs" / "000002.json").read_text(encoding="utf-8")
    )
    assert working_config == immutable_revision
    assert working_config["alignment"] == default_resolved_alignment_block
    assert working_config["samples"] == [str(sample_a)]
    assert working_config["priority"] == 6
    assert working_config["statistics"] == {"kmer_strand": "forward", "kmers": []}
    assert working_config["input_directory_max_depth"] == 3
    assert working_config["ncbi_max_retries"] == 3

    unchanged = run_update_analytical_task(
        task_id=task_id,
        config_json=json.dumps(
            {"priority": 6, "samples": [str(sample_a)], "schema_version": 1},
            ensure_ascii=False,
            indent=4,
        ),
        raw_overrides=(),
        core_config_service=service,
    )
    assert unchanged.ok is True
    assert unchanged.value is not None
    assert unchanged.value.result.value == "already_satisfied"
    assert unchanged.value.current_config_revision == 2
    assert not (task_dir / "configs" / "000003.json").exists()


def test_run_update_analytical_task_rejects_invalid_config_without_mutation(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id
    task_dir = initialized.value.task_dir

    registry = AnalyticalTaskRegistryService(
        database_path=service.load_resolved_config().database_path
    )
    before_task = registry.get_task(task_id=task_id)
    before_payload = (task_dir / "config.json").read_text(encoding="utf-8")

    update = run_update_analytical_task(
        task_id=task_id,
        config_json="{",
        raw_overrides=(),
        core_config_service=service,
    )
    assert update.ok is False
    assert update.error is not None
    assert update.error.event.code == 2010

    invalid_execution = run_update_analytical_task(
        task_id=task_id,
        config_json=None,
        raw_overrides=("--execution.target=unknown",),
        core_config_service=service,
    )
    assert invalid_execution.ok is False
    assert invalid_execution.error is not None

    after_task = registry.get_task(task_id=task_id)
    assert after_task.current_config_revision == before_task.current_config_revision
    assert after_task.current_config_hash == before_task.current_config_hash
    assert (task_dir / "config.json").read_text(encoding="utf-8") == before_payload
    assert not (task_dir / "configs" / "000002.json").exists()


def test_system_alignment_default_change_does_not_rewrite_existing_task_revision(
    tmp_path: Path,
    default_resolved_alignment_block: dict[str, object],
) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id

    resolved = service.load_resolved_config()
    task_dir = resolved.tasks_dir / task_id
    before_revision = json.loads((task_dir / "configs" / "000001.json").read_text(encoding="utf-8"))
    before_hash = initialized.value.current_config_hash

    updated_system = service.set_parameter(parameter="default_alignment_mode", value="none")
    assert updated_system.default_alignment_mode == "none"

    start_result = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert start_result.ok is True
    assert start_result.value is not None

    after_revision = json.loads((task_dir / "configs" / "000001.json").read_text(encoding="utf-8"))
    snapshot = AnalyticalTaskRegistryService(
        database_path=service.load_resolved_config().database_path
    ).get_task(task_id=task_id)

    assert after_revision == before_revision
    assert snapshot.current_config_revision == 1
    assert snapshot.current_config_hash == before_hash
    assert after_revision["alignment"] == default_resolved_alignment_block


def test_run_update_analytical_task_is_rejected_when_active_job_exists(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id

    registry = AnalyticalTaskRegistryService(
        database_path=service.load_resolved_config().database_path
    )
    started = registry.start(task_id=task_id)
    assert started.result_type.value == "applied"

    update = run_update_analytical_task(
        task_id=task_id,
        config_json=json.dumps({"schema_version": 1, "samples": [str(sample)], "priority": 4}),
        raw_overrides=(),
        core_config_service=service,
    )
    assert update.ok is False
    assert update.error is not None
    assert update.error.event.code == 2267


def test_run_task_samples_add_list_remove_updates_config_revisions(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample_a = tmp_path / "sample-a.fasta"
    sample_b = tmp_path / "sample-b.fasta"
    _write_sample(sample_a, sample_id="a")
    _write_sample(sample_b, sample_id="b")

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample_a),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id

    listed_before = run_list_analytical_task_samples(
        task_id=task_id,
        core_config_service=service,
    )
    assert listed_before.ok is True
    assert listed_before.value == [str(sample_a)]

    added = run_add_analytical_task_samples(
        task_id=task_id,
        sources=(str(sample_b), "ACGT ACGT"),
        core_config_service=service,
    )
    assert added.ok is True
    assert added.value is not None
    assert added.value.current_config_revision == 2

    listed_after_add = run_list_analytical_task_samples(
        task_id=task_id,
        core_config_service=service,
    )
    assert listed_after_add.ok is True
    assert listed_after_add.value == [str(sample_a), str(sample_b), "ACGT ACGT"]

    removed = run_remove_analytical_task_samples(
        task_id=task_id,
        indices=(2, 2),
        core_config_service=service,
    )
    assert removed.ok is True
    assert removed.value is not None
    assert removed.value.current_config_revision == 3

    listed_after_remove = run_list_analytical_task_samples(
        task_id=task_id,
        core_config_service=service,
    )
    assert listed_after_remove.ok is True
    assert listed_after_remove.value == [str(sample_a), str(sample_b)]


def test_run_start_emits_input_acquisition_completed_with_expected_counters(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="success")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is True
    assert started.value is not None

    resolved = service.load_resolved_config()
    task_events = _read_jsonl(resolved.tasks_dir / task_id / TASK_EVENTS_LOG_FILENAME)
    completed_events = [
        event for event in task_events if event["name"] == "CORE_INPUT_ACQUISITION_COMPLETED"
    ]
    assert len(completed_events) == 1
    context = completed_events[0].get("context")
    assert isinstance(context, dict)
    assert context["task_id"] == task_id
    assert context["job_id"] == started.value.job.job_id
    assert context["stage_id"] == "input_acquisition"
    assert context["provided_sources_count"] == 1
    assert context["unique_sources_count"] == 1
    assert context["materialized_files_count"] == 1
    assert context["local_files_count"] == 1
    assert context["ncbi_records_count"] == 0
    assert context["inline_sequences_count"] == 0
    assert context["duplicates_skipped_count"] == 0
    assert context["manifest_path"] == "inputs/input_manifest.json"
    assert context["hidden_count"] == 0
    paths = context["materialized_paths"]
    assert isinstance(paths, list)
    assert len(paths) == 1
    assert str(paths[0]).startswith("inputs/files/")


def test_run_start_persists_input_acquisition_completed_in_task_and_system_logs(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="persisted")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is True
    assert started.value is not None

    resolved = service.load_resolved_config()
    task_events = _read_jsonl(resolved.tasks_dir / task_id / TASK_EVENTS_LOG_FILENAME)
    system_events = _read_jsonl(resolved.logs_dir / "system-events.jsonl")
    assert any(event["name"] == "CORE_INPUT_ACQUISITION_COMPLETED" for event in task_events)
    assert any(
        event["name"] == "CORE_INPUT_ACQUISITION_COMPLETED" and event.get("task_id") == task_id
        for event in system_events
    )


def test_run_start_persists_input_processing_file_events_per_physical_file(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    first = tmp_path / "first.fasta"
    second = tmp_path / "second.fasta"
    first.write_text(">ok\nACGT\n>bad\nAXGT\n", encoding="utf-8")
    second.write_text(">ok2\nACGA\n", encoding="utf-8")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(first, second))

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is True
    assert started.value is not None

    resolved = service.load_resolved_config()
    task_events = _read_jsonl(resolved.tasks_dir / task_id / TASK_EVENTS_LOG_FILENAME)
    started_events = [
        event for event in task_events if event["name"] == "CORE_INPUT_PROCESSING_STARTED"
    ]
    file_events = [
        event for event in task_events if event["name"] == "CORE_INPUT_PROCESSING_FILE_PROCESSED"
    ]
    completed_events = [
        event for event in task_events if event["name"] == "CORE_INPUT_PROCESSING_COMPLETED"
    ]
    assert len(started_events) == 1
    assert len(file_events) == 2
    assert len(completed_events) == 1
    assert not any(
        event["name"] == "CORE_INPUT_PROCESSING_RECORD_PROCESSED" for event in task_events
    )

    for event in file_events:
        assert event.get("task_id") == task_id
        assert event.get("component") == "core"
        assert event.get("operation_id") == "tasks.start"
        context = event.get("context")
        assert isinstance(context, dict)
        assert context.get("task_id") == task_id
        assert context.get("job_id") == started.value.job.job_id
        assert "relative_path" in context
        assert "format_hint" in context
        assert "file_index" in context
        assert "total_file_count" in context
        assert "parsed_record_count" in context
        assert "valid_sample_count" in context
        assert "invalid_sample_count" in context
        assert "issue_count" in context
        assert "issue_count_by_code" in context
        assert "issue_count_by_severity" in context
        assert "processing_status" in context
        assert "normalized_sequence" not in context
        assert "kmer_hits" not in context
        assert "api_key" not in context


def test_run_start_reports_dataset_validation_failure_as_input_processing_event(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    invalid = tmp_path / "invalid.fasta"
    invalid.write_text(">bad\nXXXX\n", encoding="utf-8")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(invalid,))

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is False
    assert started.error is not None
    assert started.error.event.name == "CORE_INPUT_PROCESSING_VALIDATION_FAILED"

    resolved = service.load_resolved_config()
    task_events = _read_jsonl(resolved.tasks_dir / task_id / TASK_EVENTS_LOG_FILENAME)
    validation_failed_events = [
        event for event in task_events if event["name"] == "CORE_INPUT_PROCESSING_VALIDATION_FAILED"
    ]
    completed_events = [
        event for event in task_events if event["name"] == "CORE_INPUT_PROCESSING_COMPLETED"
    ]
    assert len(validation_failed_events) >= 1
    assert len(completed_events) == 0
    detailed_events = [
        event
        for event in validation_failed_events
        if isinstance((context := event.get("context")), dict) and "manifest_path" in context
    ]
    assert len(detailed_events) == 1
    context = detailed_events[0].get("context")
    assert isinstance(context, dict)
    assert context["manifest_path"] == "input_processing/input_processing_manifest.json"
    assert context["comparative_analysis_available"] is False
    issue_codes = context["dataset_issue_codes"]
    assert isinstance(issue_codes, list)
    assert "no_valid_samples" in issue_codes


def test_run_start_writes_single_runtime_job_completed_event_per_job(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="single-job-completed")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is True
    assert started.value is not None
    job_id = started.value.job.job_id

    resolved = service.load_resolved_config()
    task_events = _read_jsonl(resolved.tasks_dir / task_id / TASK_EVENTS_LOG_FILENAME)
    system_events = _read_jsonl(resolved.logs_dir / "system-events.jsonl")

    task_completed_count = sum(
        1
        for event in task_events
        if event["name"] == "CORE_RUNTIME_JOB_COMPLETED"
        and isinstance((context := event.get("context")), dict)
        and context.get("job_id") == job_id
    )
    system_completed_count = sum(
        1
        for event in system_events
        if event["name"] == "CORE_RUNTIME_JOB_COMPLETED"
        and isinstance((context := event.get("context")), dict)
        and context.get("job_id") == job_id
    )
    assert task_completed_count == 1
    assert system_completed_count == 1


def test_runtime_events_without_task_id_are_not_written_to_task_log(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="runtime-scope")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is True

    resolved = service.load_resolved_config()
    task_events = _read_jsonl(resolved.tasks_dir / task_id / TASK_EVENTS_LOG_FILENAME)
    system_events = _read_jsonl(resolved.logs_dir / "system-events.jsonl")
    runtime_global_names = {
        "CORE_RUNTIME_LEASE_ACQUIRED",
        "CORE_RUNTIME_SCHEDULER_STARTED",
        "CORE_RUNTIME_SCHEDULER_STOPPED",
        "CORE_RUNTIME_LEASE_RELEASED",
    }

    assert not any(event["name"] in runtime_global_names for event in task_events)
    for event_name in runtime_global_names:
        matching = [event for event in system_events if event["name"] == event_name]
        assert len(matching) >= 1
        assert all(event.get("task_id") is None for event in matching)


def test_run_start_persists_input_acquisition_warning_and_error_events_in_logs(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    warning_directory = tmp_path / "mixed"
    warning_directory.mkdir()
    _write_sample(warning_directory / "sample.fasta", sample_id="warn")
    (warning_directory / "notes.csv").write_text("x\n", encoding="utf-8")

    warning_task_id = _initialize_task_for_runtime(
        service=service,
        sample_paths=(warning_directory,),
    )
    warning_started = run_start_analytical_task(
        task_id=warning_task_id, core_config_service=service
    )
    assert warning_started.ok is True

    resolved = service.load_resolved_config()
    warning_task_events = _read_jsonl(
        resolved.tasks_dir / warning_task_id / TASK_EVENTS_LOG_FILENAME
    )
    system_events_after_warning = _read_jsonl(resolved.logs_dir / "system-events.jsonl")
    assert any(
        event["name"] == "CORE_INPUT_UNSUPPORTED_FILES_SKIPPED" for event in warning_task_events
    )
    assert any(
        event["name"] == "CORE_INPUT_UNSUPPORTED_FILES_SKIPPED"
        and event.get("task_id") == warning_task_id
        for event in system_events_after_warning
    )

    missing_source = tmp_path / "missing-source.fasta"
    error_task_id = _initialize_task_for_runtime(service=service, sample_paths=(missing_source,))
    error_started = run_start_analytical_task(task_id=error_task_id, core_config_service=service)
    assert error_started.ok is False

    error_task_events = _read_jsonl(resolved.tasks_dir / error_task_id / TASK_EVENTS_LOG_FILENAME)
    system_events_after_error = _read_jsonl(resolved.logs_dir / "system-events.jsonl")
    assert any(event["name"] == "CORE_INPUT_PATH_NOT_FOUND" for event in error_task_events)
    assert any(
        event["name"] == "CORE_INPUT_PATH_NOT_FOUND" and event.get("task_id") == error_task_id
        for event in system_events_after_error
    )


def test_run_task_samples_add_and_remove_persist_update_events_in_both_logs(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="samples")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))

    added = run_add_analytical_task_samples(
        task_id=task_id,
        sources=("ACGT ACGT",),
        core_config_service=service,
    )
    assert added.ok is True
    removed = run_remove_analytical_task_samples(
        task_id=task_id,
        indices=(1,),
        core_config_service=service,
    )
    assert removed.ok is True

    resolved = service.load_resolved_config()
    task_events = _read_jsonl(resolved.tasks_dir / task_id / TASK_EVENTS_LOG_FILENAME)
    system_events = _read_jsonl(resolved.logs_dir / "system-events.jsonl")

    assert any(
        event["name"] == "CORE_ANALYTICAL_TASK_UPDATE_APPLIED"
        and event.get("operation_id") == "tasks.samples.add"
        for event in task_events
    )
    assert any(
        event["name"] == "CORE_ANALYTICAL_TASK_UPDATE_APPLIED"
        and event.get("operation_id") == "tasks.samples.remove"
        for event in task_events
    )
    assert any(
        event["name"] == "CORE_ANALYTICAL_TASK_UPDATE_APPLIED"
        and event.get("operation_id") == "tasks.samples.add"
        for event in system_events
    )
    assert any(
        event["name"] == "CORE_ANALYTICAL_TASK_UPDATE_APPLIED"
        and event.get("operation_id") == "tasks.samples.remove"
        for event in system_events
    )


def test_jsonl_logs_do_not_include_ncbi_api_key_or_full_inline_sequence(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    service.set_parameter(parameter="ncbi_api_key", value="super-secret-key")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="redaction")
    inline_source = "ACGT ACGT ACGT"
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))
    update_inline = run_add_analytical_task_samples(
        task_id=task_id,
        sources=(inline_source,),
        core_config_service=service,
    )
    assert update_inline.ok is True
    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is True

    resolved = service.load_resolved_config()
    task_log_text = (resolved.tasks_dir / task_id / TASK_EVENTS_LOG_FILENAME).read_text(
        encoding="utf-8"
    )
    system_log_text = (resolved.logs_dir / "system-events.jsonl").read_text(encoding="utf-8")
    assert "super-secret-key" not in task_log_text
    assert "super-secret-key" not in system_log_text
    assert inline_source not in task_log_text
    assert inline_source not in system_log_text
    assert inline_source.replace(" ", "") not in task_log_text
    assert inline_source.replace(" ", "") not in system_log_text


def test_run_task_samples_remove_rejects_invalid_index(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))

    removed = run_remove_analytical_task_samples(
        task_id=task_id,
        indices=(9,),
        core_config_service=service,
    )

    assert removed.ok is False
    assert removed.error is not None
    assert removed.error.event.code == 2212


def test_run_task_samples_add_rejected_for_completed_task(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is True

    added = run_add_analytical_task_samples(
        task_id=task_id,
        sources=("ACGT",),
        core_config_service=service,
    )
    assert added.ok is False
    assert added.error is not None
    assert added.error.event.code == 2267


def test_queued_job_uses_pinned_input_depth_after_system_config_changes(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    source_directory = tmp_path / "sources"
    nested_directory = source_directory / "nested"
    nested_directory.mkdir(parents=True)
    sample = nested_directory / "sample.fasta"
    _write_sample(sample, sample_id="nested")

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(source_directory),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id

    queued = registry.start(task_id=task_id)
    assert queued.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert queued.job is not None
    job_id = queued.job.job_id

    service.set_parameter(parameter="input_directory_max_depth", value="0")

    continued = run_runtime_continue(core_config_service=service)
    assert continued.ok is True
    assert continued.value is not None
    assert continued.value.completed_jobs >= 1

    snapshot = registry.get_task_snapshot(task_id=task_id)
    assert snapshot.task.state is AnalyticalTaskState.COMPLETED
    assert registry.get_job(job_id=job_id).state is AnalyticalTaskState.COMPLETED


def test_runtime_execution_target_stops_at_selected_prefix_and_builds_package(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="targeted")

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=("--execution.target=sequence_statistics",),
        positional_sources=(str(sample),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id
    queued = registry.start(task_id=task_id)
    assert queued.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert queued.job is not None

    continued = run_runtime_continue(core_config_service=service)

    assert continued.ok is True
    task_dir = resolved.tasks_dir / task_id
    stage_root = task_dir / "jobs" / queued.job.job_id / "stages"
    assert {path.name for path in stage_root.iterdir() if path.is_dir()} == {
        "initialize_job",
        "input_acquisition",
        "input_processing",
        "result_package",
    }
    assert (task_dir / RESULT_PACKAGE_LINK_FILENAME).is_file()
    assert registry.get_task(task_id=task_id).state is AnalyticalTaskState.COMPLETED


def test_run_reprioritize_analytical_task_mutates_only_active_job_priority(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=("--priority=3",),
        positional_sources=(str(sample),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id

    registry = AnalyticalTaskRegistryService(
        database_path=service.load_resolved_config().database_path
    )
    queued = registry.start(task_id=task_id)
    assert queued.result_type.value == "applied"

    reprioritize = run_reprioritize_analytical_task(
        task_id=task_id,
        priority=9,
        core_config_service=service,
    )
    assert reprioritize.ok is True
    assert reprioritize.value is not None
    assert reprioritize.value.result.value == "applied"
    assert reprioritize.value.state is AnalyticalTaskState.QUEUED
    assert reprioritize.value.old_priority == 3
    assert reprioritize.value.new_priority == 9

    repeated = run_reprioritize_analytical_task(
        task_id=task_id,
        priority=9,
        core_config_service=service,
    )
    assert repeated.ok is True
    assert repeated.value is not None
    assert repeated.value.result.value == "already_satisfied"

    task = registry.get_task(task_id=task_id)
    assert task.default_priority == 3
    assert task.current_config_revision == 1


def test_run_reprioritize_analytical_task_rejects_invalid_requests(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id

    no_active = run_reprioritize_analytical_task(
        task_id=task_id,
        priority=5,
        core_config_service=service,
    )
    assert no_active.ok is False
    assert no_active.error is not None
    assert no_active.error.event.code == 2271

    invalid_priority = run_reprioritize_analytical_task(
        task_id=task_id,
        priority=0,
        core_config_service=service,
    )
    assert invalid_priority.ok is False
    assert invalid_priority.error is not None
    assert invalid_priority.error.event.code == 2212


def test_run_delete_analytical_tasks_batch_mixed_result_with_dedup(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="delete-batch")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))
    resolved = service.load_resolved_config()
    task_dir = resolved.tasks_dir / task_id

    result = run_delete_analytical_tasks(
        task_ids=(task_id, "missing-task", task_id),
        core_config_service=service,
    )

    assert result.ok is True
    assert result.value is not None
    batch = result.value
    assert batch.result.value == "partially_applied"
    assert batch.requested_count == 3
    assert batch.unique_count == 2
    assert [item.task_id for item in batch.items] == [task_id, "missing-task"]
    assert batch.items[0].result.value == "deleted"
    assert batch.items[1].result.value == "not_found"
    assert not task_dir.exists()

    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    with pytest.raises(AnalyticalTaskNotFoundError):
        registry.get_task(task_id=task_id)


def test_run_delete_analytical_tasks_immediately_deletes_queued_paused_and_completed_tasks(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    queued_sample = tmp_path / "queued.fasta"
    paused_sample = tmp_path / "paused.fasta"
    completed_sample = tmp_path / "completed.fasta"
    _write_sample(queued_sample, sample_id="queued")
    _write_sample(paused_sample, sample_id="paused")
    _write_sample(completed_sample, sample_id="completed")

    queued_task_id = _initialize_task_for_runtime(service=service, sample_paths=(queued_sample,))
    paused_task_id = _initialize_task_for_runtime(service=service, sample_paths=(paused_sample,))
    completed_task_id = _initialize_task_for_runtime(
        service=service, sample_paths=(completed_sample,)
    )

    assert registry.start(task_id=queued_task_id).result_type.value == "applied"
    assert registry.start(task_id=paused_task_id).result_type.value == "applied"
    assert registry.pause(task_id=paused_task_id).result_type.value == "applied"
    start_completed = run_start_analytical_task(
        task_id=completed_task_id,
        core_config_service=service,
    )
    assert start_completed.ok is True

    delete_result = run_delete_analytical_tasks(
        task_ids=(queued_task_id, paused_task_id, completed_task_id),
        core_config_service=service,
    )
    assert delete_result.ok is True
    assert delete_result.value is not None
    assert delete_result.value.result.value == "applied"
    assert all(item.result.value == "deleted" for item in delete_result.value.items)

    for task_id in (queued_task_id, paused_task_id, completed_task_id):
        with pytest.raises(AnalyticalTaskNotFoundError):
            registry.get_task(task_id=task_id)
        assert not (resolved.tasks_dir / task_id).exists()


def test_run_delete_analytical_tasks_rejects_unsafe_workspace_path(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="unsafe-delete")
    task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))
    resolved = service.load_resolved_config()
    task_dir = resolved.tasks_dir / task_id

    connection = sqlite3.connect(resolved.database_path)
    try:
        connection.execute(
            "UPDATE analytical_tasks SET task_dir_relative_path = '.' WHERE task_id = ?",
            (task_id,),
        )
        connection.commit()
    finally:
        connection.close()

    delete_result = run_delete_analytical_tasks(
        task_ids=(task_id,),
        core_config_service=service,
    )
    assert delete_result.ok is False
    assert delete_result.error is not None
    assert delete_result.error.event.code == 2212
    assert task_dir.exists()


def test_run_watch_analytical_task_returns_terminal_result_and_rejects_no_job(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    sample_a = tmp_path / "a.fasta"
    sample_b = tmp_path / "b.fasta"
    _write_sample(sample_a, sample_id="watch-terminal")
    _write_sample(sample_b, sample_id="watch-empty")

    terminal_task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample_a,))
    no_job_task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample_b,))
    start_result = run_start_analytical_task(
        task_id=terminal_task_id,
        core_config_service=service,
    )
    assert start_result.ok is True
    assert start_result.value is not None
    terminal_job_id = start_result.value.job.job_id

    watch_result = run_watch_analytical_task(
        task_id=terminal_task_id,
        core_config_service=service,
    )
    assert watch_result.ok is True
    assert watch_result.value is not None
    assert watch_result.value.task_id == terminal_task_id
    assert watch_result.value.job_id == terminal_job_id
    assert watch_result.value.result.value == "completed"
    assert watch_result.value.state is AnalyticalTaskState.COMPLETED

    no_job_watch = run_watch_analytical_task(
        task_id=no_job_task_id,
        core_config_service=service,
    )
    assert no_job_watch.ok is False
    assert no_job_watch.error is not None
    assert no_job_watch.error.event.name == "CORE_ANALYTICAL_TASK_WATCH_REJECTED"


def test_run_start_reports_input_path_not_found_for_missing_source(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    service = _initialize_core(jelica_home)
    missing_source = tmp_path / "missing.fasta"

    initialized = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(missing_source),),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None
    task_id = initialized.value.task_id

    resolved_config = service.load_resolved_config()
    records = AnalyticalTaskRegistryService(database_path=resolved_config.database_path).list_tasks(
        limit=None
    )
    assert len(records) == 1
    assert records[0].task_id == task_id

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is False
    assert started.error is not None
    assert started.error.event.code == 2289

    snapshot = AnalyticalTaskRegistryService(
        database_path=resolved_config.database_path
    ).get_task_snapshot(task_id=task_id)
    assert snapshot.task.state is AnalyticalTaskState.FAILED
    assert snapshot.active_or_latest_job is not None
    assert snapshot.active_or_latest_job.state is AnalyticalTaskState.FAILED


def test_run_start_rejects_explicit_from_phase_without_workspace_source(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="from-phase")
    task_id = _initialize_task_for_runtime(
        service=service,
        sample_paths=(sample,),
        raw_overrides=("--execution.from_phase=alignment",),
    )

    started = run_start_analytical_task(task_id=task_id, core_config_service=service)
    assert started.ok is False
    assert started.error is not None
    assert started.error.event.name == "CORE_ANALYTICAL_TASK_START_REJECTED"
    assert "requires exactly one local task workspace directory source" in started.error.event.message


def test_run_start_reuses_workspace_snapshots_for_explicit_from_phase(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="seed-source")
    source_task_id = _initialize_task_for_runtime(service=service, sample_paths=(sample,))

    source_started = run_start_analytical_task(task_id=source_task_id, core_config_service=service)
    assert source_started.ok is True
    assert source_started.value is not None

    resolved = service.load_resolved_config()
    source_workspace = resolved.tasks_dir / source_task_id
    derived_task_id = _initialize_task_for_runtime(
        service=service,
        sample_paths=(source_workspace,),
        raw_overrides=("--execution.from_phase=alignment",),
    )

    derived_started = run_start_analytical_task(task_id=derived_task_id, core_config_service=service)
    assert derived_started.ok is True
    assert derived_started.value is not None

    derived_events = _read_jsonl(resolved.tasks_dir / derived_task_id / TASK_EVENTS_LOG_FILENAME)
    derived_event_names = {str(event["name"]) for event in derived_events}
    assert "CORE_INPUT_ACQUISITION_COMPLETED" not in derived_event_names
    assert "CORE_INPUT_PROCESSING_COMPLETED" not in derived_event_names
    assert "CORE_ANALYTICAL_TASK_START_APPLIED" in derived_event_names
    assert "CORE_RUNTIME_JOB_COMPLETED" in derived_event_names


def test_config_init_rejects_foreign_database_with_structured_error_and_no_mutation(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    data_dir = tmp_path / "external-data"
    database_path = data_dir / "jelica.db"
    _create_custom_registry_database(
        database_path=database_path,
        application_id=98_765,
        user_version=7,
    )
    service = CoreConfigService(jelica_home=jelica_home)

    result = run_config_init(
        data_directory=str(data_dir),
        max_workers=None,
        log_level=None,
        force=False,
        core_config_service=service,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.event.code == 2204
    assert result.error.event.diagnostics is None
    assert not service.get_config_path().exists()

    connection = sqlite3.connect(database_path)
    try:
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        payload = connection.execute("SELECT payload FROM sentinel").fetchone()
    finally:
        connection.close()

    assert app_id == 98_765
    assert user_version == 7
    assert payload is not None
    assert str(payload[0]) == "keep-me"


def test_config_validate_rejects_newer_registry_schema_with_structured_error(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved_config = service.load_resolved_config()
    database_path = resolved_config.database_path

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(f"PRAGMA application_id = {TASK_REGISTRY_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {TASK_REGISTRY_SCHEMA_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()

    result = run_config_validate(core_config_service=service)

    assert result.ok is False
    assert result.error is not None
    assert result.error.event.code == 2205
    assert result.error.event.diagnostics is None

    connection = sqlite3.connect(database_path)
    try:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    assert user_version == TASK_REGISTRY_SCHEMA_VERSION + 1


def test_config_init_with_unusable_database_parent_is_structured_and_leaves_no_db(
    tmp_path: Path,
) -> None:
    jelica_home = tmp_path / "home"
    blocked_root = tmp_path / "blocked-root"
    blocked_root.write_text("not-a-directory", encoding="utf-8")
    requested_data_dir = blocked_root / "data"
    database_path = requested_data_dir / "jelica.db"
    service = CoreConfigService(jelica_home=jelica_home)

    result = run_config_init(
        data_directory=str(requested_data_dir),
        max_workers=None,
        log_level=None,
        force=False,
        core_config_service=service,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.event.diagnostics is None
    assert not database_path.exists()


def test_analyze_registry_errors_roll_back_workspace_and_registry(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    jelica_home = tmp_path / "home"
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")
    service = _initialize_core(jelica_home)
    resolved_config = service.load_resolved_config()

    def _failing_register(
        self: AnalyticalTaskRegistryService,
        *,
        task_id: str,
        name: str | None = None,
        automatic_name_base: str | None = None,
        task_dir_relative_path: str,
        default_priority: int = 1,
        current_config_revision: int = 1,
        current_config_relative_path: str,
        current_config_hash: str,
    ) -> None:
        raise AnalyticalTaskRegistryDatabaseUnavailableError(
            database_path=resolved_config.database_path,
            detail="simulated open failure",
            sqlite_exception_type="OperationalError",
        )

    monkeypatch.setattr(AnalyticalTaskRegistryService, "register_task", _failing_register)

    result = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample),),
        core_config_service=service,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.event.code == 2202
    assert _task_directories(resolved_config.tasks_dir) == []
    assert (
        AnalyticalTaskRegistryService(database_path=resolved_config.database_path).list_tasks(
            limit=None
        )
        == []
    )


def test_analyze_compensation_failure_preserves_structured_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jelica_home = tmp_path / "home"
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")
    service = _initialize_core(jelica_home)
    resolved_config = service.load_resolved_config()

    def _failing_register(
        self: AnalyticalTaskRegistryService,
        *,
        task_id: str,
        name: str | None = None,
        automatic_name_base: str | None = None,
        task_dir_relative_path: str,
        default_priority: int = 1,
        current_config_revision: int = 1,
        current_config_relative_path: str,
        current_config_hash: str,
    ) -> None:
        raise RuntimeError("simulated register failure")

    def _failing_rmtree(path: Path) -> None:
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(AnalyticalTaskRegistryService, "register_task", _failing_register)
    monkeypatch.setattr("jelica_core.events.operations.shutil.rmtree", _failing_rmtree)

    result = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample),),
        core_config_service=service,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.event.code == 2213
    assert result.error.safe_details is not None
    assert result.error.safe_details["original_exception_type"] == "RuntimeError"
    assert result.error.safe_details["cleanup_exception_type"] == "OSError"
    assert len(_task_directories(resolved_config.tasks_dir)) == 1


def test_run_list_show_and_jobs_history_use_new_task_snapshot_model(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved_config = service.load_resolved_config()
    sample_a = tmp_path / "a.fasta"
    sample_b = tmp_path / "b.fasta"
    sample_c = tmp_path / "c.fasta"
    _write_sample(sample_a, sample_id="a")
    _write_sample(sample_b, sample_id="b")
    _write_sample(sample_c, sample_id="c")

    _create_registered_task_for_listing(
        service=service, task_id="task-a", sample_path=str(sample_a), priority=2
    )
    _create_registered_task_for_listing(
        service=service, task_id="task-b", sample_path=str(sample_b), priority=3
    )

    registry_service = AnalyticalTaskRegistryService(database_path=resolved_config.database_path)
    start_a = registry_service.start(task_id="task-a")
    assert start_a.task is not None
    assert start_a.task.state is AnalyticalTaskState.QUEUED
    running_a = registry_service.transition_active_job_state(
        task_id="task-a",
        to_state=AnalyticalTaskState.RUNNING,
    )
    assert running_a.task is not None
    failed_a = registry_service.transition_active_job_state(
        task_id="task-a",
        to_state=AnalyticalTaskState.FAILED,
        finished_reason="failed",
        error_event_code=2011,
    )
    assert failed_a.task is not None
    restart_a = registry_service.start(task_id="task-a")
    assert restart_a.result_type is not None

    listed = run_list_analytical_tasks(
        states=("waiting", "queued"),
        limit=10,
        offset=0,
        core_config_service=service,
    )
    assert listed.ok is True
    assert listed.value is not None
    assert len(listed.value) == 2
    assert all(
        snapshot.task.state in {AnalyticalTaskState.WAITING, AnalyticalTaskState.QUEUED}
        for snapshot in listed.value
    )

    shown = run_get_analytical_task(task_id="task-a", core_config_service=service)
    assert shown.ok is True
    assert shown.value is not None
    assert shown.value.task.task_id == "task-a"
    assert shown.value.active_or_latest_job is not None

    jobs = run_list_analytical_task_jobs(task_id="task-a", core_config_service=service)
    assert jobs.ok is True
    assert jobs.value is not None
    assert len(jobs.value) == 2
    assert jobs.value[0].task_id == "task-a"
    assert jobs.value[1].task_id == "task-a"


@pytest.mark.parametrize(
    ("states", "limit", "offset"),
    [
        (("unknown_state",), None, 0),
        (None, 0, 0),
        (None, 501, 0),
        (None, 50, -1),
    ],
)
def test_run_list_rejects_invalid_request(
    tmp_path: Path,
    states: tuple[str, ...] | None,
    limit: int | None,
    offset: int,
) -> None:
    service = _initialize_core(tmp_path / "home")

    result = run_list_analytical_tasks(
        states=states,
        limit=limit,
        offset=offset,
        core_config_service=service,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.event.code == 2212


def test_run_show_returns_not_found_for_unknown_task(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")

    result = run_get_analytical_task(task_id="unknown-task", core_config_service=service)

    assert result.ok is False
    assert result.error is not None
    assert result.error.event.code == 2210


def test_task_log_write_failure_rolls_back_workspace_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jelica_home = tmp_path / "home"
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")
    service = _initialize_core(jelica_home)
    original_emit = JsonlFileEventSink._emit

    def _failing_emit(self: JsonlFileEventSink, event: Any) -> None:
        if self.path.name == TASK_EVENTS_LOG_FILENAME:
            raise EventSinkError(
                sink_name=JsonlFileEventSink.__name__,
                detail="simulated task log write failure",
                path=self.path,
            )
        original_emit(self, event)

    monkeypatch.setattr(JsonlFileEventSink, "_emit", _failing_emit)

    result = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample),),
        core_config_service=service,
    )

    assert result.ok is False
    resolved_config = service.load_resolved_config()
    assert _task_directories(resolved_config.tasks_dir) == []
    assert (
        AnalyticalTaskRegistryService(database_path=resolved_config.database_path).list_tasks(
            limit=None
        )
        == []
    )


def test_analyze_registry_registration_conflict_rolls_back_workspace_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jelica_home = tmp_path / "home"
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="a")
    service = _initialize_core(jelica_home)
    resolved_config = service.load_resolved_config()

    def _failing_register(
        self: AnalyticalTaskRegistryService,
        *,
        task_id: str,
        name: str | None = None,
        automatic_name_base: str | None = None,
        task_dir_relative_path: str,
        default_priority: int = 1,
        current_config_revision: int = 1,
        current_config_relative_path: str,
        current_config_hash: str,
    ) -> None:
        raise AnalyticalTaskAlreadyExistsError(field_name="task_id", field_value=task_id)

    monkeypatch.setattr(AnalyticalTaskRegistryService, "register_task", _failing_register)

    result = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample),),
        core_config_service=service,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.event.code == 2211
    assert _task_directories(resolved_config.tasks_dir) == []
    assert (
        AnalyticalTaskRegistryService(database_path=resolved_config.database_path).list_tasks(
            limit=None
        )
        == []
    )
