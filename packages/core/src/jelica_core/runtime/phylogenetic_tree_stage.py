from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from jelica_core.config import ResolvedAnalysisConfig
from jelica_core.distance_matrix import (
    DISTANCE_MATRIX_JSON_RELATIVE_PATH,
    DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
    DISTANCE_MATRIX_STAGE_ID,
    DistanceMatrixManifest,
    DistanceMatrixResult,
    DistanceMatrixStatus,
)
from jelica_core.phylogenetic_tree import (
    PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
    PHYLOGENETIC_TREE_STAGE_ID,
    TREE_DIAGNOSTICS_RELATIVE_PATH,
    TREE_JSON_RELATIVE_PATH,
    TREE_ROOTED_NWK_RELATIVE_PATH,
    TREE_UNROOTED_NWK_RELATIVE_PATH,
    PhylogeneticTreeComputationError,
    PhylogeneticTreeConstructionMode,
    PhylogeneticTreeManifest,
    PhylogeneticTreeStatus,
    artifact_metadata,
    build_phylogenetic_tree,
    phylogenetic_tree_artifact_paths,
)
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .artifacts import (
    STAGE_MANIFEST_FILENAME,
    StageCommitError,
    StageSnapshotErrorCode,
    validate_committed_stage_snapshot,
)
from .pipeline import ProgressReporter, StageContext, StageRunResult

PHYLOGENETIC_TREE_STARTED_EVENT: Final = "PHYLOGENETIC_TREE_STARTED"
PHYLOGENETIC_TREE_SKIPPED_EVENT: Final = "PHYLOGENETIC_TREE_SKIPPED"
PHYLOGENETIC_TREE_PROGRESS_EVENT: Final = "PHYLOGENETIC_TREE_PROGRESS"
PHYLOGENETIC_TREE_RESULT_PUBLISHED_EVENT: Final = "PHYLOGENETIC_TREE_RESULT_PUBLISHED"
PHYLOGENETIC_TREE_COMPLETED_EVENT: Final = "PHYLOGENETIC_TREE_COMPLETED"
PHYLOGENETIC_TREE_FAILED_EVENT: Final = "PHYLOGENETIC_TREE_FAILED"

_INTERNAL_TASK_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_directory_max_depth", "ncbi_max_retries"}
)
_EXPECTED_SOURCE_ARTIFACTS: Final[tuple[str, str]] = (
    f"stages/{DISTANCE_MATRIX_STAGE_ID}/{DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH}",
    f"stages/{DISTANCE_MATRIX_STAGE_ID}/{DISTANCE_MATRIX_JSON_RELATIVE_PATH}",
)
_DISTANCE_MATRIX_COMMIT_WAIT_TIMEOUT_SECONDS: Final[float] = 5.0
_DISTANCE_MATRIX_COMMIT_WAIT_POLL_SECONDS: Final[float] = 0.05


