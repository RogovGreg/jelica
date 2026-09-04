from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final

from pydantic import BaseModel

from jelica_core.alignment import (
    ALIGNED_NUCLEOTIDE_SYMBOLS,
    ALIGNMENT_MANIFEST_RELATIVE_PATH,
    ALIGNMENT_STAGE_ID,
    AlignmentManifest,
    AlignmentStageOutcome,
    parse_aligned_fasta,
)
from jelica_core.alignment.models import ALIGNMENT_MANIFEST_SCHEMA_VERSION
from jelica_core.config import AnalysisAlignmentMode, ResolvedAnalysisConfig
from jelica_core.distance_matrix import (
    AMBIGUITY_POLICY_PAIRWISE_DELETION,
    DISTANCE_MATRIX_JSON_RELATIVE_PATH,
    DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
    DISTANCE_MATRIX_STAGE_ID,
    DISTANCE_MATRIX_TSV_RELATIVE_PATH,
    DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
    GAP_POLICY_PAIRWISE_DELETION,
    URACIL_THYMINE_POLICY_EQUIVALENT,
    DistanceMatrixAggregateCounts,
    DistanceMatrixJsonlWriter,
    DistanceMatrixManifest,
    DistanceMatrixResult,
    DistanceMatrixSequenceReference,
    DistanceMatrixStatus,
    DistancePairRecord,
    DistancePairState,
    artifact_metadata,
    compute_p_distance_pair,
    distance_matrix_artifact_paths,
    expected_pair_count,
    initialize_distance_matrix,
    iter_unordered_index_pairs,
    matrix_to_tuple,
    serialize_distance_matrix_tsv,
    set_symmetric_distance,
)
from jelica_core.runtime.input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    INPUT_PROCESSING_STAGE_ID,
    InputProcessingManifest,
    InputProcessingState,
)
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .pipeline import ProgressReporter, StageContext, StageRunResult

DISTANCE_MATRIX_STARTED_EVENT: Final = "DISTANCE_MATRIX_STARTED"
DISTANCE_MATRIX_SKIPPED_EVENT: Final = "DISTANCE_MATRIX_SKIPPED"
DISTANCE_MATRIX_PROGRESS_EVENT: Final = "DISTANCE_MATRIX_PROGRESS"
DISTANCE_MATRIX_RESULT_PUBLISHED_EVENT: Final = "DISTANCE_MATRIX_RESULT_PUBLISHED"
DISTANCE_MATRIX_COMPLETED_EVENT: Final = "DISTANCE_MATRIX_COMPLETED"
DISTANCE_MATRIX_PARTIAL_SUCCESS_EVENT: Final = "DISTANCE_MATRIX_PARTIAL_SUCCESS"
DISTANCE_MATRIX_FAILED_EVENT: Final = "DISTANCE_MATRIX_FAILED"

_INTERNAL_TASK_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_directory_max_depth", "ncbi_max_retries"}
)


class DistanceMatrixStageError(RuntimeError):
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
        self.event_name = DISTANCE_MATRIX_FAILED_EVENT
        self.context = context or {}
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _InputArtifacts:
    candidate_roots: tuple[Path, ...]
    selected_root: Path
    manifest: InputProcessingManifest


@dataclass(frozen=True, slots=True)
class _AlignmentSnapshot:
    candidate_roots: tuple[Path, ...]
    selected_root: Path
    staging_root: Path
    manifest_sha256: str
    manifest: AlignmentManifest


@dataclass(frozen=True, slots=True)
class _AlignmentArtifacts:
    candidate_roots: tuple[Path, ...]
    selected_root: Path
    staging_root: Path
    manifest_sha256: str
    manifest: AlignmentManifest
    sequence_ids: tuple[str, ...]
    aligned_sequences: tuple[str, ...]
    logical_sample_ids_by_sequence_id: dict[str, tuple[str, ...]]


class _AlignmentSnapshotMoved(RuntimeError):
    """Signal one expected worker-staging to committed-directory move."""


