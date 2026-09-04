from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from jelica_core.clade_detection import (
    CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
    CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
    CLADE_DETECTION_STAGE_ID,
    CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
    INFERRED_CLADES_JSON_RELATIVE_PATH,
    CladeDetectionComputationError,
    CladeDetectionManifest,
    CladeDetectionStatus,
    artifact_metadata,
    clade_detection_artifact_paths,
    detect_inferred_clades,
    parse_clade_assignments_tsv,
    serialize_clade_assignments_tsv,
    serialize_clade_memberships_jsonl,
    validate_published_inferred_clades,
)
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
    TREE_JSON_RELATIVE_PATH,
    PhylogeneticTreeManifest,
    PhylogeneticTreeResult,
    PhylogeneticTreeStatus,
)
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .artifacts import StageCommitError, validate_committed_stage_snapshot
from .pipeline import ProgressReporter, StageContext, StageRunResult

CLADE_DETECTION_STARTED_EVENT: Final = "CLADE_DETECTION_STARTED"
CLADE_DETECTION_SKIPPED_EVENT: Final = "CLADE_DETECTION_SKIPPED"
CLADE_DETECTION_PROGRESS_EVENT: Final = "CLADE_DETECTION_PROGRESS"
CLADE_DETECTION_RESULT_PUBLISHED_EVENT: Final = "CLADE_DETECTION_RESULT_PUBLISHED"
CLADE_DETECTION_COMPLETED_EVENT: Final = "CLADE_DETECTION_COMPLETED"
CLADE_DETECTION_FAILED_EVENT: Final = "CLADE_DETECTION_FAILED"

_INTERNAL_TASK_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_directory_max_depth", "ncbi_max_retries"}
)
_EXPECTED_TREE_SOURCE_ARTIFACTS: Final[tuple[str, str]] = (
    f"stages/{PHYLOGENETIC_TREE_STAGE_ID}/{PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH}",
    f"stages/{PHYLOGENETIC_TREE_STAGE_ID}/{TREE_JSON_RELATIVE_PATH}",
)
_EXPECTED_DISTANCE_SOURCE_ARTIFACTS: Final[tuple[str, str]] = (
    f"stages/{DISTANCE_MATRIX_STAGE_ID}/{DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH}",
    f"stages/{DISTANCE_MATRIX_STAGE_ID}/{DISTANCE_MATRIX_JSON_RELATIVE_PATH}",
)
_EXPECTED_SOURCE_ARTIFACTS: Final[tuple[str, ...]] = (
    *_EXPECTED_TREE_SOURCE_ARTIFACTS,
    *_EXPECTED_DISTANCE_SOURCE_ARTIFACTS,
)
_SAFE_REASON_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789_"
)


class CladeDetectionStageError(RuntimeError):
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
        self.event_name = CLADE_DETECTION_FAILED_EVENT
        self.context = context or {}
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _UpstreamInputs:
    tree_snapshot_manifest_sha256: str
    matrix_snapshot_manifest_sha256: str
    source_artifacts: tuple[str, ...]
    tree_manifest: PhylogeneticTreeManifest
    tree_result: PhylogeneticTreeResult
    matrix_manifest: DistanceMatrixManifest
    matrix_result: DistanceMatrixResult


