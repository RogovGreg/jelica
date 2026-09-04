from __future__ import annotations

import hashlib
import json
import math
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jelica_core.tasks.storage import write_text_atomically

if TYPE_CHECKING:
    from jelica_core.alignment import AlignmentManifest
    from jelica_core.clade_detection import CladeDetectionManifest
    from jelica_core.comparative_analysis import ComparativeAnalysisManifest
    from jelica_core.distance_matrix import DistanceMatrixManifest
    from jelica_core.phylogenetic_tree import PhylogeneticTreeManifest

_ModelT = TypeVar("_ModelT", bound=BaseModel)

STAGE_MANIFEST_FILENAME = "stage_manifest.json"
_TREE_VALIDATION_ABS_TOLERANCE = 1e-9
_TREE_ZERO_ABS_TOLERANCE = 1e-12
_TREE_PAIRWISE_DISTANCE_MAX_LEAF_COUNT = 256


class StageArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    worker_instance_id: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    artifacts: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator(
        "stage_id",
        "job_id",
        "worker_instance_id",
        "pipeline_version",
        "completed_at",
    )
    @classmethod
    def _normalize_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("text field must not be empty")
        return normalized

    @field_validator("artifacts")
    @classmethod
    def _validate_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for artifact in value:
            stripped = artifact.strip().replace("\\", "/")
            posix_path = PurePosixPath(stripped)
            windows_path = PureWindowsPath(stripped)
            if stripped == "":
                raise ValueError("artifact path must not be empty")
            if posix_path.is_absolute() or windows_path.is_absolute():
                raise ValueError("artifact paths must be relative")
            if ".." in posix_path.parts or ".." in windows_path.parts:
                raise ValueError("artifact paths must not escape stage directory")
            normalized.append(posix_path.as_posix())
        return tuple(normalized)


class StageSnapshotErrorCode(StrEnum):
    INVALID = "COMMITTED_SNAPSHOT_INVALID"
    IDENTITY_MISMATCH = "COMMITTED_SNAPSHOT_IDENTITY_MISMATCH"
    ARTIFACT_MISSING = "COMMITTED_SNAPSHOT_ARTIFACT_MISSING"
    ARTIFACT_UNREADABLE = "COMMITTED_SNAPSHOT_ARTIFACT_UNREADABLE"
    HASH_MISMATCH = "COMMITTED_SNAPSHOT_HASH_MISMATCH"
    SIZE_MISMATCH = "COMMITTED_SNAPSHOT_SIZE_MISMATCH"
    RECORD_COUNT_MISMATCH = "COMMITTED_SNAPSHOT_RECORD_COUNT_MISMATCH"
    UPSTREAM_INVALID = "COMMITTED_SNAPSHOT_UPSTREAM_INVALID"
    COMMIT_CONFLICT = "STAGE_COMMIT_CONFLICT"