class PhylogeneticTreeStageError(RuntimeError):
    """A bounded, sequence-safe fatal stage error."""

    def __init__(
        self,
        *,
        reason: str,
        detail: str,
        context: dict[str, object] | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.event_name = PHYLOGENETIC_TREE_FAILED_EVENT
        self.context = context or {}
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _DistanceMatrixInput:
    snapshot_manifest_sha256: str
    source_artifacts: tuple[str, ...]
    manifest: DistanceMatrixManifest
    result: DistanceMatrixResult


@dataclass(frozen=True, slots=True)
class PhylogeneticTreeStage:
    stage_id: str = PHYLOGENETIC_TREE_STAGE_ID
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)
        (context.stage_staging_directory / "phylogenetic_tree").mkdir(
            parents=True,
            exist_ok=True,
        )

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        started_at_value = utc_now()
        started_monotonic = time.monotonic()
        context.check_control()
        config = _load_resolved_config(context.launch_spec.config_revision_path)
        tree_config = config.phylogenetic_tree

        context.emit_event(
            PHYLOGENETIC_TREE_STARTED_EVENT,
            {
                "method": tree_config.method.value,
                "rooting": tree_config.rooting.value,
                "detail": "Phylogenetic-tree stage started.",
            },
        )
        if not tree_config.enabled:
            return self._run_disabled(
                context=context,
                progress_reporter=progress_reporter,
                config=config,
                started_at=started_at_value,
                started_monotonic=started_monotonic,
            )

        _update_progress_description(
            progress_reporter,
            description="Phylogenetic tree: validating published distance-matrix snapshot.",
        )
        progress_reporter(0.1)
        distance_input = _load_distance_matrix_input(context=context)
        context.check_control()
        context.emit_event(
            PHYLOGENETIC_TREE_PROGRESS_EVENT,
            {
                "leaf_count": distance_input.result.unique_sequence_count,
                "detail": "Phylogenetic tree: building canonical tree representations.",
            },
        )
        _update_progress_description(
            progress_reporter,
            description="Phylogenetic tree: constructing canonical tree representations.",
        )
        progress_reporter(0.4)
        try:
            computation = build_phylogenetic_tree(
                distance_matrix_result=distance_input.result,
                method=tree_config.method,
                rooting=tree_config.rooting,
                input_snapshot_manifest_sha256=distance_input.snapshot_manifest_sha256,
            )
        except PhylogeneticTreeComputationError as error:
            raise PhylogeneticTreeStageError(
                reason=error.reason,
                detail=(
                    "Phylogenetic tree could not be constructed from the published "
                    "distance matrix."
                ),
            ) from error

        context.check_control()
        progress_reporter(0.7)
        _update_progress_description(
            progress_reporter,
            description="Phylogenetic tree: publishing stage artifacts.",
        )
        root = context.stage_staging_directory
        _write_newick_artifact(
            path=root / TREE_UNROOTED_NWK_RELATIVE_PATH,
            newick=computation.unrooted_newick,
        )
        unrooted_metadata = artifact_metadata(
            root / TREE_UNROOTED_NWK_RELATIVE_PATH,
            relative_path=TREE_UNROOTED_NWK_RELATIVE_PATH,
        )
        _write_newick_artifact(
            path=root / TREE_ROOTED_NWK_RELATIVE_PATH,
            newick=computation.rooted_newick,
        )
        rooted_metadata = artifact_metadata(
            root / TREE_ROOTED_NWK_RELATIVE_PATH,
            relative_path=TREE_ROOTED_NWK_RELATIVE_PATH,
        )
        tree_metadata = _write_json_model(
            path=root / TREE_JSON_RELATIVE_PATH,
            model=computation.result,
            relative_path=TREE_JSON_RELATIVE_PATH,
        )
        diagnostics_metadata = _write_json_model(
            path=root / TREE_DIAGNOSTICS_RELATIVE_PATH,
            model=computation.diagnostics,
            relative_path=TREE_DIAGNOSTICS_RELATIVE_PATH,
        )

        completed_at_value = utc_now()
        manifest = PhylogeneticTreeManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=True,
            normalized_settings=tree_config,
            status=PhylogeneticTreeStatus.COMPLETED,
            method=tree_config.method,
            requested_rooting=tree_config.rooting,
            applied_rooting=computation.result.applied_rooting,
            construction_mode=computation.result.construction_mode,
            inference_performed=computation.result.inference_performed,
            input_distance_model=computation.result.input_distance_model,
            input_snapshot_manifest_sha256=distance_input.snapshot_manifest_sha256,
            leaf_count=computation.diagnostics.leaf_count,
            internal_node_count=computation.diagnostics.internal_node_count,
            edge_count=computation.diagnostics.edge_count,
            has_negative_branches=computation.result.raw_negative_branch_count > 0,
            raw_negative_branch_count=computation.result.raw_negative_branch_count,
            zero_diameter=computation.result.zero_diameter,
            started_at=serialize_utc_datetime(started_at_value),
            completed_at=serialize_utc_datetime(completed_at_value),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            source_artifacts=distance_input.source_artifacts,
            artifacts=(
                unrooted_metadata,
                rooted_metadata,
                tree_metadata,
                diagnostics_metadata,
            ),
        )
        _write_json_model(
            path=root / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
            model=manifest,
            relative_path=PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
        )
        _validate_published_snapshot(root=root, manifest=manifest)
        context.emit_event(
            PHYLOGENETIC_TREE_PROGRESS_EVENT,
            {
                "phase": "ready_to_commit",
                "status": manifest.status.value,
                "leaf_count": manifest.leaf_count,
                "internal_node_count": manifest.internal_node_count,
                "edge_count": manifest.edge_count,
                "construction_mode": manifest.construction_mode.value,
                "applied_rooting": manifest.applied_rooting,
                "detail": (
                    "Phylogenetic tree: staged artifacts validated and ready for commit."
                ),
            },
        )
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=phylogenetic_tree_artifact_paths(manifest),
            check_control_before_commit=True,
        )

    def _run_disabled(
        self,
        *,
        context: StageContext,
        progress_reporter: ProgressReporter,
        config: ResolvedAnalysisConfig,
        started_at: datetime,
        started_monotonic: float,
    ) -> StageRunResult:
        context.emit_event(
            PHYLOGENETIC_TREE_SKIPPED_EVENT,
            {
                "reason": "phylogenetic_tree_disabled",
                "detail": "Phylogenetic tree was skipped because it is disabled.",
            },
        )
        progress_reporter(0.5)
        completed_at = utc_now()
        manifest = PhylogeneticTreeManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=False,
            normalized_settings=config.phylogenetic_tree,
            skipped_reason="phylogenetic_tree_disabled",
            status=PhylogeneticTreeStatus.COMPLETED,
            method=config.phylogenetic_tree.method,
            requested_rooting=config.phylogenetic_tree.rooting,
            applied_rooting=config.phylogenetic_tree.rooting.value,
            construction_mode=PhylogeneticTreeConstructionMode.TRIVIAL_SINGLETON,
            inference_performed=False,
            input_snapshot_manifest_sha256=None,
            leaf_count=0,
            internal_node_count=0,
            edge_count=0,
            has_negative_branches=False,
            raw_negative_branch_count=0,
            zero_diameter=False,
            started_at=serialize_utc_datetime(started_at),
            completed_at=serialize_utc_datetime(completed_at),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
        )
        root = context.stage_staging_directory
        _write_json_model(
            path=root / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
            model=manifest,
            relative_path=PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
        )
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=(PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,),
            check_control_before_commit=True,
        )


