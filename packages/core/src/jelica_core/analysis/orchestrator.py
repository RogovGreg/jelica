from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from jelica_core.config import (
    AnalysisAlignmentMode,
    AnalysisConfigInput,
    AnalysisConfigResolutionResult,
    ConfigParser,
    apply_config_overrides,
    convert_config_validation_error,
    resolve_analysis_config,
)
from jelica_core.input_sources import resolve_submission_reference, resolve_submission_source
from jelica_core.sample_metadata import load_sample_metadata_csv
from jelica_core.tasks import (
    InitializedAnalysisTask,
    LocalTaskStorage,
    TaskDirectoryAlreadyExistsError,
    TaskStorageError,
)
from jelica_core.tasks.names import generate_automatic_task_name
from jelica_core.tasks.timestamps import utc_now

from .errors import AnalysisTaskInitializationError
from .models import InitializeAnalysisTaskRequest
from .planning import resolve_analysis_execution_selection

TASK_ID_COLLISION_RETRY_LIMIT = 5


class AnalysisOrchestrator:
    """Orchestrate initialization of an analysis task (stage 1 only)."""

    def __init__(
        self,
        *,
        config_parser: ConfigParser | None = None,
        task_id_generator: Callable[[], UUID] | None = None,
        trace_id_generator: Callable[[], UUID] | None = None,
        clock: Callable[[], datetime] | None = None,
        collision_retry_limit: int = TASK_ID_COLLISION_RETRY_LIMIT,
    ) -> None:
        self._config_parser = config_parser or ConfigParser()
        self._task_id_generator = task_id_generator or uuid4
        self._trace_id_generator = trace_id_generator or uuid4
        self._clock = clock or utc_now
        self._collision_retry_limit = collision_retry_limit

    def initialize_task(
        self,
        *,
        request: InitializeAnalysisTaskRequest,
        task_storage: LocalTaskStorage,
        default_alignment_mode: AnalysisAlignmentMode = AnalysisAlignmentMode.COMPUTE,
    ) -> InitializedAnalysisTask:
        resolution = self.resolve_request(
            request=request,
            default_alignment_mode=default_alignment_mode,
        )
        trace_id = request.trace_id or resolution.config.trace_id or self._trace_id_generator()
        task_config = resolution.config.model_copy(update={"trace_id": trace_id})

        for _ in range(self._collision_retry_limit):
            task_id = str(self._task_id_generator())
            task_name = request.name or generate_automatic_task_name(self._clock())
            try:
                workspace = task_storage.create_task_workspace(task_id=task_id, config=task_config)
            except TaskDirectoryAlreadyExistsError:
                continue
            except TaskStorageError as error:
                raise AnalysisTaskInitializationError(
                    f"Failed to initialize analysis task: {error}"
                ) from error

            return InitializedAnalysisTask(
                task_id=task_id,
                name=task_name,
                task_dir=workspace.task_dir,
                config_path=workspace.config_path,
                config=task_config,
                current_config_revision=workspace.current_config_revision,
                current_config_relative_path=workspace.current_config_relative_path,
                current_config_hash=workspace.current_config_hash,
                warnings=resolution.warnings,
            )

        raise AnalysisTaskInitializationError(
            "Failed to initialize analysis task: cannot allocate a unique task_id."
        )

    def resolve_request(
        self,
        *,
        request: InitializeAnalysisTaskRequest,
        default_alignment_mode: AnalysisAlignmentMode = AnalysisAlignmentMode.COMPUTE,
    ) -> AnalysisConfigResolutionResult:
        """Resolve one initialization request without creating task state."""

        config_input = self._parse_input_config(request.config_json)
        config_with_overrides = apply_config_overrides(
            base_config=config_input,
            overrides=request.overrides,
        )
        final_input = _apply_positional_samples(
            config_input=config_with_overrides,
            positional_sources=request.positional_sources,
        )
        final_input = bind_submission_paths(
            config_input=final_input,
            base_directory=request.submission_base_dir,
        )
        resolution = resolve_analysis_config(
            final_input,
            default_alignment_mode=default_alignment_mode,
        )
        if request.sample_metadata_csv is not None:
            metadata_overrides = load_sample_metadata_csv(
                metadata_csv=request.sample_metadata_csv,
                submission_base_dir=request.submission_base_dir,
            )
            resolution = resolution.model_copy(
                update={
                    "config": resolution.config.model_copy(
                        update={"sample_metadata": metadata_overrides}
                    )
                }
            )
        resolve_analysis_execution_selection(
            config=resolution.config,
            allow_explicit_from_phase=True,
        )
        return resolution

    def _parse_input_config(self, config_json: str | None) -> AnalysisConfigInput:
        if config_json is None:
            return AnalysisConfigInput()
        return self._config_parser.parse(config_json)


def _apply_positional_samples(
    *,
    config_input: AnalysisConfigInput,
    positional_sources: tuple[str, ...],
) -> AnalysisConfigInput:
    if len(positional_sources) == 0:
        return config_input

    mutable_config = config_input.model_dump(mode="python")
    mutable_config["samples"] = list(positional_sources)
    try:
        return AnalysisConfigInput.model_validate(mutable_config)
    except ValidationError as error:
        raise convert_config_validation_error(error) from error


def bind_submission_paths(
    *,
    config_input: AnalysisConfigInput,
    base_directory: Path | None,
) -> AnalysisConfigInput:
    if base_directory is None:
        return config_input
    payload = config_input.model_dump(mode="python")
    samples = payload.get("samples")
    if isinstance(samples, list):
        payload["samples"] = [
            None
            if source is None
            else resolve_submission_source(source=source, base_directory=base_directory)
            for source in samples
        ]
    reference = payload.get("reference")
    if isinstance(reference, str):
        payload["reference"] = resolve_submission_reference(
            reference=reference,
            base_directory=base_directory,
        )
    try:
        return AnalysisConfigInput.model_validate(payload)
    except ValidationError as error:
        raise convert_config_validation_error(error) from error
