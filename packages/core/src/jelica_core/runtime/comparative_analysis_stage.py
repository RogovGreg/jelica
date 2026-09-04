from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final

from pydantic import BaseModel

from jelica_core.alignment import (
    ALIGNED_NUCLEOTIDE_SYMBOLS,
    ALIGNMENT_MANIFEST_RELATIVE_PATH,
    ALIGNMENT_STAGE_ID,
    AlignmentManifest,
    AlignmentStageOutcome,
    CanonicalAlignmentRow,
    ReferenceCoordinateMap,
    parse_aligned_fasta,
)
from jelica_core.alignment.models import ALIGNMENT_MANIFEST_SCHEMA_VERSION
from jelica_core.comparative_analysis.aligned_comparator import (
    AlignedDifferenceEvent,
    AlignedSequenceComparator,
    ComparisonIdentity,
    DifferenceEventType,
    DirectedAlignedComparison,
    DirectedComparisonProjector,
)
from jelica_core.comparative_analysis.artifacts import (
    COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
    COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
    COMPARATIVE_ANALYSIS_STAGE_ID,
    DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
    PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH,
    PAIRWISE_DIFFERENCES_RELATIVE_PATH,
    REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH,
    REFERENCE_DIFFERENCES_RELATIVE_PATH,
    STATISTICAL_DIFFERENCES_RELATIVE_PATH,
    ComparativeAnalysisManifest,
    ComparativeAnalysisStatus,
    ComparativeArtifactMetadata,
    ComparativeCategoryExecution,
    ComparativeCategoryStatus,
    ComparativeFailureRecord,
    ComparativePlanExecutionCounts,
    ComparativeResultStatus,
    JsonlArtifactWriter,
    PublishedSequenceComparisonSummary,
    RequestedDifferenceCategorySummary,
    SequenceComparisonSummaryRecord,
    StatisticalDatasetArtifact,
    StatisticalDifferenceRecord,
    artifact_metadata,
    materialize_difference_record,
)
from jelica_core.comparative_analysis.errors import ComparisonDomainError
from jelica_core.comparative_analysis.planning import (
    ComparisonPlan,
    ComparisonPlanBuilder,
    ComparisonPlanCounts,
    ComparisonSourceKind,
    DirectedLogicalComparison,
    SequenceComparisonComputation,
)
from jelica_core.comparative_analysis.statistics import (
    DatasetStatisticalSummarizer,
    SequenceCountComparisons,
    SequenceFactsComparison,
    SequenceFactsDatasetSummary,
    SequenceProportionComparisons,
    StatisticalComparator,
)
from jelica_core.config import AnalysisAlignmentMode, ResolvedAnalysisConfig
from jelica_core.runtime.input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    INPUT_PROCESSING_STAGE_ID,
    InputProcessingManifest,
    InputProcessingState,
    SequenceFacts,
)
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .pipeline import ProgressReporter, StageContext, StageFailure, StageRunResult

COMPARATIVE_ANALYSIS_STARTED_EVENT: Final = "COMPARATIVE_ANALYSIS_STARTED"
COMPARATIVE_ANALYSIS_SKIPPED_EVENT: Final = "COMPARATIVE_ANALYSIS_SKIPPED"
COMPARATIVE_ANALYSIS_PHASE_STARTED_EVENT: Final = "COMPARATIVE_ANALYSIS_PHASE_STARTED"
COMPARATIVE_ANALYSIS_PROGRESS_EVENT: Final = "COMPARATIVE_ANALYSIS_PROGRESS"
COMPARATIVE_ANALYSIS_OPERATION_FAILED_EVENT: Final = (
    "COMPARATIVE_ANALYSIS_OPERATION_FAILED"
)
COMPARATIVE_ANALYSIS_RESULT_PUBLISHED_EVENT: Final = (
    "COMPARATIVE_ANALYSIS_RESULT_PUBLISHED"
)
COMPARATIVE_ANALYSIS_COMPLETED_EVENT: Final = "COMPARATIVE_ANALYSIS_COMPLETED"
COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS_EVENT: Final = (
    "COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS"
)
COMPARATIVE_ANALYSIS_FAILED_EVENT: Final = "COMPARATIVE_ANALYSIS_FAILED"

_INTERNAL_TASK_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_directory_max_depth", "ncbi_max_retries"}
)
_STATISTICS_RAW_RELATIVE_PATH: Final = "comparative_analysis/.statistics-raw.jsonl"


class ComparativeAnalysisStageError(RuntimeError):
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
        self.event_name = COMPARATIVE_ANALYSIS_FAILED_EVENT
        self.context = context or {}
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _InputArtifacts:
    candidate_roots: tuple[Path, ...]
    selected_root: Path
    manifest: InputProcessingManifest


@dataclass(frozen=True, slots=True)
class _AlignmentArtifacts:
    candidate_roots: tuple[Path, ...]
    selected_root: Path
    staging_root: Path
    manifest_sha256: str
    manifest: AlignmentManifest
    rows_by_sample_id: dict[str, CanonicalAlignmentRow]
    rows_by_sequence_id: dict[str, CanonicalAlignmentRow]


@dataclass(frozen=True, slots=True)
class _AlignmentSnapshot:
    candidate_roots: tuple[Path, ...]
    selected_root: Path
    staging_root: Path
    manifest_sha256: str
    manifest: AlignmentManifest


class _AlignmentSnapshotMoved(RuntimeError):
    """Signal one expected worker-staging to committed-directory move."""


@dataclass(frozen=True, slots=True)
class _ReferenceCoordinateLookup:
    coordinate_map: ReferenceCoordinateMap
    nearest_left: tuple[int | None, ...]
    nearest_right: tuple[int | None, ...]


@dataclass(frozen=True, slots=True)
class _StatisticalMetric:
    metric_id: str
    metric_name: str
    dataset_path: tuple[str, ...]
    comparison_path: tuple[str, ...]
    kmer_query: str | None = None


@dataclass(slots=True)
class _CategoryCounter:
    requested: bool
    total: int
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    unavailable: int = 0
    artifact_paths: tuple[str, ...] = tuple()

    @property
    def completed(self) -> int:
        return self.successful + self.failed

    def to_contract(self) -> ComparativeCategoryExecution:
        if not self.requested:
            status = ComparativeCategoryStatus.NOT_REQUESTED
        elif self.total == 0:
            status = ComparativeCategoryStatus.SKIPPED
        elif self.failed == 0:
            status = ComparativeCategoryStatus.COMPLETED
        elif self.successful == 0:
            status = ComparativeCategoryStatus.FAILED
        else:
            status = ComparativeCategoryStatus.PARTIAL_SUCCESS
        return ComparativeCategoryExecution(
            status=status,
            requested=self.requested,
            total=self.total,
            completed=self.completed,
            successful=self.successful,
            failed=self.failed,
            skipped=self.skipped,
            unavailable=self.unavailable,
            available=self.successful > 0,
            artifact_paths=self.artifact_paths,
        )


@dataclass(frozen=True, slots=True)
class _Phase:
    name: str
    operation_kind: str
    total: int


@dataclass(slots=True)
class _ProgressTracker:
    context: StageContext
    reporter: ProgressReporter
    phases: tuple[_Phase, ...]
    _phase_index: int = 0
    _phase_completed: int = 0
    _phase_successful: int = 0
    _phase_failed: int = 0
    _last_emitted_completed: int = -1

    def begin(self, index: int) -> None:
        self.context.check_control()
        self._phase_index = index
        self._phase_completed = 0
        self._phase_successful = 0
        self._phase_failed = 0
        self._last_emitted_completed = -1
        phase = self.phases[index]
        self.context.emit_event(
            COMPARATIVE_ANALYSIS_PHASE_STARTED_EVENT,
            {
                "phase_index": index + 1,
                "phase_total": len(self.phases),
                "operation_kind": phase.operation_kind,
                "total": phase.total,
                "detail": f"Comparative-analysis phase started: {phase.name}.",
            },
        )
        self._emit(force=True)

    def advance(self, *, successful: int = 0, failed: int = 0) -> None:
        increment = successful + failed
        if increment < 1:
            raise ValueError("progress advance requires at least one completed operation")
        self._phase_completed += increment
        self._phase_successful += successful
        self._phase_failed += failed
        phase = self.phases[self._phase_index]
        if self._phase_completed > phase.total:
            raise ValueError("phase progress exceeds its deterministic total")
        milestone = max(1, math.ceil(max(phase.total, 1) / 20))
        force = self._phase_completed == phase.total
        if force or self._phase_completed - self._last_emitted_completed >= milestone:
            self._emit(force=force)

    def complete_empty(self) -> None:
        phase = self.phases[self._phase_index]
        if phase.total != 0:
            raise ValueError("only an empty phase can be completed without an operation")
        self._emit(force=True)

    def _emit(self, *, force: bool) -> None:
        phase = self.phases[self._phase_index]
        if not force and self._last_emitted_completed == self._phase_completed:
            return
        self._last_emitted_completed = self._phase_completed
        phase_fraction = (
            1.0 if phase.total == 0 else self._phase_completed / phase.total
        )
        overall = (self._phase_index + phase_fraction) / max(len(self.phases), 1)
        description = (
            f"{phase.name}: {self._phase_completed}/{phase.total}; "
            f"successful: {self._phase_successful}, failed: {self._phase_failed}"
        )
        self.reporter.update(description=description, progress=overall)
        self.context.emit_event(
            COMPARATIVE_ANALYSIS_PROGRESS_EVENT,
            {
                "phase_index": self._phase_index + 1,
                "phase_total": len(self.phases),
                "operation_kind": phase.operation_kind,
                "completed": self._phase_completed,
                "total": phase.total,
                "successful": self._phase_successful,
                "failed": self._phase_failed,
                "detail": description,
            },
        )