@dataclass(frozen=True, slots=True)
class DistanceMatrixStage:
    stage_id: str = DISTANCE_MATRIX_STAGE_ID
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)
        (context.stage_staging_directory / "distance_matrix").mkdir(
            parents=True,
            exist_ok=True,
        )

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        started_at_value = utc_now()
        started_monotonic = time.monotonic()
        context.check_control()
        config = _load_resolved_config(context.launch_spec.config_revision_path)
        distance_config = config.distance_matrix
        if not distance_config.enabled:
            return self._run_disabled(
                context=context,
                progress_reporter=progress_reporter,
                config=config,
                started_at=started_at_value,
                started_monotonic=started_monotonic,
            )

        input_artifacts = _load_input_artifacts(context=context)
        alignment = _load_alignment_artifacts(
            context=context,
            input_manifest=input_artifacts.manifest,
            expected_mode=config.alignment.mode,
        )
        sequence_count = len(alignment.sequence_ids)
        if sequence_count == 0:
            raise DistanceMatrixStageError(
                reason="distance_matrix_sequence_set_empty",
                detail="Published alignment has no unique sequences for distance matrix.",
            )
        pair_total = expected_pair_count(sequence_count)
        context.emit_event(
            DISTANCE_MATRIX_STARTED_EVENT,
            {
                "model": distance_config.model.value,
                "unique_sequence_count": sequence_count,
                "total_pairs": pair_total,
                "detail": "Distance-matrix stage started.",
            },
        )
        _update_progress_description(
            progress_reporter,
            description=(
                "Distance matrix: preparing canonical unique sequence inputs "
                f"({sequence_count} sequences)."
            ),
        )
        progress_reporter(0.1)

        matrix = initialize_distance_matrix(sequence_count)
        root = context.stage_staging_directory
        pair_writer = DistanceMatrixJsonlWriter(
            root / DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
            relative_path=DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
        )
        mismatch_count_sum = 0
        comparable_site_count_sum = 0
        excluded_gap_site_count_sum = 0
        excluded_ambiguous_site_count_sum = 0
        defined_distance_count = 0
        undefined_distance_count = 0
        completed_pairs = 0
        milestone = max(1, math.ceil(max(pair_total, 1) / 20))
        last_emitted_completed = -1

        def emit_progress(*, force: bool = False) -> None:
            nonlocal last_emitted_completed
            if not force and completed_pairs - last_emitted_completed < milestone:
                return
            if not force and completed_pairs == last_emitted_completed:
                return
            last_emitted_completed = completed_pairs
            ratio = 1.0 if pair_total == 0 else completed_pairs / pair_total
            _update_progress_description(
                progress_reporter,
                description=(
                    "Distance matrix: processed "
                    f"{completed_pairs}/{pair_total} pairs; "
                    f"defined: {defined_distance_count}, undefined: {undefined_distance_count}."
                ),
            )
            progress_reporter(0.1 + (0.75 * ratio))
            context.emit_event(
                DISTANCE_MATRIX_PROGRESS_EVENT,
                {
                    "completed_pairs": completed_pairs,
                    "total_pairs": pair_total,
                    "defined_distance_count": defined_distance_count,
                    "undefined_distance_count": undefined_distance_count,
                    "detail": (
                        "Distance matrix progress: "
                        f"{completed_pairs}/{pair_total} pairs."
                    ),
                },
            )

        emit_progress(force=True)
        try:
            for left_index, right_index in iter_unordered_index_pairs(sequence_count):
                context.check_control()
                computation = compute_p_distance_pair(
                    left_aligned_sequence=alignment.aligned_sequences[left_index],
                    right_aligned_sequence=alignment.aligned_sequences[right_index],
                    control_check=context.check_control,
                )
                mismatch_count_sum += computation.mismatch_count
                comparable_site_count_sum += computation.comparable_site_count
                excluded_gap_site_count_sum += computation.excluded_gap_site_count
                excluded_ambiguous_site_count_sum += (
                    computation.excluded_ambiguous_site_count
                )
                state = (
                    DistancePairState.UNDEFINED_NO_COMPARABLE_SITES
                    if computation.distance is None
                    else DistancePairState.DEFINED
                )
                if state is DistancePairState.DEFINED:
                    defined_distance_count += 1
                else:
                    undefined_distance_count += 1
                set_symmetric_distance(
                    matrix,
                    left_index=left_index,
                    right_index=right_index,
                    distance=computation.distance,
                )
                pair_writer.write_model(
                    DistancePairRecord(
                        left_sequence_id=alignment.sequence_ids[left_index],
                        right_sequence_id=alignment.sequence_ids[right_index],
                        left_index=left_index,
                        right_index=right_index,
                        mismatch_count=computation.mismatch_count,
                        comparable_site_count=computation.comparable_site_count,
                        excluded_gap_site_count=computation.excluded_gap_site_count,
                        excluded_ambiguous_site_count=(
                            computation.excluded_ambiguous_site_count
                        ),
                        distance=computation.distance,
                        state=state,
                    )
                )
                completed_pairs += 1
                if (
                    completed_pairs == pair_total
                    or completed_pairs - last_emitted_completed >= milestone
                ):
                    emit_progress(force=True)
            context.check_control()
            pair_metadata = pair_writer.close()
        except Exception:
            pair_writer.abort()
            raise

        status = (
            DistanceMatrixStatus.PARTIAL_SUCCESS
            if undefined_distance_count > 0
            else DistanceMatrixStatus.COMPLETED
        )
        context.check_control()
        progress_reporter(0.9)
        sequence_references = tuple(
            DistanceMatrixSequenceReference(
                index=index,
                sequence_id=sequence_id,
                logical_sample_ids=alignment.logical_sample_ids_by_sequence_id[sequence_id],
            )
            for index, sequence_id in enumerate(alignment.sequence_ids)
        )
        distance_matrix_result = DistanceMatrixResult(
            model=distance_config.model,
            gap_policy=GAP_POLICY_PAIRWISE_DELETION,
            ambiguity_policy=AMBIGUITY_POLICY_PAIRWISE_DELETION,
            uracil_thymine_policy=URACIL_THYMINE_POLICY_EQUIVALENT,
            sequence_references=sequence_references,
            matrix=matrix_to_tuple(matrix),
            unique_sequence_count=sequence_count,
            expected_pair_count=pair_total,
            processed_pair_count=pair_total,
            defined_distance_count=defined_distance_count,
            undefined_distance_count=undefined_distance_count,
            aggregate_counts=DistanceMatrixAggregateCounts(
                mismatch_count_sum=mismatch_count_sum,
                comparable_site_count_sum=comparable_site_count_sum,
                excluded_gap_site_count_sum=excluded_gap_site_count_sum,
                excluded_ambiguous_site_count_sum=excluded_ambiguous_site_count_sum,
            ),
        )
        matrix_metadata = _write_json_model(
            path=root / DISTANCE_MATRIX_JSON_RELATIVE_PATH,
            model=distance_matrix_result,
            relative_path=DISTANCE_MATRIX_JSON_RELATIVE_PATH,
        )
        context.check_control()
        tsv_payload = serialize_distance_matrix_tsv(
            sequence_ids=alignment.sequence_ids,
            matrix=matrix,
            undefined_marker="NA",
        )
        write_text_atomically(
            path=root / DISTANCE_MATRIX_TSV_RELATIVE_PATH,
            payload=tsv_payload,
        )
        tsv_metadata = artifact_metadata(
            root / DISTANCE_MATRIX_TSV_RELATIVE_PATH,
            relative_path=DISTANCE_MATRIX_TSV_RELATIVE_PATH,
        )

        source_artifacts = (
            f"stages/{INPUT_PROCESSING_STAGE_ID}/{INPUT_PROCESSING_MANIFEST_RELATIVE_PATH}",
            f"stages/{ALIGNMENT_STAGE_ID}/{ALIGNMENT_MANIFEST_RELATIVE_PATH}",
            f"stages/{ALIGNMENT_STAGE_ID}/{alignment.manifest.aligned_fasta_path}",
        )
        completed_at_value = utc_now()
        manifest = DistanceMatrixManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=True,
            normalized_settings=distance_config,
            status=status,
            model=distance_config.model,
            gap_policy=GAP_POLICY_PAIRWISE_DELETION,
            ambiguity_policy=AMBIGUITY_POLICY_PAIRWISE_DELETION,
            uracil_thymine_policy=URACIL_THYMINE_POLICY_EQUIVALENT,
            unique_sequence_count=sequence_count,
            expected_pair_count=pair_total,
            processed_pair_count=pair_total,
            defined_distance_count=defined_distance_count,
            undefined_distance_count=undefined_distance_count,
            matrix_dimensions=(sequence_count, sequence_count),
            started_at=serialize_utc_datetime(started_at_value),
            completed_at=serialize_utc_datetime(completed_at_value),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            source_artifacts=source_artifacts,
            artifacts=(matrix_metadata, pair_metadata, tsv_metadata),
        )
        _write_json_model(
            path=root / DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
            model=manifest,
            relative_path=DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
        )
        _validate_published_snapshot(root=root, manifest=manifest)
        context.emit_event(
            DISTANCE_MATRIX_PROGRESS_EVENT,
            {
                "phase": "ready_to_commit",
                "status": status.value,
                "unique_sequence_count": sequence_count,
                "total_pairs": pair_total,
                "defined_distance_count": defined_distance_count,
                "undefined_distance_count": undefined_distance_count,
                "detail": "Distance matrix: staged artifacts validated and ready for commit.",
            },
        )
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=distance_matrix_artifact_paths(manifest),
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
            DISTANCE_MATRIX_STARTED_EVENT,
            {
                "model": config.distance_matrix.model.value,
                "unique_sequence_count": 0,
                "total_pairs": 0,
                "detail": "Distance-matrix stage started.",
            },
        )
        context.emit_event(
            DISTANCE_MATRIX_SKIPPED_EVENT,
            {
                "reason": "distance_matrix_disabled",
                "detail": "Distance matrix was skipped because it is disabled.",
            },
        )
        progress_reporter(0.5)
        completed_at = utc_now()
        manifest = DistanceMatrixManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=False,
            normalized_settings=config.distance_matrix,
            skipped_reason="distance_matrix_disabled",
            status=DistanceMatrixStatus.COMPLETED,
            model=config.distance_matrix.model,
            gap_policy=GAP_POLICY_PAIRWISE_DELETION,
            ambiguity_policy=AMBIGUITY_POLICY_PAIRWISE_DELETION,
            uracil_thymine_policy=URACIL_THYMINE_POLICY_EQUIVALENT,
            unique_sequence_count=0,
            expected_pair_count=0,
            processed_pair_count=0,
            defined_distance_count=0,
            undefined_distance_count=0,
            matrix_dimensions=(0, 0),
            started_at=serialize_utc_datetime(started_at),
            completed_at=serialize_utc_datetime(completed_at),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
        )
        root = context.stage_staging_directory
        _write_json_model(
            path=root / DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
            model=manifest,
            relative_path=DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
        )
        progress_reporter(1.0)
        return StageRunResult(
            artifacts=(DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,),
            check_control_before_commit=True,
        )