class StageCommitError(RuntimeError):
    """A sequence-safe structured failure at the stage publication boundary."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "STAGE_COMMIT_ERROR",
        stage_id: str | None = None,
        relative_path: str | None = None,
    ) -> None:
        self.code = code
        self.stage_id = stage_id
        self.relative_path = relative_path
        super().__init__(detail)


class StageSnapshotValidationError(StageCommitError):
    """Raised when a committed or prospective stage snapshot is not trustworthy."""

    def __init__(
        self,
        *,
        code: StageSnapshotErrorCode,
        stage_id: str,
        detail: str,
        relative_path: str | None = None,
    ) -> None:
        super().__init__(
            detail,
            code=code.value,
            stage_id=stage_id,
            relative_path=relative_path,
        )


@dataclass(frozen=True, slots=True)
class StageArtifactFingerprint:
    relative_path: str
    size_bytes: int
    sha256: str
    record_count: int | None = None


@dataclass(frozen=True, slots=True)
class ValidatedStageSnapshot:
    manifest: StageArtifactManifest
    artifact_fingerprints: tuple[StageArtifactFingerprint, ...]
    domain_manifest_sha256: str | None = None
    domain_status: str | None = None
    config_hash: str | None = None
    source_artifacts: tuple[str, ...] = tuple()


def write_stage_manifest(
    *,
    directory: Path,
    manifest: StageArtifactManifest,
) -> Path:
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    manifest_path = directory / STAGE_MANIFEST_FILENAME
    write_text_atomically(path=manifest_path, payload=f"{payload}\n")
    return manifest_path


def load_stage_manifest(*, path: Path) -> StageArtifactManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StageCommitError(
            "The generic stage manifest could not be read.",
            code=StageSnapshotErrorCode.INVALID.value,
        ) from error
    if not isinstance(payload, dict):
        raise StageCommitError(
            "The generic stage manifest must be a JSON object.",
            code=StageSnapshotErrorCode.INVALID.value,
        )
    try:
        return StageArtifactManifest.model_validate(payload)
    except Exception as error:
        raise StageCommitError(
            "The generic stage manifest is invalid.",
            code=StageSnapshotErrorCode.INVALID.value,
        ) from error


def validate_committed_stage_snapshot(
    *,
    job_dir: Path,
    stage_id: str,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_task_id: str | None = None,
    expected_config_hash: str | None = None,
) -> ValidatedStageSnapshot:
    """Validate one immutable stage snapshot and its required upstream prefix."""

    stage_root = job_dir / "stages" / stage_id
    return _validate_stage_snapshot(
        job_dir=job_dir,
        stage_root=stage_root,
        snapshot_container=job_dir / "stages",
        manifest_path=stage_root / STAGE_MANIFEST_FILENAME,
        expected_stage_id=stage_id,
        expected_job_id=expected_job_id,
        expected_pipeline_version=expected_pipeline_version,
        expected_task_id=expected_task_id,
        expected_config_hash=expected_config_hash,
        expected_worker_instance_id=None,
        validate_upstream=True,
    )


def commit_stage_directory(
    *,
    job_dir: Path,
    stage_id: str,
    job_id: str,
    worker_instance_id: str,
    pipeline_version: str,
    staging_directory: Path,
    manifest_path: Path,
    task_id: str | None = None,
    config_hash: str | None = None,
) -> StageArtifactManifest:
    expected_staging_directory = (
        job_dir / "staging" / stage_id / worker_instance_id
    )
    if staging_directory.resolve(strict=False) != expected_staging_directory.resolve(
        strict=False
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=stage_id,
            detail="The staging directory does not match the expected worker snapshot.",
        )
    new_snapshot = _validate_stage_snapshot(
        job_dir=job_dir,
        stage_root=staging_directory,
        snapshot_container=job_dir / "staging",
        manifest_path=manifest_path,
        expected_stage_id=stage_id,
        expected_job_id=job_id,
        expected_pipeline_version=pipeline_version,
        expected_task_id=task_id,
        expected_config_hash=config_hash,
        expected_worker_instance_id=worker_instance_id,
        validate_upstream=True,
    )

    stages_dir = job_dir / "stages"
    target_stage_directory = stages_dir / stage_id
    if target_stage_directory.exists():
        existing_snapshot = validate_committed_stage_snapshot(
            job_dir=job_dir,
            stage_id=stage_id,
            expected_job_id=job_id,
            expected_pipeline_version=pipeline_version,
            expected_task_id=task_id,
            expected_config_hash=config_hash,
        )
        if not _is_idempotent_commit(
            existing_snapshot=existing_snapshot,
            new_snapshot=new_snapshot,
        ):
            raise StageCommitError(
                "The stage is already committed with a different canonical outcome.",
                code=StageSnapshotErrorCode.COMMIT_CONFLICT.value,
                stage_id=stage_id,
            )
        try:
            shutil.rmtree(staging_directory, ignore_errors=False)
            _cleanup_empty_parents(
                staging_directory.parent,
                stop_at=job_dir / "staging",
            )
        except OSError as error:
            raise StageCommitError(
                "The idempotent staging snapshot could not be cleaned up.",
                stage_id=stage_id,
            ) from error
        return existing_snapshot.manifest

    try:
        stages_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise StageCommitError(
            "The committed stages directory could not be prepared.",
            stage_id=stage_id,
        ) from error
    try:
        stages_dir.resolve(strict=True).relative_to(job_dir.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="The committed stages directory is outside the job workspace.",
        ) from error
    try:
        staging_directory.replace(target_stage_directory)
    except OSError as error:
        raise StageCommitError(
            "The validated stage snapshot could not be published atomically.",
            stage_id=stage_id,
        ) from error

    _cleanup_empty_parents(staging_directory.parent, stop_at=job_dir / "staging")
    return new_snapshot.manifest


def cleanup_worker_staging(*, job_dir: Path, worker_instance_id: str) -> None:
    staging_root = job_dir / "staging"
    if not staging_root.exists():
        return

    for stage_dir in staging_root.iterdir():
        if not stage_dir.is_dir():
            continue
        worker_dir = stage_dir / worker_instance_id
        if worker_dir.exists():
            shutil.rmtree(worker_dir, ignore_errors=False)
        _cleanup_empty_parents(worker_dir.parent, stop_at=staging_root)


def list_committed_stage_manifests(*, job_dir: Path) -> dict[str, StageArtifactManifest]:
    """Load generic manifests without asserting that their snapshots are valid."""

    stages_root = job_dir / "stages"
    if not stages_root.is_dir():
        return {}

    manifests: dict[str, StageArtifactManifest] = {}
    for stage_dir in sorted(stages_root.iterdir()):
        if not stage_dir.is_dir():
            continue
        manifest_path = stage_dir / STAGE_MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = load_stage_manifest(path=manifest_path)
        manifests[manifest.stage_id] = manifest
    return manifests


def _validate_stage_snapshot(
    *,
    job_dir: Path,
    stage_root: Path,
    snapshot_container: Path,
    manifest_path: Path,
    expected_stage_id: str,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_task_id: str | None,
    expected_config_hash: str | None,
    expected_worker_instance_id: str | None,
    validate_upstream: bool,
) -> ValidatedStageSnapshot:
    expected_manifest_path = stage_root / STAGE_MANIFEST_FILENAME
    if manifest_path.resolve(strict=False) != expected_manifest_path.resolve(strict=False):
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=expected_stage_id,
            detail="The generic manifest is outside the expected stage snapshot.",
        )
    try:
        resolved_job_dir = job_dir.resolve(strict=True)
        resolved_container = snapshot_container.resolve(strict=True)
        resolved_root = stage_root.resolve(strict=True)
        resolved_manifest_path = manifest_path.resolve(strict=True)
        resolved_container.relative_to(resolved_job_dir)
        resolved_root.relative_to(resolved_container)
        resolved_manifest_path.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=expected_stage_id,
            detail="The generic stage manifest is missing or outside its snapshot.",
        ) from error
    if not resolved_manifest_path.is_file():
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=expected_stage_id,
            detail="The generic stage manifest is not a regular file.",
        )
    try:
        manifest = load_stage_manifest(path=resolved_manifest_path)
    except StageCommitError as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=expected_stage_id,
            detail="The generic stage manifest is missing or invalid.",
        ) from error
    _validate_manifest_identity(
        manifest=manifest,
        expected_stage_id=expected_stage_id,
        expected_job_id=expected_job_id,
        expected_worker_instance_id=expected_worker_instance_id,
        expected_pipeline_version=expected_pipeline_version,
    )

    fingerprints = {
        relative_path: _inspect_artifact(
            stage_root=stage_root,
            stage_id=expected_stage_id,
            relative_path=relative_path,
        )
        for relative_path in manifest.artifacts
    }
    domain_manifest_sha256: str | None = None
    domain_status: str | None = None
    domain_config_hash: str | None = None
    source_artifacts: tuple[str, ...] = tuple()

    if expected_stage_id == "initialize_job":
        domain_config_hash = _validate_untyped_domain_identity(
            stage_root=stage_root,
            generic_manifest=manifest,
            relative_path="execution_manifest.json",
            expected_task_id=expected_task_id,
            expected_job_id=expected_job_id,
            expected_config_hash=expected_config_hash,
            expected_pipeline_version=expected_pipeline_version,
            require_worker_identity=True,
        )
    elif expected_stage_id == "input_acquisition":
        from .input_parsers import INPUT_MANIFEST_RELATIVE_PATH

        domain_config_hash = _validate_untyped_domain_identity(
            stage_root=stage_root,
            generic_manifest=manifest,
            relative_path=INPUT_MANIFEST_RELATIVE_PATH,
            expected_task_id=expected_task_id,
            expected_job_id=expected_job_id,
            expected_config_hash=expected_config_hash,
            expected_pipeline_version=None,
            require_worker_identity=False,
        )
    elif expected_stage_id == "input_processing":
        domain_config_hash = _validate_input_processing_domain(
            stage_root=stage_root,
            generic_manifest=manifest,
            expected_task_id=expected_task_id,
            expected_job_id=expected_job_id,
            expected_config_hash=expected_config_hash,
        )
    elif expected_stage_id == "alignment":
        alignment = _validate_alignment_domain(
            stage_root=stage_root,
            generic_manifest=manifest,
            expected_task_id=expected_task_id,
            expected_job_id=expected_job_id,
            expected_config_hash=expected_config_hash,
            fingerprints=fingerprints,
        )
        domain_config_hash = alignment.config_hash
        domain_status = alignment.outcome.value
    elif expected_stage_id == "comparative_analysis":
        (
            domain_manifest_sha256,
            domain_status,
            domain_config_hash,
            source_artifacts,
        ) = _validate_comparative_domain(
            job_dir=job_dir,
            stage_root=stage_root,
            generic_manifest=manifest,
            expected_task_id=expected_task_id,
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_config_hash=expected_config_hash,
            fingerprints=fingerprints,
            validate_upstream=validate_upstream,
        )
    elif expected_stage_id == "distance_matrix":
        (
            domain_manifest_sha256,
            domain_status,
            domain_config_hash,
            source_artifacts,
        ) = _validate_distance_matrix_domain(
            job_dir=job_dir,
            stage_root=stage_root,
            generic_manifest=manifest,
            expected_task_id=expected_task_id,
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_config_hash=expected_config_hash,
            fingerprints=fingerprints,
            validate_upstream=validate_upstream,
        )
    elif expected_stage_id == "phylogenetic_tree":
        (
            domain_manifest_sha256,
            domain_status,
            domain_config_hash,
            source_artifacts,
        ) = _validate_phylogenetic_tree_domain(
            job_dir=job_dir,
            stage_root=stage_root,
            generic_manifest=manifest,
            expected_task_id=expected_task_id,
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_config_hash=expected_config_hash,
            fingerprints=fingerprints,
            validate_upstream=validate_upstream,
        )
    elif expected_stage_id == "clade_detection":
        (
            domain_manifest_sha256,
            domain_status,
            domain_config_hash,
            source_artifacts,
        ) = _validate_clade_detection_domain(
            job_dir=job_dir,
            stage_root=stage_root,
            generic_manifest=manifest,
            expected_task_id=expected_task_id,
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_config_hash=expected_config_hash,
            fingerprints=fingerprints,
            validate_upstream=validate_upstream,
        )
    elif expected_stage_id == "result_package":
        (
            domain_manifest_sha256,
            domain_status,
            domain_config_hash,
            source_artifacts,
        ) = _validate_result_package_domain(
            job_dir=job_dir,
            stage_root=stage_root,
            generic_manifest=manifest,
            expected_task_id=expected_task_id,
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_config_hash=expected_config_hash,
            fingerprints=fingerprints,
            validate_upstream=validate_upstream,
        )

    return ValidatedStageSnapshot(
        manifest=manifest,
        artifact_fingerprints=tuple(fingerprints[path] for path in sorted(fingerprints)),
        domain_manifest_sha256=domain_manifest_sha256,
        domain_status=domain_status,
        config_hash=domain_config_hash,
        source_artifacts=source_artifacts,
    )


def _validate_manifest_identity(
    *,
    manifest: StageArtifactManifest,
    expected_stage_id: str,
    expected_job_id: str,
    expected_worker_instance_id: str | None,
    expected_pipeline_version: str,
) -> None:
    mismatch = (
        manifest.stage_id != expected_stage_id
        or manifest.job_id != expected_job_id
        or manifest.pipeline_version != expected_pipeline_version
        or (
            expected_worker_instance_id is not None
            and manifest.worker_instance_id != expected_worker_instance_id
        )
    )
    if mismatch:
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=expected_stage_id,
            detail="The generic stage identity does not match the expected job snapshot.",
        )


def _validate_untyped_domain_identity(
    *,
    stage_root: Path,
    generic_manifest: StageArtifactManifest,
    relative_path: str,
    expected_task_id: str | None,
    expected_job_id: str,
    expected_config_hash: str | None,
    expected_pipeline_version: str | None,
    require_worker_identity: bool,
) -> str | None:
    if generic_manifest.artifacts != (relative_path,):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The generic and stage domain artifact sets are inconsistent.",
        )
    path = _resolve_artifact_path(
        stage_root=stage_root,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
    )
    try:
        payload = json.loads(path.read_bytes())
    except Exception as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The stage domain manifest is invalid.",
            relative_path=relative_path,
        ) from error
    if not isinstance(payload, dict):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The stage domain manifest must be a JSON object.",
            relative_path=relative_path,
        )
    config_hash = payload.get("config_hash")
    identity_mismatch = (
        payload.get("job_id") != expected_job_id
        or (expected_task_id is not None and payload.get("task_id") != expected_task_id)
        or (
            expected_config_hash is not None
            and config_hash != expected_config_hash
        )
        or (
            expected_pipeline_version is not None
            and payload.get("pipeline_version") != expected_pipeline_version
        )
        or (
            require_worker_identity
            and payload.get("worker_instance_id")
            != generic_manifest.worker_instance_id
        )
    )
    if identity_mismatch:
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=generic_manifest.stage_id,
            detail="The stage domain identity does not match the job snapshot.",
            relative_path=relative_path,
        )
    return config_hash if isinstance(config_hash, str) else None


def _validate_input_processing_domain(
    *,
    stage_root: Path,
    generic_manifest: StageArtifactManifest,
    expected_task_id: str | None,
    expected_job_id: str,
    expected_config_hash: str | None,
) -> str:
    from .input_processing_models import (
        INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
        InputProcessingManifest,
        InputProcessingState,
        input_processing_artifact_paths,
    )

    relative_path = INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
    _require_generic_artifact(
        generic_manifest=generic_manifest,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
    )
    payload = _load_typed_json(
        stage_root=stage_root,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
        model=InputProcessingManifest,
    )
    if (
        payload.job_id != expected_job_id
        or (expected_task_id is not None and payload.task_id != expected_task_id)
        or (
            expected_config_hash is not None
            and payload.config_hash != expected_config_hash
        )
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=generic_manifest.stage_id,
            detail="The input-processing domain identity does not match the job.",
            relative_path=relative_path,
        )
    if payload.processing_state is not InputProcessingState.COMPLETED:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The input-processing domain result is not complete.",
            relative_path=relative_path,
        )
    if generic_manifest.artifacts != input_processing_artifact_paths(payload):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The generic and input-processing artifact sets are inconsistent.",
        )
    return payload.config_hash


def _validate_alignment_domain(
    *,
    stage_root: Path,
    generic_manifest: StageArtifactManifest,
    expected_task_id: str | None,
    expected_job_id: str,
    expected_config_hash: str | None,
    fingerprints: dict[str, StageArtifactFingerprint],
) -> AlignmentManifest:
    from jelica_core.alignment import ALIGNMENT_MANIFEST_RELATIVE_PATH, AlignmentManifest

    relative_path = ALIGNMENT_MANIFEST_RELATIVE_PATH
    _require_generic_artifact(
        generic_manifest=generic_manifest,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
    )
    payload = _load_typed_json(
        stage_root=stage_root,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
        model=AlignmentManifest,
    )
    if (
        payload.job_id != expected_job_id
        or (expected_task_id is not None and payload.task_id != expected_task_id)
        or (
            expected_config_hash is not None
            and payload.config_hash != expected_config_hash
        )
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=generic_manifest.stage_id,
            detail="The alignment domain identity does not match the job.",
            relative_path=relative_path,
        )
    if payload.aligned_fasta_path is not None:
        aligned_path = payload.aligned_fasta_path
        _require_generic_artifact(
            generic_manifest=generic_manifest,
            stage_id=generic_manifest.stage_id,
            relative_path=aligned_path,
        )
        fingerprint = fingerprints[aligned_path]
        if payload.result_sha256 != fingerprint.sha256:
            raise _snapshot_error(
                StageSnapshotErrorCode.HASH_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="The canonical alignment digest does not match its manifest.",
                relative_path=aligned_path,
            )
    expected_artifacts = tuple(
        item
        for item in (
            relative_path,
            payload.aligned_fasta_path,
            payload.reference_coordinate_map_path,
            payload.diagnostics_path,
        )
        if item is not None
    )
    if generic_manifest.artifacts != expected_artifacts:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The generic and alignment artifact sets are inconsistent.",
        )
    return payload


def _validate_comparative_domain(
    *,
    job_dir: Path,
    stage_root: Path,
    generic_manifest: StageArtifactManifest,
    expected_task_id: str | None,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_config_hash: str | None,
    fingerprints: dict[str, StageArtifactFingerprint],
    validate_upstream: bool,
) -> tuple[str, str, str, tuple[str, ...]]:
    from jelica_core.comparative_analysis import (
        COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
        ComparativeAnalysisManifest,
    )

    relative_path = COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH
    _require_generic_artifact(
        generic_manifest=generic_manifest,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
    )
    manifest = _load_typed_json(
        stage_root=stage_root,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
        model=ComparativeAnalysisManifest,
    )
    if (
        manifest.job_id != expected_job_id
        or (expected_task_id is not None and manifest.task_id != expected_task_id)
        or (
            expected_config_hash is not None
            and manifest.config_hash != expected_config_hash
        )
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=generic_manifest.stage_id,
            detail="The comparative domain identity does not match the job.",
            relative_path=relative_path,
        )

    expected_artifacts = (
        relative_path,
        *(metadata.relative_path for metadata in manifest.artifacts),
    )
    if generic_manifest.artifacts != expected_artifacts:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The generic and comparative artifact sets are inconsistent.",
        )
    metadata_paths = {metadata.relative_path for metadata in manifest.artifacts}
    for category in manifest.category_execution.values():
        if any(path not in metadata_paths for path in category.artifact_paths):
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=generic_manifest.stage_id,
                detail="A comparative category references an unpublished artifact.",
            )
    for metadata in manifest.artifacts:
        fingerprint = fingerprints[metadata.relative_path]
        if fingerprint.size_bytes != metadata.size_bytes:
            raise _snapshot_error(
                StageSnapshotErrorCode.SIZE_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A comparative artifact size does not match its metadata.",
                relative_path=metadata.relative_path,
            )
        if fingerprint.sha256 != metadata.sha256:
            raise _snapshot_error(
                StageSnapshotErrorCode.HASH_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A comparative artifact digest does not match its metadata.",
                relative_path=metadata.relative_path,
            )
        if (
            metadata.record_count is not None
            and fingerprint.record_count != metadata.record_count
        ):
            raise _snapshot_error(
                StageSnapshotErrorCode.RECORD_COUNT_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A comparative JSONL record count does not match its metadata.",
                relative_path=metadata.relative_path,
            )

    if validate_upstream:
        _validate_comparative_upstream_prefix(
            job_dir=job_dir,
            manifest=manifest,
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_task_id=expected_task_id,
            expected_config_hash=expected_config_hash,
        )

    domain_hash = fingerprints[relative_path].sha256
    return (
        domain_hash,
        manifest.status.value,
        manifest.config_hash,
        manifest.source_artifacts,
    )


def _validate_comparative_upstream_prefix(
    *,
    job_dir: Path,
    manifest: ComparativeAnalysisManifest,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_task_id: str | None,
    expected_config_hash: str | None,
) -> None:
    from jelica_core.alignment import (
        ALIGNMENT_FASTA_RELATIVE_PATH,
        ALIGNMENT_MANIFEST_RELATIVE_PATH,
    )

    from .input_processing_models import INPUT_PROCESSING_MANIFEST_RELATIVE_PATH

    input_prefix = "stages/input_processing/"
    alignment_prefix = "stages/alignment/"
    input_manifest_reference = f"{input_prefix}{INPUT_PROCESSING_MANIFEST_RELATIVE_PATH}"
    if manifest.enabled and input_manifest_reference not in manifest.source_artifacts:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The comparative snapshot lacks its input-processing manifest reference.",
        )
    try:
        input_snapshot = validate_committed_stage_snapshot(
            job_dir=job_dir,
            stage_id="input_processing",
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_task_id=expected_task_id,
            expected_config_hash=expected_config_hash,
        )
    except StageCommitError as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The comparative input-processing prefix is not a valid snapshot.",
        ) from error
    if input_snapshot.config_hash != manifest.config_hash:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The comparative and input-processing configurations do not match.",
        )

    alignment_snapshot: ValidatedStageSnapshot | None = None
    if manifest.normalized_settings.sequence_differences.enabled:
        alignment_manifest_reference = f"{alignment_prefix}{ALIGNMENT_MANIFEST_RELATIVE_PATH}"
        aligned_result_reference = f"{alignment_prefix}{ALIGNMENT_FASTA_RELATIVE_PATH}"
        if (
            alignment_manifest_reference not in manifest.source_artifacts
            or aligned_result_reference not in manifest.source_artifacts
        ):
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="The comparative snapshot lacks its required alignment reference.",
            )
        try:
            alignment_snapshot = validate_committed_stage_snapshot(
                job_dir=job_dir,
                stage_id="alignment",
                expected_job_id=expected_job_id,
                expected_pipeline_version=expected_pipeline_version,
                expected_task_id=expected_task_id,
                expected_config_hash=expected_config_hash,
            )
        except StageCommitError as error:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="The comparative alignment prefix is not a valid snapshot.",
            ) from error
        if alignment_snapshot.domain_status not in {"completed", "skipped_not_required"}:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="The required alignment snapshot has no canonical result.",
            )
        if alignment_snapshot.config_hash != manifest.config_hash:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="The comparative and alignment configurations do not match.",
            )

    input_artifacts = set(input_snapshot.manifest.artifacts)
    alignment_artifacts = (
        set(alignment_snapshot.manifest.artifacts)
        if alignment_snapshot is not None
        else set()
    )
    for source_reference in manifest.source_artifacts:
        if source_reference.startswith(input_prefix):
            relative_path = source_reference.removeprefix(input_prefix)
            valid = relative_path in input_artifacts
        elif source_reference.startswith(alignment_prefix):
            relative_path = source_reference.removeprefix(alignment_prefix)
            valid = relative_path in alignment_artifacts
        else:
            valid = False
        if not valid:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="A comparative upstream reference is not in its committed snapshot.",
                relative_path=source_reference,
            )


def _validate_distance_matrix_domain(
    *,
    job_dir: Path,
    stage_root: Path,
    generic_manifest: StageArtifactManifest,
    expected_task_id: str | None,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_config_hash: str | None,
    fingerprints: dict[str, StageArtifactFingerprint],
    validate_upstream: bool,
) -> tuple[str, str, str, tuple[str, ...]]:
    from jelica_core.distance_matrix import (
        DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
        DistanceMatrixManifest,
        distance_matrix_artifact_paths,
    )

    relative_path = DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH
    _require_generic_artifact(
        generic_manifest=generic_manifest,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
    )
    manifest = _load_typed_json(
        stage_root=stage_root,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
        model=DistanceMatrixManifest,
    )
    if (
        manifest.job_id != expected_job_id
        or (expected_task_id is not None and manifest.task_id != expected_task_id)
        or (
            expected_config_hash is not None
            and manifest.config_hash != expected_config_hash
        )
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=generic_manifest.stage_id,
            detail="The distance-matrix domain identity does not match the job.",
            relative_path=relative_path,
        )
    if generic_manifest.artifacts != distance_matrix_artifact_paths(manifest):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The generic and distance-matrix artifact sets are inconsistent.",
        )
    for metadata in manifest.artifacts:
        fingerprint = fingerprints[metadata.relative_path]
        if fingerprint.size_bytes != metadata.size_bytes:
            raise _snapshot_error(
                StageSnapshotErrorCode.SIZE_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A distance-matrix artifact size does not match its metadata.",
                relative_path=metadata.relative_path,
            )
        if fingerprint.sha256 != metadata.sha256:
            raise _snapshot_error(
                StageSnapshotErrorCode.HASH_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A distance-matrix artifact digest does not match its metadata.",
                relative_path=metadata.relative_path,
            )
        if (
            metadata.record_count is not None
            and fingerprint.record_count != metadata.record_count
        ):
            raise _snapshot_error(
                StageSnapshotErrorCode.RECORD_COUNT_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A distance-matrix JSONL record count does not match its metadata.",
                relative_path=metadata.relative_path,
            )

    if validate_upstream:
        _validate_distance_matrix_upstream_prefix(
            job_dir=job_dir,
            manifest=manifest,
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_task_id=expected_task_id,
            expected_config_hash=expected_config_hash,
        )

    domain_hash = fingerprints[relative_path].sha256
    return (
        domain_hash,
        manifest.status.value,
        manifest.config_hash,
        manifest.source_artifacts,
    )


def _validate_distance_matrix_upstream_prefix(
    *,
    job_dir: Path,
    manifest: DistanceMatrixManifest,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_task_id: str | None,
    expected_config_hash: str | None,
) -> None:
    from jelica_core.alignment import (
        ALIGNMENT_FASTA_RELATIVE_PATH,
        ALIGNMENT_MANIFEST_RELATIVE_PATH,
    )

    from .input_processing_models import INPUT_PROCESSING_MANIFEST_RELATIVE_PATH

    input_prefix = "stages/input_processing/"
    alignment_prefix = "stages/alignment/"
    input_manifest_reference = f"{input_prefix}{INPUT_PROCESSING_MANIFEST_RELATIVE_PATH}"
    alignment_manifest_reference = f"{alignment_prefix}{ALIGNMENT_MANIFEST_RELATIVE_PATH}"
    aligned_result_reference = f"{alignment_prefix}{ALIGNMENT_FASTA_RELATIVE_PATH}"
    if manifest.enabled and input_manifest_reference not in manifest.source_artifacts:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The distance-matrix snapshot lacks its input-processing manifest reference.",
        )
    if (
        manifest.enabled
        and (
            alignment_manifest_reference not in manifest.source_artifacts
            or aligned_result_reference not in manifest.source_artifacts
        )
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The distance-matrix snapshot lacks its required alignment references.",
        )

    try:
        input_snapshot = validate_committed_stage_snapshot(
            job_dir=job_dir,
            stage_id="input_processing",
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_task_id=expected_task_id,
            expected_config_hash=expected_config_hash,
        )
    except StageCommitError as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The distance-matrix input-processing prefix is not a valid snapshot.",
        ) from error
    if input_snapshot.config_hash != manifest.config_hash:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The distance-matrix and input-processing configurations do not match.",
        )

    alignment_snapshot: ValidatedStageSnapshot | None = None
    if manifest.enabled:
        try:
            alignment_snapshot = validate_committed_stage_snapshot(
                job_dir=job_dir,
                stage_id="alignment",
                expected_job_id=expected_job_id,
                expected_pipeline_version=expected_pipeline_version,
                expected_task_id=expected_task_id,
                expected_config_hash=expected_config_hash,
            )
        except StageCommitError as error:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="The distance-matrix alignment prefix is not a valid snapshot.",
            ) from error
        if alignment_snapshot.domain_status not in {"completed", "skipped_not_required"}:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="The required alignment snapshot has no canonical result.",
            )
        if alignment_snapshot.config_hash != manifest.config_hash:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="The distance-matrix and alignment configurations do not match.",
            )

    input_artifacts = set(input_snapshot.manifest.artifacts)
    alignment_artifacts = (
        set(alignment_snapshot.manifest.artifacts)
        if alignment_snapshot is not None
        else set()
    )
    for source_reference in manifest.source_artifacts:
        if source_reference.startswith(input_prefix):
            relative_path = source_reference.removeprefix(input_prefix)
            valid = relative_path in input_artifacts
        elif source_reference.startswith(alignment_prefix):
            relative_path = source_reference.removeprefix(alignment_prefix)
            valid = relative_path in alignment_artifacts
        else:
            valid = False
        if not valid:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="A distance-matrix upstream reference is not in its committed snapshot.",
                relative_path=source_reference,
            )


def _validate_phylogenetic_tree_domain(
    *,
    job_dir: Path,
    stage_root: Path,
    generic_manifest: StageArtifactManifest,
    expected_task_id: str | None,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_config_hash: str | None,
    fingerprints: dict[str, StageArtifactFingerprint],
    validate_upstream: bool,
) -> tuple[str, str, str, tuple[str, ...]]:
    from jelica_core.phylogenetic_tree import (
        PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
        PhylogeneticTreeManifest,
        phylogenetic_tree_artifact_paths,
    )

    relative_path = PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH
    _require_generic_artifact(
        generic_manifest=generic_manifest,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
    )
    manifest = _load_typed_json(
        stage_root=stage_root,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
        model=PhylogeneticTreeManifest,
    )
    if (
        manifest.job_id != expected_job_id
        or (expected_task_id is not None and manifest.task_id != expected_task_id)
        or (
            expected_config_hash is not None
            and manifest.config_hash != expected_config_hash
        )
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=generic_manifest.stage_id,
            detail="The phylogenetic-tree domain identity does not match the job.",
            relative_path=relative_path,
        )
    if generic_manifest.artifacts != phylogenetic_tree_artifact_paths(manifest):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The generic and phylogenetic-tree artifact sets are inconsistent.",
        )
    for metadata in manifest.artifacts:
        fingerprint = fingerprints[metadata.relative_path]
        if fingerprint.size_bytes != metadata.size_bytes:
            raise _snapshot_error(
                StageSnapshotErrorCode.SIZE_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A phylogenetic-tree artifact size does not match its metadata.",
                relative_path=metadata.relative_path,
            )
        if fingerprint.sha256 != metadata.sha256:
            raise _snapshot_error(
                StageSnapshotErrorCode.HASH_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A phylogenetic-tree artifact digest does not match its metadata.",
                relative_path=metadata.relative_path,
            )
        if (
            metadata.record_count is not None
            and fingerprint.record_count != metadata.record_count
        ):
            raise _snapshot_error(
                StageSnapshotErrorCode.RECORD_COUNT_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A phylogenetic-tree record count does not match its metadata.",
                relative_path=metadata.relative_path,
            )

    distance_snapshot: ValidatedStageSnapshot | None = None
    if validate_upstream:
        distance_snapshot = _validate_phylogenetic_tree_upstream_prefix(
            job_dir=job_dir,
            manifest=manifest,
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_task_id=expected_task_id,
            expected_config_hash=expected_config_hash,
        )
    if manifest.enabled:
        _validate_phylogenetic_tree_semantics(
            job_dir=job_dir,
            stage_root=stage_root,
            manifest=manifest,
            stage_id=generic_manifest.stage_id,
            distance_snapshot=distance_snapshot,
        )

    domain_hash = fingerprints[relative_path].sha256
    return (
        domain_hash,
        manifest.status.value,
        manifest.config_hash,
        manifest.source_artifacts,
    )


def _validate_phylogenetic_tree_upstream_prefix(
    *,
    job_dir: Path,
    manifest: PhylogeneticTreeManifest,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_task_id: str | None,
    expected_config_hash: str | None,
) -> ValidatedStageSnapshot | None:
    from jelica_core.distance_matrix import (
        DISTANCE_MATRIX_JSON_RELATIVE_PATH,
        DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
    )

    distance_prefix = "stages/distance_matrix/"
    distance_manifest_reference = (
        f"{distance_prefix}{DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH}"
    )
    distance_result_reference = f"{distance_prefix}{DISTANCE_MATRIX_JSON_RELATIVE_PATH}"

    if manifest.enabled and (
        distance_manifest_reference not in manifest.source_artifacts
        or distance_result_reference not in manifest.source_artifacts
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The phylogenetic-tree snapshot lacks required distance-matrix references.",
        )
    if not manifest.enabled:
        if len(manifest.source_artifacts) != 0:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail=(
                    "Disabled phylogenetic-tree snapshots must not include upstream "
                    "references."
                ),
            )
        return None

    try:
        distance_snapshot = validate_committed_stage_snapshot(
            job_dir=job_dir,
            stage_id="distance_matrix",
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_task_id=expected_task_id,
            expected_config_hash=expected_config_hash,
        )
    except StageCommitError as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The phylogenetic-tree distance-matrix prefix is not a valid snapshot.",
        ) from error
    if distance_snapshot.domain_status != "completed":
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The required distance-matrix snapshot is not completed.",
        )
    if distance_snapshot.config_hash != manifest.config_hash:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The phylogenetic-tree and distance-matrix configurations do not match.",
        )

    distance_artifacts = set(distance_snapshot.manifest.artifacts)
    for source_reference in manifest.source_artifacts:
        if source_reference.startswith(distance_prefix):
            relative_path = source_reference.removeprefix(distance_prefix)
            valid = relative_path in distance_artifacts
        else:
            valid = False
        if not valid:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail=(
                    "A phylogenetic-tree upstream reference is not in its committed "
                    "distance-matrix snapshot."
                ),
                relative_path=source_reference,
            )
    return distance_snapshot


def _validate_clade_detection_domain(
    *,
    job_dir: Path,
    stage_root: Path,
    generic_manifest: StageArtifactManifest,
    expected_task_id: str | None,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_config_hash: str | None,
    fingerprints: dict[str, StageArtifactFingerprint],
    validate_upstream: bool,
) -> tuple[str, str, str, tuple[str, ...]]:
    from jelica_core.clade_detection import (
        CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
        CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
        CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
        INFERRED_CLADES_JSON_RELATIVE_PATH,
        CladeDetectionComputationError,
        CladeDetectionManifest,
        InferredCladeMembershipRecord,
        InferredCladesResult,
        clade_detection_artifact_paths,
        parse_clade_assignments_tsv,
        validate_published_inferred_clades,
    )
    from jelica_core.distance_matrix import (
        DISTANCE_MATRIX_JSON_RELATIVE_PATH,
        DistanceMatrixResult,
    )
    from jelica_core.phylogenetic_tree import TREE_JSON_RELATIVE_PATH, PhylogeneticTreeResult

    relative_path = CLADE_DETECTION_MANIFEST_RELATIVE_PATH
    _require_generic_artifact(
        generic_manifest=generic_manifest,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
    )
    manifest = _load_typed_json(
        stage_root=stage_root,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
        model=CladeDetectionManifest,
    )
    if (
        manifest.job_id != expected_job_id
        or (expected_task_id is not None and manifest.task_id != expected_task_id)
        or (
            expected_config_hash is not None
            and manifest.config_hash != expected_config_hash
        )
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=generic_manifest.stage_id,
            detail="The clade-detection domain identity does not match the job.",
            relative_path=relative_path,
        )
    if generic_manifest.artifacts != clade_detection_artifact_paths(manifest):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The generic and clade-detection artifact sets are inconsistent.",
        )
    for metadata in manifest.artifacts:
        fingerprint = fingerprints[metadata.relative_path]
        if fingerprint.size_bytes != metadata.size_bytes:
            raise _snapshot_error(
                StageSnapshotErrorCode.SIZE_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A clade-detection artifact size does not match its metadata.",
                relative_path=metadata.relative_path,
            )
        if fingerprint.sha256 != metadata.sha256:
            raise _snapshot_error(
                StageSnapshotErrorCode.HASH_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A clade-detection artifact digest does not match its metadata.",
                relative_path=metadata.relative_path,
            )
        if (
            metadata.record_count is not None
            and fingerprint.record_count != metadata.record_count
        ):
            raise _snapshot_error(
                StageSnapshotErrorCode.RECORD_COUNT_MISMATCH,
                stage_id=generic_manifest.stage_id,
                detail="A clade-detection JSONL record count does not match its metadata.",
                relative_path=metadata.relative_path,
            )

    tree_snapshot: ValidatedStageSnapshot | None = None
    matrix_snapshot: ValidatedStageSnapshot | None = None
    if validate_upstream or manifest.enabled:
        tree_snapshot, matrix_snapshot = _validate_clade_detection_upstream_prefix(
            job_dir=job_dir,
            manifest=manifest,
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_task_id=expected_task_id,
            expected_config_hash=expected_config_hash,
        )
    if manifest.enabled:
        if tree_snapshot is None or matrix_snapshot is None:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=generic_manifest.stage_id,
                detail="Clade-detection semantic validation requires tree and matrix snapshots.",
            )
        result = _load_typed_json(
            stage_root=stage_root,
            stage_id=generic_manifest.stage_id,
            relative_path=INFERRED_CLADES_JSON_RELATIVE_PATH,
            model=InferredCladesResult,
        )
        membership_path = _resolve_artifact_path(
            stage_root=stage_root,
            stage_id=generic_manifest.stage_id,
            relative_path=CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
        )
        membership_records = _load_typed_jsonl(
            stage_id=generic_manifest.stage_id,
            relative_path=CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
            path=membership_path,
            model=InferredCladeMembershipRecord,
        )
        assignments_path = _resolve_artifact_path(
            stage_root=stage_root,
            stage_id=generic_manifest.stage_id,
            relative_path=CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
        )
        try:
            assignment_records = parse_clade_assignments_tsv(
                assignments_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=generic_manifest.stage_id,
                detail="clade_assignments.tsv is invalid or not parseable.",
                relative_path=CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
            ) from error

        tree_result = _load_typed_json(
            stage_root=job_dir / "stages" / "phylogenetic_tree",
            stage_id="phylogenetic_tree",
            relative_path=TREE_JSON_RELATIVE_PATH,
            model=PhylogeneticTreeResult,
        )
        matrix_result = _load_typed_json(
            stage_root=job_dir / "stages" / "distance_matrix",
            stage_id="distance_matrix",
            relative_path=DISTANCE_MATRIX_JSON_RELATIVE_PATH,
            model=DistanceMatrixResult,
        )
        if manifest.tree_snapshot_manifest_sha256 != tree_snapshot.domain_manifest_sha256:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=generic_manifest.stage_id,
                detail=(
                    "Clade-detection manifest tree snapshot digest is inconsistent with the "
                    "committed phylogenetic-tree snapshot."
                ),
                relative_path=INFERRED_CLADES_JSON_RELATIVE_PATH,
            )
        if (
            manifest.matrix_snapshot_manifest_sha256
            != matrix_snapshot.domain_manifest_sha256
        ):
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=generic_manifest.stage_id,
                detail=(
                    "Clade-detection manifest matrix snapshot digest is inconsistent with the "
                    "committed distance-matrix snapshot."
                ),
                relative_path=INFERRED_CLADES_JSON_RELATIVE_PATH,
            )
        if result.tree_snapshot_manifest_sha256 != manifest.tree_snapshot_manifest_sha256:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=generic_manifest.stage_id,
                detail="Inferred-clades result tree digest is inconsistent with manifest.",
                relative_path=INFERRED_CLADES_JSON_RELATIVE_PATH,
            )
        if result.matrix_snapshot_manifest_sha256 != manifest.matrix_snapshot_manifest_sha256:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=generic_manifest.stage_id,
                detail="Inferred-clades result matrix digest is inconsistent with manifest.",
                relative_path=INFERRED_CLADES_JSON_RELATIVE_PATH,
            )
        try:
            validate_published_inferred_clades(
                phylogenetic_tree_result=tree_result,
                distance_matrix_result=matrix_result,
                result=result,
                membership_records=membership_records,
                assignment_records=assignment_records,
            )
        except CladeDetectionComputationError as error:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=generic_manifest.stage_id,
                detail=(
                    "Clade-detection semantic validation failed: "
                    f"{error.detail}"
                ),
                relative_path=INFERRED_CLADES_JSON_RELATIVE_PATH,
            ) from error

    domain_hash = fingerprints[relative_path].sha256
    return (
        domain_hash,
        manifest.status.value,
        manifest.config_hash,
        manifest.source_artifacts,
    )


def _validate_clade_detection_upstream_prefix(
    *,
    job_dir: Path,
    manifest: CladeDetectionManifest,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_task_id: str | None,
    expected_config_hash: str | None,
) -> tuple[ValidatedStageSnapshot | None, ValidatedStageSnapshot | None]:
    from jelica_core.clade_detection import (
        CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
    )
    from jelica_core.distance_matrix import (
        DISTANCE_MATRIX_JSON_RELATIVE_PATH,
        DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
        DistanceMatrixStatus,
    )
    from jelica_core.phylogenetic_tree import (
        PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
        TREE_JSON_RELATIVE_PATH,
        PhylogeneticTreeStatus,
    )

    tree_prefix = "stages/phylogenetic_tree/"
    matrix_prefix = "stages/distance_matrix/"
    required_references = (
        f"{tree_prefix}{PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH}",
        f"{tree_prefix}{TREE_JSON_RELATIVE_PATH}",
        f"{matrix_prefix}{DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH}",
        f"{matrix_prefix}{DISTANCE_MATRIX_JSON_RELATIVE_PATH}",
    )
    if not manifest.enabled:
        if len(manifest.source_artifacts) != 0:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail=(
                    "Disabled clade-detection snapshots must not include upstream "
                    "artifact references."
                ),
                relative_path=CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
            )
        return None, None
    for reference in required_references:
        if reference not in manifest.source_artifacts:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail="Clade-detection snapshot lacks required upstream references.",
                relative_path=CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
            )

    try:
        tree_snapshot = validate_committed_stage_snapshot(
            job_dir=job_dir,
            stage_id="phylogenetic_tree",
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_task_id=expected_task_id,
            expected_config_hash=expected_config_hash,
        )
    except StageCommitError as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The clade-detection phylogenetic-tree prefix is not a valid snapshot.",
        ) from error
    try:
        matrix_snapshot = validate_committed_stage_snapshot(
            job_dir=job_dir,
            stage_id="distance_matrix",
            expected_job_id=expected_job_id,
            expected_pipeline_version=expected_pipeline_version,
            expected_task_id=expected_task_id,
            expected_config_hash=expected_config_hash,
        )
    except StageCommitError as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The clade-detection distance-matrix prefix is not a valid snapshot.",
        ) from error

    if tree_snapshot.domain_status != PhylogeneticTreeStatus.COMPLETED.value:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The required phylogenetic-tree snapshot is not completed.",
        )
    if matrix_snapshot.domain_status != DistanceMatrixStatus.COMPLETED.value:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="The required distance-matrix snapshot is not completed.",
        )
    if tree_snapshot.config_hash != manifest.config_hash:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="Clade-detection and phylogenetic-tree configurations do not match.",
        )
    if matrix_snapshot.config_hash != manifest.config_hash:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=manifest.stage_id,
            detail="Clade-detection and distance-matrix configurations do not match.",
        )
    tree_artifacts = set(tree_snapshot.manifest.artifacts)
    matrix_artifacts = set(matrix_snapshot.manifest.artifacts)
    for source_reference in manifest.source_artifacts:
        if source_reference.startswith(tree_prefix):
            relative_path = source_reference.removeprefix(tree_prefix)
            valid = relative_path in tree_artifacts
        elif source_reference.startswith(matrix_prefix):
            relative_path = source_reference.removeprefix(matrix_prefix)
            valid = relative_path in matrix_artifacts
        else:
            valid = False
        if not valid:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=manifest.stage_id,
                detail=(
                    "A clade-detection upstream reference is not in its committed "
                    "phylogenetic-tree or distance-matrix snapshot."
                ),
                relative_path=source_reference,
            )
    return tree_snapshot, matrix_snapshot


def _load_typed_jsonl(
    *,
    stage_id: str,
    relative_path: str,
    path: Path,
    model: type[_ModelT],
) -> tuple[_ModelT, ...]:
    rows: list[_ModelT] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() == "":
                continue
            rows.append(model.model_validate(json.loads(line)))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="A clade-detection JSONL artifact is invalid.",
            relative_path=relative_path,
        ) from error
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class _DistanceMatrixSnapshotData:
    manifest_sha256: str
    manifest: object
    result: object
    matrix_minimum: float
    matrix_maximum: float
    zero_distance_pair_count: int
    zero_diameter: bool


@dataclass(frozen=True, slots=True)
class _RepresentationValidationResult:
    leaf_labels: tuple[str, ...]
    branch_signatures: dict[tuple[str, ...], float]
    pairwise_leaf_distances: dict[tuple[str, str], float] | None
    internal_node_count: int
    negative_branch_count: int
    minimum_branch_length: float | None
    adjacency: dict[str, tuple[tuple[str, float], ...]]


def _validate_phylogenetic_tree_semantics(
    *,
    job_dir: Path,
    stage_root: Path,
    manifest: PhylogeneticTreeManifest,
    stage_id: str,
    distance_snapshot: ValidatedStageSnapshot | None,
) -> None:
    from jelica_core.phylogenetic_tree import (
        NEGATIVE_BRANCH_POLICY_CLAMP_TO_ZERO,
        TREE_DIAGNOSTICS_RELATIVE_PATH,
        TREE_JSON_RELATIVE_PATH,
        TREE_ROOTED_NWK_RELATIVE_PATH,
        TREE_UNROOTED_NWK_RELATIVE_PATH,
        ZERO_DIAMETER_ROOTING_FALLBACK,
        PhylogeneticTreeConstructionMode,
        PhylogeneticTreeDiagnostics,
        PhylogeneticTreeResult,
    )

    result = _load_typed_json(
        stage_root=stage_root,
        stage_id=stage_id,
        relative_path=TREE_JSON_RELATIVE_PATH,
        model=PhylogeneticTreeResult,
    )
    diagnostics = _load_typed_json(
        stage_root=stage_root,
        stage_id=stage_id,
        relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        model=PhylogeneticTreeDiagnostics,
    )
    _validate_tree_manifest_result_diagnostics_consistency(
        stage_id=stage_id,
        manifest=manifest,
        result=result,
        diagnostics=diagnostics,
    )
    _validate_tree_leaf_mapping_internal(
        stage_id=stage_id,
        result=result,
    )
    if result.negative_branch_policy != NEGATIVE_BRANCH_POLICY_CLAMP_TO_ZERO:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree negative-branch policy is unsupported for committed snapshots.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )

    canonical_leaf_order = result.canonical_leaf_order
    unrooted_validation = _validate_tree_representation(
        stage_id=stage_id,
        representation=result.unrooted,
        relative_path=TREE_JSON_RELATIVE_PATH,
        rooted=False,
        canonical_leaf_order=canonical_leaf_order,
    )
    rooted_validation = _validate_tree_representation(
        stage_id=stage_id,
        representation=result.rooted,
        relative_path=TREE_JSON_RELATIVE_PATH,
        rooted=True,
        canonical_leaf_order=canonical_leaf_order,
    )

    if unrooted_validation.leaf_labels != rooted_validation.leaf_labels:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree rooted and unrooted leaf sets are inconsistent.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )

    if len(canonical_leaf_order) == 1:
        expected_mode = PhylogeneticTreeConstructionMode.TRIVIAL_SINGLETON
    elif len(canonical_leaf_order) == 2:
        expected_mode = PhylogeneticTreeConstructionMode.TRIVIAL_PAIR
    else:
        expected_mode = PhylogeneticTreeConstructionMode.NEIGHBOR_JOINING
    if result.construction_mode is not expected_mode:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree construction mode does not match leaf-count constraints.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if (
        result.construction_mode is PhylogeneticTreeConstructionMode.NEIGHBOR_JOINING
        and result.zero_diameter
        and result.applied_rooting != ZERO_DIAMETER_ROOTING_FALLBACK
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree rooting metadata is inconsistent with zero-diameter fallback policy.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )

    raw_negative_count = unrooted_validation.negative_branch_count
    if raw_negative_count != result.raw_negative_branch_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree raw negative-branch count does not match unrooted representation.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if diagnostics.raw_negative_branch_count != result.raw_negative_branch_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics raw negative-branch count is inconsistent with tree result.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if manifest.raw_negative_branch_count != result.raw_negative_branch_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest raw negative-branch count is inconsistent with tree result.",
            relative_path=manifest.artifacts[0].relative_path if manifest.artifacts else None,
        )

    _validate_optional_float_match(
        stage_id=stage_id,
        relative_path=TREE_JSON_RELATIVE_PATH,
        detail="Tree minimum raw branch length is inconsistent with unrooted representation.",
        expected=unrooted_validation.minimum_branch_length,
        observed=result.minimum_raw_branch_length,
    )
    _validate_optional_float_match(
        stage_id=stage_id,
        relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        detail="Tree diagnostics minimum raw branch length is inconsistent with tree result.",
        expected=result.minimum_raw_branch_length,
        observed=diagnostics.minimum_raw_branch_length,
    )
    if diagnostics.normalized_negative_branch_count != result.normalized_negative_branch_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail=(
                "Tree diagnostics normalized negative-branch count is inconsistent with tree "
                "result."
            ),
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if rooted_validation.negative_branch_count != 0:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Rooted tree representation contains a negative branch length.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )

    unrooted_tree = _parse_tree_newick_artifact(
        stage_root=stage_root,
        stage_id=stage_id,
        relative_path=TREE_UNROOTED_NWK_RELATIVE_PATH,
    )
    rooted_tree = _parse_tree_newick_artifact(
        stage_root=stage_root,
        stage_id=stage_id,
        relative_path=TREE_ROOTED_NWK_RELATIVE_PATH,
    )
    _validate_newick_against_representation(
        stage_id=stage_id,
        relative_path=TREE_UNROOTED_NWK_RELATIVE_PATH,
        tree=unrooted_tree,
        representation_validation=unrooted_validation,
        canonical_leaf_order=canonical_leaf_order,
        rooted=False,
    )
    _validate_newick_against_representation(
        stage_id=stage_id,
        relative_path=TREE_ROOTED_NWK_RELATIVE_PATH,
        tree=rooted_tree,
        representation_validation=rooted_validation,
        canonical_leaf_order=canonical_leaf_order,
        rooted=True,
    )

    distance_data = _load_distance_matrix_snapshot_data(
        stage_id=stage_id,
        job_dir=job_dir,
        distance_snapshot=distance_snapshot,
    )
    _validate_tree_upstream_link(
        stage_id=stage_id,
        tree_manifest=manifest,
        tree_result=result,
        distance_data=distance_data,
    )
    _validate_tree_leaf_mapping_against_upstream(
        stage_id=stage_id,
        tree_result=result,
        distance_data=distance_data,
    )
    _validate_tree_diagnostics_consistency(
        stage_id=stage_id,
        diagnostics=diagnostics,
        result=result,
        rooted_validation=rooted_validation,
        distance_data=distance_data,
    )
    _validate_tree_warning_privacy(
        stage_id=stage_id,
        diagnostics=diagnostics,
        result=result,
    )


def _load_distance_matrix_snapshot_data(
    *,
    stage_id: str,
    job_dir: Path,
    distance_snapshot: ValidatedStageSnapshot | None,
) -> _DistanceMatrixSnapshotData:
    from jelica_core.distance_matrix import (
        DISTANCE_MATRIX_JSON_RELATIVE_PATH,
        DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
        DistanceMatrixManifest,
        DistanceMatrixResult,
        DistanceMatrixStatus,
    )

    if distance_snapshot is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree snapshot semantic validation requires a distance-matrix snapshot.",
        )
    if distance_snapshot.manifest.stage_id != "distance_matrix":
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree upstream stage identity is not distance_matrix.",
        )
    if distance_snapshot.domain_status != DistanceMatrixStatus.COMPLETED.value:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree upstream distance-matrix snapshot is not completed.",
        )
    if distance_snapshot.domain_manifest_sha256 is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree upstream distance-matrix manifest digest is unavailable.",
        )

    distance_root = job_dir / "stages" / "distance_matrix"
    distance_manifest = _load_typed_json(
        stage_root=distance_root,
        stage_id="distance_matrix",
        relative_path=DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
        model=DistanceMatrixManifest,
    )
    distance_result = _load_typed_json(
        stage_root=distance_root,
        stage_id="distance_matrix",
        relative_path=DISTANCE_MATRIX_JSON_RELATIVE_PATH,
        model=DistanceMatrixResult,
    )

    off_diagonal_values: list[float] = []
    zero_pair_count = 0
    sequence_count = len(distance_result.sequence_references)
    for row_index, row in enumerate(distance_result.matrix):
        if len(row) != sequence_count:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=stage_id,
                detail="Tree upstream distance matrix dimensions are inconsistent.",
                relative_path=DISTANCE_MATRIX_JSON_RELATIVE_PATH,
            )
        for column_index in range(row_index + 1, sequence_count):
            value = row[column_index]
            if value is None:
                raise _snapshot_error(
                    StageSnapshotErrorCode.UPSTREAM_INVALID,
                    stage_id=stage_id,
                    detail=(
                        "Tree upstream distance matrix contains undefined pair distances in "
                        "completed status."
                    ),
                    relative_path=DISTANCE_MATRIX_JSON_RELATIVE_PATH,
                )
            numeric_value = float(value)
            off_diagonal_values.append(numeric_value)
            if math.isclose(
                numeric_value,
                0.0,
                rel_tol=0.0,
                abs_tol=_TREE_ZERO_ABS_TOLERANCE,
            ):
                zero_pair_count += 1
    if len(off_diagonal_values) == 0:
        matrix_minimum = 0.0
        matrix_maximum = 0.0
        zero_diameter = True
    else:
        matrix_minimum = min(off_diagonal_values)
        matrix_maximum = max(off_diagonal_values)
        zero_diameter = all(
            math.isclose(value, 0.0, rel_tol=0.0, abs_tol=_TREE_ZERO_ABS_TOLERANCE)
            for value in off_diagonal_values
        )

    return _DistanceMatrixSnapshotData(
        manifest_sha256=distance_snapshot.domain_manifest_sha256,
        manifest=distance_manifest,
        result=distance_result,
        matrix_minimum=matrix_minimum,
        matrix_maximum=matrix_maximum,
        zero_distance_pair_count=zero_pair_count,
        zero_diameter=zero_diameter,
    )


def _validate_tree_manifest_result_diagnostics_consistency(
    *,
    stage_id: str,
    manifest: PhylogeneticTreeManifest,
    result: object,
    diagnostics: object,
) -> None:
    from jelica_core.phylogenetic_tree import (
        TREE_DIAGNOSTICS_RELATIVE_PATH,
        TREE_JSON_RELATIVE_PATH,
        PhylogeneticTreeDiagnostics,
        PhylogeneticTreeResult,
    )

    typed_result = result if isinstance(result, PhylogeneticTreeResult) else None
    typed_diagnostics = (
        diagnostics if isinstance(diagnostics, PhylogeneticTreeDiagnostics) else None
    )
    if typed_result is None or typed_diagnostics is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree semantic validation received invalid typed artifacts.",
        )

    if manifest.method is not typed_result.method:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest method is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.construction_mode is not typed_result.construction_mode:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest construction mode is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.inference_performed != typed_result.inference_performed:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest inference flag is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.requested_rooting is not typed_result.requested_rooting:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest requested rooting is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.applied_rooting != typed_result.applied_rooting:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest applied rooting is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.input_distance_model is not typed_result.input_distance_model:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest input distance model is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.leaf_count != len(typed_result.canonical_leaf_order):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest leaf count is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.edge_count != typed_result.edge_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest edge count is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.raw_negative_branch_count != typed_result.raw_negative_branch_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest negative-branch metadata is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.zero_diameter != typed_result.zero_diameter:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree manifest zero-diameter flag is inconsistent with tree result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if manifest.applied_rooting != typed_diagnostics.applied_rooting:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics applied rooting is inconsistent with manifest.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if manifest.requested_rooting is not typed_diagnostics.requested_rooting:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics requested rooting is inconsistent with manifest.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if manifest.construction_mode is not typed_diagnostics.construction_mode:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics construction mode is inconsistent with manifest.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if manifest.leaf_count != typed_diagnostics.leaf_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics leaf count is inconsistent with manifest.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if manifest.internal_node_count != typed_diagnostics.internal_node_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics internal-node count is inconsistent with manifest.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if manifest.edge_count != typed_diagnostics.edge_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics edge count is inconsistent with manifest.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if manifest.raw_negative_branch_count != typed_diagnostics.raw_negative_branch_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics negative-branch metadata is inconsistent with manifest.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if manifest.zero_diameter != typed_diagnostics.zero_diameter:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics zero-diameter flag is inconsistent with manifest.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if manifest.input_snapshot_manifest_sha256 != typed_result.input_snapshot_manifest_sha256:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree input snapshot digest is inconsistent across manifest and result.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )


def _validate_tree_leaf_mapping_internal(
    *,
    stage_id: str,
    result: object,
) -> None:
    from jelica_core.phylogenetic_tree import TREE_JSON_RELATIVE_PATH, PhylogeneticTreeResult

    typed_result = result if isinstance(result, PhylogeneticTreeResult) else None
    if typed_result is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree result artifact is not available for semantic validation.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )

    seen_leaf_labels: set[str] = set()
    seen_sequence_indexes: set[int] = set()
    seen_sequence_ids: set[str] = set()
    seen_logical_sample_ids: set[str] = set()
    leaf_count = len(typed_result.canonical_leaf_order)
    for mapping_index, mapping in enumerate(typed_result.leaf_mappings):
        if mapping.leaf_label in seen_leaf_labels:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree leaf mappings contain duplicate leaf labels.",
                relative_path=TREE_JSON_RELATIVE_PATH,
            )
        seen_leaf_labels.add(mapping.leaf_label)
        if mapping.sequence_index in seen_sequence_indexes:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree leaf mappings contain duplicate sequence indexes.",
                relative_path=TREE_JSON_RELATIVE_PATH,
            )
        seen_sequence_indexes.add(mapping.sequence_index)
        if mapping.sequence_id in seen_sequence_ids:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree leaf mappings contain duplicate sequence identifiers.",
                relative_path=TREE_JSON_RELATIVE_PATH,
            )
        seen_sequence_ids.add(mapping.sequence_id)
        if mapping.sequence_index != mapping_index:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree leaf mapping sequence indexes are not canonical.",
                relative_path=TREE_JSON_RELATIVE_PATH,
            )
        for logical_sample_id in mapping.logical_sample_ids:
            if logical_sample_id in seen_logical_sample_ids:
                raise _snapshot_error(
                    StageSnapshotErrorCode.INVALID,
                    stage_id=stage_id,
                    detail=(
                        "Tree logical-sample mapping assigns one logical sample to multiple "
                        "leaves."
                    ),
                    relative_path=TREE_JSON_RELATIVE_PATH,
                )
            seen_logical_sample_ids.add(logical_sample_id)

    expected_indexes = set(range(leaf_count))
    if seen_sequence_indexes != expected_indexes:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree leaf mappings do not cover the canonical sequence-index range.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )


def _validate_tree_representation(
    *,
    stage_id: str,
    representation: object,
    relative_path: str,
    rooted: bool,
    canonical_leaf_order: tuple[str, ...],
) -> _RepresentationValidationResult:
    from jelica_core.phylogenetic_tree import (
        PhylogeneticTreeRepresentation,
        TreeNodeKind,
    )

    typed_representation = (
        representation if isinstance(representation, PhylogeneticTreeRepresentation) else None
    )
    if typed_representation is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree representation is invalid.",
            relative_path=relative_path,
        )

    if typed_representation.rooted != rooted:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree representation rooted flag is inconsistent with artifact role.",
            relative_path=relative_path,
        )
    if rooted and typed_representation.root_id is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Rooted tree representation does not provide root_id.",
            relative_path=relative_path,
        )
    if not rooted and typed_representation.root_id is not None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Unrooted tree representation unexpectedly defines root_id.",
            relative_path=relative_path,
        )

    canonical_leaf_set = set(canonical_leaf_order)
    canonical_leaf_index = {
        leaf_label: index
        for index, leaf_label in enumerate(canonical_leaf_order)
    }
    nodes_by_id = {node.node_id: node for node in typed_representation.nodes}
    incoming_count = {node_id: 0 for node_id in nodes_by_id}
    children_by_parent: dict[str, list[str]] = {}
    undirected_edge_pairs: set[tuple[str, str]] = set()
    adjacency: dict[str, list[tuple[str, float]]] = {node_id: [] for node_id in nodes_by_id}
    negative_branch_count = 0
    minimum_branch_length: float | None = None

    for edge in typed_representation.edges:
        if edge.parent_id not in nodes_by_id or edge.child_id not in nodes_by_id:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree edge references an unknown node.",
                relative_path=relative_path,
            )
        canonical_pair = (
            edge.parent_id
            if edge.parent_id < edge.child_id
            else edge.child_id,
            edge.child_id
            if edge.parent_id < edge.child_id
            else edge.parent_id,
        )
        if canonical_pair in undirected_edge_pairs:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree representation contains duplicate edges.",
                relative_path=relative_path,
            )
        undirected_edge_pairs.add(canonical_pair)
        branch_length = _normalize_branch_length_for_validation(
            stage_id=stage_id,
            relative_path=relative_path,
            value=edge.branch_length,
        )
        if rooted and branch_length < -_TREE_ZERO_ABS_TOLERANCE:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Rooted tree representation contains a negative branch length.",
                relative_path=relative_path,
            )
        if branch_length < -_TREE_ZERO_ABS_TOLERANCE:
            negative_branch_count += 1
        minimum_branch_length = (
            branch_length
            if minimum_branch_length is None
            else min(minimum_branch_length, branch_length)
        )

        incoming_count[edge.child_id] += 1
        children_by_parent.setdefault(edge.parent_id, []).append(edge.child_id)
        adjacency[edge.parent_id].append((edge.child_id, branch_length))
        adjacency[edge.child_id].append((edge.parent_id, branch_length))

    traversal_root_id = typed_representation.traversal_root_id
    if traversal_root_id not in nodes_by_id:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree traversal root does not reference an existing node.",
            relative_path=relative_path,
        )
    if incoming_count[traversal_root_id] != 0:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree traversal root unexpectedly has a parent edge.",
            relative_path=relative_path,
        )
    for node_id, parent_count in incoming_count.items():
        if node_id == traversal_root_id:
            continue
        if parent_count != 1:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree representation parent assignments are inconsistent.",
                relative_path=relative_path,
            )

    traversal_order: list[str] = []
    visited: set[str] = set()
    stack = [traversal_root_id]
    while stack:
        node_id = stack.pop()
        if node_id in visited:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree representation contains a cycle in parent-child traversal.",
                relative_path=relative_path,
            )
        visited.add(node_id)
        traversal_order.append(node_id)
        for child_id in children_by_parent.get(node_id, ()):
            stack.append(child_id)
    if len(visited) != len(nodes_by_id):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree representation contains unreachable or disconnected nodes.",
            relative_path=relative_path,
        )

    leaf_labels: list[str] = []
    leaf_node_ids_by_label: dict[str, str] = {}
    for node in typed_representation.nodes:
        children = children_by_parent.get(node.node_id, [])
        if node.kind is TreeNodeKind.LEAF:
            if len(children) != 0:
                raise _snapshot_error(
                    StageSnapshotErrorCode.INVALID,
                    stage_id=stage_id,
                    detail="Tree leaf node has child edges.",
                    relative_path=relative_path,
                )
            assert node.leaf_label is not None
            if node.leaf_label not in canonical_leaf_set:
                raise _snapshot_error(
                    StageSnapshotErrorCode.INVALID,
                    stage_id=stage_id,
                    detail="Tree representation contains an unknown canonical leaf label.",
                    relative_path=relative_path,
                )
            if node.leaf_label in leaf_node_ids_by_label:
                raise _snapshot_error(
                    StageSnapshotErrorCode.INVALID,
                    stage_id=stage_id,
                    detail="Tree representation contains duplicate leaf nodes.",
                    relative_path=relative_path,
                )
            leaf_node_ids_by_label[node.leaf_label] = node.node_id
            leaf_labels.append(node.leaf_label)
            continue
        if len(children) == 0 and len(nodes_by_id) > 1:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree internal node has no child edges.",
                relative_path=relative_path,
            )

    if set(leaf_node_ids_by_label) != canonical_leaf_set:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree representation canonical leaf coverage is inconsistent.",
            relative_path=relative_path,
        )
    ordered_leaf_labels = tuple(
        sorted(leaf_labels, key=lambda value: (canonical_leaf_index[value], value))
    )

    descendant_leaf_sets: dict[str, set[str]] = {}
    for node_id in reversed(traversal_order):
        node = nodes_by_id[node_id]
        if node.kind is TreeNodeKind.LEAF:
            assert node.leaf_label is not None
            descendant_leaf_sets[node_id] = {node.leaf_label}
            continue
        union: set[str] = set()
        for child_id in children_by_parent.get(node_id, ()):
            union.update(descendant_leaf_sets[child_id])
        if len(union) == 0:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree internal node has no descendant leaves.",
                relative_path=relative_path,
            )
        descendant_leaf_sets[node_id] = union

    branch_signatures: dict[tuple[str, ...], float] = {}
    for edge in typed_representation.edges:
        descendant_leaf_labels = descendant_leaf_sets[edge.child_id]
        signature = _edge_signature(
            descendant_leaf_labels=descendant_leaf_labels,
        )
        if signature in branch_signatures:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree representation contains duplicate topology edge signatures.",
                relative_path=relative_path,
            )
        branch_signatures[signature] = _normalize_branch_length_for_validation(
            stage_id=stage_id,
            relative_path=relative_path,
            value=edge.branch_length,
        )

    pairwise_distances = (
        _pairwise_leaf_distances_from_graph(
            stage_id=stage_id,
            relative_path=relative_path,
            leaf_node_ids_by_label=leaf_node_ids_by_label,
            ordered_leaf_labels=ordered_leaf_labels,
            adjacency={
                node_id: tuple(neighbors)
                for node_id, neighbors in adjacency.items()
            },
        )
        if len(ordered_leaf_labels) <= _TREE_PAIRWISE_DISTANCE_MAX_LEAF_COUNT
        else None
    )

    return _RepresentationValidationResult(
        leaf_labels=ordered_leaf_labels,
        branch_signatures=branch_signatures,
        pairwise_leaf_distances=pairwise_distances,
        internal_node_count=sum(
            1
            for node in typed_representation.nodes
            if node.kind is not TreeNodeKind.LEAF
        ),
        negative_branch_count=negative_branch_count,
        minimum_branch_length=minimum_branch_length,
        adjacency={
            node_id: tuple(neighbors)
            for node_id, neighbors in adjacency.items()
        },
    )


def _parse_tree_newick_artifact(
    *,
    stage_root: Path,
    stage_id: str,
    relative_path: str,
):
    from io import StringIO

    from Bio import Phylo

    path = _resolve_artifact_path(
        stage_root=stage_root,
        stage_id=stage_id,
        relative_path=relative_path,
    )
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.ARTIFACT_UNREADABLE,
            stage_id=stage_id,
            detail="Tree Newick artifact could not be read.",
            relative_path=relative_path,
        ) from error
    try:
        trees = list(Phylo.parse(StringIO(payload), "newick"))
    except Exception as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree Newick artifact is not parseable.",
            relative_path=relative_path,
        ) from error
    if len(trees) != 1:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree Newick artifact must contain exactly one tree.",
            relative_path=relative_path,
        )
    return trees[0]


def _validate_newick_against_representation(
    *,
    stage_id: str,
    relative_path: str,
    tree,
    representation_validation: _RepresentationValidationResult,
    canonical_leaf_order: tuple[str, ...],
    rooted: bool,
) -> None:
    canonical_leaf_index = {label: index for index, label in enumerate(canonical_leaf_order)}
    leaves = list(tree.get_terminals())
    observed_labels: list[str] = []
    seen_labels: set[str] = set()
    for leaf in leaves:
        name = leaf.name.strip() if isinstance(leaf.name, str) else ""
        if name == "":
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree Newick artifact contains an unnamed leaf.",
                relative_path=relative_path,
            )
        if name in seen_labels:
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree Newick artifact contains duplicate leaf labels.",
                relative_path=relative_path,
            )
        seen_labels.add(name)
        observed_labels.append(name)
    ordered_observed_labels = tuple(
        sorted(observed_labels, key=lambda value: (canonical_leaf_index.get(value, -1), value))
    )
    if set(ordered_observed_labels) != set(representation_validation.leaf_labels):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree Newick leaf labels are inconsistent with tree.json leaf labels.",
            relative_path=relative_path,
        )

    clade_stack = [tree.root]
    traversal_order = []
    while clade_stack:
        clade = clade_stack.pop()
        traversal_order.append(clade)
        for child in clade.clades:
            length = _normalize_branch_length_for_validation(
                stage_id=stage_id,
                relative_path=relative_path,
                value=0.0 if child.branch_length is None else float(child.branch_length),
            )
            if rooted and length < -_TREE_ZERO_ABS_TOLERANCE:
                raise _snapshot_error(
                    StageSnapshotErrorCode.INVALID,
                    stage_id=stage_id,
                    detail="Rooted Newick artifact contains a negative branch length.",
                    relative_path=relative_path,
                )
            clade_stack.append(child)

    clade_children = {clade: list(clade.clades) for clade in traversal_order}
    descendant_leaf_sets: dict[object, set[str]] = {}
    for clade in reversed(traversal_order):
        children = clade_children[clade]
        if len(children) == 0:
            leaf_name = clade.name.strip() if isinstance(clade.name, str) else ""
            descendant_leaf_sets[clade] = {leaf_name}
            continue
        union: set[str] = set()
        for child in children:
            union.update(descendant_leaf_sets[child])
        descendant_leaf_sets[clade] = union

    signatures: dict[tuple[str, ...], float] = {}
    adjacency: dict[object, list[tuple[object, float]]] = {clade: [] for clade in traversal_order}
    leaf_nodes_by_label: dict[str, object] = {}
    for clade in traversal_order:
        if len(clade_children[clade]) == 0:
            label = clade.name.strip() if isinstance(clade.name, str) else ""
            leaf_nodes_by_label[label] = clade
            continue
        for child in clade_children[clade]:
            length = _normalize_branch_length_for_validation(
                stage_id=stage_id,
                relative_path=relative_path,
                value=0.0 if child.branch_length is None else float(child.branch_length),
            )
            signature = _edge_signature(
                descendant_leaf_labels=descendant_leaf_sets[child],
            )
            if signature in signatures:
                raise _snapshot_error(
                    StageSnapshotErrorCode.INVALID,
                    stage_id=stage_id,
                    detail="Tree Newick artifact contains duplicate topology edge signatures.",
                    relative_path=relative_path,
                )
            signatures[signature] = length
            adjacency[clade].append((child, length))
            adjacency[child].append((clade, length))

    if signatures.keys() != representation_validation.branch_signatures.keys():
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree Newick topology is inconsistent with tree.json representation.",
            relative_path=relative_path,
        )
    for signature, expected_length in representation_validation.branch_signatures.items():
        observed_length = signatures[signature]
        if not _float_matches(expected=expected_length, observed=observed_length):
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail=(
                    "Tree Newick branch lengths are inconsistent with tree.json edge "
                    "metadata."
                ),
                relative_path=relative_path,
            )

    if (
        len(representation_validation.leaf_labels) <= _TREE_PAIRWISE_DISTANCE_MAX_LEAF_COUNT
        and representation_validation.pairwise_leaf_distances is not None
    ):
        ordered_leaf_labels = tuple(
            sorted(
                leaf_nodes_by_label,
                key=lambda value: (canonical_leaf_index[value], value),
            )
        )
        observed_pairwise = _pairwise_leaf_distances_from_graph(
            stage_id=stage_id,
            relative_path=relative_path,
            leaf_node_ids_by_label=leaf_nodes_by_label,
            ordered_leaf_labels=ordered_leaf_labels,
            adjacency={
                node: tuple(neighbors)
                for node, neighbors in adjacency.items()
            },
        )
        if observed_pairwise.keys() != representation_validation.pairwise_leaf_distances.keys():
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail="Tree Newick pairwise leaf distances are incomplete.",
                relative_path=relative_path,
            )
        for pair, expected_distance in representation_validation.pairwise_leaf_distances.items():
            observed_distance = observed_pairwise[pair]
            if not _float_matches(expected=expected_distance, observed=observed_distance):
                raise _snapshot_error(
                    StageSnapshotErrorCode.INVALID,
                    stage_id=stage_id,
                    detail="Tree Newick pairwise leaf distances differ from tree.json.",
                    relative_path=relative_path,
                )


def _validate_tree_upstream_link(
    *,
    stage_id: str,
    tree_manifest: PhylogeneticTreeManifest,
    tree_result: object,
    distance_data: _DistanceMatrixSnapshotData,
) -> None:
    from jelica_core.config import AnalysisDistanceMatrixModel
    from jelica_core.phylogenetic_tree import (
        TREE_JSON_RELATIVE_PATH,
        PhylogeneticTreeResult,
    )

    typed_result = tree_result if isinstance(tree_result, PhylogeneticTreeResult) else None
    if typed_result is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree semantic validation received invalid typed artifacts.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )

    if tree_manifest.input_snapshot_manifest_sha256 != distance_data.manifest_sha256:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree manifest input snapshot digest does not match distance-matrix snapshot.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if typed_result.input_snapshot_manifest_sha256 != distance_data.manifest_sha256:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree result input snapshot digest does not match distance-matrix snapshot.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    if typed_result.input_distance_model is not AnalysisDistanceMatrixModel.P_DISTANCE:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree result uses an unsupported upstream distance model.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    distance_result = distance_data.result
    if typed_result.input_distance_model is not distance_result.model:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree result distance model is inconsistent with upstream distance matrix.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )


def _validate_tree_leaf_mapping_against_upstream(
    *,
    stage_id: str,
    tree_result: object,
    distance_data: _DistanceMatrixSnapshotData,
) -> None:
    from jelica_core.phylogenetic_tree import TREE_JSON_RELATIVE_PATH, PhylogeneticTreeResult

    typed_result = tree_result if isinstance(tree_result, PhylogeneticTreeResult) else None
    if typed_result is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree result artifact is unavailable for upstream mapping checks.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )

    distance_result = distance_data.result
    if len(typed_result.canonical_leaf_order) != len(distance_result.sequence_references):
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree leaf count is inconsistent with upstream distance-matrix dimensions.",
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
    seen_logical_samples: set[str] = set()
    for index, mapping in enumerate(typed_result.leaf_mappings):
        reference = distance_result.sequence_references[index]
        if mapping.sequence_index != reference.index:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=stage_id,
                detail="Tree sequence-index mapping is inconsistent with upstream matrix order.",
                relative_path=TREE_JSON_RELATIVE_PATH,
            )
        if mapping.sequence_id != reference.sequence_id:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=stage_id,
                detail="Tree sequence mapping is inconsistent with upstream sequence references.",
                relative_path=TREE_JSON_RELATIVE_PATH,
            )
        if mapping.logical_sample_ids != reference.logical_sample_ids:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=stage_id,
                detail=(
                    "Tree logical-sample mapping is inconsistent with upstream sequence "
                    "references."
                ),
                relative_path=TREE_JSON_RELATIVE_PATH,
            )
        for logical_sample_id in mapping.logical_sample_ids:
            if logical_sample_id in seen_logical_samples:
                raise _snapshot_error(
                    StageSnapshotErrorCode.UPSTREAM_INVALID,
                    stage_id=stage_id,
                    detail=(
                        "Tree logical-sample mapping duplicates a logical sample across "
                        "multiple leaves."
                    ),
                    relative_path=TREE_JSON_RELATIVE_PATH,
                )
            seen_logical_samples.add(logical_sample_id)


def _validate_tree_diagnostics_consistency(
    *,
    stage_id: str,
    diagnostics: object,
    result: object,
    rooted_validation: _RepresentationValidationResult,
    distance_data: _DistanceMatrixSnapshotData,
) -> None:
    from jelica_core.phylogenetic_tree import (
        TREE_DIAGNOSTICS_RELATIVE_PATH,
        ZERO_DIAMETER_ROOTING_FALLBACK,
        PhylogeneticTreeDiagnostics,
        PhylogeneticTreeResult,
    )

    typed_diagnostics = (
        diagnostics if isinstance(diagnostics, PhylogeneticTreeDiagnostics) else None
    )
    typed_result = result if isinstance(result, PhylogeneticTreeResult) else None
    if typed_diagnostics is None or typed_result is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics validation received invalid typed artifacts.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )

    if typed_diagnostics.leaf_count != len(typed_result.canonical_leaf_order):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics leaf count is inconsistent with tree result.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if typed_diagnostics.internal_node_count != rooted_validation.internal_node_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics internal-node count is inconsistent with rooted topology.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if typed_diagnostics.edge_count != typed_result.edge_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics edge count is inconsistent with tree result.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    expected_dimensions = (
        len(distance_data.result.sequence_references),
        len(distance_data.result.sequence_references),
    )
    if typed_diagnostics.input_matrix_dimensions != expected_dimensions:
        raise _snapshot_error(
            StageSnapshotErrorCode.UPSTREAM_INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics matrix dimensions are inconsistent with upstream matrix.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if not _float_matches(
        expected=distance_data.matrix_minimum,
        observed=typed_diagnostics.input_distance_min,
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics minimum input distance is inconsistent with upstream matrix.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if not _float_matches(
        expected=distance_data.matrix_maximum,
        observed=typed_diagnostics.input_distance_max,
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics maximum input distance is inconsistent with upstream matrix.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if typed_diagnostics.zero_distance_pair_count != distance_data.zero_distance_pair_count:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail=(
                "Tree diagnostics zero-distance pair count is inconsistent with upstream "
                "matrix."
            ),
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if typed_diagnostics.zero_diameter != distance_data.zero_diameter:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics zero-diameter flag is inconsistent with upstream matrix.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    computed_tree_diameter = _tree_diameter_from_graph(rooted_validation.adjacency)
    if not _float_matches(
        expected=computed_tree_diameter,
        observed=typed_diagnostics.tree_diameter,
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics diameter is inconsistent with rooted branch lengths.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if (
        typed_diagnostics.applied_rooting == ZERO_DIAMETER_ROOTING_FALLBACK
        and not typed_diagnostics.zero_diameter
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics zero-diameter fallback metadata is inconsistent.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )

    warning_codes = tuple(warning.code for warning in typed_diagnostics.warnings)
    if (
        "zero_diameter_distance_matrix" in warning_codes
        and not typed_diagnostics.zero_diameter
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics warnings are inconsistent with zero-diameter metadata.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if (
        "raw_negative_branch_lengths_detected" in warning_codes
        and typed_diagnostics.raw_negative_branch_count == 0
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics warnings are inconsistent with negative-branch metadata.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )
    if (
        "negative_branch_lengths_normalized_for_rooting" in warning_codes
        and typed_diagnostics.normalized_negative_branch_count == 0
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics warnings are inconsistent with normalization metadata.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )


def _validate_tree_warning_privacy(
    *,
    stage_id: str,
    diagnostics: object,
    result: object,
) -> None:
    from jelica_core.phylogenetic_tree import (
        TREE_DIAGNOSTICS_RELATIVE_PATH,
        PhylogeneticTreeDiagnostics,
        PhylogeneticTreeResult,
    )

    typed_diagnostics = (
        diagnostics if isinstance(diagnostics, PhylogeneticTreeDiagnostics) else None
    )
    typed_result = result if isinstance(result, PhylogeneticTreeResult) else None
    if typed_diagnostics is None or typed_result is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree diagnostics privacy validation received invalid typed artifacts.",
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )

    sensitive_tokens: list[str] = list(typed_result.canonical_leaf_order)
    for mapping in typed_result.leaf_mappings:
        sensitive_tokens.append(mapping.sequence_id)
        sensitive_tokens.extend(mapping.logical_sample_ids)
    for index, warning in enumerate(typed_diagnostics.warnings):
        detail = warning.detail
        if any(token for token in sensitive_tokens if token and token in detail):
            raise _snapshot_error(
                StageSnapshotErrorCode.INVALID,
                stage_id=stage_id,
                detail=(
                    "Tree diagnostics warning payload contains sensitive mapping values "
                    f"(warning_index={index})."
                ),
                relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
            )


def _normalize_branch_length_for_validation(
    *,
    stage_id: str,
    relative_path: str,
    value: float,
) -> float:
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="Tree branch length contains a non-finite value.",
            relative_path=relative_path,
        )
    if abs(numeric_value) <= _TREE_ZERO_ABS_TOLERANCE:
        return 0.0
    return numeric_value


def _edge_signature(
    *,
    descendant_leaf_labels: set[str],
) -> tuple[str, ...]:
    return tuple(sorted(descendant_leaf_labels))


def _pairwise_leaf_distances_from_graph(
    *,
    stage_id: str,
    relative_path: str,
    leaf_node_ids_by_label: dict[str, object],
    ordered_leaf_labels: tuple[str, ...],
    adjacency: dict[object, tuple[tuple[object, float], ...]]
) -> dict[tuple[str, str], float]:
    distances: dict[tuple[str, str], float] = {}
    for left_index, left_label in enumerate(ordered_leaf_labels):
        start_node = leaf_node_ids_by_label[left_label]
        distances_from_start = _distances_from_node(start_node=start_node, adjacency=adjacency)
        for right_label in ordered_leaf_labels[left_index + 1 :]:
            target_node = leaf_node_ids_by_label[right_label]
            distance = distances_from_start.get(target_node)
            if distance is None:
                raise _snapshot_error(
                    StageSnapshotErrorCode.INVALID,
                    stage_id=stage_id,
                    detail="Tree graph distance traversal is disconnected.",
                    relative_path=relative_path,
                )
            distances[(left_label, right_label)] = distance
    return distances


def _distances_from_node(
    *,
    start_node: object,
    adjacency: dict[object, tuple[tuple[object, float], ...]],
) -> dict[object, float]:
    distances: dict[object, float] = {start_node: 0.0}
    stack: list[tuple[object, object | None]] = [(start_node, None)]
    while stack:
        node_id, parent_id = stack.pop()
        base_distance = distances[node_id]
        for neighbor_id, edge_length in adjacency.get(node_id, ()):
            if neighbor_id == parent_id:
                continue
            distances[neighbor_id] = base_distance + edge_length
            stack.append((neighbor_id, node_id))
    return distances


def _tree_diameter_from_graph(adjacency: dict[str, tuple[tuple[str, float], ...]]) -> float:
    if len(adjacency) == 0:
        return 0.0
    start_node = next(iter(adjacency))
    far_node, _ = _farthest_node(start_node=start_node, adjacency=adjacency)
    _other_node, diameter = _farthest_node(start_node=far_node, adjacency=adjacency)
    return diameter


def _farthest_node(
    *,
    start_node: str,
    adjacency: dict[str, tuple[tuple[str, float], ...]],
) -> tuple[str, float]:
    distances = _distances_from_node(start_node=start_node, adjacency=adjacency)
    return max(
        distances.items(),
        key=lambda item: (item[1], item[0]),
    )


def _validate_optional_float_match(
    *,
    stage_id: str,
    relative_path: str,
    detail: str,
    expected: float | None,
    observed: float | None,
) -> None:
    if expected is None and observed is None:
        return
    if expected is None or observed is None:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail=detail,
            relative_path=relative_path,
        )
    if not _float_matches(expected=expected, observed=observed):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail=detail,
            relative_path=relative_path,
        )


def _float_matches(*, expected: float, observed: float) -> bool:
    return math.isclose(
        expected,
        observed,
        rel_tol=0.0,
        abs_tol=_TREE_VALIDATION_ABS_TOLERANCE,
    )


def _inspect_artifact(
    *,
    stage_root: Path,
    stage_id: str,
    relative_path: str,
) -> StageArtifactFingerprint:
    path = _resolve_artifact_path(
        stage_root=stage_root,
        stage_id=stage_id,
        relative_path=relative_path,
    )
    digest = hashlib.sha256()
    size_bytes = 0
    record_count: int | None = None
    is_jsonl = relative_path.endswith(".jsonl")
    is_json = relative_path.endswith(".json")
    json_payload = bytearray() if is_json else None
    if is_jsonl:
        record_count = 0
    try:
        with path.open("rb") as handle:
            for line in handle:
                digest.update(line)
                size_bytes += len(line)
                if json_payload is not None:
                    json_payload.extend(line)
                if is_jsonl and line.strip():
                    json.loads(line)
                    assert record_count is not None
                    record_count += 1
        if json_payload is not None:
            json.loads(json_payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.ARTIFACT_UNREADABLE,
            stage_id=stage_id,
            detail="A stage artifact could not be read as its published format.",
            relative_path=relative_path,
        ) from error
    return StageArtifactFingerprint(
        relative_path=relative_path,
        size_bytes=size_bytes,
        sha256=digest.hexdigest(),
        record_count=record_count,
    )


def _validate_result_package_domain(
    *,
    job_dir: Path,
    stage_root: Path,
    generic_manifest: StageArtifactManifest,
    expected_task_id: str | None,
    expected_job_id: str,
    expected_pipeline_version: str,
    expected_config_hash: str | None,
    fingerprints: dict[str, StageArtifactFingerprint],
    validate_upstream: bool,
) -> tuple[str, str, str, tuple[str, ...]]:
    from jelica_core.result_package import (
        RESULT_PACKAGE_STAGE_ID,
        RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,
        ResultPackageValidationError,
        load_result_package_stage_manifest,
        relative_package_path_from_task,
        result_package_artifact_paths,
        result_package_target_path,
    )

    relative_path = RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH
    _require_generic_artifact(
        generic_manifest=generic_manifest,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
    )
    stage_manifest_path = _resolve_artifact_path(
        stage_root=stage_root,
        stage_id=generic_manifest.stage_id,
        relative_path=relative_path,
    )
    try:
        stage_manifest = load_result_package_stage_manifest(path=stage_manifest_path)
    except ResultPackageValidationError as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The result-package stage manifest is invalid.",
            relative_path=relative_path,
        ) from error

    if (
        stage_manifest.job_id != expected_job_id
        or (expected_task_id is not None and stage_manifest.task_id != expected_task_id)
        or (
            expected_config_hash is not None
            and stage_manifest.config_hash != expected_config_hash
        )
        or stage_manifest.task.task_id != stage_manifest.task_id
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.IDENTITY_MISMATCH,
            stage_id=generic_manifest.stage_id,
            detail="The result-package identity does not match the expected job snapshot.",
            relative_path=relative_path,
        )

    expected_generic_artifacts = result_package_artifact_paths(stage_manifest)
    legacy_generic_artifacts = (
        RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,
        stage_manifest.prepared_package_relative_path,
    )
    if generic_manifest.artifacts not in {
        expected_generic_artifacts,
        legacy_generic_artifacts,
    }:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="The generic and result-package artifact sets are inconsistent.",
        )

    task_dir = job_dir.parent.parent
    expected_published_relative_path = relative_package_path_from_task(
        task_dir=task_dir,
        package_path=result_package_target_path(
            task_dir=task_dir,
            content_digest=stage_manifest.content_digest,
        ),
    )
    if stage_manifest.published_package_relative_path != expected_published_relative_path:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=generic_manifest.stage_id,
            detail="Result-package publication path does not match expected content digest.",
            relative_path=relative_path,
        )

    if validate_upstream:
        source_stage_ids = stage_manifest.source_stage_ids
        if RESULT_PACKAGE_STAGE_ID in source_stage_ids:
            raise _snapshot_error(
                StageSnapshotErrorCode.UPSTREAM_INVALID,
                stage_id=generic_manifest.stage_id,
                detail="result_package source_stage_ids must not reference result_package itself.",
                relative_path=relative_path,
            )
        for source_stage_id in source_stage_ids:
            try:
                upstream_snapshot = validate_committed_stage_snapshot(
                    job_dir=job_dir,
                    stage_id=source_stage_id,
                    expected_job_id=expected_job_id,
                    expected_pipeline_version=expected_pipeline_version,
                    expected_task_id=expected_task_id,
                    expected_config_hash=expected_config_hash,
                )
            except StageCommitError as error:
                raise _snapshot_error(
                    StageSnapshotErrorCode.UPSTREAM_INVALID,
                    stage_id=generic_manifest.stage_id,
                    detail=(
                        "A result-package source stage does not resolve to a valid committed "
                        "snapshot."
                    ),
                    relative_path=relative_path,
                ) from error
            if upstream_snapshot.domain_status == "failed":
                raise _snapshot_error(
                    StageSnapshotErrorCode.UPSTREAM_INVALID,
                    stage_id=generic_manifest.stage_id,
                    detail="A result-package source stage has failed status.",
                    relative_path=relative_path,
                )

    domain_hash = fingerprints[relative_path].sha256
    return (
        domain_hash,
        stage_manifest.task_status.value,
        stage_manifest.config_hash,
        tuple(stage_manifest.source_stage_ids),
    )


def _resolve_artifact_path(
    *,
    stage_root: Path,
    stage_id: str,
    relative_path: str,
) -> Path:
    normalized = _normalize_relative_path(relative_path, stage_id=stage_id)
    try:
        resolved_root = stage_root.resolve(strict=True)
        resolved_path = (stage_root / PurePosixPath(normalized)).resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.ARTIFACT_MISSING,
            stage_id=stage_id,
            detail="A listed stage artifact is missing or outside the stage snapshot.",
            relative_path=normalized,
        ) from error
    if not resolved_path.is_file():
        raise _snapshot_error(
            StageSnapshotErrorCode.ARTIFACT_MISSING,
            stage_id=stage_id,
            detail="A listed stage artifact is not a regular file.",
            relative_path=normalized,
        )
    return resolved_path


def _normalize_relative_path(value: str, *, stage_id: str) -> str:
    normalized = value.strip().replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if (
        normalized == ""
        or posix.is_absolute()
        or windows.is_absolute()
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="A stage artifact reference is not a safe relative path.",
        )
    return posix.as_posix()


def _load_typed_json(
    *,
    stage_root: Path,
    stage_id: str,
    relative_path: str,
    model: type[_ModelT],
) -> _ModelT:
    path = _resolve_artifact_path(
        stage_root=stage_root,
        stage_id=stage_id,
        relative_path=relative_path,
    )
    try:
        return model.model_validate_json(path.read_bytes())
    except Exception as error:
        raise _snapshot_error(
            StageSnapshotErrorCode.INVALID,
            stage_id=stage_id,
            detail="A stage domain manifest is invalid.",
            relative_path=relative_path,
        ) from error


def _require_generic_artifact(
    *,
    generic_manifest: StageArtifactManifest,
    stage_id: str,
    relative_path: str,
) -> None:
    if relative_path not in generic_manifest.artifacts:
        raise _snapshot_error(
            StageSnapshotErrorCode.ARTIFACT_MISSING,
            stage_id=stage_id,
            detail="The domain manifest is not listed by the generic stage manifest.",
            relative_path=relative_path,
        )


def _is_idempotent_commit(
    *,
    existing_snapshot: ValidatedStageSnapshot,
    new_snapshot: ValidatedStageSnapshot,
) -> bool:
    existing = existing_snapshot.manifest
    new = new_snapshot.manifest
    return (
        existing.stage_id == new.stage_id
        and existing.job_id == new.job_id
        and existing.pipeline_version == new.pipeline_version
        and existing.artifacts == new.artifacts
        and existing_snapshot.artifact_fingerprints
        == new_snapshot.artifact_fingerprints
        and existing_snapshot.domain_manifest_sha256
        == new_snapshot.domain_manifest_sha256
        and existing_snapshot.domain_status == new_snapshot.domain_status
        and existing_snapshot.config_hash == new_snapshot.config_hash
        and existing_snapshot.source_artifacts == new_snapshot.source_artifacts
    )


def _snapshot_error(
    code: StageSnapshotErrorCode,
    *,
    stage_id: str,
    detail: str,
    relative_path: str | None = None,
) -> StageSnapshotValidationError:
    return StageSnapshotValidationError(
        code=code,
        stage_id=stage_id,
        detail=detail,
        relative_path=relative_path,
    )


def _cleanup_empty_parents(start_path: Path, *, stop_at: Path) -> None:
    current = start_path
    while True:
        if not current.exists() or not current.is_dir():
            break
        try:
            current.rmdir()
        except OSError:
            break
        if current == stop_at:
            break
        current = current.parent