@dataclass(slots=True)
class _FailureAccumulator:
    context: StageContext
    writer: JsonlArtifactWriter
    count: int = 0

    def record(
        self,
        *,
        category: str,
        error_code: str,
        detail: str,
        phase: str,
        metric_id: str | None = None,
        computation_index: int | None = None,
        affected_logical_result_count: int = 1,
        sample_ids: Sequence[str] = tuple(),
    ) -> str:
        self.count += 1
        failure_id = f"failure-{self.count:06d}"
        record = ComparativeFailureRecord(
            failure_id=failure_id,
            category=category,
            error_code=error_code,
            detail=detail,
            phase=phase,
            metric_id=metric_id,
            computation_index=computation_index,
            affected_logical_result_count=affected_logical_result_count,
            sample_ids=tuple(dict.fromkeys(sample_ids))[:4],
        )
        self.writer.write_model(record)
        self.context.emit_event(
            COMPARATIVE_ANALYSIS_OPERATION_FAILED_EVENT,
            {
                "failure_id": failure_id,
                "category": category,
                "error_code": error_code,
                "operation_kind": phase,
                "affected_logical_result_count": affected_logical_result_count,
                "detail": detail,
            },
        )
        return failure_id


def _empty_plan_counts() -> ComparisonPlanCounts:
    return ComparisonPlanCounts(
        occurrence_count=0,
        unique_logical_operation_count=0,
        duplicate_occurrence_count=0,
        scan_computation_count=0,
        identical_sequence_projection_count=0,
    )


