from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from jelica_core.comparative_analysis import (
    COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
    COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
    COMPARATIVE_ANALYSIS_STAGE_ID,
    DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
    ComparativeAnalysisManifest,
    ComparativeAnalysisStatus,
    ComparativeArtifactMetadata,
    ComparisonPlanCounts,
)
from jelica_core.runtime.artifacts import (
    StageArtifactManifest,
    StageCommitError,
    StageSnapshotErrorCode,
    StageSnapshotValidationError,
    commit_stage_directory,
    validate_committed_stage_snapshot,
    write_stage_manifest,
)
from jelica_core.runtime.engine import ExecutionRuntime
from jelica_core.runtime.input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    INPUT_PROCESSING_STAGE_ID,
    InputProcessingDatasetSummary,
    InputProcessingManifest,
    InputProcessingState,
)
from jelica_core.runtime.models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RuntimeStateCheckpoint,
)
from jelica_core.runtime.pipeline import PipelineDefinition, PipelineStage
from jelica_core.tasks.storage import write_text_atomically

_TASK_ID = "task-snapshot"
_JOB_ID = "job-snapshot"
_CONFIG_HASH = "c" * 64
_FIRST_WORKER_ID = "worker-first"
_SECOND_WORKER_ID = "worker-second"
_INPUT_MANIFEST_SOURCE = (
    f"stages/{INPUT_PROCESSING_STAGE_ID}/{INPUT_PROCESSING_MANIFEST_RELATIVE_PATH}"
)
_SUMMARY_PAYLOAD = b'{"metric_count":0}\n'


def _input_processing_manifest() -> InputProcessingManifest:
    return InputProcessingManifest(
        task_id=_TASK_ID,
        job_id=_JOB_ID,
        config_revision_path="configs/000001.json",
        config_hash=_CONFIG_HASH,
        generated_at="2026-08-05T00:00:00Z",
        processing_state=InputProcessingState.COMPLETED,
        dataset_summary=InputProcessingDatasetSummary(
            discovered_record_count=0,
            valid_sample_count=0,
            invalid_sample_count=0,
            unique_sequence_count=0,
            duplicate_logical_sample_count=0,
            comparative_analysis_available=False,
        ),
    )


def _not_requested_category() -> dict[str, object]:
    return {
        "status": "not_requested",
        "requested": False,
        "total": 0,
        "completed": 0,
        "successful": 0,
        "failed": 0,
    }


def _comparative_manifest(
    *,
    summary_payload: bytes = _SUMMARY_PAYLOAD,
    task_id: str = _TASK_ID,
    job_id: str = _JOB_ID,
    config_hash: str = _CONFIG_HASH,
    status: ComparativeAnalysisStatus = ComparativeAnalysisStatus.COMPLETED,
    source_artifacts: tuple[str, ...] = (_INPUT_MANIFEST_SOURCE,),
    published_sha256: str | None = None,
    published_size: int | None = None,
) -> ComparativeAnalysisManifest:
    partial = status is ComparativeAnalysisStatus.PARTIAL_SUCCESS
    failed = status is ComparativeAnalysisStatus.FAILED
    metadata = ComparativeArtifactMetadata(
        relative_path=DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
        size_bytes=len(summary_payload) if published_size is None else published_size,
        sha256=published_sha256 or hashlib.sha256(summary_payload).hexdigest(),
    )
    return ComparativeAnalysisManifest(
        task_id=task_id,
        job_id=job_id,
        config_hash=config_hash,
        enabled=True,
        normalized_settings={
            "enabled": True,
            "statistics": {"enabled": True},
            "reference": {"mode": "disabled"},
        },
        status=status,
        alignment_mode="none",
        reference_mode="disabled",
        uracil_thymine_equivalent=False,
        started_at="2026-08-05T00:00:00Z",
        completed_at="2026-08-05T00:00:01Z",
        duration_seconds=1.0,
        source_artifacts=source_artifacts,
        plan_counts=ComparisonPlanCounts(
            occurrence_count=0,
            unique_logical_operation_count=0,
            duplicate_occurrence_count=0,
            scan_computation_count=0,
            identical_sequence_projection_count=0,
        ),
        category_execution={
            "statistics": {
                "status": (
                    "failed" if failed else "partial_success" if partial else "completed"
                ),
                "requested": True,
                "total": 2 if partial else 1,
                "completed": 2 if partial else 1,
                "successful": 0 if failed else 1,
                "failed": 1 if partial or failed else 0,
                "available": not failed,
                "artifact_paths": (metadata.relative_path,),
            },
            "reference_sequence_differences": _not_requested_category(),
            "pairwise_sequence_differences": _not_requested_category(),
        },
        successful_result_count=0 if failed else 1,
        failed_result_count=1 if partial or failed else 0,
        failure_count=1 if partial or failed else 0,
        artifacts=(metadata,),
    )


