from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import jelica_core.result_package.artifacts as result_package_artifacts_module
from jelica_core.result_package import (
    JELICA_PACKAGE_CONFIGURATION_PATH,
    JELICA_PACKAGE_INPUT_MANIFEST_PATH,
    JELICA_PACKAGE_MANIFEST_PATH,
    JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
    JELICA_PACKAGE_NOTES_PATH,
    JELICA_PACKAGE_TASK_PATH,
    RESULT_PACKAGE_LINK_FILENAME,
    RESULT_PACKAGE_PREPARED_DIRNAME,
    RESULT_PACKAGE_STAGE_ID,
    RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,
    JelicaPackageManifest,
    ResultPackageArtifactInfo,
    ResultPackageLink,
    ResultPackageProducerInfo,
    ResultPackagePublicationError,
    ResultPackageStageInfo,
    ResultPackageStageManifest,
    ResultPackageTaskInfo,
    ResultPackageTaskStatus,
    ResultPackageValidationError,
    compute_content_id,
    content_digest_from_content_id,
    infer_media_type,
    load_result_package_link,
    publish_prepared_result_package,
    relative_package_path_from_task,
    result_package_target_path,
    serialize_stable_json,
    validate_result_package_file,
    write_model_json,
    write_result_package_link,
)
from jelica_core.runtime import engine as engine_module
from jelica_core.runtime.artifacts import (
    StageArtifactManifest,
    validate_committed_stage_snapshot,
    write_stage_manifest,
)
from jelica_core.runtime.engine import ExecutionRuntime
from jelica_core.runtime.messages import StageReadyToCommitMessage
from jelica_core.runtime.models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RuntimeStateCheckpoint,
)
from jelica_core.runtime.pipeline import PipelineDefinition


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _manual_content_id(artifacts: tuple[ResultPackageArtifactInfo, ...]) -> str:
    digest = hashlib.sha256()
    sorted_artifacts = sorted(artifacts, key=lambda item: item.path.encode("utf-8"))
    for artifact in sorted_artifacts:
        digest.update(artifact.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(artifact.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(artifact.sha256.encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _protected_payloads() -> dict[str, bytes]:
    return {
        JELICA_PACKAGE_TASK_PATH: b'{"task_id":"task-1","status":"completed"}\n',
        JELICA_PACKAGE_CONFIGURATION_PATH: b'{"alignment":{"mode":"none"}}\n',
        JELICA_PACKAGE_INPUT_MANIFEST_PATH: b'{"sources":[]}\n',
        JELICA_PACKAGE_NORMALIZED_FASTA_PATH: b">sample-a\nACTG\n",
        "results/comparative_analysis/manifest.json": b'{"status":"completed"}\n',
    }


def _artifact_stage(path: str) -> str | None:
    if path == JELICA_PACKAGE_INPUT_MANIFEST_PATH:
        return "input_acquisition"
    if path == JELICA_PACKAGE_NORMALIZED_FASTA_PATH:
        return "input_processing"
    if path.startswith("results/comparative_analysis/"):
        return "comparative_analysis"
    return None


def _build_manifest(
    *,
    package_created_at: str = "2026-08-01T00:00:00Z",
) -> tuple[JelicaPackageManifest, dict[str, bytes]]:
    payloads = _protected_payloads()
    artifacts = tuple(
        ResultPackageArtifactInfo(
            path=path,
            stage=_artifact_stage(path),
            media_type=infer_media_type(path),
            size=len(payload),
            sha256=_sha256(payload),
        )
        for path, payload in sorted(payloads.items())
    )
    content_id = compute_content_id(artifacts=artifacts)
    manifest = JelicaPackageManifest(
        content_id=content_id,
        producer=ResultPackageProducerInfo(version="1.0.0-test"),
        package_created_at=package_created_at,
        task=ResultPackageTaskInfo(
            task_id="task-1",
            status=ResultPackageTaskStatus.COMPLETED,
            created_at="2026-08-01T00:00:00Z",
            completed_at="2026-08-01T00:00:01Z",
        ),
        stages=(
            ResultPackageStageInfo(
                name="input_acquisition",
                status="completed",
                artifacts=(JELICA_PACKAGE_INPUT_MANIFEST_PATH,),
            ),
            ResultPackageStageInfo(
                name="input_processing",
                status="completed",
                artifacts=(JELICA_PACKAGE_NORMALIZED_FASTA_PATH,),
            ),
            ResultPackageStageInfo(
                name="comparative_analysis",
                status="completed",
                artifacts=("results/comparative_analysis/manifest.json",),
            ),
        ),
        artifacts=artifacts,
    )
    return manifest, payloads


def _write_package(
    *,
    package_path: Path,
    manifest: JelicaPackageManifest,
    payloads: dict[str, bytes],
    include_notes: bool = False,
    extra_entries: dict[str, bytes] | None = None,
) -> None:
    package_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        package_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for path, payload in sorted(payloads.items()):
            archive.writestr(path, payload)
        if include_notes:
            archive.writestr(JELICA_PACKAGE_NOTES_PATH, "user notes\n".encode("utf-8"))
        for path, payload in sorted((extra_entries or {}).items()):
            archive.writestr(path, payload)
        archive.writestr(
            JELICA_PACKAGE_MANIFEST_PATH,
            serialize_stable_json(manifest.model_dump(mode="json")).encode("utf-8"),
        )


def _build_stage_manifest(
    *,
    task_dir: Path,
    prepared_package_relative_path: str,
    manifest: JelicaPackageManifest,
) -> ResultPackageStageManifest:
    content_digest = content_digest_from_content_id(manifest.content_id)
    target_path = result_package_target_path(
        task_dir=task_dir,
        content_digest=content_digest,
    )
    return ResultPackageStageManifest(
        task_id=manifest.task.task_id,
        job_id="job-1",
        config_hash="0" * 64,
        task_status=manifest.task.status,
        content_id=manifest.content_id,
        content_digest=content_digest,
        package_created_at=manifest.package_created_at,
        prepared_package_relative_path=prepared_package_relative_path,
        published_package_relative_path=relative_package_path_from_task(
            task_dir=task_dir,
            package_path=target_path,
        ),
        task=manifest.task,
        source_stage_ids=("comparative_analysis",),
        artifact_count=len(manifest.artifacts),
        stage_count=1,
    )


def test_compute_content_id_matches_specified_algorithm_and_is_order_independent() -> None:
    manifest, _ = _build_manifest()
    artifacts = manifest.artifacts
    reversed_artifacts = tuple(reversed(artifacts))

    expected = _manual_content_id(artifacts)

    assert compute_content_id(artifacts=artifacts) == expected
    assert compute_content_id(artifacts=reversed_artifacts) == expected


def test_compute_content_id_changes_when_protected_artifact_changes() -> None:
    manifest, _ = _build_manifest()
    baseline = manifest.artifacts
    mutated = list(baseline)
    original = mutated[0]
    mutated[0] = ResultPackageArtifactInfo(
        path=original.path,
        stage=original.stage,
        media_type=original.media_type,
        size=original.size + 1,
        sha256=original.sha256,
    )

    assert compute_content_id(artifacts=tuple(mutated)) != compute_content_id(artifacts=baseline)


def test_content_id_is_stable_across_different_package_timestamps() -> None:
    manifest_a, _ = _build_manifest(package_created_at="2026-08-01T00:00:00Z")
    manifest_b, _ = _build_manifest(package_created_at="2026-08-02T12:30:45Z")

    assert manifest_a.content_id == manifest_b.content_id
    assert manifest_a.package_created_at != manifest_b.package_created_at


def test_validate_result_package_file_accepts_valid_package_and_notes_when_allowed(
    tmp_path: Path,
) -> None:
    manifest, payloads = _build_manifest()
    package_path = tmp_path / "valid.jelica"
    _write_package(
        package_path=package_path,
        manifest=manifest,
        payloads=payloads,
        include_notes=True,
    )

    validated = validate_result_package_file(
        path=package_path,
        expected_content_id=manifest.content_id,
        require_notes_absent=False,
    )

    assert validated.content_id == manifest.content_id
    assert validated.has_notes is True


def test_validate_result_package_file_rejects_notes_for_automatic_package(
    tmp_path: Path,
) -> None:
    manifest, payloads = _build_manifest()
    package_path = tmp_path / "with-notes.jelica"
    _write_package(
        package_path=package_path,
        manifest=manifest,
        payloads=payloads,
        include_notes=True,
    )

    with pytest.raises(ResultPackageValidationError):
        validate_result_package_file(
            path=package_path,
            expected_content_id=manifest.content_id,
            require_notes_absent=True,
        )


def test_validate_result_package_file_rejects_unknown_entries(tmp_path: Path) -> None:
    manifest, payloads = _build_manifest()
    package_path = tmp_path / "unknown-entry.jelica"
    _write_package(
        package_path=package_path,
        manifest=manifest,
        payloads=payloads,
        extra_entries={"unexpected.bin": b"1"},
    )

    with pytest.raises(ResultPackageValidationError):
        validate_result_package_file(path=package_path)


def test_publish_prepared_result_package_reuses_existing_valid_package(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "tasks" / "task-1"
    stage_root = task_dir / "jobs" / "job-1" / "stages" / RESULT_PACKAGE_STAGE_ID
    task_dir.mkdir(parents=True, exist_ok=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    manifest, payloads = _build_manifest()
    content_digest = content_digest_from_content_id(manifest.content_id)
    prepared_relative_path = f".result_package_prepared/{content_digest}.jelica"
    prepared_path = stage_root / prepared_relative_path
    _write_package(
        package_path=prepared_path,
        manifest=manifest,
        payloads=payloads,
        include_notes=False,
    )

    target_path = result_package_target_path(task_dir=task_dir, content_digest=content_digest)
    _write_package(
        package_path=target_path,
        manifest=manifest,
        payloads=payloads,
        include_notes=True,
    )
    previous_bytes = target_path.read_bytes()
    stage_manifest = _build_stage_manifest(
        task_dir=task_dir,
        prepared_package_relative_path=prepared_relative_path,
        manifest=manifest,
    )

    published_path = publish_prepared_result_package(
        prepared_package_path=prepared_path,
        task_dir=task_dir,
        stage_manifest=stage_manifest,
    )

    assert published_path == target_path
    assert target_path.read_bytes() == previous_bytes
    assert not prepared_path.exists()
    assert not (stage_root / RESULT_PACKAGE_PREPARED_DIRNAME).exists()
    assert list(task_dir.rglob("*.jelica")) == []


def test_publish_prepared_result_package_creates_target_and_cleans_prepared_source(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "tasks" / "task-1"
    stage_root = task_dir / "jobs" / "job-1" / "stages" / RESULT_PACKAGE_STAGE_ID
    task_dir.mkdir(parents=True, exist_ok=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    manifest, payloads = _build_manifest()
    content_digest = content_digest_from_content_id(manifest.content_id)
    prepared_relative_path = f".result_package_prepared/{content_digest}.jelica"
    prepared_path = stage_root / prepared_relative_path
    _write_package(
        package_path=prepared_path,
        manifest=manifest,
        payloads=payloads,
        include_notes=False,
    )
    stage_manifest = _build_stage_manifest(
        task_dir=task_dir,
        prepared_package_relative_path=prepared_relative_path,
        manifest=manifest,
    )

    published_path = publish_prepared_result_package(
        prepared_package_path=prepared_path,
        task_dir=task_dir,
        stage_manifest=stage_manifest,
    )

    assert published_path.is_file()
    assert not prepared_path.exists()
    assert not (stage_root / RESULT_PACKAGE_PREPARED_DIRNAME).exists()
    assert list(task_dir.rglob("*.jelica")) == []


def test_publish_prepared_result_package_fails_for_existing_corrupted_target(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "tasks" / "task-1"
    task_dir.mkdir(parents=True, exist_ok=True)
    manifest, payloads = _build_manifest()
    content_digest = content_digest_from_content_id(manifest.content_id)
    prepared_relative_path = f".result_package_prepared/{content_digest}.jelica"
    prepared_path = tmp_path / "prepared" / f"{content_digest}.jelica"
    _write_package(
        package_path=prepared_path,
        manifest=manifest,
        payloads=payloads,
        include_notes=False,
    )

    target_path = result_package_target_path(task_dir=task_dir, content_digest=content_digest)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(b"corrupted-bytes")
    previous_bytes = target_path.read_bytes()
    stage_manifest = _build_stage_manifest(
        task_dir=task_dir,
        prepared_package_relative_path=prepared_relative_path,
        manifest=manifest,
    )

    with pytest.raises(ResultPackagePublicationError):
        publish_prepared_result_package(
            prepared_package_path=prepared_path,
            task_dir=task_dir,
            stage_manifest=stage_manifest,
        )

    assert target_path.read_bytes() == previous_bytes


def test_publish_prepared_result_package_failure_keeps_prepared_and_no_partial_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = tmp_path / "tasks" / "task-1"
    stage_root = task_dir / "jobs" / "job-1" / "stages" / RESULT_PACKAGE_STAGE_ID
    task_dir.mkdir(parents=True, exist_ok=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    manifest, payloads = _build_manifest()
    content_digest = content_digest_from_content_id(manifest.content_id)
    prepared_relative_path = f".result_package_prepared/{content_digest}.jelica"
    prepared_path = stage_root / prepared_relative_path
    _write_package(
        package_path=prepared_path,
        manifest=manifest,
        payloads=payloads,
        include_notes=False,
    )
    stage_manifest = _build_stage_manifest(
        task_dir=task_dir,
        prepared_package_relative_path=prepared_relative_path,
        manifest=manifest,
    )
    target_path = result_package_target_path(task_dir=task_dir, content_digest=content_digest)

    def _failing_copy(*, source_path: Path, target_path: Path) -> None:
        _ = source_path
        _ = target_path
        raise OSError("copy failed")

    monkeypatch.setattr(result_package_artifacts_module, "_copy_file_atomically", _failing_copy)

    with pytest.raises(ResultPackagePublicationError):
        publish_prepared_result_package(
            prepared_package_path=prepared_path,
            task_dir=task_dir,
            stage_manifest=stage_manifest,
        )

    assert not target_path.exists()
    assert not (task_dir / RESULT_PACKAGE_LINK_FILENAME).exists()
    assert prepared_path.is_file()
    assert (stage_root / RESULT_PACKAGE_PREPARED_DIRNAME).exists()


def test_write_result_package_link_writes_relative_posix_path(tmp_path: Path) -> None:
    task_dir = tmp_path / "tasks" / "task-1"
    task_dir.mkdir(parents=True, exist_ok=True)
    link = ResultPackageLink(
        content_id=f"sha256:{'a' * 64}",
        path="../../result_packages/" + ("a" * 64) + ".jelica",
        format_version="1.0",
    )

    link_path = write_result_package_link(task_dir=task_dir, link=link)
    payload = json.loads(link_path.read_text(encoding="utf-8"))

    assert payload["content_id"] == link.content_id
    assert payload["format_version"] == "1.0"
    assert payload["path"].startswith("../")
    assert "\\" not in payload["path"]


def test_committed_result_package_snapshot_remains_valid_without_prepared_file(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "tasks" / "task-1"
    job_dir = task_dir / "jobs" / "job-1"
    stage_root = job_dir / "stages" / RESULT_PACKAGE_STAGE_ID
    stage_root.mkdir(parents=True, exist_ok=True)
    manifest, _payloads = _build_manifest()
    content_digest = content_digest_from_content_id(manifest.content_id)
    stage_manifest = _build_stage_manifest(
        task_dir=task_dir,
        prepared_package_relative_path=f".result_package_prepared/{content_digest}.jelica",
        manifest=manifest,
    ).model_copy(update={"source_stage_ids": tuple(), "stage_count": 0})
    (stage_root / "result_package").mkdir(parents=True, exist_ok=True)
    write_model_json(
        path=stage_root / RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,
        model=stage_manifest,
    )
    write_stage_manifest(
        directory=stage_root,
        manifest=StageArtifactManifest(
            stage_id=RESULT_PACKAGE_STAGE_ID,
            job_id="job-1",
            worker_instance_id="worker-1",
            pipeline_version=DEFAULT_PIPELINE_VERSION,
            completed_at="2026-08-01T00:00:02Z",
            artifacts=(RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,),
        ),
    )

    snapshot = validate_committed_stage_snapshot(
        job_dir=job_dir,
        stage_id=RESULT_PACKAGE_STAGE_ID,
        expected_job_id="job-1",
        expected_pipeline_version=DEFAULT_PIPELINE_VERSION,
        expected_task_id="task-1",
        expected_config_hash="0" * 64,
    )

    assert snapshot.manifest.artifacts == (RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,)
    assert not (stage_root / RESULT_PACKAGE_PREPARED_DIRNAME).exists()


def test_engine_post_commit_result_package_publication_creates_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = tmp_path / "tasks" / "task-1"
    job_dir = task_dir / "jobs" / "job-1"
    stage_root = job_dir / "stages" / RESULT_PACKAGE_STAGE_ID
    stage_root.mkdir(parents=True, exist_ok=True)
    manifest, payloads = _build_manifest()
    content_digest = content_digest_from_content_id(manifest.content_id)
    prepared_relative_path = f".result_package_prepared/{content_digest}.jelica"
    prepared_path = stage_root / prepared_relative_path
    _write_package(
        package_path=prepared_path,
        manifest=manifest,
        payloads=payloads,
        include_notes=False,
    )
    stage_manifest = _build_stage_manifest(
        task_dir=task_dir,
        prepared_package_relative_path=prepared_relative_path,
        manifest=manifest,
    )
    (stage_root / "result_package").mkdir(parents=True, exist_ok=True)
    write_model_json(
        path=stage_root / RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,
        model=stage_manifest,
    )
    generic_manifest = StageArtifactManifest(
        stage_id=RESULT_PACKAGE_STAGE_ID,
        job_id="job-1",
        worker_instance_id="worker-1",
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        completed_at="2026-08-01T00:00:02Z",
        artifacts=(RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,),
    )

    def _commit(**_kwargs: object) -> StageArtifactManifest:
        return generic_manifest

    monkeypatch.setattr(engine_module, "commit_stage_directory", _commit)
    handle = cast(
        engine_module._WorkerHandle,
        SimpleNamespace(
            task_id="task-1",
            job_id="job-1",
            worker_instance_id="worker-1",
            checkpoint=RuntimeStateCheckpoint.new(
                pipeline_version=DEFAULT_PIPELINE_VERSION
            ),
            pipeline_definition=PipelineDefinition(
                name=DEFAULT_PIPELINE_NAME,
                version=DEFAULT_PIPELINE_VERSION,
                stages=(),
            ),
            job_dir=job_dir,
            current_stage=RESULT_PACKAGE_STAGE_ID,
            current_stage_progress=1.0,
        ),
    )
    failures: list[dict[str, object]] = []
    emitted: list[str] = []

    class _RuntimeHarness:
        def _persist_progress(self, **_kwargs: object) -> None:
            return

        def _emit(self, event_name: str, context: dict[str, object] | None) -> None:
            assert context is not None
            emitted.append(event_name)

        def _mark_job_failed(self, **kwargs: object) -> None:
            failures.append(cast(dict[str, object], kwargs))

    message = StageReadyToCommitMessage(
        task_id=handle.task_id,
        job_id=handle.job_id,
        worker_instance_id=handle.worker_instance_id,
        lease_token="lease-1",
        stage_id=RESULT_PACKAGE_STAGE_ID,
        staging_directory=str(tmp_path / "staging"),
        manifest_path=str(tmp_path / "staging" / "stage_manifest.json"),
    )

    ExecutionRuntime._handle_stage_ready_to_commit(
        _RuntimeHarness(),  # type: ignore[arg-type]
        handle=handle,
        message=message,
    )

    assert failures == []
    assert emitted == [engine_module.RUNTIME_EVENT_STAGE_COMMITTED]
    link_path = task_dir / RESULT_PACKAGE_LINK_FILENAME
    assert link_path.is_file()
    link = load_result_package_link(path=link_path)
    assert link.content_id == manifest.content_id
    published_path = (task_dir / Path(link.path)).resolve(strict=True)
    assert published_path.is_file()
    validated = validate_result_package_file(
        path=published_path,
        expected_content_id=manifest.content_id,
        require_notes_absent=False,
    )
    assert validated.content_id == manifest.content_id
    assert not prepared_path.exists()
    assert not (stage_root / RESULT_PACKAGE_PREPARED_DIRNAME).exists()
    assert list(task_dir.rglob("*.jelica")) == []