def _load_resolved_config(path: Path) -> ResolvedAnalysisConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ComparativeAnalysisStageError(
            reason="comparative_analysis_config_unreadable",
            detail="Immutable analysis configuration could not be read.",
        ) from error
    if not isinstance(payload, dict):
        raise ComparativeAnalysisStageError(
            reason="comparative_analysis_config_invalid",
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
        raise ComparativeAnalysisStageError(
            reason="comparative_analysis_config_invalid",
            detail="Immutable analysis configuration is invalid for comparative analysis.",
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
            # A preceding stage can move from worker staging to committed storage
            # while this stage is opening it. Try the other canonical root.
            continue
        except Exception as error:
            raise ComparativeAnalysisStageError(
                reason="input_processing_manifest_invalid",
                detail="Published input-processing manifest is invalid.",
            ) from error
        if manifest.stage_id != INPUT_PROCESSING_STAGE_ID:
            raise ComparativeAnalysisStageError(
                reason="input_processing_manifest_invalid",
                detail="Published input-processing manifest has an invalid stage identity.",
            )
        if manifest.processing_state is not InputProcessingState.COMPLETED:
            raise ComparativeAnalysisStageError(
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
    raise ComparativeAnalysisStageError(
        reason="input_processing_manifest_missing",
        detail="Published input-processing manifest is missing for comparative analysis.",
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
        raise ComparativeAnalysisStageError(
            reason="comparative_analysis_upstream_identity_mismatch",
            detail=f"The {source_name} identity does not match this job.",
        )
    if config_hash != context.launch_spec.config_hash:
        raise ComparativeAnalysisStageError(
            reason="comparative_analysis_upstream_config_mismatch",
            detail=f"The {source_name} configuration hash does not match this job.",
        )


def _validate_relative_artifact_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if normalized == "" or posix.is_absolute() or windows.is_absolute():
        raise ComparativeAnalysisStageError(
            reason="comparative_analysis_artifact_path_invalid",
            detail="An upstream artifact path is not a safe relative path.",
        )
    if ".." in posix.parts or ".." in windows.parts:
        raise ComparativeAnalysisStageError(
            reason="comparative_analysis_artifact_path_invalid",
            detail="An upstream artifact path escapes its stage directory.",
        )
    return posix.as_posix()


def _find_artifact(
    *,
    candidate_roots: Sequence[Path],
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
    raise ComparativeAnalysisStageError(
        reason="comparative_analysis_artifact_missing",
        detail="An upstream artifact required for comparative analysis is missing.",
        context={"relative_path": normalized},
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ComparativeAnalysisStageError(
            reason="comparative_analysis_artifact_unreadable",
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
                    raise ComparativeAnalysisStageError(
                        reason="alignment_manifest_invalid",
                        detail="Published alignment manifest is invalid.",
                    ) from error
                root = committed_root
            else:
                continue
        except Exception as error:
            raise ComparativeAnalysisStageError(
                reason="alignment_manifest_invalid",
                detail="Published alignment manifest is invalid.",
            ) from error
        selected_root = root
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        break
    if manifest is None or selected_root is None or manifest_sha256 is None:
        raise ComparativeAnalysisStageError(
            reason="alignment_manifest_missing",
            detail="Sequence differences require a published alignment manifest.",
        )
    if (
        manifest.schema_version != ALIGNMENT_MANIFEST_SCHEMA_VERSION
        or manifest.stage_id != ALIGNMENT_STAGE_ID
        or manifest.mode is not expected_mode
    ):
        raise ComparativeAnalysisStageError(
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
        raise ComparativeAnalysisStageError(
            reason="alignment_result_unavailable",
            detail="Sequence differences require an available canonical alignment.",
        )
    if (
        manifest.aligned_fasta_path is None
        or manifest.alignment_length is None
        or manifest.alignment_length < 1
        or manifest.result_sha256 is None
    ):
        raise ComparativeAnalysisStageError(
            reason="alignment_manifest_incomplete",
            detail="Published alignment metadata is incomplete for sequence differences.",
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
        raise ComparativeAnalysisStageError(
            reason="alignment_snapshot_manifest_changed",
            detail="Published alignment metadata changed during a controlled read retry.",
        )


def _alignment_snapshot_unstable(error: BaseException) -> ComparativeAnalysisStageError:
    return ComparativeAnalysisStageError(
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
    except ComparativeAnalysisStageError as error:
        if error.reason == "comparative_analysis_artifact_missing":
            _signal_alignment_snapshot_move(snapshot=snapshot, error=error)
        raise
    try:
        aligned_sha256 = _sha256_file(aligned_path)
    except ComparativeAnalysisStageError as error:
        if error.reason == "comparative_analysis_artifact_unreadable":
            _signal_alignment_snapshot_move(snapshot=snapshot, error=error)
        raise
    if aligned_sha256 != manifest.result_sha256:
        raise ComparativeAnalysisStageError(
            reason="alignment_result_hash_mismatch",
            detail="Canonical alignment content does not match its published digest.",
        )
    try:
        parsed_rows = parse_aligned_fasta(path=str(aligned_path))
    except Exception as error:
        if _caused_by_os_error(error):
            _signal_alignment_snapshot_move(snapshot=snapshot, error=error)
        raise ComparativeAnalysisStageError(
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
        raise ComparativeAnalysisStageError(
            reason="alignment_sample_set_mismatch",
            detail="Canonical alignment sample counts are inconsistent with input processing.",
        )

    rows_by_sample_id: dict[str, CanonicalAlignmentRow] = {}
    rows_by_sequence_id: dict[str, CanonicalAlignmentRow] = {}
    for sample_id, aligned_sequence in parsed_rows:
        sample = sample_catalog.get(sample_id)
        if sample is None or sample.sequence_id is None or sample_id in rows_by_sample_id:
            raise ComparativeAnalysisStageError(
                reason="alignment_sample_set_mismatch",
                detail="Canonical alignment sample identities are inconsistent.",
            )
        if len(aligned_sequence) != manifest.alignment_length:
            raise ComparativeAnalysisStageError(
                reason="alignment_length_mismatch",
                detail="Canonical alignment rows do not match the published alignment length.",
            )
        if any(symbol not in ALIGNED_NUCLEOTIDE_SYMBOLS for symbol in aligned_sequence):
            raise ComparativeAnalysisStageError(
                reason="alignment_symbol_invalid",
                detail="Canonical alignment contains a symbol outside the accepted alphabet.",
            )
        row = CanonicalAlignmentRow(
            sample_id=sample_id,
            sequence_id=sample.sequence_id,
            aligned_sequence=aligned_sequence,
        )
        previous = rows_by_sequence_id.get(sample.sequence_id)
        if previous is not None and previous.aligned_sequence != aligned_sequence:
            raise ComparativeAnalysisStageError(
                reason="alignment_identical_sequence_mismatch",
                detail="Canonical alignment rows sharing one sequence identity are inconsistent.",
            )
        rows_by_sample_id[sample_id] = row
        rows_by_sequence_id.setdefault(sample.sequence_id, row)
    if set(rows_by_sample_id) != set(sample_catalog):
        raise ComparativeAnalysisStageError(
            reason="alignment_sample_set_mismatch",
            detail="Canonical alignment does not contain the expected logical sample set.",
        )
    if manifest.unique_sequence_count != len(rows_by_sequence_id):
        raise ComparativeAnalysisStageError(
            reason="alignment_sequence_set_mismatch",
            detail="Canonical alignment sequence counts are inconsistent with input processing.",
        )
    published_sequence_ids = tuple(
        item.sequence_id for item in input_manifest.unique_sequences
    )
    if (
        len(set(published_sequence_ids)) != len(published_sequence_ids)
        or set(published_sequence_ids) != set(rows_by_sequence_id)
        or input_manifest.dataset_summary.unique_sequence_count
        != len(published_sequence_ids)
    ):
        raise ComparativeAnalysisStageError(
            reason="alignment_sequence_set_mismatch",
            detail=(
                "Canonical alignment sequence identities are inconsistent with "
                "input processing."
            ),
        )
    return _AlignmentArtifacts(
        candidate_roots=snapshot.candidate_roots,
        selected_root=snapshot.selected_root,
        staging_root=snapshot.staging_root,
        manifest_sha256=snapshot.manifest_sha256,
        manifest=manifest,
        rows_by_sample_id=rows_by_sample_id,
        rows_by_sequence_id=rows_by_sequence_id,
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


def _load_reference_coordinate_map_once(
    *,
    alignment: _AlignmentArtifacts,
    input_manifest: InputProcessingManifest,
) -> _ReferenceCoordinateLookup:
    resolved_reference = input_manifest.resolved_reference
    relative_path = alignment.manifest.reference_coordinate_map_path
    if resolved_reference is None or relative_path is None:
        raise ComparativeAnalysisStageError(
            reason="reference_coordinate_map_missing",
            detail="Reference sequence comparisons require a published coordinate map.",
        )
    snapshot = _AlignmentSnapshot(
        candidate_roots=alignment.candidate_roots,
        selected_root=alignment.selected_root,
        staging_root=alignment.staging_root,
        manifest_sha256=alignment.manifest_sha256,
        manifest=alignment.manifest,
    )
    try:
        path = _find_artifact(
            candidate_roots=alignment.candidate_roots,
            selected_root=alignment.selected_root,
            relative_path=relative_path,
            snapshot_manifest_relative_path=ALIGNMENT_MANIFEST_RELATIVE_PATH,
            snapshot_manifest_sha256=alignment.manifest_sha256,
        )
    except ComparativeAnalysisStageError as error:
        if error.reason == "comparative_analysis_artifact_missing":
            _signal_alignment_snapshot_move(snapshot=snapshot, error=error)
        raise
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as error:
        _signal_alignment_snapshot_move(snapshot=snapshot, error=error)
        raise ComparativeAnalysisStageError(
            reason="reference_coordinate_map_invalid",
            detail="Published reference coordinate map is invalid.",
        ) from error
    try:
        raw_payload = json.loads(raw_text)
        coordinate_map = ReferenceCoordinateMap.model_validate(raw_payload)
    except Exception as error:
        raise ComparativeAnalysisStageError(
            reason="reference_coordinate_map_invalid",
            detail="Published reference coordinate map is invalid.",
        ) from error
    if (
        coordinate_map.schema_version != 1
        or coordinate_map.coordinate_system != "one_based_reference_positions"
        or coordinate_map.alignment_length != alignment.manifest.alignment_length
        or coordinate_map.reference_sample_id != resolved_reference.sample_id
        or coordinate_map.reference_sequence_id != resolved_reference.sequence_id
        or alignment.manifest.reference_sample_id != resolved_reference.sample_id
        or alignment.manifest.reference_sequence_id != resolved_reference.sequence_id
    ):
        raise ComparativeAnalysisStageError(
            reason="reference_coordinate_map_mismatch",
            detail=(
                "Published reference coordinate map is inconsistent with the "
                "resolved reference."
            ),
        )
    reference_row = alignment.rows_by_sample_id.get(resolved_reference.sample_id)
    if (
        reference_row is None
        or reference_row.sequence_id != resolved_reference.sequence_id
        or len(reference_row.aligned_sequence) != coordinate_map.alignment_length
    ):
        raise ComparativeAnalysisStageError(
            reason="reference_coordinate_map_mismatch",
            detail="The resolved reference row is inconsistent with its coordinate map.",
        )
    expected_position = 0
    for symbol, position in zip(
        reference_row.aligned_sequence,
        coordinate_map.reference_positions,
        strict=True,
    ):
        if symbol == "-":
            if position is not None:
                raise ComparativeAnalysisStageError(
                    reason="reference_coordinate_map_gap_mismatch",
                    detail="Reference gaps are inconsistent with the published coordinate map.",
                )
            continue
        expected_position += 1
        if position != expected_position:
            raise ComparativeAnalysisStageError(
                reason="reference_coordinate_map_position_mismatch",
                detail="Reference positions are inconsistent with the canonical reference row.",
            )
    nearest_left_values: list[int | None] = [None]
    current_left: int | None = None
    for position in coordinate_map.reference_positions:
        if position is not None:
            current_left = position
        nearest_left_values.append(current_left)
    nearest_right_values: list[int | None] = [None] * (
        coordinate_map.alignment_length + 1
    )
    current_right: int | None = None
    for index in range(coordinate_map.alignment_length - 1, -1, -1):
        position = coordinate_map.reference_positions[index]
        if position is not None:
            current_right = position
        nearest_right_values[index] = current_right
    return _ReferenceCoordinateLookup(
        coordinate_map=coordinate_map,
        nearest_left=tuple(nearest_left_values),
        nearest_right=tuple(nearest_right_values),
    )


def _load_reference_coordinate_map(
    *,
    context: StageContext,
    alignment: _AlignmentArtifacts,
    input_manifest: InputProcessingManifest,
    expected_mode: AnalysisAlignmentMode,
) -> tuple[_AlignmentArtifacts, _ReferenceCoordinateLookup]:
    try:
        return alignment, _load_reference_coordinate_map_once(
            alignment=alignment,
            input_manifest=input_manifest,
        )
    except _AlignmentSnapshotMoved:
        retried_alignment = _reload_alignment_artifacts_once(
            context=context,
            input_manifest=input_manifest,
            expected_mode=expected_mode,
            expected_manifest_sha256=alignment.manifest_sha256,
        )
        try:
            coordinate_lookup = _load_reference_coordinate_map_once(
                alignment=retried_alignment,
                input_manifest=input_manifest,
            )
        except _AlignmentSnapshotMoved as error:
            raise _alignment_snapshot_unstable(error) from error
        return retried_alignment, coordinate_lookup


def _facts_by_sequence_id(manifest: InputProcessingManifest) -> dict[str, SequenceFacts]:
    facts_by_sequence_id: dict[str, SequenceFacts] = {}
    for item in manifest.unique_sequences:
        if item.sequence_id in facts_by_sequence_id:
            raise ComparativeAnalysisStageError(
                reason="input_processing_sequence_set_invalid",
                detail="Published input-processing sequence identities are not unique.",
            )
        facts_by_sequence_id[item.sequence_id] = item.facts
    return facts_by_sequence_id


def _logical_facts(
    *,
    plan: ComparisonPlan,
    facts_by_sequence_id: Mapping[str, SequenceFacts],
) -> tuple[SequenceFacts | None, ...]:
    return tuple(facts_by_sequence_id.get(sample.sequence_id) for sample in plan.samples)


def _build_statistical_metrics(
    *,
    facts_values: Sequence[SequenceFacts | None],
) -> tuple[_StatisticalMetric, ...]:
    metrics: list[_StatisticalMetric] = []
    scalar_index = 0
    for field_name in SequenceFacts.model_fields:
        dataset_path: tuple[str, ...] | None = None
        comparison_path: tuple[str, ...] | None = None
        if field_name in SequenceCountComparisons.model_fields:
            dataset_path = ("numeric", field_name)
            comparison_path = ("counts", field_name)
        elif field_name == "expected_gc_count":
            dataset_path = ("numeric", field_name)
            comparison_path = (field_name,)
        elif field_name in SequenceProportionComparisons.model_fields:
            dataset_path = ("numeric", field_name)
            comparison_path = ("proportions", field_name)
        elif field_name in {
            "symbol_counts",
            "invalid_symbol_counts",
            "invalid_positions_truncated",
        }:
            dataset_path = (field_name,)
            comparison_path = (field_name,)
        if dataset_path is None or comparison_path is None:
            continue
        scalar_index += 1
        metrics.append(
            _StatisticalMetric(
                metric_id=f"metric-{scalar_index:03d}",
                metric_name=field_name,
                dataset_path=dataset_path,
                comparison_path=comparison_path,
            )
        )
    for component in ("definite", "potential"):
        scalar_index += 1
        metrics.append(
            _StatisticalMetric(
                metric_id=f"metric-{scalar_index:03d}",
                metric_name=f"base_counts.{component}",
                dataset_path=("base_counts", component),
                comparison_path=("base_counts", component),
            )
        )
    queries: list[str] = []
    seen_queries: set[str] = set()
    for facts in facts_values:
        if facts is None:
            continue
        for summary in facts.kmer_summaries:
            if summary.query in seen_queries:
                continue
            seen_queries.add(summary.query)
            queries.append(summary.query)
    for query in queries:
        scalar_index += 1
        metrics.append(
            _StatisticalMetric(
                metric_id=f"metric-{scalar_index:03d}",
                metric_name="kmer_summary",
                dataset_path=("kmer_summaries",),
                comparison_path=("kmer_summaries",),
                kmer_query=query,
            )
        )
    return tuple(metrics)


def _extract_model_value(
    payload: Mapping[str, Any],
    *,
    path: tuple[str, ...],
    kmer_query: str | None,
) -> Any:
    value: Any = payload
    for segment in path:
        if not isinstance(value, Mapping) or segment not in value:
            raise KeyError(segment)
        value = value[segment]
    if kmer_query is None:
        return value
    if not isinstance(value, list):
        raise TypeError("k-mer metric payload must be a list")
    for item in value:
        if isinstance(item, Mapping) and item.get("query") == kmer_query:
            return dict(item)
    return None


class _CachedComputationFailure(RuntimeError):
    def __init__(
        self,
        *,
        key: str,
        computation_index: int | None,
        sequence_id: str | None = None,
    ) -> None:
        self.key = key
        self.computation_index = computation_index
        self.sequence_id = sequence_id
        super().__init__("a cached comparative computation failed")


@dataclass(slots=True)
class _SequenceComparisonCache:
    plan: ComparisonPlan
    alignment: _AlignmentArtifacts
    comparator: AlignedSequenceComparator
    projector: DirectedComparisonProjector
    scheduled_operations: tuple[DirectedLogicalComparison, ...]
    uracil_thymine_equivalent: bool = False
    _computations_by_index: dict[int, SequenceComparisonComputation] = field(
        init=False
    )
    _remaining_by_computation: dict[int, int] = field(init=False)
    _remaining_by_identical_sequence: dict[str, int] = field(init=False)
    _comparison_cache: dict[int, DirectedAlignedComparison] = field(default_factory=dict)
    _identical_cache: dict[str, DirectedAlignedComparison] = field(default_factory=dict)
    _failed_computations: set[int] = field(default_factory=set)
    _failed_identical_sequences: set[str] = field(default_factory=set)
    attempted_physical_scan_count: int = 0
    successful_physical_scan_count: int = 0
    failed_physical_scan_count: int = 0
    attempted_identical_profile_count: int = 0
    successful_identical_profile_count: int = 0
    failed_identical_profile_count: int = 0
    reused_projection_count: int = 0

    def __post_init__(self) -> None:
        self._computations_by_index = {
            item.computation_index: item for item in self.plan.computations
        }
        remaining_computations: dict[int, int] = {}
        remaining_identical: dict[str, int] = {}
        for operation in self.scheduled_operations:
            if operation.computation_index is not None:
                remaining_computations[operation.computation_index] = (
                    remaining_computations.get(operation.computation_index, 0) + 1
                )
            else:
                remaining_identical[operation.left_sequence_id] = (
                    remaining_identical.get(operation.left_sequence_id, 0) + 1
                )
        self._remaining_by_computation = remaining_computations
        self._remaining_by_identical_sequence = remaining_identical

    def resolve(
        self,
        operation: DirectedLogicalComparison,
    ) -> tuple[DirectedAlignedComparison, bool]:
        left_identity = ComparisonIdentity(
            sample_id=operation.left_sample_id,
            sequence_id=operation.left_sequence_id,
        )
        right_identity = ComparisonIdentity(
            sample_id=operation.right_sample_id,
            sequence_id=operation.right_sequence_id,
        )
        if operation.computation_index is None:
            key = operation.left_sequence_id
            if key in self._failed_identical_sequences:
                self.reused_projection_count += 1
                self._consume_identical(key)
                raise _CachedComputationFailure(
                    key=f"identical:{key}",
                    computation_index=None,
                    sequence_id=key,
                )
            base = self._identical_cache.get(key)
            was_cached = base is not None
            if base is None:
                row = self.alignment.rows_by_sequence_id.get(key)
                if row is None:
                    self._failed_identical_sequences.add(key)
                    self._consume_identical(key)
                    raise _CachedComputationFailure(
                        key=f"identical:{key}",
                        computation_index=None,
                        sequence_id=key,
                    )
                self.attempted_identical_profile_count += 1
                try:
                    base = self.comparator.compare(
                        left_aligned_sequence=row.aligned_sequence,
                        right_aligned_sequence=row.aligned_sequence,
                        left_identity=ComparisonIdentity(
                            sample_id=row.sample_id,
                            sequence_id=row.sequence_id,
                        ),
                        right_identity=ComparisonIdentity(
                            sample_id=row.sample_id,
                            sequence_id=row.sequence_id,
                        ),
                        uracil_thymine_equivalent=self.uracil_thymine_equivalent,
                    )
                except Exception as error:
                    self._failed_identical_sequences.add(key)
                    self.failed_identical_profile_count += 1
                    self._consume_identical(key)
                    raise _CachedComputationFailure(
                        key=f"identical:{key}",
                        computation_index=None,
                        sequence_id=key,
                    ) from error
                self.successful_identical_profile_count += 1
                self._identical_cache[key] = base
            elif was_cached:
                self.reused_projection_count += 1
            projected = self.projector.project(
                base,
                left_identity=left_identity,
                right_identity=right_identity,
            )
            self._consume_identical(key)
            return projected, was_cached

        index = operation.computation_index
        if index in self._failed_computations:
            self.reused_projection_count += 1
            self._consume_computation(index)
            raise _CachedComputationFailure(
                key=f"physical:{index}",
                computation_index=index,
            )
        base = self._comparison_cache.get(index)
        was_cached = base is not None
        if base is None:
            computation = self._computations_by_index.get(index)
            if computation is None:
                self._failed_computations.add(index)
                self._consume_computation(index)
                raise _CachedComputationFailure(
                    key=f"physical:{index}",
                    computation_index=index,
                )
            first = self.alignment.rows_by_sequence_id.get(computation.first_sequence_id)
            second = self.alignment.rows_by_sequence_id.get(computation.second_sequence_id)
            if first is None or second is None:
                self._failed_computations.add(index)
                self._consume_computation(index)
                raise _CachedComputationFailure(
                    key=f"physical:{index}",
                    computation_index=index,
                )
            self.attempted_physical_scan_count += 1
            try:
                base = self.comparator.compare(
                    left_aligned_sequence=first.aligned_sequence,
                    right_aligned_sequence=second.aligned_sequence,
                    left_identity=ComparisonIdentity(
                        sample_id=first.sample_id,
                        sequence_id=first.sequence_id,
                    ),
                    right_identity=ComparisonIdentity(
                        sample_id=second.sample_id,
                        sequence_id=second.sequence_id,
                    ),
                    uracil_thymine_equivalent=self.uracil_thymine_equivalent,
                )
            except Exception as error:
                self._failed_computations.add(index)
                self.failed_physical_scan_count += 1
                self._consume_computation(index)
                raise _CachedComputationFailure(
                    key=f"physical:{index}",
                    computation_index=index,
                ) from error
            self.successful_physical_scan_count += 1
            self._comparison_cache[index] = base
        elif was_cached:
            self.reused_projection_count += 1
        projected = self.projector.project(
            base,
            left_identity=left_identity,
            right_identity=right_identity,
            reverse=operation.reverse_computation,
        )
        self._consume_computation(index)
        computation = self._computations_by_index[index]
        return projected, was_cached or computation.logical_projection_count > 1

    def _consume_computation(self, index: int) -> None:
        remaining = self._remaining_by_computation.get(index, 0) - 1
        if remaining <= 0:
            self._remaining_by_computation.pop(index, None)
            self._comparison_cache.pop(index, None)
        else:
            self._remaining_by_computation[index] = remaining

    def _consume_identical(self, sequence_id: str) -> None:
        remaining = self._remaining_by_identical_sequence.get(sequence_id, 0) - 1
        if remaining <= 0:
            self._remaining_by_identical_sequence.pop(sequence_id, None)
            self._identical_cache.pop(sequence_id, None)
        else:
            self._remaining_by_identical_sequence[sequence_id] = remaining


def _ordered_sequence_operations(
    operations: Sequence[DirectedLogicalComparison],
) -> tuple[DirectedLogicalComparison, ...]:
    """Group dependent projections so all-vs-all base results stay bounded."""

    indexed = tuple(enumerate(operations))

    def operation_key(
        item: tuple[int, DirectedLogicalComparison],
    ) -> tuple[int, int | str, int]:
        original_index, operation = item
        if operation.computation_index is None:
            return (1, operation.left_sequence_id, original_index)
        return (0, operation.computation_index, original_index)

    return tuple(operation for _index, operation in sorted(indexed, key=operation_key))


def _requested_difference_types(
    config: ResolvedAnalysisConfig,
) -> tuple[DifferenceEventType, ...]:
    differences = config.comparative_analysis.sequence_differences
    requested: list[DifferenceEventType] = []
    if differences.substitutions:
        requested.append(DifferenceEventType.SUBSTITUTION)
    if differences.insertions:
        requested.append(DifferenceEventType.INSERTION)
    if differences.deletions:
        requested.append(DifferenceEventType.DELETION)
    return tuple(requested)


def _event_is_requested(
    event: AlignedDifferenceEvent,
    *,
    requested: tuple[DifferenceEventType, ...],
) -> bool:
    return event.type is DifferenceEventType.UNCERTAIN or event.type in requested


def _filtered_summary(
    comparison: DirectedAlignedComparison,
    *,
    requested: tuple[DifferenceEventType, ...],
) -> PublishedSequenceComparisonSummary:
    source = comparison.summary

    def category(
        event_type: DifferenceEventType,
        *,
        event_count: int,
        base_count: int,
    ) -> RequestedDifferenceCategorySummary:
        is_requested = event_type in requested
        return RequestedDifferenceCategorySummary(
            requested=is_requested,
            event_count=event_count if is_requested else None,
            base_count=base_count if is_requested else None,
        )

    return PublishedSequenceComparisonSummary(
        msa_column_count=source.msa_column_count,
        both_gap_column_count=source.both_gap_column_count,
        comparable_base_count=source.comparable_base_count,
        matching_base_count=source.matching_base_count,
        substitutions=category(
            DifferenceEventType.SUBSTITUTION,
            event_count=source.substitution_event_count,
            base_count=source.substituted_base_count,
        ),
        insertions=category(
            DifferenceEventType.INSERTION,
            event_count=source.insertion_event_count,
            base_count=source.inserted_base_count,
        ),
        deletions=category(
            DifferenceEventType.DELETION,
            event_count=source.deletion_event_count,
            base_count=source.deleted_base_count,
        ),
        uncertain_event_count=source.uncertain_event_count,
        uncertain_column_count=source.uncertain_column_count,
        identity_on_comparable_bases=source.identity_on_comparable_bases,
    )


def _reference_coordinates_for_event(
    *,
    event: AlignedDifferenceEvent,
    lookup: _ReferenceCoordinateLookup,
) -> dict[str, int | None]:
    coordinate_map = lookup.coordinate_map
    start = event.msa_column_start - 1
    stop = event.msa_column_end
    if stop > len(coordinate_map.reference_positions):
        raise ValueError("event span exceeds reference coordinate map")
    positions = coordinate_map.reference_positions[start:stop]
    present = [position for position in positions if position is not None]
    if not present:
        if event.type not in {
            DifferenceEventType.INSERTION,
            DifferenceEventType.UNCERTAIN,
        }:
            raise ValueError("reference event has no mapped reference position")
        if any(position is not None for position in positions):
            raise ValueError("reference gap event contains a mapped reference position")
        after = lookup.nearest_left[start]
        before = lookup.nearest_right[stop]
        if event.after_left_position != after or event.before_left_position != before:
            raise ValueError("reference insertion anchors are inconsistent")
        return {
            "reference_start": None,
            "reference_end": None,
            "after_reference_position": after,
            "before_reference_position": before,
        }
    return {
        "reference_start": present[0] if present else None,
        "reference_end": present[-1] if present else None,
        "after_reference_position": None,
        "before_reference_position": None,
    }


def _failure_code(error: BaseException, *, fallback: str) -> str:
    if isinstance(error, ComparisonDomainError):
        return error.code.value
    if isinstance(error, ComparativeAnalysisStageError):
        return error.reason
    return fallback


def _summary_record(
    *,
    operation: DirectedLogicalComparison,
    status: ComparativeResultStatus,
    requested: tuple[DifferenceEventType, ...],
    comparison: DirectedAlignedComparison | None = None,
    failure_id: str | None = None,
    reused: bool = False,
) -> SequenceComparisonSummaryRecord:
    return SequenceComparisonSummaryRecord(
        left=ComparisonIdentity(
            sample_id=operation.left_sample_id,
            sequence_id=operation.left_sequence_id,
        ),
        right=ComparisonIdentity(
            sample_id=operation.right_sample_id,
            sequence_id=operation.right_sequence_id,
        ),
        source_kinds=operation.source_kinds,
        source_occurrence_count=operation.source_occurrence_count,
        status=status,
        failure_id=failure_id,
        computation_index=operation.computation_index,
        reverse_projection=operation.reverse_computation,
        reused_physical_computation=reused,
        identical_sequence_shortcut=(
            comparison.identical_sequence_shortcut
            if comparison is not None
            else not operation.requires_scan
        ),
        requested_categories=requested,
        summary=(
            _filtered_summary(comparison, requested=requested)
            if comparison is not None
            else None
        ),
    )


def _write_summary_record(
    writer: JsonlArtifactWriter,
    record: SequenceComparisonSummaryRecord,
) -> None:
    writer.write(record.model_dump(mode="json", exclude_none=True))


def _write_json_model(
    *,
    path: Path,
    model: BaseModel,
    relative_path: str,
) -> ComparativeArtifactMetadata:
    payload = model.model_dump(mode="json")
    type(model).model_validate(payload)
    write_text_atomically(
        path=path,
        payload=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return artifact_metadata(path, relative_path=relative_path)


def _statistical_raw_record(
    *,
    operation: DirectedLogicalComparison,
    comparison: SequenceFactsComparison,
) -> dict[str, Any]:
    return {
        "left": {
            "sample_id": operation.left_sample_id,
            "sequence_id": operation.left_sequence_id,
        },
        "right": {
            "sample_id": operation.right_sample_id,
            "sequence_id": operation.right_sequence_id,
        },
        "source_kinds": [kind.value for kind in operation.source_kinds],
        "source_occurrence_count": operation.source_occurrence_count,
        "comparison": comparison.model_dump(mode="json"),
    }


def _run_statistics_phase(
    *,
    context: StageContext,
    root: Path,
    plan: ComparisonPlan,
    facts_values: tuple[SequenceFacts | None, ...],
    facts_by_sequence_id: Mapping[str, SequenceFacts],
    metrics: tuple[_StatisticalMetric, ...],
    summarizer: DatasetStatisticalSummarizer,
    comparator: StatisticalComparator,
    metric_guard: Callable[[str], None] | None,
    failures: _FailureAccumulator,
    progress: _ProgressTracker,
    phase_index: int,
) -> tuple[_CategoryCounter, tuple[ComparativeArtifactMetadata, ...]]:
    counter = _CategoryCounter(
        requested=True,
        total=len(metrics),
        artifact_paths=(
            DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
            STATISTICAL_DIFFERENCES_RELATIVE_PATH,
        ),
    )
    progress.begin(phase_index)
    raw_path = root / _STATISTICS_RAW_RELATIVE_PATH
    raw_writer = JsonlArtifactWriter(raw_path, relative_path=_STATISTICS_RAW_RELATIVE_PATH)
    dataset_summary: SequenceFactsDatasetSummary | None = None
    try:
        dataset_summary = summarizer.summarize(facts_values)
    except Exception as error:
        error_code = _failure_code(error, fallback="STATISTICS_DATASET_SUMMARY_FAILED")
        raw_writer.abort()
        for metric in metrics:
            context.check_control()
            failures.record(
                category="statistics",
                error_code=error_code,
                detail="A statistical metric could not be produced from published facts.",
                phase="statistics_metric",
                metric_id=metric.metric_id,
            )
            counter.failed += 1
            progress.advance(failed=1)
        empty_differences = JsonlArtifactWriter(
            root / STATISTICAL_DIFFERENCES_RELATIVE_PATH,
            relative_path=STATISTICAL_DIFFERENCES_RELATIVE_PATH,
        ).close()
        dataset_artifact = StatisticalDatasetArtifact(
            sample_count=len(facts_values),
            metric_total=len(metrics),
            metric_successful=0,
            metric_failed=len(metrics),
            metrics={},
        )
        dataset_metadata = _write_json_model(
            path=root / DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
            model=dataset_artifact,
            relative_path=DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
        )
        return counter, (dataset_metadata, empty_differences)

    sample_by_id = {sample.sample_id: sample for sample in plan.samples}
    logical_comparison_failure_count = 0
    for operation in plan.logical_operations:
        context.check_control()
        if (
            operation.left_sample_id not in sample_by_id
            or operation.right_sample_id not in sample_by_id
        ):
            failures.record(
                category="statistics",
                error_code="STATISTICS_PLAN_IDENTITY_MISMATCH",
                detail="A statistical comparison identity is absent from the plan catalog.",
                phase="statistics_comparison",
                sample_ids=(operation.left_sample_id, operation.right_sample_id),
            )
            logical_comparison_failure_count += 1
            continue
        left = facts_by_sequence_id.get(operation.left_sequence_id)
        right = facts_by_sequence_id.get(operation.right_sequence_id)
        try:
            comparison = comparator.compare(left, right)
        except Exception as error:
            failures.record(
                category="statistics",
                error_code=_failure_code(
                    error,
                    fallback="STATISTICS_LOGICAL_COMPARISON_FAILED",
                ),
                detail="A statistical logical comparison could not be produced.",
                phase="statistics_comparison",
                sample_ids=(operation.left_sample_id, operation.right_sample_id),
            )
            logical_comparison_failure_count += 1
            continue
        raw_writer.write(
            _statistical_raw_record(operation=operation, comparison=comparison)
        )
    raw_writer.close()

    dataset_payload = dataset_summary.model_dump(mode="json")
    final_writer = JsonlArtifactWriter(
        root / STATISTICAL_DIFFERENCES_RELATIVE_PATH,
        relative_path=STATISTICAL_DIFFERENCES_RELATIVE_PATH,
    )
    published_metrics: dict[str, Any] = {}
    try:
        for metric in metrics:
            context.check_control()
            metric_path = root / "comparative_analysis" / f".{metric.metric_id}.jsonl"
            metric_writer = JsonlArtifactWriter(
                metric_path,
                relative_path=f"comparative_analysis/.{metric.metric_id}.jsonl",
            )
            try:
                if metric_guard is not None:
                    metric_guard(metric.metric_id)
                if logical_comparison_failure_count:
                    raise ValueError(
                        "statistical logical comparison results are incomplete"
                    )
                dataset_value = _extract_model_value(
                    dataset_payload,
                    path=metric.dataset_path,
                    kmer_query=metric.kmer_query,
                )
                with raw_path.open("r", encoding="utf-8") as raw_handle:
                    for line in raw_handle:
                        raw = json.loads(line)
                        if not isinstance(raw, dict):
                            raise TypeError("statistics raw record must be an object")
                        comparison_payload = raw.get("comparison")
                        if not isinstance(comparison_payload, dict):
                            raise TypeError("statistics raw comparison must be an object")
                        value = _extract_model_value(
                            comparison_payload,
                            path=metric.comparison_path,
                            kmer_query=metric.kmer_query,
                        )
                        left = raw.get("left")
                        right = raw.get("right")
                        source_kinds = raw.get("source_kinds")
                        if (
                            not isinstance(left, dict)
                            or not isinstance(right, dict)
                            or not isinstance(source_kinds, list)
                        ):
                            raise TypeError("statistics raw identity is invalid")
                        record = StatisticalDifferenceRecord(
                            metric_id=metric.metric_id,
                            metric_name=metric.metric_name,
                            left=ComparisonIdentity.model_validate(left),
                            right=ComparisonIdentity.model_validate(right),
                            source_kinds=tuple(
                                ComparisonSourceKind(item) for item in source_kinds
                            ),
                            source_occurrence_count=int(
                                raw.get("source_occurrence_count", 0)
                            ),
                            value=value,
                        )
                        metric_writer.write_model(record)
                metric_writer.close()
                with metric_path.open("r", encoding="utf-8") as metric_handle:
                    for line in metric_handle:
                        parsed = json.loads(line)
                        if not isinstance(parsed, dict):
                            raise TypeError("statistics metric record must be an object")
                        final_writer.write(parsed)
                metric_path.unlink(missing_ok=True)
                published_metrics[metric.metric_id] = {
                    "name": metric.metric_name,
                    "value": dataset_value,
                }
                counter.successful += 1
                progress.advance(successful=1)
            except Exception as error:
                metric_writer.abort()
                metric_path.unlink(missing_ok=True)
                failures.record(
                    category="statistics",
                    error_code=_failure_code(
                        error,
                        fallback="STATISTICS_METRIC_FAILED",
                    ),
                    detail="A statistical metric could not be completed.",
                    phase="statistics_metric",
                    metric_id=metric.metric_id,
                )
                counter.failed += 1
                progress.advance(failed=1)
    finally:
        raw_path.unlink(missing_ok=True)
    differences_metadata = final_writer.close()
    dataset_artifact = StatisticalDatasetArtifact(
        sample_count=len(facts_values),
        metric_total=len(metrics),
        metric_successful=counter.successful,
        metric_failed=counter.failed,
        metrics=published_metrics,
    )
    dataset_metadata = _write_json_model(
        path=root / DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
        model=dataset_artifact,
        relative_path=DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
    )
    return counter, (dataset_metadata, differences_metadata)


def _physical_failure_affected_count(
    *,
    scheduled_operations: Sequence[DirectedLogicalComparison],
    failure: _CachedComputationFailure,
) -> int:
    if failure.computation_index is None:
        return sum(
            1
            for item in scheduled_operations
            if item.computation_index is None
            and item.left_sequence_id == failure.sequence_id
        )
    return sum(
        1
        for item in scheduled_operations
        if item.computation_index == failure.computation_index
    )


def _run_sequence_phase(
    *,
    context: StageContext,
    operations: tuple[DirectedLogicalComparison, ...],
    cache: _SequenceComparisonCache,
    alignment: _AlignmentArtifacts,
    coordinate_lookup: _ReferenceCoordinateLookup | None,
    requested_types: tuple[DifferenceEventType, ...],
    summary_writer: JsonlArtifactWriter,
    differences_writer: JsonlArtifactWriter,
    failures: _FailureAccumulator,
    progress: _ProgressTracker,
    phase_index: int,
    category: str,
    failure_ids: dict[str, str],
) -> _CategoryCounter:
    counter = _CategoryCounter(requested=True, total=len(operations))
    progress.begin(phase_index)
    if not operations:
        progress.complete_empty()
        return counter
    for operation in operations:
        context.check_control()
        try:
            comparison, reused = cache.resolve(operation)
        except _CachedComputationFailure as error:
            failure_id = failure_ids.get(error.key)
            if failure_id is None:
                failure_id = failures.record(
                    category="sequence_differences",
                    error_code="SEQUENCE_COMPUTATION_FAILED",
                    detail="A sequence comparison computation failed.",
                    phase="physical_sequence_computation",
                    computation_index=error.computation_index,
                    affected_logical_result_count=_physical_failure_affected_count(
                        scheduled_operations=cache.scheduled_operations,
                        failure=error,
                    ),
                    sample_ids=(operation.left_sample_id, operation.right_sample_id),
                )
                failure_ids[error.key] = failure_id
            _write_summary_record(
                summary_writer,
                _summary_record(
                    operation=operation,
                    status=ComparativeResultStatus.FAILED,
                    requested=requested_types,
                    failure_id=failure_id,
                ),
            )
            counter.failed += 1
            progress.advance(failed=1)
            continue

        try:
            materialized = []
            left_row = alignment.rows_by_sample_id[operation.left_sample_id]
            right_row = alignment.rows_by_sample_id[operation.right_sample_id]
            for event in comparison.events:
                if not _event_is_requested(event, requested=requested_types):
                    continue
                reference_coordinates = (
                    _reference_coordinates_for_event(
                        event=event,
                        lookup=coordinate_lookup,
                    )
                    if coordinate_lookup is not None
                    else None
                )
                materialized.append(
                    materialize_difference_record(
                        left=comparison.left,
                        right=comparison.right,
                        source_kinds=operation.source_kinds,
                        event=event,
                        left_aligned_sequence=left_row.aligned_sequence,
                        right_aligned_sequence=right_row.aligned_sequence,
                        reference_coordinates=reference_coordinates,
                    )
                )
            summary = _summary_record(
                operation=operation,
                status=ComparativeResultStatus.COMPLETED,
                requested=requested_types,
                comparison=comparison,
                reused=reused,
            )
        except Exception as error:
            failure_id = failures.record(
                category=category,
                error_code=_failure_code(
                    error,
                    fallback="SEQUENCE_RESULT_MATERIALIZATION_FAILED",
                ),
                detail="A sequence comparison result could not be materialized.",
                phase=f"{category}_comparison",
                computation_index=operation.computation_index,
                sample_ids=(operation.left_sample_id, operation.right_sample_id),
            )
            _write_summary_record(
                summary_writer,
                _summary_record(
                    operation=operation,
                    status=ComparativeResultStatus.FAILED,
                    requested=requested_types,
                    failure_id=failure_id,
                ),
            )
            counter.failed += 1
            progress.advance(failed=1)
            continue
        _write_summary_record(summary_writer, summary)
        for record in materialized:
            differences_writer.write_model(record)
        counter.successful += 1
        progress.advance(successful=1)
    return counter


def _fail_sequence_phase_without_computation(
    *,
    context: StageContext,
    operations: tuple[DirectedLogicalComparison, ...],
    requested_types: tuple[DifferenceEventType, ...],
    summary_writer: JsonlArtifactWriter,
    failures: _FailureAccumulator,
    progress: _ProgressTracker,
    phase_index: int,
    category: str,
    error: BaseException,
    phase: str,
) -> _CategoryCounter:
    counter = _CategoryCounter(requested=True, total=len(operations))
    progress.begin(phase_index)
    if not operations:
        progress.complete_empty()
        return counter
    failure_id = failures.record(
        category=category,
        error_code=_failure_code(error, fallback="SEQUENCE_INPUT_UNAVAILABLE"),
        detail="A required sequence-comparison input is unavailable or inconsistent.",
        phase=phase,
        affected_logical_result_count=len(operations),
    )
    for operation in operations:
        context.check_control()
        _write_summary_record(
            summary_writer,
            _summary_record(
                operation=operation,
                status=ComparativeResultStatus.FAILED,
                requested=requested_types,
                failure_id=failure_id,
            ),
        )
        counter.failed += 1
        progress.advance(failed=1)
    return counter


def _category_counter(
    *,
    requested: bool,
    total: int = 0,
    artifact_paths: tuple[str, ...] = tuple(),
) -> _CategoryCounter:
    return _CategoryCounter(
        requested=requested,
        total=total,
        artifact_paths=artifact_paths,
    )


def _plan_execution_counts(
    *,
    plan: ComparisonPlan,
    reference_operations: Sequence[DirectedLogicalComparison],
    pairwise_operations: Sequence[DirectedLogicalComparison],
    cache: _SequenceComparisonCache | None,
    sequence_differences_enabled: bool,
) -> ComparativePlanExecutionCounts:
    pairwise_occurrences = sum(
        operation.source_occurrence_count
        - int(ComparisonSourceKind.REFERENCE in operation.source_kinds)
        for operation in pairwise_operations
    )
    planned_reused = (
        sum(
            max(0, computation.logical_projection_count - 1)
            for computation in plan.computations
        )
        if sequence_differences_enabled
        else 0
    )
    return ComparativePlanExecutionCounts(
        logical_sample_count=len(plan.samples),
        unique_sequence_count=len({sample.sequence_id for sample in plan.samples}),
        reference_logical_comparison_count=len(reference_operations),
        pairwise_logical_comparison_occurrence_count=pairwise_occurrences,
        pairwise_unique_directed_logical_comparison_count=len(pairwise_operations),
        duplicate_occurrence_count=plan.counts.duplicate_occurrence_count,
        planned_physical_scan_count=(
            plan.counts.scan_computation_count if sequence_differences_enabled else 0
        ),
        attempted_physical_scan_count=(
            cache.attempted_physical_scan_count if cache is not None else 0
        ),
        successful_physical_scan_count=(
            cache.successful_physical_scan_count if cache is not None else 0
        ),
        failed_physical_scan_count=(
            cache.failed_physical_scan_count if cache is not None else 0
        ),
        identical_projection_count=(
            plan.counts.identical_sequence_projection_count
            if sequence_differences_enabled
            else 0
        ),
        attempted_identical_profile_count=(
            cache.attempted_identical_profile_count if cache is not None else 0
        ),
        successful_identical_profile_count=(
            cache.successful_identical_profile_count if cache is not None else 0
        ),
        failed_identical_profile_count=(
            cache.failed_identical_profile_count if cache is not None else 0
        ),
        planned_reused_projection_count=planned_reused,
        executed_reused_projection_count=(
            cache.reused_projection_count if cache is not None else 0
        ),
    )


@dataclass(frozen=True, slots=True)
class ComparativeAnalysisStage:
    stage_id: str = COMPARATIVE_ANALYSIS_STAGE_ID
    weight: float = 1.0
    plan_builder: ComparisonPlanBuilder | None = None
    aligned_comparator: AlignedSequenceComparator | None = None
    comparison_projector: DirectedComparisonProjector | None = None
    statistical_comparator: StatisticalComparator | None = None
    statistical_summarizer: DatasetStatisticalSummarizer | None = None
    statistics_metric_guard: Callable[[str], None] | None = None

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)
        (context.stage_staging_directory / "comparative_analysis").mkdir(
            parents=True,
            exist_ok=True,
        )

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        started_at_value = utc_now()
        started_monotonic = time.monotonic()
        context.check_control()
        config = _load_resolved_config(context.launch_spec.config_revision_path)
        comparative_config = config.comparative_analysis
        if not comparative_config.enabled:
            return self._run_disabled(
                context=context,
                progress_reporter=progress_reporter,
                config=config,
                started_at=started_at_value,
                started_monotonic=started_monotonic,
            )

        input_source = _load_input_artifacts(context=context)
        try:
            plan = (self.plan_builder or ComparisonPlanBuilder()).build_from_manifest(
                config=config,
                manifest=input_source.manifest,
            )
        except ComparisonDomainError as error:
            raise ComparativeAnalysisStageError(
                reason=error.code.value,
                detail="Comparative-analysis planning failed for the normalized selection.",
                context={"error_code": error.code.value},
            ) from error

        reference_operations = _ordered_sequence_operations(
            tuple(
                operation
                for operation in plan.logical_operations
                if ComparisonSourceKind.REFERENCE in operation.source_kinds
            )
        )
        pairwise_operations = _ordered_sequence_operations(
            tuple(
                operation
                for operation in plan.logical_operations
                if any(
                    source_kind is not ComparisonSourceKind.REFERENCE
                    for source_kind in operation.source_kinds
                )
            )
        )
        facts_by_sequence_id = _facts_by_sequence_id(input_source.manifest)
        facts_values = _logical_facts(
            plan=plan,
            facts_by_sequence_id=facts_by_sequence_id,
        )
        statistical_metrics = (
            _build_statistical_metrics(facts_values=facts_values)
            if comparative_config.statistics.enabled
            else tuple()
        )
        sequence_differences_enabled = comparative_config.sequence_differences.enabled
        phases_list = [_Phase("Preparation and planning", "preparation", 1)]
        if comparative_config.statistics.enabled:
            phases_list.append(
                _Phase(
                    "Statistical metrics",
                    "statistics_metric",
                    len(statistical_metrics),
                )
            )
        if sequence_differences_enabled and reference_operations:
            phases_list.append(
                _Phase(
                    "Reference comparisons",
                    "reference_comparison",
                    len(reference_operations),
                )
            )
        if sequence_differences_enabled and pairwise_operations:
            phases_list.append(
                _Phase(
                    "Pairwise comparisons",
                    "pairwise_comparison",
                    len(pairwise_operations),
                )
            )
        phases_list.append(_Phase("Aggregation and publication", "publication", 1))
        phases = tuple(phases_list)
        progress = _ProgressTracker(
            context=context,
            reporter=progress_reporter,
            phases=phases,
        )
        context.emit_event(
            COMPARATIVE_ANALYSIS_STARTED_EVENT,
            {
                "phase_total": len(phases),
                "logical_operation_count": plan.counts.unique_logical_operation_count,
                "physical_computation_count": plan.counts.scan_computation_count,
                "detail": "Comparative-analysis stage started.",
            },
        )
        progress.begin(0)
        progress.advance(successful=1)

        root = context.stage_staging_directory
        failure_writer = JsonlArtifactWriter(
            root / COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
            relative_path=COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
        )
        failures = _FailureAccumulator(context=context, writer=failure_writer)
        metadata: list[ComparativeArtifactMetadata] = []
        sequence_cache: _SequenceComparisonCache | None = None
        statistics_counter = _category_counter(
            requested=comparative_config.statistics.enabled,
            total=len(statistical_metrics),
        )
        reference_counter = _category_counter(
            requested=(
                sequence_differences_enabled
                and comparative_config.reference.mode.value != "disabled"
            ),
            total=len(reference_operations) if sequence_differences_enabled else 0,
        )
        pairwise_counter = _category_counter(
            requested=(
                sequence_differences_enabled and comparative_config.pairwise.enabled
            ),
            total=len(pairwise_operations) if sequence_differences_enabled else 0,
        )
        phase_index = 1
        if comparative_config.statistics.enabled:
            statistics_counter, statistics_metadata = _run_statistics_phase(
                context=context,
                root=root,
                plan=plan,
                facts_values=facts_values,
                facts_by_sequence_id=facts_by_sequence_id,
                metrics=statistical_metrics,
                summarizer=self.statistical_summarizer
                or DatasetStatisticalSummarizer(),
                comparator=self.statistical_comparator or StatisticalComparator(),
                metric_guard=self.statistics_metric_guard,
                failures=failures,
                progress=progress,
                phase_index=phase_index,
            )
            metadata.extend(statistics_metadata)
            phase_index += 1

        source_artifacts = [
            f"stages/{INPUT_PROCESSING_STAGE_ID}/{INPUT_PROCESSING_MANIFEST_RELATIVE_PATH}"
        ]
        requested_types = _requested_difference_types(config)
        if sequence_differences_enabled:
            context.check_control()
            alignment: _AlignmentArtifacts | None = None
            alignment_error: ComparativeAnalysisStageError | None = None
            try:
                alignment = _load_alignment_artifacts(
                    context=context,
                    input_manifest=input_source.manifest,
                    expected_mode=config.alignment.mode,
                )
            except ComparativeAnalysisStageError as error:
                alignment_error = error

            reference_summary_writer: JsonlArtifactWriter | None = None
            reference_differences_writer: JsonlArtifactWriter | None = None
            pairwise_summary_writer: JsonlArtifactWriter | None = None
            pairwise_differences_writer: JsonlArtifactWriter | None = None
            if reference_operations:
                reference_counter.artifact_paths = (
                    REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH,
                    REFERENCE_DIFFERENCES_RELATIVE_PATH,
                )
                reference_summary_writer = JsonlArtifactWriter(
                    root / REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH,
                    relative_path=REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH,
                )
                reference_differences_writer = JsonlArtifactWriter(
                    root / REFERENCE_DIFFERENCES_RELATIVE_PATH,
                    relative_path=REFERENCE_DIFFERENCES_RELATIVE_PATH,
                )
            if pairwise_operations:
                pairwise_counter.artifact_paths = (
                    PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH,
                    PAIRWISE_DIFFERENCES_RELATIVE_PATH,
                )
                pairwise_summary_writer = JsonlArtifactWriter(
                    root / PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH,
                    relative_path=PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH,
                )
                pairwise_differences_writer = JsonlArtifactWriter(
                    root / PAIRWISE_DIFFERENCES_RELATIVE_PATH,
                    relative_path=PAIRWISE_DIFFERENCES_RELATIVE_PATH,
                )

            if alignment_error is not None:
                if reference_operations:
                    assert reference_summary_writer is not None
                    reference_counter = _fail_sequence_phase_without_computation(
                        context=context,
                        operations=reference_operations,
                        requested_types=requested_types,
                        summary_writer=reference_summary_writer,
                        failures=failures,
                        progress=progress,
                        phase_index=phase_index,
                        category="reference_sequence",
                        error=alignment_error,
                        phase="alignment_preparation",
                    )
                    reference_counter.artifact_paths = (
                        REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH,
                        REFERENCE_DIFFERENCES_RELATIVE_PATH,
                    )
                    phase_index += 1
                if pairwise_operations:
                    assert pairwise_summary_writer is not None
                    pairwise_counter = _fail_sequence_phase_without_computation(
                        context=context,
                        operations=pairwise_operations,
                        requested_types=requested_types,
                        summary_writer=pairwise_summary_writer,
                        failures=failures,
                        progress=progress,
                        phase_index=phase_index,
                        category="pairwise_sequence",
                        error=alignment_error,
                        phase="alignment_preparation",
                    )
                    pairwise_counter.artifact_paths = (
                        PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH,
                        PAIRWISE_DIFFERENCES_RELATIVE_PATH,
                    )
                    phase_index += 1
                if not reference_operations and not pairwise_operations:
                    failures.record(
                        category="sequence_differences",
                        error_code=alignment_error.reason,
                        detail="A required canonical alignment is unavailable or inconsistent.",
                        phase="alignment_preparation",
                    )
            else:
                assert alignment is not None
                source_artifacts.extend(
                    [
                        f"stages/{ALIGNMENT_STAGE_ID}/{ALIGNMENT_MANIFEST_RELATIVE_PATH}",
                        (
                            f"stages/{ALIGNMENT_STAGE_ID}/"
                            f"{alignment.manifest.aligned_fasta_path}"
                        ),
                    ]
                )
                coordinate_lookup: _ReferenceCoordinateLookup | None = None
                reference_map_error: ComparativeAnalysisStageError | None = None
                if reference_operations:
                    try:
                        alignment, coordinate_lookup = _load_reference_coordinate_map(
                            context=context,
                            alignment=alignment,
                            input_manifest=input_source.manifest,
                            expected_mode=config.alignment.mode,
                        )
                    except ComparativeAnalysisStageError as error:
                        reference_map_error = error
                    if coordinate_lookup is not None:
                        assert alignment.manifest.reference_coordinate_map_path is not None
                        source_artifacts.append(
                            f"stages/{ALIGNMENT_STAGE_ID}/"
                            f"{alignment.manifest.reference_coordinate_map_path}"
                        )
                scheduled_operations = (
                    tuple()
                    if not pairwise_operations and reference_map_error is not None
                    else (
                        (*reference_operations, *pairwise_operations)
                        if reference_map_error is None
                        else pairwise_operations
                    )
                )
                sequence_cache = _SequenceComparisonCache(
                    plan=plan,
                    alignment=alignment,
                    comparator=self.aligned_comparator or AlignedSequenceComparator(),
                    projector=self.comparison_projector or DirectedComparisonProjector(),
                    scheduled_operations=tuple(scheduled_operations),
                    uracil_thymine_equivalent=(
                        comparative_config.sequence_differences.symbol_policy
                        .uracil_thymine_equivalent
                    ),
                )
                failure_ids: dict[str, str] = {}
                if reference_operations:
                    assert reference_summary_writer is not None
                    assert reference_differences_writer is not None
                    if reference_map_error is not None:
                        reference_counter = _fail_sequence_phase_without_computation(
                            context=context,
                            operations=reference_operations,
                            requested_types=requested_types,
                            summary_writer=reference_summary_writer,
                            failures=failures,
                            progress=progress,
                            phase_index=phase_index,
                            category="reference_sequence",
                            error=reference_map_error,
                            phase="reference_map_preparation",
                        )
                    else:
                        assert coordinate_lookup is not None
                        reference_counter = _run_sequence_phase(
                            context=context,
                            operations=reference_operations,
                            cache=sequence_cache,
                            alignment=alignment,
                            coordinate_lookup=coordinate_lookup,
                            requested_types=requested_types,
                            summary_writer=reference_summary_writer,
                            differences_writer=reference_differences_writer,
                            failures=failures,
                            progress=progress,
                            phase_index=phase_index,
                            category="reference_sequence",
                            failure_ids=failure_ids,
                        )
                    reference_counter.artifact_paths = (
                        REFERENCE_COMPARISON_SUMMARY_RELATIVE_PATH,
                        REFERENCE_DIFFERENCES_RELATIVE_PATH,
                    )
                    phase_index += 1
                if pairwise_operations:
                    assert pairwise_summary_writer is not None
                    assert pairwise_differences_writer is not None
                    pairwise_counter = _run_sequence_phase(
                        context=context,
                        operations=pairwise_operations,
                        cache=sequence_cache,
                        alignment=alignment,
                        coordinate_lookup=None,
                        requested_types=requested_types,
                        summary_writer=pairwise_summary_writer,
                        differences_writer=pairwise_differences_writer,
                        failures=failures,
                        progress=progress,
                        phase_index=phase_index,
                        category="pairwise_sequence",
                        failure_ids=failure_ids,
                    )
                    pairwise_counter.artifact_paths = (
                        PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH,
                        PAIRWISE_DIFFERENCES_RELATIVE_PATH,
                    )
                    phase_index += 1

            for writer in (
                reference_summary_writer,
                reference_differences_writer,
                pairwise_summary_writer,
                pairwise_differences_writer,
            ):
                if writer is not None:
                    metadata.append(writer.close())

        context.check_control()
        progress.begin(phase_index)
        failures_metadata = failure_writer.close()
        metadata.append(failures_metadata)
        successful_result_count = (
            statistics_counter.successful
            + reference_counter.successful
            + pairwise_counter.successful
        )
        failed_result_count = (
            statistics_counter.failed + reference_counter.failed + pairwise_counter.failed
        )
        if failures.count == 0:
            status = ComparativeAnalysisStatus.COMPLETED
        elif successful_result_count > 0:
            status = ComparativeAnalysisStatus.PARTIAL_SUCCESS
        else:
            status = ComparativeAnalysisStatus.FAILED
        resolved_reference = input_source.manifest.resolved_reference
        completed_at_value = utc_now()
        manifest = ComparativeAnalysisManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=True,
            normalized_settings=comparative_config,
            status=status,
            alignment_mode=config.alignment.mode.value,
            reference_mode=comparative_config.reference.mode.value,
            reference_sample_id=(
                resolved_reference.sample_id if resolved_reference is not None else None
            ),
            reference_sequence_id=(
                resolved_reference.sequence_id if resolved_reference is not None else None
            ),
            uracil_thymine_equivalent=(
                comparative_config.sequence_differences.symbol_policy
                .uracil_thymine_equivalent
            ),
            requested_difference_categories=requested_types,
            started_at=serialize_utc_datetime(started_at_value),
            completed_at=serialize_utc_datetime(completed_at_value),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            source_artifacts=tuple(dict.fromkeys(source_artifacts)),
            phase_names=tuple(phase.name for phase in phases),
            plan_counts=plan.counts,
            plan_execution_counts=_plan_execution_counts(
                plan=plan,
                reference_operations=reference_operations,
                pairwise_operations=pairwise_operations,
                cache=sequence_cache,
                sequence_differences_enabled=sequence_differences_enabled,
            ),
            category_execution={
                "statistics": statistics_counter.to_contract(),
                "reference_sequence_differences": reference_counter.to_contract(),
                "pairwise_sequence_differences": pairwise_counter.to_contract(),
            },
            successful_result_count=successful_result_count,
            failed_result_count=failed_result_count,
            failure_count=failures.count,
            artifacts=tuple(metadata),
        )
        manifest_path = root / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH
        _write_json_model(
            path=manifest_path,
            model=manifest,
            relative_path=COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
        )
        _validate_published_snapshot(root=root, manifest=manifest)
        progress.advance(
            successful=1 if status is not ComparativeAnalysisStatus.FAILED else 0,
            failed=1 if status is ComparativeAnalysisStatus.FAILED else 0,
        )
        artifacts = (
            COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
            *(item.relative_path for item in metadata),
        )
        failure = None
        if status is ComparativeAnalysisStatus.FAILED:
            failure = StageFailure(
                reason="comparative_analysis_failed",
                detail="Comparative analysis produced no successful requested results.",
                failure_event_name=COMPARATIVE_ANALYSIS_FAILED_EVENT,
                failure_context={
                    "failure_count": failures.count,
                    "failures_path": COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
                    "detail": "Comparative analysis failed.",
                },
            )
        return StageRunResult(
            artifacts=tuple(artifacts),
            failure=failure,
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
        phases = (
            _Phase("Preparation and planning", "preparation", 1),
            _Phase("Aggregation and publication", "publication", 1),
        )
        progress = _ProgressTracker(
            context=context,
            reporter=progress_reporter,
            phases=phases,
        )
        context.emit_event(
            COMPARATIVE_ANALYSIS_STARTED_EVENT,
            {
                "phase_total": len(phases),
                "logical_operation_count": 0,
                "physical_computation_count": 0,
                "detail": "Comparative-analysis stage started.",
            },
        )
        progress.begin(0)
        progress.advance(successful=1)
        context.emit_event(
            COMPARATIVE_ANALYSIS_SKIPPED_EVENT,
            {
                "reason": "comparative_analysis_disabled",
                "detail": "Comparative analysis was skipped because it is disabled.",
            },
        )
        context.check_control()
        progress.begin(1)
        completed_at = utc_now()
        manifest = ComparativeAnalysisManifest(
            task_id=context.launch_spec.task_id,
            job_id=context.launch_spec.job_id,
            config_hash=context.launch_spec.config_hash,
            enabled=False,
            normalized_settings=config.comparative_analysis,
            skipped_reason="comparative_analysis_disabled",
            status=ComparativeAnalysisStatus.COMPLETED,
            alignment_mode=config.alignment.mode.value,
            reference_mode=config.comparative_analysis.reference.mode.value,
            uracil_thymine_equivalent=False,
            started_at=serialize_utc_datetime(started_at),
            completed_at=serialize_utc_datetime(completed_at),
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            phase_names=tuple(phase.name for phase in phases),
            plan_counts=_empty_plan_counts(),
            category_execution={
                "statistics": _category_counter(requested=False).to_contract(),
                "reference_sequence_differences": _category_counter(
                    requested=False
                ).to_contract(),
                "pairwise_sequence_differences": _category_counter(
                    requested=False
                ).to_contract(),
            },
            successful_result_count=0,
            failed_result_count=0,
            failure_count=0,
        )
        root = context.stage_staging_directory
        _write_json_model(
            path=root / COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
            model=manifest,
            relative_path=COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
        )
        _validate_published_snapshot(root=root, manifest=manifest)
        progress.advance(successful=1)
        return StageRunResult(
            artifacts=(COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,),
            check_control_before_commit=True,
        )


def _validate_published_snapshot(
    *,
    root: Path,
    manifest: ComparativeAnalysisManifest,
) -> None:
    for metadata in manifest.artifacts:
        path = root / metadata.relative_path
        if not path.is_file():
            raise ComparativeAnalysisStageError(
                reason="comparative_analysis_artifact_missing",
                detail="A comparative-analysis artifact is missing before publication.",
                context={"relative_path": metadata.relative_path},
            )
        if path.stat().st_size != metadata.size_bytes or _sha256_file(path) != metadata.sha256:
            raise ComparativeAnalysisStageError(
                reason="comparative_analysis_artifact_integrity_failed",
                detail="A comparative-analysis artifact failed integrity validation.",
                context={"relative_path": metadata.relative_path},
            )


__all__ = [
    "COMPARATIVE_ANALYSIS_COMPLETED_EVENT",
    "COMPARATIVE_ANALYSIS_FAILED_EVENT",
    "COMPARATIVE_ANALYSIS_OPERATION_FAILED_EVENT",
    "COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS_EVENT",
    "COMPARATIVE_ANALYSIS_PHASE_STARTED_EVENT",
    "COMPARATIVE_ANALYSIS_PROGRESS_EVENT",
    "COMPARATIVE_ANALYSIS_RESULT_PUBLISHED_EVENT",
    "COMPARATIVE_ANALYSIS_SKIPPED_EVENT",
    "COMPARATIVE_ANALYSIS_STARTED_EVENT",
    "ComparativeAnalysisStage",
    "ComparativeAnalysisStageError",
]
