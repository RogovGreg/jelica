from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final
from uuid import UUID

from jelica_core import __version__ as JELICA_CORE_VERSION
from jelica_core.config import ResolvedAnalysisConfig
from jelica_core.result_package import (
    JELICA_PACKAGE_CONFIGURATION_PATH,
    JELICA_PACKAGE_INPUT_MANIFEST_PATH,
    JELICA_PACKAGE_MANIFEST_PATH,
    JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
    JELICA_PACKAGE_TASK_PATH,
    RESULT_PACKAGE_PREPARED_DIRNAME,
    RESULT_PACKAGE_STAGE_ID,
    RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,
    JelicaPackageManifest,
    ResultPackageArtifactInfo,
    ResultPackageProducerInfo,
    ResultPackageStageInfo,
    ResultPackageStageManifest,
    ResultPackageTaskInfo,
    ResultPackageTaskStatus,
    compute_content_id,
    infer_media_type,
    relative_package_path_from_task,
    result_package_artifact_paths,
    result_package_target_path,
    serialize_stable_json,
    validate_result_package_file,
    write_model_json,
)
from jelica_core.tasks import (
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryService,
    AnalyticalTaskState,
)
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .artifacts import (
    StageSnapshotValidationError,
    ValidatedStageSnapshot,
    validate_committed_stage_snapshot,
)
from .input_parsers import INPUT_MANIFEST_RELATIVE_PATH
from .input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    InputProcessingManifest,
)
from .pipeline import ProgressReporter, StageContext, StageRunResult, build_pipeline_definition

RESULT_PACKAGE_FAILED_EVENT: Final = "RESULT_PACKAGE_FAILED"
RESULT_PACKAGE_STARTED_EVENT: Final = "RESULT_PACKAGE_STARTED"
RESULT_PACKAGE_PROGRESS_EVENT: Final = "RESULT_PACKAGE_PROGRESS"

_INPUT_ACQUISITION_STAGE_ID: Final = "input_acquisition"
_INPUT_PROCESSING_STAGE_ID: Final = "input_processing"
_INITIALIZE_STAGE_ID: Final = "initialize_job"
_EXECUTION_MANIFEST_RELATIVE_PATH: Final = "execution_manifest.json"
_INTERNAL_TASK_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_directory_max_depth", "ncbi_max_retries"}
)
_COPY_CHUNK_SIZE: Final = 1024 * 1024
_ABSOLUTE_PATH_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"([A-Za-z]:[\\/][^,\s;]+|/[^,\s;]+)"
)


class ResultPackageStageError(RuntimeError):
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
        self.event_name = RESULT_PACKAGE_FAILED_EVENT
        self.context = context or {}
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _CommittedStageSnapshot:
    stage_id: str
    snapshot: ValidatedStageSnapshot
    stage_root: Path


@dataclass(frozen=True, slots=True)
class _ProtectedFile:
    package_path: str
    source_path: Path
    stage_id: str | None