def _load_resolved_config(path: Path) -> ResolvedAnalysisConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PhylogeneticTreeStageError(
            reason="phylogenetic_tree_config_unreadable",
            detail="Immutable analysis configuration could not be read.",
        ) from error
    if not isinstance(payload, dict):
        raise PhylogeneticTreeStageError(
            reason="phylogenetic_tree_config_invalid",
            detail="Immutable analysis configuration must be a JSON object.",
        )
    filtered = {
        str(key): value
        for key, value in payload.items()
        if str(key) not in _INTERNAL_TASK_CONFIG_FIELDS
    }
    try:
        return ResolvedAnalysisConfig.model_validate(filtered)
    except Exception as error:
        raise PhylogeneticTreeStageError(
            reason="phylogenetic_tree_config_invalid",
            detail="Immutable analysis configuration is invalid for phylogenetic tree.",
        ) from error


def _load_distance_matrix_input(*, context: StageContext) -> _DistanceMatrixInput:
    committed_root = context.launch_spec.job_dir / "stages" / DISTANCE_MATRIX_STAGE_ID
    staged_manifest_path = (
        context.launch_spec.job_dir
        / "staging"
        / DISTANCE_MATRIX_STAGE_ID
        / context.launch_spec.worker_instance_id
        / STAGE_MANIFEST_FILENAME
    )
    wait_deadline = time.monotonic() + _DISTANCE_MATRIX_COMMIT_WAIT_TIMEOUT_SECONDS
    while True:
        try:
            snapshot = validate_committed_stage_snapshot(
                job_dir=context.launch_spec.job_dir,
                stage_id=DISTANCE_MATRIX_STAGE_ID,
                expected_job_id=context.launch_spec.job_id,
                expected_pipeline_version=context.launch_spec.pipeline_version,
                expected_task_id=context.launch_spec.task_id,
                expected_config_hash=context.launch_spec.config_hash,
            )
            break
        except StageCommitError as error:
            pending_commit = (
                error.code == StageSnapshotErrorCode.INVALID.value
                and not committed_root.exists()
                and staged_manifest_path.is_file()
            )
            if not pending_commit or time.monotonic() >= wait_deadline:
                raise PhylogeneticTreeStageError(
                    reason="distance_matrix_snapshot_invalid",
                    detail="Published distance-matrix snapshot is missing or invalid.",
                ) from error
            context.check_control()
            time.sleep(_DISTANCE_MATRIX_COMMIT_WAIT_POLL_SECONDS)
    if snapshot.domain_manifest_sha256 is None:
        raise PhylogeneticTreeStageError(
            reason="distance_matrix_snapshot_invalid",
            detail="Published distance-matrix snapshot digest is missing.",
        )
    stage_root = context.launch_spec.job_dir / "stages" / DISTANCE_MATRIX_STAGE_ID
    manifest_path = stage_root / DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH
    result_path = stage_root / DISTANCE_MATRIX_JSON_RELATIVE_PATH
    try:
        manifest = DistanceMatrixManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        result = DistanceMatrixResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except Exception as error:
        raise PhylogeneticTreeStageError(
            reason="distance_matrix_snapshot_invalid",
            detail="Published distance-matrix artifacts are invalid.",
        ) from error

    if not manifest.enabled:
        raise PhylogeneticTreeStageError(
            reason="distance_matrix_disabled",
            detail="Phylogenetic tree requires an enabled distance-matrix stage result.",
        )
    if (
        manifest.status is not DistanceMatrixStatus.COMPLETED
        or manifest.undefined_distance_count != 0
        or result.undefined_distance_count != 0
    ):
        raise PhylogeneticTreeStageError(
            reason="distance_matrix_incomplete",
            detail="Phylogenetic tree requires a complete distance matrix without null values.",
        )
    if manifest.model is not result.model:
        raise PhylogeneticTreeStageError(
            reason="distance_matrix_model_mismatch",
            detail="Distance-matrix model identity is inconsistent across published artifacts.",
        )
    if (
        manifest.unique_sequence_count != result.unique_sequence_count
        or manifest.expected_pair_count != result.expected_pair_count
        or manifest.defined_distance_count != result.defined_distance_count
    ):
        raise PhylogeneticTreeStageError(
            reason="distance_matrix_count_mismatch",
            detail="Distance-matrix counters are inconsistent across published artifacts.",
        )
    stage_artifact_set = set(snapshot.manifest.artifacts)
    for source_reference in _EXPECTED_SOURCE_ARTIFACTS:
        relative_path = source_reference.removeprefix(f"stages/{DISTANCE_MATRIX_STAGE_ID}/")
        if relative_path not in stage_artifact_set:
            raise PhylogeneticTreeStageError(
                reason="distance_matrix_snapshot_invalid",
                detail="Distance-matrix snapshot is missing required canonical artifacts.",
            )
    return _DistanceMatrixInput(
        snapshot_manifest_sha256=snapshot.domain_manifest_sha256,
        source_artifacts=_EXPECTED_SOURCE_ARTIFACTS,
        manifest=manifest,
        result=result,
    )