def _load_resolved_config(path: Path) -> ResolvedAnalysisConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DistanceMatrixStageError(
            reason="distance_matrix_config_unreadable",
            detail="Immutable analysis configuration could not be read.",
        ) from error
    if not isinstance(payload, dict):
        raise DistanceMatrixStageError(
            reason="distance_matrix_config_invalid",
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
        raise DistanceMatrixStageError(
            reason="distance_matrix_config_invalid",
            detail="Immutable analysis configuration is invalid for distance matrix.",
        ) from error


def _load_input_artifacts(*, context: StageContext) -> _InputArtifacts:
    committed_root = context.launch_spec.job_dir / "stages" / INPUT_PROCESSING_STAGE_ID
    staging_root = (
        context.launch_spec.job_dir
        / "staging"
        / INPUT_PROCESSING_STAGE_ID
        / context.launch_spec.worker_instance_id
    )
    candidate_roots = (committed_root, staging_root)
    for root in candidate_roots:
        path = root / INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
        if not path.is_file():
            continue
        try:
            manifest = InputProcessingManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except OSError:
            continue
        except Exception as error:
            raise DistanceMatrixStageError(
                reason="input_processing_manifest_invalid",
                detail="Published input-processing manifest is invalid.",
            ) from error
        if manifest.stage_id != INPUT_PROCESSING_STAGE_ID:
            raise DistanceMatrixStageError(
                reason="input_processing_manifest_invalid",
                detail="Published input-processing manifest has an invalid stage identity.",
            )
        if manifest.processing_state is not InputProcessingState.COMPLETED:
            raise DistanceMatrixStageError(
                reason="input_processing_result_unavailable",
                detail="Published input processing is not complete.",
            )
        _validate_upstream_identity(
            context=context,
            task_id=manifest.task_id,
            job_id=manifest.job_id,
            config_hash=manifest.config_hash,
            source_name="input-processing manifest",
        )
        return _InputArtifacts(
            candidate_roots=candidate_roots,
            selected_root=root,
            manifest=manifest,
        )
    raise DistanceMatrixStageError(
        reason="input_processing_manifest_missing",
        detail="Published input-processing manifest is missing for distance matrix.",
    )


def _validate_upstream_identity(
    *,
    context: StageContext,
    task_id: str,
    job_id: str,
    config_hash: str,
    source_name: str,
) -> None:
    if task_id != context.launch_spec.task_id or job_id != context.launch_spec.job_id:
        raise DistanceMatrixStageError(
            reason="distance_matrix_upstream_identity_mismatch",
            detail=f"The {source_name} identity does not match this job.",
        )
    if config_hash != context.launch_spec.config_hash:
        raise DistanceMatrixStageError(
            reason="distance_matrix_upstream_config_mismatch",
            detail=f"The {source_name} configuration hash does not match this job.",
        )


def _validate_relative_artifact_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if normalized == "" or posix.is_absolute() or windows.is_absolute():
        raise DistanceMatrixStageError(
            reason="distance_matrix_artifact_path_invalid",
            detail="An upstream artifact path is not a safe relative path.",
        )
    if ".." in posix.parts or ".." in windows.parts:
        raise DistanceMatrixStageError(
            reason="distance_matrix_artifact_path_invalid",
            detail="An upstream artifact path escapes its stage directory.",
        )
    return posix.as_posix()


def _find_artifact(
    *,
    candidate_roots: tuple[Path, ...],
    selected_root: Path,
    relative_path: str,
    snapshot_manifest_relative_path: str,
    snapshot_manifest_sha256: str,
) -> Path:
    normalized = _validate_relative_artifact_path(relative_path)
    roots = (selected_root, *(root for root in candidate_roots if root != selected_root))
    for root in roots:
        if root != selected_root:
            snapshot_manifest_path = root / snapshot_manifest_relative_path
            if (
                not snapshot_manifest_path.is_file()
                or _sha256_file(snapshot_manifest_path) != snapshot_manifest_sha256
            ):
                continue
        candidate = root / Path(PurePosixPath(normalized))
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError:
            continue
        if resolved_candidate.is_file():
            return resolved_candidate
    raise DistanceMatrixStageError(
        reason="distance_matrix_artifact_missing",
        detail="An upstream artifact required for distance matrix is missing.",
        context={"relative_path": normalized},
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise DistanceMatrixStageError(
            reason="distance_matrix_artifact_unreadable",
            detail="An upstream artifact could not be read.",
        ) from error
    return digest.hexdigest()


def _alignment_snapshot_move_confirmed(snapshot: _AlignmentSnapshot) -> bool:
    return (
        snapshot.selected_root == snapshot.staging_root
        and not snapshot.staging_root.exists()
    )


def _caused_by_os_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, OSError):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def _signal_alignment_snapshot_move(
    *,
    snapshot: _AlignmentSnapshot,
    error: BaseException,
) -> None:
    if _alignment_snapshot_move_confirmed(snapshot):
        raise _AlignmentSnapshotMoved from error


def _resolve_alignment_snapshot(
    *,
    context: StageContext,
    expected_mode: AnalysisAlignmentMode,
) -> _AlignmentSnapshot:
    committed_root = context.launch_spec.job_dir / "stages" / ALIGNMENT_STAGE_ID
    staging_root = (
        context.launch_spec.job_dir
        / "staging"
        / ALIGNMENT_STAGE_ID
        / context.launch_spec.worker_instance_id
    )
    candidate_roots = (committed_root, staging_root)
    selected_root: Path | None = None
    manifest: AlignmentManifest | None = None
    manifest_sha256: str | None = None
    for root in candidate_roots:
        manifest_path = root / ALIGNMENT_MANIFEST_RELATIVE_PATH
        try:
            manifest_payload = manifest_path.read_bytes()
            manifest = AlignmentManifest.model_validate_json(manifest_payload)
        except OSError:
            if root == staging_root and not staging_root.exists():
                try:
                    manifest_path = committed_root / ALIGNMENT_MANIFEST_RELATIVE_PATH
                    manifest_payload = manifest_path.read_bytes()
                    manifest = AlignmentManifest.model_validate_json(manifest_payload)
                except OSError:
                    continue
                except Exception as error:
                    raise DistanceMatrixStageError(
                        reason="alignment_manifest_invalid",
                        detail="Published alignment manifest is invalid.",
                    ) from error
                root = committed_root
            else:
                continue
        except Exception as error:
            raise DistanceMatrixStageError(
                reason="alignment_manifest_invalid",
                detail="Published alignment manifest is invalid.",
            ) from error
        selected_root = root
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        break
    if manifest is None or selected_root is None or manifest_sha256 is None:
        raise DistanceMatrixStageError(
            reason="alignment_manifest_missing",
            detail="Distance matrix requires a published alignment manifest.",
        )
    if (
        manifest.schema_version != ALIGNMENT_MANIFEST_SCHEMA_VERSION
        or manifest.stage_id != ALIGNMENT_STAGE_ID
        or manifest.mode is not expected_mode
    ):
        raise DistanceMatrixStageError(
            reason="alignment_manifest_invalid",
            detail="Published alignment manifest has an invalid schema or stage identity.",
        )
    _validate_upstream_identity(
        context=context,
        task_id=manifest.task_id,
        job_id=manifest.job_id,
        config_hash=manifest.config_hash,
        source_name="alignment manifest",
    )
    if manifest.outcome not in {
        AlignmentStageOutcome.COMPLETED,
        AlignmentStageOutcome.SKIPPED_NOT_REQUIRED,
    }:
        raise DistanceMatrixStageError(
            reason="alignment_result_unavailable",
            detail="Distance matrix requires an available canonical alignment.",
        )
    if (
        manifest.aligned_fasta_path is None
        or manifest.alignment_length is None
        or manifest.alignment_length < 1
        or manifest.result_sha256 is None
    ):
        raise DistanceMatrixStageError(
            reason="alignment_manifest_incomplete",
            detail="Published alignment metadata is incomplete for distance matrix.",
        )
    return _AlignmentSnapshot(
        candidate_roots=candidate_roots,
        selected_root=selected_root,
        staging_root=staging_root,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )


def _require_same_alignment_manifest(
    *,
    expected_sha256: str,
    snapshot: _AlignmentSnapshot,
) -> None:
    if snapshot.manifest_sha256 != expected_sha256:
        raise DistanceMatrixStageError(
            reason="alignment_snapshot_manifest_changed",
            detail="Published alignment metadata changed during a controlled read retry.",
        )


def _alignment_snapshot_unstable(error: BaseException) -> DistanceMatrixStageError:
    return DistanceMatrixStageError(
        reason="alignment_snapshot_unstable",
        detail="Published alignment artifacts could not be read after one controlled retry.",
    )


def _load_alignment_artifacts_once(
    *,
    snapshot: _AlignmentSnapshot,
    input_manifest: InputProcessingManifest,
) -> _AlignmentArtifacts:
    manifest = snapshot.manifest
    try:
        aligned_path = _find_artifact(
            candidate_roots=snapshot.candidate_roots,
            selected_root=snapshot.selected_root,
            relative_path=str(manifest.aligned_fasta_path),
            snapshot_manifest_relative_path=ALIGNMENT_MANIFEST_RELATIVE_PATH,
            snapshot_manifest_sha256=snapshot.manifest_sha256,
        )
    except DistanceMatrixStageError as error:
        if error.reason == "distance_matrix_artifact_missing":
            _signal_alignment_snapshot_move(snapshot=snapshot, error=error)
        raise

    try:
        aligned_sha256 = _sha256_file(aligned_path)
    except DistanceMatrixStageError as error:
        if error.reason == "distance_matrix_artifact_unreadable":
            _signal_alignment_snapshot_move(snapshot=snapshot, error=error)
        raise
    if aligned_sha256 != manifest.result_sha256:
        raise DistanceMatrixStageError(
            reason="alignment_result_hash_mismatch",
            detail="Canonical alignment content does not match its published digest.",
        )

    try:
        parsed_rows = parse_aligned_fasta(path=str(aligned_path))
    except Exception as error:
        if _caused_by_os_error(error):
            _signal_alignment_snapshot_move(snapshot=snapshot, error=error)
        raise DistanceMatrixStageError(
            reason="alignment_result_invalid",
            detail="Canonical alignment could not be parsed safely.",
        ) from error

    expected_samples = tuple(
        sample for sample in input_manifest.logical_samples if sample.eligible_for_analysis
    )
    sample_catalog = {sample.sample_id: sample for sample in expected_samples}
    if (
        len(sample_catalog) != len(expected_samples)
        or len(parsed_rows) != len(expected_samples)
        or manifest.logical_sample_count != len(expected_samples)
    ):
        raise DistanceMatrixStageError(
            reason="alignment_sample_set_mismatch",
            detail="Canonical alignment sample counts are inconsistent with input processing.",
        )

    aligned_by_sequence_id: dict[str, str] = {}
    observed_sample_ids_by_sequence_id: dict[str, list[str]] = {}
    for expected_sample, (sample_id, aligned_sequence) in zip(
        expected_samples,
        parsed_rows,
        strict=True,
    ):
        if (
            expected_sample.sample_id != sample_id
            or expected_sample.sequence_id is None
        ):
            raise DistanceMatrixStageError(
                reason="alignment_sample_set_mismatch",
                detail=(
                    "Canonical alignment record order is inconsistent with input processing."
                ),
            )
        if len(aligned_sequence) != manifest.alignment_length:
            raise DistanceMatrixStageError(
                reason="alignment_length_mismatch",
                detail="Canonical alignment rows do not match the published alignment length.",
            )
        if any(symbol not in ALIGNED_NUCLEOTIDE_SYMBOLS for symbol in aligned_sequence):
            raise DistanceMatrixStageError(
                reason="alignment_symbol_invalid",
                detail="Canonical alignment contains a symbol outside the accepted alphabet.",
            )
        sequence_id = expected_sample.sequence_id
        previous = aligned_by_sequence_id.get(sequence_id)
        if previous is not None and previous != aligned_sequence:
            raise DistanceMatrixStageError(
                reason="alignment_identical_sequence_mismatch",
                detail="Canonical alignment rows sharing one sequence identity are inconsistent.",
            )
        aligned_by_sequence_id.setdefault(sequence_id, aligned_sequence)
        observed_sample_ids_by_sequence_id.setdefault(sequence_id, []).append(sample_id)

    if manifest.unique_sequence_count != len(aligned_by_sequence_id):
        raise DistanceMatrixStageError(
            reason="alignment_sequence_set_mismatch",
            detail="Canonical alignment sequence counts are inconsistent with input processing.",
        )

    sequence_ids = tuple(item.sequence_id for item in input_manifest.unique_sequences)
    if (
        len(set(sequence_ids)) != len(sequence_ids)
        or set(sequence_ids) != set(aligned_by_sequence_id)
        or input_manifest.dataset_summary.unique_sequence_count != len(sequence_ids)
    ):
        raise DistanceMatrixStageError(
            reason="alignment_sequence_set_mismatch",
            detail=(
                "Canonical alignment sequence identities are inconsistent with "
                "input processing."
            ),
        )

    logical_sample_ids_by_sequence_id: dict[str, tuple[str, ...]] = {}
    for item in input_manifest.unique_sequences:
        observed = tuple(observed_sample_ids_by_sequence_id.get(item.sequence_id, ()))
        if observed != item.logical_sample_ids:
            raise DistanceMatrixStageError(
                reason="alignment_sequence_mapping_mismatch",
                detail=(
                    "Canonical alignment sequence-to-sample mapping is inconsistent "
                    "with input processing."
                ),
            )
        logical_sample_ids_by_sequence_id[item.sequence_id] = observed

    aligned_sequences = tuple(aligned_by_sequence_id[sequence_id] for sequence_id in sequence_ids)
    return _AlignmentArtifacts(
        candidate_roots=snapshot.candidate_roots,
        selected_root=snapshot.selected_root,
        staging_root=snapshot.staging_root,
        manifest_sha256=snapshot.manifest_sha256,
        manifest=manifest,
        sequence_ids=sequence_ids,
        aligned_sequences=aligned_sequences,
        logical_sample_ids_by_sequence_id=logical_sample_ids_by_sequence_id,
    )


def _reload_alignment_artifacts_once(
    *,
    context: StageContext,
    input_manifest: InputProcessingManifest,
    expected_mode: AnalysisAlignmentMode,
    expected_manifest_sha256: str,
) -> _AlignmentArtifacts:
    snapshot = _resolve_alignment_snapshot(context=context, expected_mode=expected_mode)
    _require_same_alignment_manifest(
        expected_sha256=expected_manifest_sha256,
        snapshot=snapshot,
    )
    try:
        return _load_alignment_artifacts_once(
            snapshot=snapshot,
            input_manifest=input_manifest,
        )
    except _AlignmentSnapshotMoved as error:
        raise _alignment_snapshot_unstable(error) from error


def _load_alignment_artifacts(
    *,
    context: StageContext,
    input_manifest: InputProcessingManifest,
    expected_mode: AnalysisAlignmentMode,
) -> _AlignmentArtifacts:
    snapshot = _resolve_alignment_snapshot(context=context, expected_mode=expected_mode)
    try:
        return _load_alignment_artifacts_once(
            snapshot=snapshot,
            input_manifest=input_manifest,
        )
    except _AlignmentSnapshotMoved:
        return _reload_alignment_artifacts_once(
            context=context,
            input_manifest=input_manifest,
            expected_mode=expected_mode,
            expected_manifest_sha256=snapshot.manifest_sha256,
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


def _validate_published_snapshot(*, root: Path, manifest: DistanceMatrixManifest) -> None:
    for metadata in manifest.artifacts:
        path = root / metadata.relative_path
        if not path.is_file():
            raise DistanceMatrixStageError(
                reason="distance_matrix_artifact_missing",
                detail="A distance-matrix artifact is missing before publication.",
                context={"relative_path": metadata.relative_path},
            )
        if path.stat().st_size != metadata.size_bytes or _sha256_file(path) != metadata.sha256:
            raise DistanceMatrixStageError(
                reason="distance_matrix_artifact_integrity_failed",
                detail="A distance-matrix artifact failed integrity validation.",
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
    "DISTANCE_MATRIX_COMPLETED_EVENT",
    "DISTANCE_MATRIX_FAILED_EVENT",
    "DISTANCE_MATRIX_PARTIAL_SUCCESS_EVENT",
    "DISTANCE_MATRIX_PROGRESS_EVENT",
    "DISTANCE_MATRIX_RESULT_PUBLISHED_EVENT",
    "DISTANCE_MATRIX_SKIPPED_EVENT",
    "DISTANCE_MATRIX_STARTED_EVENT",
    "DistanceMatrixStage",
    "DistanceMatrixStageError",
]
