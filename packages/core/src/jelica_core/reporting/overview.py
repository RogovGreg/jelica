from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jelica_core.alignment import ALIGNMENT_MANIFEST_RELATIVE_PATH, AlignmentManifest
from jelica_core.distance_matrix import (
    DISTANCE_MATRIX_JSON_RELATIVE_PATH,
    DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
    DistanceMatrixResult,
    DistancePairRecord,
)
from jelica_core.phylogenetic_tree import TREE_JSON_RELATIVE_PATH, PhylogeneticTreeResult
from jelica_core.result_package import (
    JelicaPackageManifest,
    JelicaPackageReader,
    JelicaPackageReaderError,
    JelicaPackageValidator,
    ResultPackageStageInfo,
)
from jelica_core.runtime.input_processing_models import (
    INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
    InputProcessingManifest,
)


class ResultOverviewBuildErrorCode(StrEnum):
    INVALID_SOURCE_PACKAGE = "invalid_source_package"
    OVERVIEW_MODEL_BUILD_FAILED = "overview_model_build_failed"


class ResultOverviewBuildError(RuntimeError):
    def __init__(self, *, code: ResultOverviewBuildErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class ResultOverviewSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str = Field(min_length=1)
    sequence_id: str | None = None
    original_record_id: str | None = None
    original_description: str | None = None
    source_reference: str = Field(min_length=1)
    validation_status: str = Field(min_length=1)
    eligible_for_analysis: bool
    source_length: int | None = Field(default=None, ge=0)
    gc_content: float | None = Field(default=None, ge=0.0, le=1.0)
    ambiguous_count: int | None = Field(default=None, ge=0)


class ResultOverviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid_sample_count: int = Field(ge=0)
    invalid_sample_count: int = Field(ge=0)
    unique_sequence_count: int = Field(ge=0)
    duplicate_logical_sample_count: int = Field(ge=0)
    samples: tuple[ResultOverviewSample, ...] = ()


class ResultOverviewAlignment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alignment_length: int | None = Field(default=None, ge=0)
    logical_sample_count: int = Field(ge=0)
    unique_sequence_count: int = Field(ge=0)
    mode: str = Field(min_length=1)
    engine: str | None = None


class ResultOverviewDistance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    expected_pair_count: int = Field(ge=0)
    processed_pair_count: int = Field(ge=0)
    defined_distance_count: int = Field(ge=0)
    undefined_distance_count: int = Field(ge=0)
    sequence_references: tuple[dict[str, object], ...] = ()
    matrix: tuple[tuple[float | None, ...], ...] = ()
    pairs: tuple[dict[str, object], ...] = ()


class ResultOverviewTree(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str = Field(min_length=1)
    applied_rooting: str = Field(min_length=1)
    canonical_leaf_order: tuple[str, ...] = ()
    leaf_mappings: tuple[dict[str, object], ...] = ()
    rooted: dict[str, object]


class ResultOverview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    input_processing: ResultOverviewInput | None = None
    alignment: ResultOverviewAlignment | None = None
    distance_matrix: ResultOverviewDistance | None = None
    phylogenetic_tree: ResultOverviewTree | None = None


class ResultOverviewBuilder:
    def build(self, *, package_path: Path | str) -> ResultOverview:
        package = Path(package_path)
        validation = JelicaPackageValidator().validate(package)
        if not validation.valid:
            issue = validation.errors[0]
            raise ResultOverviewBuildError(
                code=ResultOverviewBuildErrorCode.INVALID_SOURCE_PACKAGE,
                message=f"Result package validation failed ({issue.code.value}).",
            )

        try:
            with JelicaPackageReader(path=package) as reader:
                manifest = reader.read_manifest()
                return self._build_from_manifest(reader=reader, manifest=manifest)
        except (
            JelicaPackageReaderError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as error:
            raise ResultOverviewBuildError(
                code=ResultOverviewBuildErrorCode.OVERVIEW_MODEL_BUILD_FAILED,
                message="Result overview could not be built from the package.",
            ) from error

    def _build_from_manifest(
        self,
        *,
        reader: JelicaPackageReader,
        manifest: JelicaPackageManifest,
    ) -> ResultOverview:
        return ResultOverview(
            task_id=manifest.task.task_id,
            content_id=manifest.content_id,
            input_processing=self._load_input_processing(reader=reader, manifest=manifest),
            alignment=self._load_alignment(reader=reader, manifest=manifest),
            distance_matrix=self._load_distance_matrix(reader=reader, manifest=manifest),
            phylogenetic_tree=self._load_tree(reader=reader, manifest=manifest),
        )

    def _load_input_processing(
        self,
        *,
        reader: JelicaPackageReader,
        manifest: JelicaPackageManifest,
    ) -> ResultOverviewInput | None:
        path = _stage_artifact_path(
            manifest=manifest,
            stage_name="input_processing",
            suffix=INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,
        )
        if path is None:
            return None
        source = InputProcessingManifest.model_validate(reader.read_json_file(path=path))
        facts_by_sequence_id = {
            sequence.sequence_id: sequence.facts for sequence in source.unique_sequences
        }
        samples = tuple(
            ResultOverviewSample(
                sample_id=sample.sample_id,
                sequence_id=sample.sequence_id,
                original_record_id=sample.original_record_id,
                original_description=sample.original_description,
                source_reference=sample.provenance.input_manifest_source_reference,
                validation_status=sample.validation_status.value,
                eligible_for_analysis=sample.eligible_for_analysis,
                source_length=(
                    facts_by_sequence_id[sample.sequence_id].source_length
                    if sample.sequence_id in facts_by_sequence_id
                    else None
                ),
                gc_content=(
                    facts_by_sequence_id[sample.sequence_id].resolved_gc_content
                    if sample.sequence_id in facts_by_sequence_id
                    else None
                ),
                ambiguous_count=(
                    facts_by_sequence_id[sample.sequence_id].ambiguous_count
                    if sample.sequence_id in facts_by_sequence_id
                    else None
                ),
            )
            for sample in source.logical_samples
        )
        summary = source.dataset_summary
        return ResultOverviewInput(
            valid_sample_count=summary.valid_sample_count,
            invalid_sample_count=summary.invalid_sample_count,
            unique_sequence_count=summary.unique_sequence_count,
            duplicate_logical_sample_count=summary.duplicate_logical_sample_count,
            samples=samples,
        )

    def _load_alignment(
        self,
        *,
        reader: JelicaPackageReader,
        manifest: JelicaPackageManifest,
    ) -> ResultOverviewAlignment | None:
        path = _stage_artifact_path(
            manifest=manifest,
            stage_name="alignment",
            suffix=ALIGNMENT_MANIFEST_RELATIVE_PATH,
        )
        if path is None:
            return None
        source = AlignmentManifest.model_validate(reader.read_json_file(path=path))
        return ResultOverviewAlignment(
            alignment_length=source.alignment_length,
            logical_sample_count=source.logical_sample_count,
            unique_sequence_count=source.unique_sequence_count,
            mode=source.mode.value,
            engine=source.resolved_engine.value if source.resolved_engine is not None else None,
        )

    def _load_distance_matrix(
        self,
        *,
        reader: JelicaPackageReader,
        manifest: JelicaPackageManifest,
    ) -> ResultOverviewDistance | None:
        stage = _stage(manifest=manifest, stage_name="distance_matrix")
        if stage is None:
            return None
        matrix_path = _artifact_path(stage=stage, suffix=DISTANCE_MATRIX_JSON_RELATIVE_PATH)
        if matrix_path is None:
            return None
        source = DistanceMatrixResult.model_validate(reader.read_json_file(path=matrix_path))
        pairs_path = _artifact_path(stage=stage, suffix=DISTANCE_PAIRS_JSONL_RELATIVE_PATH)
        pairs = (
            _read_distance_pairs(reader=reader, path=pairs_path)
            if pairs_path is not None
            else tuple()
        )
        return ResultOverviewDistance(
            model=source.model.value,
            expected_pair_count=source.expected_pair_count,
            processed_pair_count=source.processed_pair_count,
            defined_distance_count=source.defined_distance_count,
            undefined_distance_count=source.undefined_distance_count,
            sequence_references=tuple(
                reference.model_dump(mode="json") for reference in source.sequence_references
            ),
            matrix=source.matrix,
            pairs=tuple(pair.model_dump(mode="json") for pair in pairs),
        )

    def _load_tree(
        self,
        *,
        reader: JelicaPackageReader,
        manifest: JelicaPackageManifest,
    ) -> ResultOverviewTree | None:
        path = _stage_artifact_path(
            manifest=manifest,
            stage_name="phylogenetic_tree",
            suffix=TREE_JSON_RELATIVE_PATH,
        )
        if path is None:
            return None
        source = PhylogeneticTreeResult.model_validate(reader.read_json_file(path=path))
        return ResultOverviewTree(
            method=source.method.value,
            applied_rooting=source.applied_rooting,
            canonical_leaf_order=source.canonical_leaf_order,
            leaf_mappings=tuple(
                mapping.model_dump(mode="json") for mapping in source.leaf_mappings
            ),
            rooted=source.rooted.model_dump(mode="json"),
        )


def build_result_overview(*, package_path: Path | str) -> ResultOverview:
    return ResultOverviewBuilder().build(package_path=package_path)


def _stage(
    *,
    manifest: JelicaPackageManifest,
    stage_name: str,
) -> ResultPackageStageInfo | None:
    return next((stage for stage in manifest.stages if stage.name == stage_name), None)


def _stage_artifact_path(
    *,
    manifest: JelicaPackageManifest,
    stage_name: str,
    suffix: str,
) -> str | None:
    stage = _stage(manifest=manifest, stage_name=stage_name)
    return _artifact_path(stage=stage, suffix=suffix) if stage is not None else None


def _artifact_path(*, stage: ResultPackageStageInfo, suffix: str) -> str | None:
    normalized_suffix = suffix.replace("\\", "/")
    for artifact_path in stage.artifacts:
        normalized = artifact_path.replace("\\", "/")
        if normalized == normalized_suffix or normalized.endswith(f"/{normalized_suffix}"):
            return normalized
    return None


def _read_distance_pairs(
    *,
    reader: JelicaPackageReader,
    path: str,
) -> tuple[DistancePairRecord, ...]:
    decoded = reader.read_bytes(path=path).decode("utf-8")
    records: list[DistancePairRecord] = []
    for line in decoded.splitlines():
        normalized = line.strip()
        if normalized == "":
            continue
        records.append(DistancePairRecord.model_validate_json(normalized))
    return tuple(records)


__all__ = [
    "ResultOverview",
    "ResultOverviewAlignment",
    "ResultOverviewBuildError",
    "ResultOverviewBuildErrorCode",
    "ResultOverviewBuilder",
    "ResultOverviewDistance",
    "ResultOverviewInput",
    "ResultOverviewSample",
    "ResultOverviewTree",
    "build_result_overview",
]
