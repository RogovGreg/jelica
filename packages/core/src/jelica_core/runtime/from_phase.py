from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jelica_contracts import JSONValue
from jelica_core.alignment import ALIGNMENT_MANIFEST_RELATIVE_PATH, AlignmentManifest
from jelica_core.analysis import resolve_analysis_execution_selection
from jelica_core.clade_detection import (
    CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
    INFERRED_CLADES_JSON_RELATIVE_PATH,
    CladeDetectionManifest,
    InferredCladesResult,
    artifact_metadata as clade_detection_artifact_metadata,
)
from jelica_core.comparative_analysis import (
    COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
    ComparativeAnalysisManifest,
)
from jelica_core.config import (
    AUTO_ANALYSIS_EXECUTION_FROM_PHASE,
    AnalysisConfigInput,
    ResolvedAnalysisConfig,
    resolve_analysis_config,
)
from jelica_core.distance_matrix import DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH, DistanceMatrixManifest
from jelica_core.input_sources import InputSourceKind, classify_input_source
from jelica_core.phylogenetic_tree import (
    PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
    TREE_JSON_RELATIVE_PATH,
    PhylogeneticTreeManifest,
    PhylogeneticTreeResult,
    artifact_metadata as phylogenetic_tree_artifact_metadata,
)
from jelica_core.tasks.storage import compute_config_hash, write_text_atomically

from .artifacts import (
    STAGE_MANIFEST_FILENAME,
    StageCommitError,
    load_stage_manifest,
    validate_committed_stage_snapshot,
    write_stage_manifest,
)
from .config_sync import _resolved_config_as_strict_input
from .input_parsers import INPUT_MANIFEST_RELATIVE_PATH
from .input_processing_models import INPUT_PROCESSING_MANIFEST_RELATIVE_PATH, InputProcessingManifest
from .models import DEFAULT_PIPELINE_NAME, RuntimeStateCheckpoint
from .pipeline import build_pipeline_definition

_LEGACY_AUTO_FROM_PHASE = "raw"
_RESULT_PACKAGE_STAGE_ID = "result_package"
_TASK_CONFIG_FILENAME = "config.json"
_TASK_CONFIGS_DIRNAME = "configs"
_TASK_JOBS_DIRNAME = "jobs"

_DOMAIN_MANIFEST_PATH_BY_STAGE: dict[str, str] = {
    "initialize_job": "execution_manifest.json",
    "input_acquisition": INPUT_MANIFEST_RELATIVE_PATH,
    "input_processing": INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    "alignment": ALIGNMENT_MANIFEST_RELATIVE_PATH,
    "comparative_analysis": COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
    "distance_matrix": DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
    "phylogenetic_tree": PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
    "clade_detection": CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
}