@dataclass(frozen=True, slots=True)
class ResultPackageStage:
    stage_id: str = RESULT_PACKAGE_STAGE_ID
    weight: float = 1.0

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)
        (context.stage_staging_directory / "result_package").mkdir(parents=True, exist_ok=True)
        (context.stage_staging_directory / RESULT_PACKAGE_PREPARED_DIRNAME).mkdir(
            parents=True,
            exist_ok=True,
        )

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        try:
            context.check_control()
            context.emit_event(
                RESULT_PACKAGE_STARTED_EVENT,
                {"detail": "Result-package stage started."},
            )
            progress_reporter(0.05)

            package_created_at = serialize_utc_datetime(utc_now())
            committed_stages = _load_committed_stages(context=context)
            stage_status_by_id = _stage_statuses(committed_stages=committed_stages)
            task_status = _resolve_task_status(stage_status_by_id=stage_status_by_id)
            progress_reporter(0.2)

            resolved_config = _load_resolved_config(context.launch_spec.config_revision_path)
            task_info = _build_task_info(
                context=context,
                package_created_at=package_created_at,
                task_status=task_status,
                trace_id=resolved_config.trace_id,
            )

            context.emit_event(
                RESULT_PACKAGE_PROGRESS_EVENT,
                {"phase": "collect_protected_files", "detail": "Collecting protected artifacts."},
            )

            with tempfile.TemporaryDirectory(
                prefix="result-package-build-",
                dir=context.stage_staging_directory,
            ) as temporary_root_raw:
                temporary_root = Path(temporary_root_raw)
                generated_root = temporary_root / "generated"
                generated_root.mkdir(parents=True, exist_ok=True)

                protected_files: dict[str, _ProtectedFile] = {}
                stage_artifacts: dict[str, list[str]] = {
                    item.stage_id: [] for item in committed_stages
                }

                task_json_path = generated_root / "task.json"
                _write_json_payload(path=task_json_path, payload=task_info.model_dump(mode="json"))
                _register_protected_file(
                    protected_files=protected_files,
                    package_path=JELICA_PACKAGE_TASK_PATH,
                    source_path=task_json_path,
                    stage_id=None,
                )

                configuration_json_path = generated_root / "configuration.json"
                _write_json_payload(
                    path=configuration_json_path,
                    payload=resolved_config.model_dump(mode="json"),
                )
                _register_protected_file(
                    protected_files=protected_files,
                    package_path=JELICA_PACKAGE_CONFIGURATION_PATH,
                    source_path=configuration_json_path,
                    stage_id=None,
                )

                input_stage = _require_stage(
                    committed_stages=committed_stages,
                    stage_id=_INPUT_ACQUISITION_STAGE_ID,
                )
                input_manifest_source = _resolve_stage_artifact(
                    stage=input_stage,
                    artifact_relative_path=INPUT_MANIFEST_RELATIVE_PATH,
                )
                input_manifest_payload = _load_json_object(path=input_manifest_source)
                sanitized_input_manifest = _sanitize_json_payload(
                    payload=input_manifest_payload,
                    task_dir=context.launch_spec.task_dir,
                    drop_source_errors=True,
                )
                input_manifest_generated_path = generated_root / "input_manifest.json"
                _write_json_payload(
                    path=input_manifest_generated_path,
                    payload=sanitized_input_manifest,
                )
                _register_protected_file(
                    protected_files=protected_files,
                    package_path=JELICA_PACKAGE_INPUT_MANIFEST_PATH,
                    source_path=input_manifest_generated_path,
                    stage_id=_INPUT_ACQUISITION_STAGE_ID,
                )
                stage_artifacts[_INPUT_ACQUISITION_STAGE_ID].append(
                    JELICA_PACKAGE_INPUT_MANIFEST_PATH
                )

                input_processing_stage = _require_stage(
                    committed_stages=committed_stages,
                    stage_id=_INPUT_PROCESSING_STAGE_ID,
                )
                input_processing_manifest_source = _resolve_stage_artifact(
                    stage=input_processing_stage,
                    artifact_relative_path=INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
                )
                input_processing_manifest = InputProcessingManifest.model_validate_json(
                    input_processing_manifest_source.read_text(encoding="utf-8")
                )
                sanitized_input_processing_payload = _sanitize_json_payload(
                    payload=_load_json_object(path=input_processing_manifest_source),
                    task_dir=context.launch_spec.task_dir,
                    drop_source_errors=False,
                )
                sanitized_input_processing_path = generated_root / "input_processing_manifest.json"
                _write_json_payload(
                    path=sanitized_input_processing_path,
                    payload=sanitized_input_processing_payload,
                )

                normalized_fasta_generated_path = generated_root / "normalized_sequences.fasta"
                _build_normalized_fasta(
                    output_path=normalized_fasta_generated_path,
                    input_processing_stage_root=input_processing_stage.stage_root,
                    input_processing_manifest=input_processing_manifest,
                )
                _register_protected_file(
                    protected_files=protected_files,
                    package_path=JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
                    source_path=normalized_fasta_generated_path,
                    stage_id=_INPUT_PROCESSING_STAGE_ID,
                )
                stage_artifacts[_INPUT_PROCESSING_STAGE_ID].append(
                    JELICA_PACKAGE_NORMALIZED_FASTA_PATH
                )

                for committed_stage in committed_stages:
                    context.check_control()
                    stage_id = committed_stage.stage_id
                    for artifact_relative_path in committed_stage.snapshot.manifest.artifacts:
                        if (
                            stage_id == _INPUT_ACQUISITION_STAGE_ID
                            and artifact_relative_path == INPUT_MANIFEST_RELATIVE_PATH
                        ):
                            continue
                        package_path = _package_path_for_stage_artifact(
                            stage_id=stage_id,
                            artifact_relative_path=artifact_relative_path,
                        )
                        if (
                            stage_id == _INITIALIZE_STAGE_ID
                            and artifact_relative_path == _EXECUTION_MANIFEST_RELATIVE_PATH
                        ):
                            execution_manifest_payload = _load_json_object(
                                path=committed_stage.stage_root / artifact_relative_path
                            )
                            sanitized_execution_manifest = _sanitize_json_payload(
                                payload=execution_manifest_payload,
                                task_dir=context.launch_spec.task_dir,
                                drop_source_errors=False,
                            )
                            sanitized_execution_manifest_path = (
                                generated_root / "execution_manifest.json"
                            )
                            _write_json_payload(
                                path=sanitized_execution_manifest_path,
                                payload=sanitized_execution_manifest,
                            )
                            source_path = sanitized_execution_manifest_path
                        elif (
                            stage_id == _INPUT_PROCESSING_STAGE_ID
                            and artifact_relative_path == INPUT_PROCESSING_MANIFEST_RELATIVE_PATH
                        ):
                            source_path = sanitized_input_processing_path
                        else:
                            source_path = _resolve_stage_artifact(
                                stage=committed_stage,
                                artifact_relative_path=artifact_relative_path,
                            )
                        _register_protected_file(
                            protected_files=protected_files,
                            package_path=package_path,
                            source_path=source_path,
                            stage_id=stage_id,
                        )
                        stage_artifacts[stage_id].append(package_path)

                context.emit_event(
                    RESULT_PACKAGE_PROGRESS_EVENT,
                    {
                        "phase": "build_manifest",
                        "detail": "Building package manifest and content digest.",
                    },
                )
                progress_reporter(0.55)

                artifact_infos = _build_artifact_infos(protected_files=protected_files)
                content_id = compute_content_id(artifacts=artifact_infos)
                content_digest = content_id.split(":", maxsplit=1)[1]

                stage_infos = tuple(
                    ResultPackageStageInfo(
                        name=committed_stage.stage_id,
                        status=stage_status_by_id[committed_stage.stage_id],
                        artifacts=tuple(sorted(stage_artifacts[committed_stage.stage_id])),
                    )
                    for committed_stage in committed_stages
                )

                package_manifest = JelicaPackageManifest(
                    content_id=content_id,
                    producer=ResultPackageProducerInfo(version=JELICA_CORE_VERSION),
                    package_created_at=package_created_at,
                    task=task_info,
                    stages=stage_infos,
                    artifacts=tuple(artifact_infos),
                )

                context.emit_event(
                    RESULT_PACKAGE_PROGRESS_EVENT,
                    {"phase": "assemble_zip", "detail": "Assembling .jelica ZIP container."},
                )
                progress_reporter(0.75)

                prepared_package_relative_path = (
                    f"{RESULT_PACKAGE_PREPARED_DIRNAME}/{content_digest}.jelica"
                )
                prepared_package_path = (
                    context.stage_staging_directory / prepared_package_relative_path
                )
                _build_package_zip(
                    package_path=prepared_package_path,
                    protected_files=protected_files,
                    package_manifest=package_manifest,
                )
                validate_result_package_file(
                    path=prepared_package_path,
                    expected_content_id=content_id,
                    require_notes_absent=True,
                )

                published_package_path = result_package_target_path(
                    task_dir=context.launch_spec.task_dir,
                    content_digest=content_digest,
                )
                published_package_relative_path = relative_package_path_from_task(
                    task_dir=context.launch_spec.task_dir,
                    package_path=published_package_path,
                )

                stage_manifest = ResultPackageStageManifest(
                    task_id=context.launch_spec.task_id,
                    job_id=context.launch_spec.job_id,
                    config_hash=context.launch_spec.config_hash,
                    format_version=package_manifest.format_version,
                    task_status=task_status,
                    content_id=content_id,
                    content_digest=content_digest,
                    package_created_at=package_created_at,
                    prepared_package_relative_path=prepared_package_relative_path,
                    published_package_relative_path=published_package_relative_path,
                    task=task_info,
                    source_stage_ids=tuple(item.stage_id for item in committed_stages),
                    artifact_count=len(package_manifest.artifacts),
                    stage_count=len(committed_stages),
                )
                write_model_json(
                    path=context.stage_staging_directory
                    / RESULT_PACKAGE_STAGE_MANIFEST_RELATIVE_PATH,
                    model=stage_manifest,
                )
                progress_reporter(1.0)
                return StageRunResult(
                    artifacts=result_package_artifact_paths(stage_manifest),
                    check_control_before_commit=True,
                )
        except StageSnapshotValidationError:
            raise
        except ResultPackageStageError:
            raise
        except AnalyticalTaskNotFoundError as error:
            raise ResultPackageStageError(
                reason="result_package_task_not_found",
                detail="Task metadata are unavailable for package generation.",
            ) from error
        except Exception as error:
            raise ResultPackageStageError(
                reason="result_package_internal_error",
                detail="Result package generation failed.",
                context={"error_type": type(error).__name__},
            ) from error