def _write_model(
    *,
    path: Path,
    model: InputProcessingManifest | ComparativeAnalysisManifest,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(path=path, payload=model.model_dump_json())


def _write_generic_manifest(
    *,
    root: Path,
    stage_id: str,
    worker_instance_id: str,
    artifacts: tuple[str, ...],
    completed_at: str = "2026-08-05T00:00:01Z",
) -> Path:
    return write_stage_manifest(
        directory=root,
        manifest=StageArtifactManifest(
            stage_id=stage_id,
            job_id=_JOB_ID,
            worker_instance_id=worker_instance_id,
            pipeline_version=DEFAULT_PIPELINE_VERSION,
            completed_at=completed_at,
            artifacts=artifacts,
        ),
    )


def _write_committed_input_prefix(job_dir: Path) -> None:
    root = job_dir / "stages" / INPUT_PROCESSING_STAGE_ID
    _write_model(
        path=root / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
        model=_input_processing_manifest(),
    )
    _write_generic_manifest(
        root=root,
        stage_id=INPUT_PROCESSING_STAGE_ID,
        worker_instance_id=_FIRST_WORKER_ID,
        artifacts=(INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,),
    )


def _write_comparative_snapshot(
    *,
    root: Path,
    worker_instance_id: str,
    summary_payload: bytes = _SUMMARY_PAYLOAD,
    manifest: ComparativeAnalysisManifest | None = None,
    completed_at: str = "2026-08-05T00:00:01Z",
) -> Path:
    domain_manifest = manifest or _comparative_manifest(summary_payload=summary_payload)
    summary_path = root / DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(summary_payload)
    _write_model(
        path=root / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
        model=domain_manifest,
    )
    return _write_generic_manifest(
        root=root,
        stage_id=COMPARATIVE_ANALYSIS_STAGE_ID,
        worker_instance_id=worker_instance_id,
        completed_at=completed_at,
        artifacts=(
            COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
            DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
        ),
    )


def _validate_comparative(job_dir: Path):
    return validate_committed_stage_snapshot(
        job_dir=job_dir,
        stage_id=COMPARATIVE_ANALYSIS_STAGE_ID,
        expected_job_id=_JOB_ID,
        expected_pipeline_version=DEFAULT_PIPELINE_VERSION,
        expected_task_id=_TASK_ID,
        expected_config_hash=_CONFIG_HASH,
    )


def _commit_comparative(
    *,
    job_dir: Path,
    staging_root: Path,
    worker_instance_id: str,
    manifest_path: Path,
) -> StageArtifactManifest:
    return commit_stage_directory(
        job_dir=job_dir,
        stage_id=COMPARATIVE_ANALYSIS_STAGE_ID,
        job_id=_JOB_ID,
        worker_instance_id=worker_instance_id,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        staging_directory=staging_root,
        manifest_path=manifest_path,
        task_id=_TASK_ID,
        config_hash=_CONFIG_HASH,
    )


def test_validate_committed_comparative_snapshot_with_valid_input_prefix(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    _write_committed_input_prefix(job_dir)
    root = job_dir / "stages" / COMPARATIVE_ANALYSIS_STAGE_ID
    _write_comparative_snapshot(
        root=root,
        worker_instance_id=_FIRST_WORKER_ID,
    )

    snapshot = _validate_comparative(job_dir)

    fingerprints = {
        item.relative_path: item for item in snapshot.artifact_fingerprints
    }
    assert snapshot.domain_status == ComparativeAnalysisStatus.COMPLETED.value
    assert snapshot.config_hash == _CONFIG_HASH
    assert snapshot.source_artifacts == (_INPUT_MANIFEST_SOURCE,)
    assert fingerprints[DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH].sha256 == (
        hashlib.sha256(_SUMMARY_PAYLOAD).hexdigest()
    )


def test_reconciliation_accepts_valid_comparative_snapshot_and_upstream_prefix(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    _write_committed_input_prefix(job_dir)
    _write_comparative_snapshot(
        root=job_dir / "stages" / COMPARATIVE_ANALYSIS_STAGE_ID,
        worker_instance_id=_FIRST_WORKER_ID,
    )
    pipeline = PipelineDefinition(
        name=DEFAULT_PIPELINE_NAME,
        version=DEFAULT_PIPELINE_VERSION,
        stages=(
            cast(
                PipelineStage,
                SimpleNamespace(stage_id=INPUT_PROCESSING_STAGE_ID, weight=1.0),
            ),
            cast(
                PipelineStage,
                SimpleNamespace(stage_id=COMPARATIVE_ANALYSIS_STAGE_ID, weight=1.0),
            ),
        ),
    )

    checkpoint = ExecutionRuntime._reconcile_committed_stages(
        cast(ExecutionRuntime, object()),
        checkpoint=RuntimeStateCheckpoint.new(
            pipeline_version=DEFAULT_PIPELINE_VERSION
        ),
        pipeline_definition=pipeline,
        job_dir=job_dir,
        task_id=_TASK_ID,
        job_id=_JOB_ID,
        config_hash=_CONFIG_HASH,
    )

    assert checkpoint.completed_stages == (
        INPUT_PROCESSING_STAGE_ID,
        COMPARATIVE_ANALYSIS_STAGE_ID,
    )


@pytest.mark.parametrize(
    ("stage_id", "relative_path"),
    (
        ("initialize_job", "execution_manifest.json"),
        ("input_acquisition", "inputs/input_manifest.json"),
    ),
)
def test_foundation_stage_domain_identity_is_validated(
    tmp_path: Path,
    stage_id: str,
    relative_path: str,
) -> None:
    job_dir = tmp_path / "job"
    root = job_dir / "stages" / stage_id
    payload = {
        "task_id": _TASK_ID,
        "job_id": _JOB_ID,
        "config_hash": _CONFIG_HASH,
    }
    if stage_id == "initialize_job":
        payload.update(
            worker_instance_id=_FIRST_WORKER_ID,
            pipeline_version=DEFAULT_PIPELINE_VERSION,
        )
    domain_path = root / relative_path
    domain_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(path=domain_path, payload=json.dumps(payload))
    _write_generic_manifest(
        root=root,
        stage_id=stage_id,
        worker_instance_id=_FIRST_WORKER_ID,
        artifacts=(relative_path,),
    )

    snapshot = validate_committed_stage_snapshot(
        job_dir=job_dir,
        stage_id=stage_id,
        expected_job_id=_JOB_ID,
        expected_pipeline_version=DEFAULT_PIPELINE_VERSION,
        expected_task_id=_TASK_ID,
        expected_config_hash=_CONFIG_HASH,
    )
    assert snapshot.config_hash == _CONFIG_HASH

    payload["task_id"] = "task-other"
    write_text_atomically(path=domain_path, payload=json.dumps(payload))
    with pytest.raises(StageSnapshotValidationError) as captured:
        validate_committed_stage_snapshot(
            job_dir=job_dir,
            stage_id=stage_id,
            expected_job_id=_JOB_ID,
            expected_pipeline_version=DEFAULT_PIPELINE_VERSION,
            expected_task_id=_TASK_ID,
            expected_config_hash=_CONFIG_HASH,
        )
    assert captured.value.code == StageSnapshotErrorCode.IDENTITY_MISMATCH.value


@pytest.mark.parametrize(
    ("corruption", "expected_code"),
    (
        ("missing-domain", StageSnapshotErrorCode.ARTIFACT_MISSING),
        ("missing-data", StageSnapshotErrorCode.ARTIFACT_MISSING),
        ("hash", StageSnapshotErrorCode.HASH_MISMATCH),
        ("size", StageSnapshotErrorCode.SIZE_MISMATCH),
        ("task-identity", StageSnapshotErrorCode.IDENTITY_MISMATCH),
        ("job-identity", StageSnapshotErrorCode.IDENTITY_MISMATCH),
        ("config-identity", StageSnapshotErrorCode.IDENTITY_MISMATCH),
        ("upstream", StageSnapshotErrorCode.UPSTREAM_INVALID),
        ("missing-upstream", StageSnapshotErrorCode.UPSTREAM_INVALID),
    ),
    ids=(
        "missing-domain",
        "missing-data",
        "hash",
        "size",
        "task-identity",
        "job-identity",
        "config-identity",
        "upstream",
        "missing-upstream",
    ),
)
def test_validator_rejects_incomplete_or_inconsistent_comparative_snapshot(
    tmp_path: Path,
    corruption: str,
    expected_code: StageSnapshotErrorCode,
) -> None:
    job_dir = tmp_path / "job"
    if corruption != "missing-upstream":
        _write_committed_input_prefix(job_dir)
    root = job_dir / "stages" / COMPARATIVE_ANALYSIS_STAGE_ID
    _write_comparative_snapshot(
        root=root,
        worker_instance_id=_FIRST_WORKER_ID,
    )

    domain_path = root / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH
    if corruption == "missing-domain":
        domain_path.unlink()
    elif corruption == "missing-data":
        (root / DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH).unlink()
    elif corruption == "hash":
        _write_model(
            path=domain_path,
            model=_comparative_manifest(published_sha256="f" * 64),
        )
    elif corruption == "size":
        _write_model(
            path=domain_path,
            model=_comparative_manifest(published_size=999),
        )
    elif corruption == "task-identity":
        _write_model(
            path=domain_path,
            model=_comparative_manifest(task_id="task-other"),
        )
    elif corruption == "job-identity":
        _write_model(
            path=domain_path,
            model=_comparative_manifest(job_id="job-other"),
        )
    elif corruption == "config-identity":
        _write_model(
            path=domain_path,
            model=_comparative_manifest(config_hash="d" * 64),
        )
    elif corruption == "upstream":
        _write_model(
            path=domain_path,
            model=_comparative_manifest(source_artifacts=()),
        )

    with pytest.raises(StageSnapshotValidationError) as captured:
        _validate_comparative(job_dir)

    assert captured.value.code == expected_code.value
    assert captured.value.stage_id == COMPARATIVE_ANALYSIS_STAGE_ID


def test_validator_checks_published_jsonl_record_count(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    _write_committed_input_prefix(job_dir)
    root = job_dir / "stages" / COMPARATIVE_ANALYSIS_STAGE_ID
    jsonl_payload = b'{"failure_id":"first"}\n{"failure_id":"second"}\n'
    metadata = ComparativeArtifactMetadata(
        relative_path=COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
        size_bytes=len(jsonl_payload),
        sha256=hashlib.sha256(jsonl_payload).hexdigest(),
        record_count=1,
    )
    base_manifest = _comparative_manifest()
    categories = dict(base_manifest.category_execution)
    categories["statistics"] = categories["statistics"].model_copy(
        update={"artifact_paths": (metadata.relative_path,)}
    )
    domain_manifest = base_manifest.model_copy(
        update={"artifacts": (metadata,), "category_execution": categories}
    )
    artifact_path = root / metadata.relative_path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(jsonl_payload)
    _write_model(
        path=root / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
        model=domain_manifest,
    )
    _write_generic_manifest(
        root=root,
        stage_id=COMPARATIVE_ANALYSIS_STAGE_ID,
        worker_instance_id=_FIRST_WORKER_ID,
        artifacts=(
            COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
            metadata.relative_path,
        ),
    )

    with pytest.raises(StageSnapshotValidationError) as captured:
        _validate_comparative(job_dir)

    assert captured.value.code == StageSnapshotErrorCode.RECORD_COUNT_MISMATCH.value


def test_commit_is_idempotent_for_identical_content_from_different_worker(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    _write_committed_input_prefix(job_dir)
    first_root = (
        job_dir / "staging" / COMPARATIVE_ANALYSIS_STAGE_ID / _FIRST_WORKER_ID
    )
    first_manifest_path = _write_comparative_snapshot(
        root=first_root,
        worker_instance_id=_FIRST_WORKER_ID,
    )
    first_committed = _commit_comparative(
        job_dir=job_dir,
        staging_root=first_root,
        worker_instance_id=_FIRST_WORKER_ID,
        manifest_path=first_manifest_path,
    )
    second_root = (
        job_dir / "staging" / COMPARATIVE_ANALYSIS_STAGE_ID / _SECOND_WORKER_ID
    )
    second_manifest_path = _write_comparative_snapshot(
        root=second_root,
        worker_instance_id=_SECOND_WORKER_ID,
        completed_at="2026-08-05T00:00:02Z",
    )

    repeated = _commit_comparative(
        job_dir=job_dir,
        staging_root=second_root,
        worker_instance_id=_SECOND_WORKER_ID,
        manifest_path=second_manifest_path,
    )

    assert repeated == first_committed
    assert repeated.worker_instance_id == _FIRST_WORKER_ID
    assert not second_root.exists()


@pytest.mark.parametrize(
    ("conflict", "expected_code"),
    (
        ("content", StageSnapshotErrorCode.COMMIT_CONFLICT),
        ("status", StageSnapshotErrorCode.COMMIT_CONFLICT),
        ("existing-failed", StageSnapshotErrorCode.COMMIT_CONFLICT),
        ("hash", StageSnapshotErrorCode.HASH_MISMATCH),
    ),
    ids=("content", "status", "existing-failed", "hash"),
)
def test_commit_rejects_content_status_or_hash_conflict(
    tmp_path: Path,
    conflict: str,
    expected_code: StageSnapshotErrorCode,
) -> None:
    job_dir = tmp_path / "job"
    _write_committed_input_prefix(job_dir)
    first_root = (
        job_dir / "staging" / COMPARATIVE_ANALYSIS_STAGE_ID / _FIRST_WORKER_ID
    )
    first_domain_manifest = (
        _comparative_manifest(status=ComparativeAnalysisStatus.FAILED)
        if conflict == "existing-failed"
        else None
    )
    first_manifest_path = _write_comparative_snapshot(
        root=first_root,
        worker_instance_id=_FIRST_WORKER_ID,
        manifest=first_domain_manifest,
    )
    _commit_comparative(
        job_dir=job_dir,
        staging_root=first_root,
        worker_instance_id=_FIRST_WORKER_ID,
        manifest_path=first_manifest_path,
    )

    second_root = (
        job_dir / "staging" / COMPARATIVE_ANALYSIS_STAGE_ID / _SECOND_WORKER_ID
    )
    summary_payload = (
        b'{"metric_count":1}\n' if conflict == "content" else _SUMMARY_PAYLOAD
    )
    if conflict == "status":
        domain_manifest = _comparative_manifest(
            status=ComparativeAnalysisStatus.PARTIAL_SUCCESS
        )
    elif conflict == "hash":
        domain_manifest = _comparative_manifest(published_sha256="f" * 64)
    else:
        domain_manifest = _comparative_manifest(summary_payload=summary_payload)
    second_manifest_path = _write_comparative_snapshot(
        root=second_root,
        worker_instance_id=_SECOND_WORKER_ID,
        summary_payload=summary_payload,
        manifest=domain_manifest,
    )

    with pytest.raises(StageCommitError) as captured:
        _commit_comparative(
            job_dir=job_dir,
            staging_root=second_root,
            worker_instance_id=_SECOND_WORKER_ID,
            manifest_path=second_manifest_path,
        )

    assert captured.value.code == expected_code.value
    assert (
        job_dir
        / "stages"
        / COMPARATIVE_ANALYSIS_STAGE_ID
        / DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH
    ).read_bytes() == _SUMMARY_PAYLOAD
    assert second_root.exists()