def _write_json_model(
    *,
    path: Path,
    model: BaseModel,
    relative_path: str,
) -> object:
    payload = model.model_dump(mode="json")
    type(model).model_validate(payload)
    write_text_atomically(
        path=path,
        payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return artifact_metadata(path, relative_path=relative_path)


def _write_newick_artifact(*, path: Path, newick: str) -> None:
    write_text_atomically(path=path, payload=f"{newick}\n")


def _sha256_file(path: Path) -> str:
    hash_value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hash_value.update(chunk)
    return hash_value.hexdigest()


def _validate_published_snapshot(*, root: Path, manifest: PhylogeneticTreeManifest) -> None:
    for metadata in manifest.artifacts:
        path = root / metadata.relative_path
        if not path.is_file():
            raise PhylogeneticTreeStageError(
                reason="phylogenetic_tree_artifact_missing",
                detail="A phylogenetic-tree artifact is missing before publication.",
                context={"relative_path": metadata.relative_path},
            )
        if path.stat().st_size != metadata.size_bytes or _sha256_file(path) != metadata.sha256:
            raise PhylogeneticTreeStageError(
                reason="phylogenetic_tree_artifact_integrity_failed",
                detail="A phylogenetic-tree artifact failed integrity validation.",
                context={"relative_path": metadata.relative_path},
            )


def _update_progress_description(
    progress_reporter: ProgressReporter,
    *,
    description: str,
) -> None:
    update = getattr(progress_reporter, "update", None)
    if callable(update):
        update(description=description)


__all__ = [
    "PHYLOGENETIC_TREE_COMPLETED_EVENT",
    "PHYLOGENETIC_TREE_FAILED_EVENT",
    "PHYLOGENETIC_TREE_PROGRESS_EVENT",
    "PHYLOGENETIC_TREE_RESULT_PUBLISHED_EVENT",
    "PHYLOGENETIC_TREE_SKIPPED_EVENT",
    "PHYLOGENETIC_TREE_STARTED_EVENT",
    "PhylogeneticTreeStage",
    "PhylogeneticTreeStageError",
]
