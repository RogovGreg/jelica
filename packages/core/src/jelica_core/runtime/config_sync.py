from __future__ import annotations

import json
from pathlib import Path

from jelica_core.config import AnalysisAlignmentMode, AnalysisConfigInput, resolve_analysis_config
from jelica_core.tasks import (
    ACTIVE_ANALYTICAL_TASK_JOB_STATES,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskRecord,
    AnalyticalTaskRegistryService,
    AnalyticalTaskSnapshot,
    AnalyticalTaskState,
)
from jelica_core.tasks.storage import TASK_CONFIG_FILENAME, compute_config_hash


class TaskConfigSynchronizationError(RuntimeError):
    """Raised when task config synchronization cannot be completed safely."""


def synchronize_task_config_before_start(
    *,
    task_snapshot: AnalyticalTaskSnapshot,
    tasks_dir: Path,
    registry_service: AnalyticalTaskRegistryService,
    input_directory_max_depth: int | None = None,
    ncbi_max_retries: int | None = None,
    default_alignment_mode: AnalysisAlignmentMode | str = AnalysisAlignmentMode.COMPUTE,
) -> AnalyticalTaskRecord:
    task = task_snapshot.task
    active_or_latest_job = task_snapshot.active_or_latest_job

    if (
        active_or_latest_job is not None
        and active_or_latest_job.state in ACTIVE_ANALYTICAL_TASK_JOB_STATES
    ):
        return task

    if (
        active_or_latest_job is not None
        and active_or_latest_job.state is AnalyticalTaskState.COMPLETED
    ):
        return task

    task_dir = tasks_dir / task.task_dir_relative_path
    config_path = task_dir / TASK_CONFIG_FILENAME
    config_document = _load_json_object(path=config_path)
    config_matches_current_revision = (
        compute_config_hash(config_document) == task.current_config_hash
    )
    normalized_config_document = _normalize_config_document(
        config_document=config_document,
        input_directory_max_depth=input_directory_max_depth,
        ncbi_max_retries=ncbi_max_retries,
        default_alignment_mode=default_alignment_mode,
        trusted_resolved_config=config_matches_current_revision,
    )
    normalized_hash = compute_config_hash(normalized_config_document)
    if normalized_hash == task.current_config_hash:
        return task

    mutation = registry_service.update_task_config(
        task_id=task.task_id,
        config_document=normalized_config_document,
    )
    if mutation.result_type in {
        AnalyticalTaskMutationResultType.APPLIED,
        AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
    }:
        if mutation.task is not None:
            return mutation.task
        return registry_service.get_task(task_id=task.task_id)

    raise TaskConfigSynchronizationError(
        "cannot synchronize task config before start: "
        f"mutation_result={mutation.result_type.value}, task_id='{task.task_id}'"
    )


def _load_json_object(*, path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise TaskConfigSynchronizationError(
            f"cannot read task config '{path}': {error}"
        ) from error
    except json.JSONDecodeError as error:
        raise TaskConfigSynchronizationError(
            f"task config '{path}' is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise TaskConfigSynchronizationError(f"task config '{path}' must be a JSON object")
    return {str(key): value for key, value in payload.items()}


def _normalize_config_document(
    *,
    config_document: dict[str, object],
    input_directory_max_depth: int | None,
    ncbi_max_retries: int | None,
    default_alignment_mode: AnalysisAlignmentMode | str,
    trusted_resolved_config: bool = False,
) -> dict[str, object]:
    input_document = (
        _resolved_config_as_strict_input(config_document)
        if trusted_resolved_config
        else config_document
    )
    config_input = AnalysisConfigInput.model_validate(input_document)
    resolved = resolve_analysis_config(
        config_input,
        default_alignment_mode=default_alignment_mode,
    )
    # Import locally to avoid an import cycle while the runtime package initializes.
    from jelica_core.analysis.planning import resolve_analysis_execution_selection

    resolve_analysis_execution_selection(
        config=resolved.config,
        allow_explicit_from_phase=True,
    )
    normalized = resolved.config.model_dump(mode="json")

    depth = _resolve_non_negative_int(
        key="input_directory_max_depth",
        explicit_value=input_directory_max_depth,
        config_document=config_document,
    )
    retries = _resolve_non_negative_int(
        key="ncbi_max_retries",
        explicit_value=ncbi_max_retries,
        config_document=config_document,
    )
    if depth is not None:
        normalized["input_directory_max_depth"] = depth
    if retries is not None:
        normalized["ncbi_max_retries"] = retries
    return normalized


def _resolved_config_as_strict_input(
    config_document: dict[str, object],
) -> dict[str, object]:
    """Remove only resolved no-op defaults that strict user input intentionally rejects."""
    normalized = dict(config_document)
    raw_alignment = normalized.get("alignment")
    if not isinstance(raw_alignment, dict):
        return normalized
    alignment = dict(raw_alignment)
    raw_mafft = alignment.get("mafft")
    if not isinstance(raw_mafft, dict) or raw_mafft.get("strategy") != "auto":
        normalized["alignment"] = alignment
        return normalized
    mafft = dict(raw_mafft)
    for field_name in ("progressive_threads", "iterative_threads"):
        if mafft.get(field_name) == "auto":
            mafft.pop(field_name)
    alignment["mafft"] = mafft
    normalized["alignment"] = alignment
    return normalized


def _resolve_non_negative_int(
    *,
    key: str,
    explicit_value: int | None,
    config_document: dict[str, object],
) -> int | None:
    if explicit_value is not None:
        if explicit_value < 0:
            raise TaskConfigSynchronizationError(f"{key} must be >= 0")
        return explicit_value

    raw_value = config_document.get(key)
    if raw_value is None:
        return None
    if type(raw_value) is not int or raw_value < 0:
        raise TaskConfigSynchronizationError(f"{key} must be a non-negative integer")
    return raw_value