def _load_resolved_config(config_revision_path: Path) -> ResolvedAnalysisConfig:
    try:
        payload = json.loads(config_revision_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResultPackageStageError(
            reason="result_package_config_unreadable",
            detail="Immutable analysis configuration could not be read.",
        ) from error
    if not isinstance(payload, dict):
        raise ResultPackageStageError(
            reason="result_package_config_invalid",
            detail="Immutable analysis configuration must be a JSON object.",
        )
    filtered_payload = {
        str(key): value
        for key, value in payload.items()
        if str(key) not in _INTERNAL_TASK_CONFIG_FIELDS
    }
    try:
        return ResolvedAnalysisConfig.model_validate(filtered_payload)
    except Exception as error:
        raise ResultPackageStageError(
            reason="result_package_config_invalid",
            detail="Immutable analysis configuration is invalid for package export.",
        ) from error


def _build_task_info(
    *,
    context: StageContext,
    package_created_at: str,
    task_status: ResultPackageTaskStatus,
    trace_id: UUID | None,
) -> ResultPackageTaskInfo:
    registry = AnalyticalTaskRegistryService(database_path=context.launch_spec.database_path)
    task_record = registry.get_task(task_id=context.launch_spec.task_id)
    if task_record.state not in {
        AnalyticalTaskState.RUNNING,
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
        AnalyticalTaskState.CANCEL_REQUESTED,
    }:
        raise ResultPackageStageError(
            reason="result_package_task_state_invalid",
            detail=(
                "Result package can only be generated while the active job is still in progress."
            ),
            context={"task_state": task_record.state.value},
        )
    return ResultPackageTaskInfo(
        task_id=task_record.task_id,
        trace_id=trace_id,
        status=task_status,
        created_at=serialize_utc_datetime(task_record.created_at),
        completed_at=package_created_at,
    )


def _load_committed_stages(*, context: StageContext) -> tuple[_CommittedStageSnapshot, ...]:
    pipeline = build_pipeline_definition(
        pipeline_name=context.launch_spec.pipeline_name,
        pipeline_version=context.launch_spec.pipeline_version,
        config_revision_path=context.launch_spec.config_revision_path,
    )
    ordered_stage_ids: list[str] = []
    result_package_found = False
    for stage in pipeline.stages:
        if stage.stage_id == RESULT_PACKAGE_STAGE_ID:
            result_package_found = True
            break
        ordered_stage_ids.append(stage.stage_id)
    if not result_package_found:
        raise ResultPackageStageError(
            reason="result_package_pipeline_invalid",
            detail="Pipeline does not include result_package as a terminal stage.",
        )

    snapshots: list[_CommittedStageSnapshot] = []
    for stage_id in ordered_stage_ids:
        snapshot = validate_committed_stage_snapshot(
            job_dir=context.launch_spec.job_dir,
            stage_id=stage_id,
            expected_job_id=context.launch_spec.job_id,
            expected_pipeline_version=context.launch_spec.pipeline_version,
            expected_task_id=context.launch_spec.task_id,
            expected_config_hash=context.launch_spec.config_hash,
        )
        snapshots.append(
            _CommittedStageSnapshot(
                stage_id=stage_id,
                snapshot=snapshot,
                stage_root=context.launch_spec.job_dir / "stages" / stage_id,
            )
        )
    return tuple(snapshots)


def _stage_statuses(
    *,
    committed_stages: tuple[_CommittedStageSnapshot, ...],
) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for stage in committed_stages:
        status = stage.snapshot.domain_status or "completed"
        normalized_status = status.strip().lower()
        if normalized_status == "failed":
            raise ResultPackageStageError(
                reason="result_package_upstream_failed",
                detail=(
                    "A committed upstream stage has failed status and cannot be exported as a "
                    "successful result package."
                ),
                context={"stage_id": stage.stage_id},
            )
        statuses[stage.stage_id] = normalized_status
    return statuses


def _resolve_task_status(
    *,
    stage_status_by_id: dict[str, str],
) -> ResultPackageTaskStatus:
    has_partial_success = any(status == "partial_success" for status in stage_status_by_id.values())
    if has_partial_success:
        return ResultPackageTaskStatus.COMPLETED_WITH_WARNINGS
    return ResultPackageTaskStatus.COMPLETED


def _write_json_payload(*, path: Path, payload: dict[str, object]) -> None:
    write_text_atomically(path=path, payload=serialize_stable_json(payload))


def _register_protected_file(
    *,
    protected_files: dict[str, _ProtectedFile],
    package_path: str,
    source_path: Path,
    stage_id: str | None,
) -> None:
    normalized_package_path = package_path.replace("\\", "/").strip()
    if normalized_package_path == "":
        raise ResultPackageStageError(
            reason="result_package_path_invalid",
            detail="Protected package path must not be empty.",
        )
    if normalized_package_path in protected_files:
        raise ResultPackageStageError(
            reason="result_package_duplicate_artifact_path",
            detail="Package contains duplicate protected artifact paths.",
            context={"path": normalized_package_path},
        )
    if not source_path.is_file() or source_path.is_symlink():
        raise ResultPackageStageError(
            reason="result_package_artifact_missing",
            detail="A protected artifact source file is missing or invalid.",
            context={"path": normalized_package_path},
        )
    protected_files[normalized_package_path] = _ProtectedFile(
        package_path=normalized_package_path,
        source_path=source_path,
        stage_id=stage_id,
    )


def _build_normalized_fasta(
    *,
    output_path: Path,
    input_processing_stage_root: Path,
    input_processing_manifest: InputProcessingManifest,
) -> None:
    with output_path.open("wb") as output_handle:
        for unique_sequence in input_processing_manifest.unique_sequences:
            sequence_path = input_processing_stage_root / unique_sequence.sequence_artifact_path
            if not sequence_path.is_file() or sequence_path.is_symlink():
                raise ResultPackageStageError(
                    reason="result_package_sequence_artifact_missing",
                    detail="A normalized input sequence artifact is missing.",
                    context={"path": unique_sequence.sequence_artifact_path},
                )
            with sequence_path.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, output_handle, length=_COPY_CHUNK_SIZE)