class FromPhasePreparationError(RuntimeError):
    """Raised when execution.from_phase preparation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class PreparedJobSeed:
    requested_job_id: str
    runtime_state: dict[str, JSONValue]
    prepared_job_dir: Path
    target: str
    requested_from_phase: str
    resolved_start_stage: str
    copied_stage_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SourceWorkspaceCandidate:
    source_job_dir: Path
    source_job_id: str
    reusable_prefix_stage_ids: tuple[str, ...]
    last_completed_at: str


def prepare_from_phase_seed_for_new_job(
    *,
    task_id: str,
    task_dir: Path,
    config_relative_path: str,
    config_hash: str,
    requested_job_id: str,
    pipeline_name: str,
    pipeline_version: str,
) -> PreparedJobSeed | None:
    if pipeline_name != DEFAULT_PIPELINE_NAME:
        return None

    normalized_task_id = task_id.strip()
    if normalized_task_id == "":
        raise FromPhasePreparationError("task_id must not be empty")

    normalized_job_id = requested_job_id.strip()
    if normalized_job_id == "":
        raise FromPhasePreparationError("requested_job_id must not be empty")

    config_revision_path = task_dir / config_relative_path
    config = _load_resolved_analysis_config(
        config_revision_path=config_revision_path,
        expected_config_hash=config_hash,
    )
    pipeline = build_pipeline_definition(
        pipeline_name=pipeline_name,
        pipeline_version=pipeline_version,
        config_revision_path=config_revision_path,
    )
    ordered_analysis_stage_ids = tuple(
        stage.stage_id
        for stage in pipeline.stages
        if stage.stage_id != _RESULT_PACKAGE_STAGE_ID
    )
    if len(ordered_analysis_stage_ids) == 0:
        return None

    selection = resolve_analysis_execution_selection(
        config=config,
        pipeline=pipeline,
        allow_explicit_from_phase=True,
    )
    source_workspace = _resolve_workspace_source_directory(config=config)

    candidate: _SourceWorkspaceCandidate | None = None
    copied_stage_ids: tuple[str, ...] = tuple()
    resolved_start_stage = selection.resolved_start_phase
    requested_from_phase = selection.from_phase

    if requested_from_phase in {AUTO_ANALYSIS_EXECUTION_FROM_PHASE, _LEGACY_AUTO_FROM_PHASE}:
        if source_workspace is None:
            return None
        candidate = _select_source_workspace_candidate(
            source_workspace=source_workspace,
            ordered_analysis_stage_ids=ordered_analysis_stage_ids,
            pipeline_version=pipeline_version,
        )
        if candidate is None or len(candidate.reusable_prefix_stage_ids) == 0:
            return None
        copied_stage_ids = candidate.reusable_prefix_stage_ids
        resolved_start_stage = (
            ordered_analysis_stage_ids[len(copied_stage_ids)]
            if len(copied_stage_ids) < len(ordered_analysis_stage_ids)
            else _RESULT_PACKAGE_STAGE_ID
        )
    else:
        required_prefix_stage_ids = _required_prefix_stage_ids(
            ordered_analysis_stage_ids=ordered_analysis_stage_ids,
            start_stage_id=selection.resolved_start_phase,
        )
        if len(required_prefix_stage_ids) == 0:
            return None
        if source_workspace is None:
            raise FromPhasePreparationError(
                "An explicit execution.from_phase requires exactly one local task "
                "workspace directory source."
            )
        candidate = _select_source_workspace_candidate(
            source_workspace=source_workspace,
            ordered_analysis_stage_ids=ordered_analysis_stage_ids,
            pipeline_version=pipeline_version,
        )
        if candidate is None:
            raise FromPhasePreparationError(
                "No compatible committed stage snapshots were found in the workspace "
                "source for execution.from_phase."
            )
        if len(candidate.reusable_prefix_stage_ids) < len(required_prefix_stage_ids):
            available_prefix = (
                ", ".join(candidate.reusable_prefix_stage_ids)
                if len(candidate.reusable_prefix_stage_ids) > 0
                else "<none>"
            )
            required_prefix = ", ".join(required_prefix_stage_ids)
            raise FromPhasePreparationError(
                "Committed workspace snapshots do not satisfy execution.from_phase prerequisites: "
                f"required_prefix=[{required_prefix}], available_prefix=[{available_prefix}]"
            )
        copied_stage_ids = required_prefix_stage_ids

    if len(copied_stage_ids) == 0:
        return None
    if candidate is None:
        raise FromPhasePreparationError(
            "Internal error while preparing execution.from_phase snapshots."
        )

    prepared_job_dir = task_dir / _TASK_JOBS_DIRNAME / normalized_job_id
    _materialize_reused_stage_snapshots(
        source_job_dir=candidate.source_job_dir,
        destination_job_dir=prepared_job_dir,
        copied_stage_ids=copied_stage_ids,
        task_id=normalized_task_id,
        job_id=normalized_job_id,
        config_hash=config_hash,
        config_relative_path=config_relative_path,
        pipeline_version=pipeline_version,
    )

    checkpoint = RuntimeStateCheckpoint.new(pipeline_version=pipeline_version)
    for stage_id in copied_stage_ids:
        stage_root = prepared_job_dir / "stages" / stage_id
        stage_manifest = load_stage_manifest(path=stage_root / STAGE_MANIFEST_FILENAME)
        checkpoint = checkpoint.with_committed_stage(
            stage_id=stage_id,
            artifacts=stage_manifest.artifacts,
        )

    return PreparedJobSeed(
        requested_job_id=normalized_job_id,
        runtime_state=checkpoint.to_runtime_state(),
        prepared_job_dir=prepared_job_dir,
        target=selection.target,
        requested_from_phase=requested_from_phase,
        resolved_start_stage=resolved_start_stage,
        copied_stage_ids=copied_stage_ids,
    )


def cleanup_prepared_job_seed(*, prepared_seed: PreparedJobSeed) -> None:
    prepared_root = prepared_seed.prepared_job_dir
    if not prepared_root.exists():
        return
    shutil.rmtree(prepared_root)


def _required_prefix_stage_ids(
    *,
    ordered_analysis_stage_ids: tuple[str, ...],
    start_stage_id: str,
) -> tuple[str, ...]:
    try:
        start_index = ordered_analysis_stage_ids.index(start_stage_id)
    except ValueError as error:
        raise FromPhasePreparationError(
            f"Start stage '{start_stage_id}' is unavailable in the selected analysis pipeline."
        ) from error
    return ordered_analysis_stage_ids[:start_index]


def _load_resolved_analysis_config(
    *,
    config_revision_path: Path,
    expected_config_hash: str,
) -> ResolvedAnalysisConfig:
    payload = _load_json_object(config_revision_path)
    observed_hash = compute_config_hash(payload)
    if observed_hash != expected_config_hash:
        raise FromPhasePreparationError(
            "Immutable task config hash does not match the registry record while preparing "
            "execution.from_phase."
        )
    try:
        config_input = AnalysisConfigInput.model_validate(_resolved_config_as_strict_input(payload))
        return resolve_analysis_config(config_input).config
    except Exception as error:
        raise FromPhasePreparationError(
            "Immutable task config is invalid while preparing execution.from_phase."
        ) from error


def _resolve_workspace_source_directory(*, config: ResolvedAnalysisConfig) -> Path | None:
    if len(config.samples) != 1:
        return None
    sample = config.samples[0]
    if sample is None:
        return None
    classification = classify_input_source(sample)
    if classification.kind is not InputSourceKind.LOCAL_PATH or classification.local_path is None:
        return None
    local_path = classification.local_path
    if not local_path.is_dir() or local_path.is_symlink():
        return None
    workspace = local_path.resolve(strict=False)
    config_path = workspace / _TASK_CONFIG_FILENAME
    configs_dir = workspace / _TASK_CONFIGS_DIRNAME
    jobs_dir = workspace / _TASK_JOBS_DIRNAME
    if not config_path.is_file() or config_path.is_symlink():
        return None
    if not configs_dir.is_dir() or configs_dir.is_symlink():
        return None
    if not jobs_dir.is_dir() or jobs_dir.is_symlink():
        return None
    return workspace


def _select_source_workspace_candidate(
    *,
    source_workspace: Path,
    ordered_analysis_stage_ids: tuple[str, ...],
    pipeline_version: str,
) -> _SourceWorkspaceCandidate | None:
    jobs_dir = source_workspace / _TASK_JOBS_DIRNAME
    candidates: list[_SourceWorkspaceCandidate] = []
    for source_job_dir in sorted(jobs_dir.iterdir(), key=lambda path: path.name):
        if not source_job_dir.is_dir() or source_job_dir.is_symlink():
            continue
        source_job_id = source_job_dir.name.strip()
        if source_job_id == "":
            continue
        reusable_prefix_stage_ids, last_completed_at = _validated_reusable_prefix(
            source_job_dir=source_job_dir,
            source_job_id=source_job_id,
            ordered_analysis_stage_ids=ordered_analysis_stage_ids,
            pipeline_version=pipeline_version,
        )
        if len(reusable_prefix_stage_ids) == 0:
            continue
        candidates.append(
            _SourceWorkspaceCandidate(
                source_job_dir=source_job_dir,
                source_job_id=source_job_id,
                reusable_prefix_stage_ids=reusable_prefix_stage_ids,
                last_completed_at=last_completed_at,
            )
        )
    if len(candidates) == 0:
        return None
    return max(
        candidates,
        key=lambda item: (
            len(item.reusable_prefix_stage_ids),
            item.last_completed_at,
            item.source_job_id,
        ),
    )


def _validated_reusable_prefix(
    *,
    source_job_dir: Path,
    source_job_id: str,
    ordered_analysis_stage_ids: tuple[str, ...],
    pipeline_version: str,
) -> tuple[tuple[str, ...], str]:
    reusable_stage_ids: list[str] = []
    last_completed_at = ""
    for stage_id in ordered_analysis_stage_ids:
        try:
            snapshot = validate_committed_stage_snapshot(
                job_dir=source_job_dir,
                stage_id=stage_id,
                expected_job_id=source_job_id,
                expected_pipeline_version=pipeline_version,
                expected_task_id=None,
                expected_config_hash=None,
            )
        except StageCommitError:
            break
        reusable_stage_ids.append(stage_id)
        last_completed_at = snapshot.manifest.completed_at
    return tuple(reusable_stage_ids), last_completed_at


def _materialize_reused_stage_snapshots(
    *,
    source_job_dir: Path,
    destination_job_dir: Path,
    copied_stage_ids: tuple[str, ...],
    task_id: str,
    job_id: str,
    config_hash: str,
    config_relative_path: str,
    pipeline_version: str,
) -> None:
    if destination_job_dir.exists():
        raise FromPhasePreparationError(
            f"Cannot prepare execution.from_phase: destination job directory already exists: "
            f"'{destination_job_dir}'."
        )
    destination_job_dir.parent.mkdir(parents=True, exist_ok=True)

    upstream_manifest_hashes: dict[str, str] = {}
    for stage_id in copied_stage_ids:
        source_stage_root = source_job_dir / "stages" / stage_id
        destination_stage_root = destination_job_dir / "stages" / stage_id
        _copy_stage_snapshot(
            source_stage_root=source_stage_root,
            destination_stage_root=destination_stage_root,
            stage_id=stage_id,
        )
        _rewrite_generic_stage_manifest(
            stage_root=destination_stage_root,
            stage_id=stage_id,
            job_id=job_id,
        )
        _rewrite_stage_domain_manifest(
            stage_root=destination_stage_root,
            stage_id=stage_id,
            task_id=task_id,
            job_id=job_id,
            config_hash=config_hash,
            config_relative_path=config_relative_path,
            pipeline_version=pipeline_version,
            upstream_manifest_hashes=upstream_manifest_hashes,
        )
        domain_manifest_relative_path = _DOMAIN_MANIFEST_PATH_BY_STAGE[stage_id]
        upstream_manifest_hashes[stage_id] = _sha256_file(
            destination_stage_root / domain_manifest_relative_path
        )
        try:
            validate_committed_stage_snapshot(
                job_dir=destination_job_dir,
                stage_id=stage_id,
                expected_job_id=job_id,
                expected_pipeline_version=pipeline_version,
                expected_task_id=task_id,
                expected_config_hash=config_hash,
            )
        except StageCommitError as error:
            raise FromPhasePreparationError(
                "Prepared stage snapshot is invalid for execution.from_phase: "
                f"stage='{stage_id}', code='{error.code}'."
            ) from error


def _copy_stage_snapshot(
    *,
    source_stage_root: Path,
    destination_stage_root: Path,
    stage_id: str,
) -> None:
    if not source_stage_root.is_dir() or source_stage_root.is_symlink():
        raise FromPhasePreparationError(
            f"Workspace snapshot for stage '{stage_id}' is missing or invalid."
        )
    _reject_symlink_entries(source_stage_root)
    destination_stage_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_stage_root, destination_stage_root)


def _reject_symlink_entries(directory: Path) -> None:
    if directory.is_symlink():
        raise FromPhasePreparationError(f"Workspace snapshot path is a symlink: '{directory}'.")
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise FromPhasePreparationError(
                f"Workspace snapshot contains a symbolic link: '{path}'."
            )


def _rewrite_generic_stage_manifest(
    *,
    stage_root: Path,
    stage_id: str,
    job_id: str,
) -> None:
    manifest_path = stage_root / STAGE_MANIFEST_FILENAME
    manifest = load_stage_manifest(path=manifest_path)
    if manifest.stage_id != stage_id:
        raise FromPhasePreparationError(
            f"Workspace snapshot generic stage identity mismatch: expected '{stage_id}', "
            f"got '{manifest.stage_id}'."
        )
    rewritten = manifest.model_copy(update={"job_id": job_id})
    write_stage_manifest(directory=stage_root, manifest=rewritten)


def _rewrite_stage_domain_manifest(
    *,
    stage_root: Path,
    stage_id: str,
    task_id: str,
    job_id: str,
    config_hash: str,
    config_relative_path: str,
    pipeline_version: str,
    upstream_manifest_hashes: dict[str, str],
) -> None:
    if stage_id == "initialize_job":
        _rewrite_initialize_job_manifest(
            stage_root=stage_root,
            task_id=task_id,
            job_id=job_id,
            config_hash=config_hash,
            config_relative_path=config_relative_path,
            pipeline_version=pipeline_version,
        )
        return
    if stage_id == "input_acquisition":
        _rewrite_input_acquisition_manifest(
            stage_root=stage_root,
            task_id=task_id,
            job_id=job_id,
            config_hash=config_hash,
            config_relative_path=config_relative_path,
        )
        return
    if stage_id == "input_processing":
        path = stage_root / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
        manifest = _load_typed_json_model(path=path, model_type=InputProcessingManifest)
        rewritten = manifest.model_copy(
            update={
                "task_id": task_id,
                "job_id": job_id,
                "config_hash": config_hash,
                "config_revision_path": config_relative_path,
            }
        )
        _write_json_model(path=path, model=rewritten)
        return
    if stage_id == "alignment":
        path = stage_root / ALIGNMENT_MANIFEST_RELATIVE_PATH
        manifest = _load_typed_json_model(path=path, model_type=AlignmentManifest)
        rewritten = manifest.model_copy(
            update={
                "task_id": task_id,
                "job_id": job_id,
                "config_hash": config_hash,
            }
        )
        _write_json_model(path=path, model=rewritten)
        return
    if stage_id == "comparative_analysis":
        path = stage_root / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH
        manifest = _load_typed_json_model(path=path, model_type=ComparativeAnalysisManifest)
        rewritten = manifest.model_copy(
            update={
                "task_id": task_id,
                "job_id": job_id,
                "config_hash": config_hash,
            }
        )
        _write_json_model(path=path, model=rewritten)
        return
    if stage_id == "distance_matrix":
        path = stage_root / DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH
        manifest = _load_typed_json_model(path=path, model_type=DistanceMatrixManifest)
        rewritten = manifest.model_copy(
            update={
                "task_id": task_id,
                "job_id": job_id,
                "config_hash": config_hash,
            }
        )
        _write_json_model(path=path, model=rewritten)
        return
    if stage_id == "phylogenetic_tree":
        _rewrite_phylogenetic_tree_snapshot(
            stage_root=stage_root,
            task_id=task_id,
            job_id=job_id,
            config_hash=config_hash,
            upstream_manifest_hashes=upstream_manifest_hashes,
        )
        return
    if stage_id == "clade_detection":
        _rewrite_clade_detection_snapshot(
            stage_root=stage_root,
            task_id=task_id,
            job_id=job_id,
            config_hash=config_hash,
            upstream_manifest_hashes=upstream_manifest_hashes,
        )
        return
    raise FromPhasePreparationError(
        f"Unsupported stage '{stage_id}' for execution.from_phase snapshot reuse."
    )


def _rewrite_initialize_job_manifest(
    *,
    stage_root: Path,
    task_id: str,
    job_id: str,
    config_hash: str,
    config_relative_path: str,
    pipeline_version: str,
) -> None:
    path = stage_root / "execution_manifest.json"
    payload = _load_json_object(path)
    payload["task_id"] = task_id
    payload["job_id"] = job_id
    payload["config_hash"] = config_hash
    payload["config_revision_path"] = config_relative_path
    payload["pipeline_version"] = pipeline_version
    _write_json_object(path=path, payload=payload)


def _rewrite_input_acquisition_manifest(
    *,
    stage_root: Path,
    task_id: str,
    job_id: str,
    config_hash: str,
    config_relative_path: str,
) -> None:
    path = stage_root / INPUT_MANIFEST_RELATIVE_PATH
    payload = _load_json_object(path)
    payload["task_id"] = task_id
    payload["job_id"] = job_id
    payload["config_hash"] = config_hash
    payload["config_revision_path"] = config_relative_path
    _write_json_object(path=path, payload=payload)


def _rewrite_phylogenetic_tree_snapshot(
    *,
    stage_root: Path,
    task_id: str,
    job_id: str,
    config_hash: str,
    upstream_manifest_hashes: dict[str, str],
) -> None:
    path = stage_root / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH
    manifest = _load_typed_json_model(path=path, model_type=PhylogeneticTreeManifest)
    updates: dict[str, object] = {
        "task_id": task_id,
        "job_id": job_id,
        "config_hash": config_hash,
    }
    if manifest.enabled:
        distance_digest = upstream_manifest_hashes.get("distance_matrix")
        if distance_digest is None:
            raise FromPhasePreparationError(
                "Phylogenetic-tree snapshot reuse requires a rewritten distance-matrix manifest."
            )
        result_path = stage_root / TREE_JSON_RELATIVE_PATH
        result = _load_typed_json_model(path=result_path, model_type=PhylogeneticTreeResult)
        rewritten_result = result.model_copy(
            update={"input_snapshot_manifest_sha256": distance_digest}
        )
        _write_json_model(path=result_path, model=rewritten_result)
        updates["input_snapshot_manifest_sha256"] = distance_digest
        updates["artifacts"] = tuple(
            phylogenetic_tree_artifact_metadata(
                stage_root / metadata.relative_path,
                record_count=metadata.record_count,
                relative_path=metadata.relative_path,
            )
            for metadata in manifest.artifacts
        )
    rewritten_manifest = manifest.model_copy(update=updates)
    _write_json_model(path=path, model=rewritten_manifest)


def _rewrite_clade_detection_snapshot(
    *,
    stage_root: Path,
    task_id: str,
    job_id: str,
    config_hash: str,
    upstream_manifest_hashes: dict[str, str],
) -> None:
    path = stage_root / CLADE_DETECTION_MANIFEST_RELATIVE_PATH
    manifest = _load_typed_json_model(path=path, model_type=CladeDetectionManifest)
    updates: dict[str, object] = {
        "task_id": task_id,
        "job_id": job_id,
        "config_hash": config_hash,
    }
    if manifest.enabled:
        tree_digest = upstream_manifest_hashes.get("phylogenetic_tree")
        matrix_digest = upstream_manifest_hashes.get("distance_matrix")
        if tree_digest is None or matrix_digest is None:
            raise FromPhasePreparationError(
                "Clade-detection snapshot reuse requires rewritten phylogenetic-tree and "
                "distance-matrix manifests."
            )
        result_path = stage_root / INFERRED_CLADES_JSON_RELATIVE_PATH
        result = _load_typed_json_model(path=result_path, model_type=InferredCladesResult)
        rewritten_result = result.model_copy(
            update={
                "tree_snapshot_manifest_sha256": tree_digest,
                "matrix_snapshot_manifest_sha256": matrix_digest,
            }
        )
        _write_json_model(path=result_path, model=rewritten_result)
        updates["tree_snapshot_manifest_sha256"] = tree_digest
        updates["matrix_snapshot_manifest_sha256"] = matrix_digest
        updates["artifacts"] = tuple(
            clade_detection_artifact_metadata(
                stage_root / metadata.relative_path,
                record_count=metadata.record_count,
                relative_path=metadata.relative_path,
            )
            for metadata in manifest.artifacts
        )
    rewritten_manifest = manifest.model_copy(update=updates)
    _write_json_model(path=path, model=rewritten_manifest)


def _load_typed_json_model(*, path: Path, model_type: type[Any]) -> Any:
    try:
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise FromPhasePreparationError(
            f"Invalid stage manifest while preparing execution.from_phase: '{path}'."
        ) from error


def _write_json_model(*, path: Path, model: Any) -> None:
    payload = model.model_dump(mode="json")
    type(model).model_validate(payload)
    write_text_atomically(
        path=path,
        payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise FromPhasePreparationError(f"Invalid JSON payload: '{path}'.") from error
    if not isinstance(payload, dict):
        raise FromPhasePreparationError(f"Expected JSON object: '{path}'.")
    return {str(key): value for key, value in payload.items()}


def _write_json_object(*, path: Path, payload: dict[str, object]) -> None:
    write_text_atomically(
        path=path,
        payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "FromPhasePreparationError",
    "PreparedJobSeed",
    "cleanup_prepared_job_seed",
    "prepare_from_phase_seed_for_new_job",
]