@dataclass(frozen=True, slots=True)
class CladeDetectionStage:
    stage_id: str = CLADE_DETECTION_STAGE_ID
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)
        (context.stage_staging_directory / "clade_detection").mkdir(
            parents=True,
            exist_ok=True,
        )

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        started_at_value = utc_now()
        started_monotonic = time.monotonic()
        context.check_control()
        config = _load_resolved_config(context.launch_spec.config_revision_path)
        clade_config = config.clade_detection
        context.emit_event(
            CLADE_DETECTION_STARTED_EVENT,
            {
                "method": clade_config.method.value,
                "enabled": clade_config.enabled,
                "threshold": clade_config.max_within_clade_distance,
                "detail": "Clade-detection stage started.",
            },
        )
        if not clade_config.enabled:
            return self._run_disabled(
                context=context,
                progress_reporter=progress_reporter,
                config=config,
                started_at=started_at_value,
                started_monotonic=started_monotonic,
            )
        threshold = clade_config.max_within_clade_distance
        if threshold is None:
            raise CladeDetectionStageError(
                reason="clade_detection_threshold_missing",
                detail=(
                    "Clade detection requires max_within_clade_distance when the stage is "
                    "enabled."
                ),
            )

        _update_progress_description(
            progress_reporter,
            description="Clade detection: validating committed tree and distance snapshots.",
        )
        context.emit_event(
            CLADE_DETECTION_PROGRESS_EVENT,
            {
                "phase": "validate_inputs",
                "detail": "Clade detection: validating upstream committed snapshots.",
            },
        )
        progress_reporter(0.1)
        inputs = _load_upstream_inputs(context=context)
        context.check_control()

        _update_progress_description(
            progress_reporter,
            description="Clade detection: preparing rooted-tree traversal metadata.",
        )
        context.emit_event(
            CLADE_DETECTION_PROGRESS_EVENT,
            {
                "phase": "prepare_rooted_tree",
                "leaf_count": inputs.tree_manifest.leaf_count,
                "detail": "Clade detection: preparing rooted tree and matrix views.",
            },
        )
        progress_reporter(0.2)
        context.check_control()

        progress_state = {"processed_nodes": -1}

        def on_node_progress(
            processed_nodes: int,
            total_nodes: int,
            processed_pairs: int,
            total_pairs: int,
        ) -> None:
            milestone = max(1, math.ceil(max(total_nodes, 1) / 20))
            if (
                processed_nodes < total_nodes
                and processed_nodes - progress_state["processed_nodes"] < milestone
            ):
                return
            progress_state["processed_nodes"] = processed_nodes
            ratio = processed_nodes / max(total_nodes, 1)
            _update_progress_description(
                progress_reporter,
                description=(
                    "Clade detection: processed "
                    f"{processed_nodes}/{total_nodes} rooted nodes; "
                    f"{processed_pairs}/{total_pairs} cross-subtree leaf pairs."
                ),
            )
            progress_reporter(0.2 + (0.35 * ratio))
            context.emit_event(
                CLADE_DETECTION_PROGRESS_EVENT,
                {
                    "phase": "compute_node_metrics",
                    "processed_nodes": processed_nodes,
                    "total_nodes": total_nodes,
                    "processed_pairs": processed_pairs,
                    "total_pairs": total_pairs,
                    "detail": "Clade detection: computing node subtree distance metrics.",
                },
            )
            context.check_control()

        try:
            computation = detect_inferred_clades(
                phylogenetic_tree_result=inputs.tree_result,
                distance_matrix_result=inputs.matrix_result,
                method=clade_config.method,
                max_within_clade_distance=threshold,
                tree_snapshot_manifest_sha256=inputs.tree_snapshot_manifest_sha256,
                matrix_snapshot_manifest_sha256=inputs.matrix_snapshot_manifest_sha256,
                control_check=context.check_control,
                node_progress_callback=on_node_progress,
            )
        except CladeDetectionComputationError as error:
            raise _as_clade_stage_computation_error(
                error=error,
                phase="compute_node_metrics",
                detail=(
                    "Clade detection could not derive inferred clades from committed tree "
                    "and distance-matrix inputs."
                ),
            ) from error
        context.check_control()
        _update_progress_description(
            progress_reporter,
            description="Clade detection: selecting maximal inferred clades.",
        )
        context.emit_event(
            CLADE_DETECTION_PROGRESS_EVENT,
            {
                "phase": "select_clades",
                "clade_count": computation.result.clade_count,
                "detail": "Clade detection: selecting maximal eligible monophyletic subtrees.",
            },
        )
        progress_reporter(0.6)

        context.check_control()
        _update_progress_description(
            progress_reporter,
            description="Clade detection: validating partition and cross-artifact consistency.",
        )
        context.emit_event(
            CLADE_DETECTION_PROGRESS_EVENT,
            {
                "phase": "validate_partition",
                "clade_count": computation.result.clade_count,
                "leaf_count": computation.result.canonical_leaf_count,
                "detail": "Clade detection: validating clade partition invariants.",
            },
        )
        try:
            validate_published_inferred_clades(
                phylogenetic_tree_result=inputs.tree_result,
                distance_matrix_result=inputs.matrix_result,
                result=computation.result,
                membership_records=computation.membership_records,
                assignment_records=computation.assignment_records,
            )
        except CladeDetectionComputationError as error:
            raise _as_clade_stage_computation_error(
                error=error,
                phase="validate_partition",
                detail=(
                    "Clade detection produced an invalid inferred-clade partition before "
                    "publication."
                ),
            ) from error
        progress_reporter(0.75)
        context.check_control()

        _update_progress_description(
            progress_reporter,
            description="Clade detection: serializing stage artifacts.",
        )
        context.emit_event(
            CLADE_DETECTION_PROGRESS_EVENT,
            {
                "phase": "serialize_artifacts",
                "clade_count": computation.result.clade_count,
                "detail": "Clade detection: serializing inferred-clade artifacts.",
            },
        )
        root = context.stage_staging_directory
        result_metadata = _write_json_model(
            path=root / INFERRED_CLADES_JSON_RELATIVE_PATH,
            model=computation.result,
            relative_path=INFERRED_CLADES_JSON_RELATIVE_PATH,
        )
        memberships_payload = serialize_clade_memberships_jsonl(computation.membership_records)
        write_text_atomically(
            path=root / CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
            payload=memberships_payload,
        )
        memberships_metadata = artifact_metadata(
            root / CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
            relative_path=CLADE_MEMBERSHIPS_JSONL_RELATIVE_PATH,
            record_count=len(computation.membership_records),
        )
        assignments_payload = serialize_clade_assignments_tsv(computation.assignment_records)
        write_text_atomically(
            path=root / CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
            payload=assignments_payload,
        )
        parsed_assignments = parse_clade_assignments_tsv(assignments_payload)
        if tuple(row.model_dump(mode="json") for row in parsed_assignments) != tuple(
            row.model_dump(mode="json") for row in computation.assignment_records
        ):
            raise CladeDetectionStageError(
                reason="clade_assignments_serialization_invalid",
                detail=(
                    "clade_assignments.tsv serialization is not parseable into canonical "
                    "assignment rows."
                ),
            )
        assignments_metadata = artifact_metadata(
            root / CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
            relative_path=CLADE_ASSIGNMENTS_TSV_RELATIVE_PATH,
        )
        progress_reporter(0.9)
        context.check_control()

        completed_at_value = utc_now()
        manifest = CladeDetectionManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=True,
            normalized_settings=clade_config,
            status=CladeDetectionStatus.COMPLETED,
            method=clade_config.method,
            max_within_clade_distance=threshold,
            input_distance_model=computation.result.input_distance_model,
            requested_rooting=computation.result.requested_rooting,
            applied_rooting=computation.result.applied_rooting,
            tree_snapshot_manifest_sha256=inputs.tree_snapshot_manifest_sha256,
            matrix_snapshot_manifest_sha256=inputs.matrix_snapshot_manifest_sha256,
            leaf_count=computation.result.canonical_leaf_count,
            clade_count=computation.result.clade_count,
            singleton_clade_count=computation.result.singleton_clade_count,
            multi_leaf_clade_count=computation.result.multi_leaf_clade_count,
            minimum_clade_size=computation.result.minimum_clade_size,
            maximum_clade_size=computation.result.maximum_clade_size,
            started_at=serialize_utc_datetime(started_at_value),
            completed_at=serialize_utc_datetime(completed_at_value),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            source_artifacts=inputs.source_artifacts,
            artifacts=(result_metadata, memberships_metadata, assignments_metadata),
        )
        _write_json_model(
            path=root / CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
            model=manifest,
            relative_path=CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
        )
        _validate_published_snapshot(root=root, manifest=manifest)
        context.check_control()
        _update_progress_description(
            progress_reporter,
            description="Clade detection: ready to commit staged artifacts.",
        )
        context.emit_event(
            CLADE_DETECTION_PROGRESS_EVENT,
            {
                "phase": "ready_to_commit",
                "leaf_count": manifest.leaf_count,
                "clade_count": manifest.clade_count,
                "detail": "Clade detection: staged artifacts validated and ready for commit.",
            },
        )
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=clade_detection_artifact_paths(manifest),
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
            CLADE_DETECTION_SKIPPED_EVENT,
            {
                "reason": "clade_detection_disabled",
                "detail": "Clade detection was skipped because it is disabled.",
            },
        )
        progress_reporter(0.5)
        completed_at = utc_now()
        manifest = CladeDetectionManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=False,
            normalized_settings=config.clade_detection,
            skipped_reason="clade_detection_disabled",
            status=CladeDetectionStatus.COMPLETED,
            method=config.clade_detection.method,
            max_within_clade_distance=config.clade_detection.max_within_clade_distance,
            started_at=serialize_utc_datetime(started_at),
            completed_at=serialize_utc_datetime(completed_at),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            leaf_count=0,
            clade_count=0,
            singleton_clade_count=0,
            multi_leaf_clade_count=0,
            minimum_clade_size=0,
            maximum_clade_size=0,
        )
        root = context.stage_staging_directory
        _write_json_model(
            path=root / CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
            model=manifest,
            relative_path=CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
        )
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=(CLADE_DETECTION_MANIFEST_RELATIVE_PATH,),
            check_control_before_commit=True,
        )


