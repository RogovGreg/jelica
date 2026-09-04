from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from jelica_core.events import (
    run_cancel_analytical_task,
    run_initialize_analysis_task_from_inputs,
    run_update_analytical_task,
)
from jelica_core.result_package import ResultPackageTaskInfo, ResultPackageTaskStatus
from jelica_core.runtime import NullProgressReporter, RuntimeStateCheckpoint, WorkerLaunchSpec
from jelica_core.runtime.pipeline import InitializeJobStage, StageContext
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import (
    AnalyticalTaskInvalidRecordDataError,
    AnalyticalTaskRegistryService,
)
from jelica_core.tasks.storage import compute_config_hash


def test_rejected_task_operation_error_uses_authoritative_trace_id(
    tmp_path: Path,
) -> None:
    service = CoreConfigService(jelica_home=tmp_path / "home")
    service.initialize_system_config(force=True)
    trace_id = UUID("8b1c9d4e-1c33-4ab9-81b6-21408cc92cc4")
    initialized = run_initialize_analysis_task_from_inputs(
        trace_id=trace_id,
        config_json=None,
        raw_overrides=(),
        positional_sources=("sample-a.fasta",),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None

    cancelled = run_cancel_analytical_task(
        task_id=initialized.value.task_id,
        core_config_service=service,
    )

    assert cancelled.ok is False
    assert cancelled.error is not None
    assert cancelled.error.event.trace_id == trace_id
    assert cancelled.error.event.task_id == initialized.value.task_id


def test_task_update_preserves_authoritative_trace_id(tmp_path: Path) -> None:
    service = CoreConfigService(jelica_home=tmp_path / "home")
    service.initialize_system_config(force=True)
    original_trace_id = UUID("8b1c9d4e-1c33-4ab9-81b6-21408cc92cc4")
    conflicting_trace_id = UUID("7f209239-3104-48f6-b634-2a72f7b035de")
    initialized = run_initialize_analysis_task_from_inputs(
        trace_id=original_trace_id,
        config_json=None,
        raw_overrides=(),
        positional_sources=("sample-a.fasta",),
        core_config_service=service,
    )
    assert initialized.ok is True
    assert initialized.value is not None

    updated = run_update_analytical_task(
        task_id=initialized.value.task_id,
        config_json=json.dumps(
            {
                "trace_id": str(conflicting_trace_id),
                "samples": ["sample-b.fasta"],
                "priority": 2,
            }
        ),
        core_config_service=service,
    )

    assert updated.ok is True
    resolved = service.require_initialized_config()
    config_path = resolved.tasks_dir / initialized.value.task_id / "config.json"
    config_document = json.loads(config_path.read_text(encoding="utf-8"))
    assert config_document["trace_id"] == str(original_trace_id)
    assert config_document["priority"] == 2
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    assert registry.get_task_trace_id(task_id=initialized.value.task_id) == original_trace_id

    config_document["trace_id"] = str(conflicting_trace_id)
    with pytest.raises(AnalyticalTaskInvalidRecordDataError, match="immutable"):
        registry.update_task_config(
            task_id=initialized.value.task_id,
            config_document=config_document,
        )


def test_registry_trace_lookup_supports_legacy_config_without_trace_id(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "data" / "jelica.db"
    registry = AnalyticalTaskRegistryService(database_path=database_path)
    task_id = "legacy-task"
    config_document = {"priority": 1}
    config_path = tmp_path / "data" / "tasks" / task_id / "configs" / "000001.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(config_document), encoding="utf-8")
    registry.register_task(
        task_id=task_id,
        task_dir_relative_path=task_id,
        current_config_relative_path="configs/000001.json",
        current_config_hash=compute_config_hash(config_document),
    )

    assert registry.get_task_trace_id(task_id=task_id) is None


def test_initialize_stage_propagates_trace_id_to_execution_manifest(tmp_path: Path) -> None:
    trace_id = "8b1c9d4e-1c33-4ab9-81b6-21408cc92cc4"
    task_dir = tmp_path / "task"
    job_dir = task_dir / "jobs" / "job-1"
    config_revision_path = task_dir / "configs" / "000001.json"
    config_revision_path.parent.mkdir(parents=True)
    config_document = {"schema_version": 1, "trace_id": trace_id}
    config_revision_path.write_text(json.dumps(config_document), encoding="utf-8")
    staging_directory = tmp_path / "staging"
    launch_spec = WorkerLaunchSpec(
        task_id="task-1",
        job_id="job-1",
        worker_instance_id="worker-1",
        lease_token="lease-1",
        database_path=tmp_path / "jelica.db",
        task_dir=task_dir,
        job_dir=job_dir,
        config_revision_path=config_revision_path,
        config_hash=compute_config_hash(config_document),
        runtime_state_json=RuntimeStateCheckpoint.new(
            pipeline_version="v1"
        ).to_runtime_state_json(),
        pipeline_name="initialize_only",
        pipeline_version="v1",
        trace_id=trace_id,
    )
    context = StageContext(
        launch_spec=launch_spec,
        stage_index=0,
        stage_staging_directory=staging_directory,
    )
    stage = InitializeJobStage()
    stage.preflight(context)

    stage.run(context, NullProgressReporter())

    manifest = json.loads(
        (staging_directory / "execution_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["trace_id"] == trace_id


def test_result_package_task_metadata_supports_trace_and_legacy_payloads() -> None:
    trace_id = UUID("8b1c9d4e-1c33-4ab9-81b6-21408cc92cc4")
    metadata = ResultPackageTaskInfo(
        task_id="task-1",
        trace_id=trace_id,
        status=ResultPackageTaskStatus.COMPLETED,
        created_at="2026-08-21T00:00:00Z",
        completed_at="2026-08-21T00:00:01Z",
    )
    legacy = ResultPackageTaskInfo.model_validate(
        {
            "task_id": "legacy-task",
            "status": "completed",
            "created_at": "2026-08-20T00:00:00Z",
            "completed_at": "2026-08-20T00:00:01Z",
        }
    )

    assert metadata.model_dump(mode="json")["trace_id"] == str(trace_id)
    assert legacy.trace_id is None
    assert "trace_id" not in legacy.model_dump(mode="json")