def _build_artifact_infos(
    *,
    protected_files: dict[str, _ProtectedFile],
) -> tuple[ResultPackageArtifactInfo, ...]:
    artifacts: list[ResultPackageArtifactInfo] = []
    for package_path in sorted(protected_files):
        protected = protected_files[package_path]
        size = protected.source_path.stat().st_size
        sha256 = _sha256_file(protected.source_path)
        artifacts.append(
            ResultPackageArtifactInfo(
                path=package_path,
                stage=protected.stage_id,
                media_type=infer_media_type(package_path),
                size=size,
                sha256=sha256,
            )
        )
    return tuple(artifacts)


def _build_package_zip(
    *,
    package_path: Path,
    protected_files: dict[str, _ProtectedFile],
    package_manifest: JelicaPackageManifest,
) -> None:
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.unlink(missing_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=package_path.parent,
            prefix=f"{package_path.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for package_member_path in sorted(protected_files):
                protected = protected_files[package_member_path]
                if protected.source_path.is_symlink():
                    raise ResultPackageStageError(
                        reason="result_package_symlink_unsupported",
                        detail="Protected artifacts must not be symbolic links.",
                        context={"path": package_member_path},
                    )
                archive.write(
                    filename=protected.source_path,
                    arcname=package_member_path,
                )
            archive.writestr(
                JELICA_PACKAGE_MANIFEST_PATH,
                serialize_stable_json(package_manifest.model_dump(mode="json")).encode("utf-8"),
            )
        os.replace(temporary_path, package_path)
    except OSError as error:
        raise ResultPackageStageError(
            reason="result_package_zip_build_failed",
            detail="Result package ZIP assembly failed.",
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_COPY_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_stage(
    *,
    committed_stages: tuple[_CommittedStageSnapshot, ...],
    stage_id: str,
) -> _CommittedStageSnapshot:
    for stage in committed_stages:
        if stage.stage_id == stage_id:
            return stage
    raise ResultPackageStageError(
        reason="result_package_stage_missing",
        detail="A required upstream stage snapshot is missing.",
        context={"stage_id": stage_id},
    )


def _resolve_stage_artifact(
    *,
    stage: _CommittedStageSnapshot,
    artifact_relative_path: str,
) -> Path:
    if artifact_relative_path not in stage.snapshot.manifest.artifacts:
        raise ResultPackageStageError(
            reason="result_package_artifact_reference_invalid",
            detail="Requested stage artifact is not declared in committed manifest.",
            context={"stage_id": stage.stage_id, "relative_path": artifact_relative_path},
        )
    path = stage.stage_root / artifact_relative_path
    if not path.is_file() or path.is_symlink():
        raise ResultPackageStageError(
            reason="result_package_artifact_missing",
            detail="A declared committed artifact is missing or invalid.",
            context={"stage_id": stage.stage_id, "relative_path": artifact_relative_path},
        )
    return path


def _package_path_for_stage_artifact(
    *,
    stage_id: str,
    artifact_relative_path: str,
) -> str:
    normalized = artifact_relative_path.replace("\\", "/").strip()
    if normalized == "":
        raise ResultPackageStageError(
            reason="result_package_path_invalid",
            detail="Stage artifact path must not be empty.",
            context={"stage_id": stage_id},
        )
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if posix.is_absolute() or windows.is_absolute() or ".." in posix.parts or ".." in windows.parts:
        raise ResultPackageStageError(
            reason="result_package_path_invalid",
            detail="Stage artifact path is not a safe relative path.",
            context={"stage_id": stage_id, "relative_path": normalized},
        )
    if normalized.startswith(f"{stage_id}/"):
        return f"results/{normalized}"
    return f"results/{stage_id}/{normalized}"


def _sanitize_json_payload(
    *,
    payload: dict[str, object],
    task_dir: Path,
    drop_source_errors: bool,
) -> dict[str, object]:
    sanitized = _sanitize_json_value(
        value=payload,
        key=None,
        task_dir=task_dir,
    )
    if not isinstance(sanitized, dict):
        raise ResultPackageStageError(
            reason="result_package_manifest_invalid",
            detail="Input manifest payload must remain a JSON object.",
        )
    if drop_source_errors:
        sanitized["source_errors"] = []
    return sanitized


def _sanitize_json_value(
    *,
    value: object,
    key: str | None,
    task_dir: Path,
) -> object:
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for child_key, child_value in value.items():
            normalized_key = str(child_key)
            if normalized_key == "source_path":
                continue
            sanitized[normalized_key] = _sanitize_json_value(
                value=child_value,
                key=normalized_key,
                task_dir=task_dir,
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_json_value(value=item, key=key, task_dir=task_dir) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value=value, key=key, task_dir=task_dir)
    return value


def _sanitize_text(*, value: str, key: str | None, task_dir: Path) -> str:
    normalized = value.strip()
    if normalized == "":
        return normalized
    if key == "selector" and "::" in normalized:
        left, right = normalized.rsplit("::", maxsplit=1)
        sanitized_left = _sanitize_text(value=left, key="source_reference", task_dir=task_dir)
        return f"{sanitized_left}::{right.strip()}"
    if key == "config_revision_path":
        relative = _to_task_relative_path(value=normalized, task_dir=task_dir)
        if relative is not None:
            return relative
    if _looks_like_absolute_path(normalized):
        basename = Path(normalized).name.strip()
        return basename if basename != "" else "local_path"
    if key == "detail":
        return _ABSOLUTE_PATH_TOKEN_PATTERN.sub(
            lambda match: _sanitize_absolute_path_token(match.group(0)),
            normalized,
        )
    return normalized


def _sanitize_absolute_path_token(token: str) -> str:
    if not _looks_like_absolute_path(token):
        return token
    basename = Path(token).name.strip()
    return basename if basename != "" else "local_path"


def _looks_like_absolute_path(value: str) -> bool:
    if "://" in value:
        return False
    candidate = value.strip()
    if candidate == "":
        return False
    posix = PurePosixPath(candidate)
    windows = PureWindowsPath(candidate)
    return posix.is_absolute() or windows.is_absolute()


def _to_task_relative_path(*, value: str, task_dir: Path) -> str | None:
    if not _looks_like_absolute_path(value):
        return None
    try:
        relative = Path(value).resolve(strict=False).relative_to(task_dir.resolve(strict=False))
    except ValueError:
        return None
    return relative.as_posix()


def _load_json_object(*, path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResultPackageStageError(
            reason="result_package_json_unreadable",
            detail="A required JSON artifact could not be read.",
            context={"path": path.as_posix()},
        ) from error
    if not isinstance(payload, dict):
        raise ResultPackageStageError(
            reason="result_package_json_invalid",
            detail="A required JSON artifact must be an object.",
            context={"path": path.as_posix()},
        )
    return {str(key): value for key, value in payload.items()}


__all__ = [
    "RESULT_PACKAGE_FAILED_EVENT",
    "RESULT_PACKAGE_PROGRESS_EVENT",
    "RESULT_PACKAGE_STARTED_EVENT",
    "ResultPackageStage",
    "ResultPackageStageError",
]