def _load_resolved_config(path: Path) -> ResolvedAnalysisConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CladeDetectionStageError(
            reason="clade_detection_config_unreadable",
            detail="Immutable analysis configuration could not be read.",
        ) from error
    if not isinstance(payload, dict):
        raise CladeDetectionStageError(
            reason="clade_detection_config_invalid",
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
        raise CladeDetectionStageError(
            reason="clade_detection_config_invalid",
            detail="Immutable analysis configuration is invalid for clade detection.",
        ) from error


def _load_upstream_inputs(*, context: StageContext) -> _UpstreamInputs:
    try:
        tree_snapshot = validate_committed_stage_snapshot(
            job_dir=context.launch_spec.job_dir,
            stage_id=PHYLOGENETIC_TREE_STAGE_ID,
            expected_job_id=context.launch_spec.job_id,
            expected_pipeline_version=context.launch_spec.pipeline_version,
            expected_task_id=context.launch_spec.task_id,
            expected_config_hash=context.launch_spec.config_hash,
        )
    except StageCommitError as error:
        raise CladeDetectionStageError(
            reason="phylogenetic_tree_snapshot_invalid",
            detail="Published phylogenetic-tree snapshot is missing or invalid.",
        ) from error
    try:
        matrix_snapshot = validate_committed_stage_snapshot(
            job_dir=context.launch_spec.job_dir,
            stage_id=DISTANCE_MATRIX_STAGE_ID,
            expected_job_id=context.launch_spec.job_id,
            expected_pipeline_version=context.launch_spec.pipeline_version,
            expected_task_id=context.launch_spec.task_id,
            expected_config_hash=context.launch_spec.config_hash,
        )
    except StageCommitError as error:
        raise CladeDetectionStageError(
            reason="distance_matrix_snapshot_invalid",
            detail="Published distance-matrix snapshot is missing or invalid.",
        ) from error

    if tree_snapshot.domain_manifest_sha256 is None:
        raise CladeDetectionStageError(
            reason="phylogenetic_tree_snapshot_invalid",
            detail="Published phylogenetic-tree manifest digest is unavailable.",
        )
    if matrix_snapshot.domain_manifest_sha256 is None:
        raise CladeDetectionStageError(
            reason="distance_matrix_snapshot_invalid",
            detail="Published distance-matrix manifest digest is unavailable.",
        )
    if tree_snapshot.domain_status != PhylogeneticTreeStatus.COMPLETED.value:
        raise CladeDetectionStageError(
            reason="phylogenetic_tree_incomplete",
            detail="Clade detection requires a completed phylogenetic-tree snapshot.",
        )
    if matrix_snapshot.domain_status != DistanceMatrixStatus.COMPLETED.value:
        raise CladeDetectionStageError(
            reason="distance_matrix_incomplete",
            detail="Clade detection requires a completed distance-matrix snapshot.",
        )

    tree_root = context.launch_spec.job_dir / "stages" / PHYLOGENETIC_TREE_STAGE_ID
    matrix_root = context.launch_spec.job_dir / "stages" / DISTANCE_MATRIX_STAGE_ID
    try:
        tree_manifest = PhylogeneticTreeManifest.model_validate_json(
            (tree_root / PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH).read_text(
                encoding="utf-8"
            )
        )
        tree_result = PhylogeneticTreeResult.model_validate_json(
            (tree_root / TREE_JSON_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        matrix_manifest = DistanceMatrixManifest.model_validate_json(
            (matrix_root / DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH).read_text(
                encoding="utf-8"
            )
        )
        matrix_result = DistanceMatrixResult.model_validate_json(
            (matrix_root / DISTANCE_MATRIX_JSON_RELATIVE_PATH).read_text(
                encoding="utf-8"
            )
        )
    except Exception as error:
        raise CladeDetectionStageError(
            reason="clade_detection_upstream_artifact_invalid",
            detail="Published tree or matrix artifacts are invalid for clade detection.",
        ) from error

    if not tree_manifest.enabled:
        raise CladeDetectionStageError(
            reason="phylogenetic_tree_disabled",
            detail="Clade detection requires an enabled phylogenetic-tree stage result.",
        )
    if not matrix_manifest.enabled:
        raise CladeDetectionStageError(
            reason="distance_matrix_disabled",
            detail="Clade detection requires an enabled distance-matrix stage result.",
        )
    if matrix_manifest.undefined_distance_count != 0 or matrix_result.undefined_distance_count != 0:
        raise CladeDetectionStageError(
            reason="distance_matrix_incomplete",
            detail="Clade detection requires a complete distance matrix without null values.",
        )
    if tree_manifest.input_snapshot_manifest_sha256 != matrix_snapshot.domain_manifest_sha256:
        raise CladeDetectionStageError(
            reason="tree_matrix_linkage_invalid",
            detail=(
                "Committed phylogenetic-tree manifest does not reference the committed "
                "distance-matrix snapshot digest."
            ),
        )
    if tree_result.input_snapshot_manifest_sha256 != matrix_snapshot.domain_manifest_sha256:
        raise CladeDetectionStageError(
            reason="tree_matrix_linkage_invalid",
            detail=(
                "Committed tree.json does not reference the committed distance-matrix "
                "snapshot digest."
            ),
        )
    for expected in _EXPECTED_TREE_SOURCE_ARTIFACTS:
        relative = expected.removeprefix(f"stages/{PHYLOGENETIC_TREE_STAGE_ID}/")
        if relative not in set(tree_snapshot.manifest.artifacts):
            raise CladeDetectionStageError(
                reason="phylogenetic_tree_snapshot_invalid",
                detail="Phylogenetic-tree snapshot is missing required canonical artifacts.",
            )
    for expected in _EXPECTED_DISTANCE_SOURCE_ARTIFACTS:
        relative = expected.removeprefix(f"stages/{DISTANCE_MATRIX_STAGE_ID}/")
        if relative not in set(matrix_snapshot.manifest.artifacts):
            raise CladeDetectionStageError(
                reason="distance_matrix_snapshot_invalid",
                detail="Distance-matrix snapshot is missing required canonical artifacts.",
            )

    return _UpstreamInputs(
        tree_snapshot_manifest_sha256=tree_snapshot.domain_manifest_sha256,
        matrix_snapshot_manifest_sha256=matrix_snapshot.domain_manifest_sha256,
        source_artifacts=_EXPECTED_SOURCE_ARTIFACTS,
        tree_manifest=tree_manifest,
        tree_result=tree_result,
        matrix_manifest=matrix_manifest,
        matrix_result=matrix_result,
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


def _sha256_file(path: Path) -> str:
    hash_value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hash_value.update(chunk)
    return hash_value.hexdigest()


def _validate_published_snapshot(*, root: Path, manifest: CladeDetectionManifest) -> None:
    for metadata in manifest.artifacts:
        path = root / metadata.relative_path
        if not path.is_file():
            raise CladeDetectionStageError(
                reason="clade_detection_artifact_missing",
                detail="A clade-detection artifact is missing before publication.",
                context={"relative_path": metadata.relative_path},
            )
        if path.stat().st_size != metadata.size_bytes or _sha256_file(path) != metadata.sha256:
            raise CladeDetectionStageError(
                reason="clade_detection_artifact_integrity_failed",
                detail="A clade-detection artifact failed integrity validation.",
                context={"relative_path": metadata.relative_path},
            )


def _as_clade_stage_computation_error(
    *,
    error: CladeDetectionComputationError,
    phase: str,
    detail: str,
) -> CladeDetectionStageError:
    return CladeDetectionStageError(
        reason=_normalize_clade_computation_reason(error.reason),
        detail=detail,
        context={"phase": phase},
    )


def _normalize_clade_computation_reason(reason: str) -> str:
    normalized = reason.strip().lower().replace("-", "_")
    if normalized == "":
        return "clade_detection_computation_failed"
    if any(character not in _SAFE_REASON_CHARS for character in normalized):
        return "clade_detection_computation_failed"
    if normalized.startswith("clade_detection_"):
        return normalized
    return f"clade_detection_{normalized}"


def _update_progress_description(
    progress_reporter: ProgressReporter,
    *,
    description: str,
) -> None:
    update = getattr(progress_reporter, "update", None)
    if callable(update):
        update(description=description)


__all__ = [
    "CLADE_DETECTION_COMPLETED_EVENT",
    "CLADE_DETECTION_FAILED_EVENT",
    "CLADE_DETECTION_PROGRESS_EVENT",
    "CLADE_DETECTION_RESULT_PUBLISHED_EVENT",
    "CLADE_DETECTION_SKIPPED_EVENT",
    "CLADE_DETECTION_STARTED_EVENT",
    "CladeDetectionStage",
    "CladeDetectionStageError",
]
