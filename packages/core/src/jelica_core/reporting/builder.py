from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from jelica_core.alignment import ALIGNMENT_MANIFEST_RELATIVE_PATH, AlignmentManifest
from jelica_core.clade_detection import (
    CLADE_DETECTION_MANIFEST_RELATIVE_PATH,
    INFERRED_CLADES_JSON_RELATIVE_PATH,
)
from jelica_core.comparative_analysis import (
    COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,
    COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,
    DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,
    PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH,
    ComparativeFailureRecord,
    SequenceComparisonSummaryRecord,
)
from jelica_core.distance_matrix import (
    DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,
    DISTANCE_PAIRS_JSONL_RELATIVE_PATH,
    DistancePairRecord,
)
from jelica_core.phylogenetic_tree import (
    PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,
    TREE_DIAGNOSTICS_RELATIVE_PATH,
    TREE_JSON_RELATIVE_PATH,
    TREE_ROOTED_NWK_RELATIVE_PATH,
    TREE_UNROOTED_NWK_RELATIVE_PATH,
)
from jelica_core.result_package import (
    JELICA_PACKAGE_INPUT_MANIFEST_PATH,
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

from .models import (
    AnalysisReportModel,
    PairwiseReportItem,
    ReportMessage,
    ReportMetadata,
    ReportMetric,
    ReportStage,
)

_STAGE_DISPLAY_NAMES: Final[dict[str, str]] = {
    "input_acquisition": "Input acquisition",
    "input_processing": "Input processing",
    "alignment": "Alignment",
    "comparative_analysis": "Comparative analysis",
    "distance_matrix": "Distance matrix",
    "phylogenetic_tree": "Phylogenetic tree",
    "clade_detection": "Clade detection",
    "result_package": "Result package",
}
_NO_STAGE_DETAILS_MESSAGE: Final = "No published details are available for this stage."
_NO_WARNING_DETAILS_MESSAGE: Final = "Detailed warning messages are not available in this package."
_NO_ERROR_DETAILS_MESSAGE: Final = "Detailed error messages are not available in this package."
_NO_FAILURE_DETAILS_MESSAGE: Final = (
    "Detailed failure diagnostics are not available in this package."
)


class AnalysisReportBuildErrorCode(StrEnum):
    INVALID_SOURCE_PACKAGE = "invalid_source_package"
    REPORT_MODEL_BUILD_FAILED = "report_model_build_failed"


class AnalysisReportBuildError(RuntimeError):
    def __init__(self, *, code: AnalysisReportBuildErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _DistancePairSummary:
    distances_by_pair: dict[tuple[str, str], float | None]
    minimum_distance: float | None
    maximum_distance: float | None
    undefined_pair_count: int


@dataclass(frozen=True, slots=True)
class _StageExtraction:
    stage: ReportStage
    pairwise_items: tuple[PairwiseReportItem, ...] = tuple()


class AnalysisReportBuilder:
    def build(self, *, package_path: Path | str) -> AnalysisReportModel:
        package = Path(package_path)
        validation = JelicaPackageValidator().validate(package)
        if not validation.valid:
            issue = validation.errors[0]
            raise AnalysisReportBuildError(
                code=AnalysisReportBuildErrorCode.INVALID_SOURCE_PACKAGE,
                message=f"Result package validation failed ({issue.code.value}).",
            )

        try:
            with JelicaPackageReader(path=package) as reader:
                manifest = reader.read_manifest()
                return self._build_from_manifest(reader=reader, manifest=manifest)
        except (JelicaPackageReaderError, OSError, ValueError) as error:
            raise AnalysisReportBuildError(
                code=AnalysisReportBuildErrorCode.REPORT_MODEL_BUILD_FAILED,
                message="Report model could not be built from the package.",
            ) from error

    def _build_from_manifest(
        self,
        *,
        reader: JelicaPackageReader,
        manifest: JelicaPackageManifest,
    ) -> AnalysisReportModel:
        metadata = ReportMetadata(
            task_id=manifest.task.task_id,
            task_status=manifest.task.status.value,
            created_at=manifest.task.created_at,
            completed_at=manifest.task.completed_at,
            package_created_at=manifest.package_created_at,
            content_id=manifest.content_id,
            format_version=manifest.format_version,
            producer_version=manifest.producer.version,
            stage_count=len(manifest.stages),
            protected_artifact_count=len(manifest.artifacts),
        )

        distance_summary = self._load_distance_pair_summary(reader=reader, manifest=manifest)
        stage_reports: list[ReportStage] = []
        pairwise_items: list[PairwiseReportItem] = []

        for stage_info in manifest.stages:
            extraction = self._build_stage_report(
                reader=reader,
                manifest=manifest,
                stage_info=stage_info,
                distance_summary=distance_summary,
            )
            stage_reports.append(extraction.stage)
            pairwise_items.extend(extraction.pairwise_items)

        return AnalysisReportModel(
            metadata=metadata,
            stages=tuple(stage_reports),
            pairwise_comparisons=tuple(pairwise_items),
        )

    def _build_stage_report(
        self,
        *,
        reader: JelicaPackageReader,
        manifest: JelicaPackageManifest,
        stage_info: ResultPackageStageInfo,
        distance_summary: _DistancePairSummary,
    ) -> _StageExtraction:
        stage_id = stage_info.name
        display_name = _STAGE_DISPLAY_NAMES.get(stage_id, stage_id.replace("_", " ").title())
        status = stage_info.status

        if stage_id == "input_acquisition":
            stage = self._build_input_acquisition_stage(
                reader=reader,
                stage_info=stage_info,
                display_name=display_name,
            )
            return _StageExtraction(stage=stage)
        if stage_id == "input_processing":
            stage = self._build_input_processing_stage(
                reader=reader,
                stage_info=stage_info,
                display_name=display_name,
            )
            return _StageExtraction(stage=stage)
        if stage_id == "alignment":
            stage = self._build_alignment_stage(
                reader=reader,
                stage_info=stage_info,
                display_name=display_name,
            )
            return _StageExtraction(stage=stage)
        if stage_id == "comparative_analysis":
            return self._build_comparative_stage(
                reader=reader,
                stage_info=stage_info,
                display_name=display_name,
                distance_summary=distance_summary,
            )
        if stage_id == "distance_matrix":
            stage = self._build_distance_matrix_stage(
                reader=reader,
                stage_info=stage_info,
                display_name=display_name,
                distance_summary=distance_summary,
            )
            return _StageExtraction(stage=stage)
        if stage_id == "phylogenetic_tree":
            stage = self._build_phylogenetic_tree_stage(
                reader=reader,
                stage_info=stage_info,
                display_name=display_name,
            )
            return _StageExtraction(stage=stage)
        if stage_id == "clade_detection":
            stage = self._build_clade_detection_stage(
                reader=reader,
                stage_info=stage_info,
                display_name=display_name,
            )
            return _StageExtraction(stage=stage)
        if stage_id == "result_package":
            stage = self._build_result_package_stage(
                manifest=manifest,
                stage_info=stage_info,
                display_name=display_name,
            )
            return _StageExtraction(stage=stage)

        return _StageExtraction(
            stage=self._finalize_stage(
                stage_id=stage_id,
                display_name=display_name,
                status=status,
                metrics=tuple(),
                warnings=tuple(),
                errors=tuple(),
                details=tuple(),
            )
        )

    def _build_input_acquisition_stage(
        self,
        *,
        reader: JelicaPackageReader,
        stage_info: ResultPackageStageInfo,
        display_name: str,
    ) -> ReportStage:
        manifest_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(JELICA_PACKAGE_INPUT_MANIFEST_PATH,),
        )
        if manifest_path is None:
            return self._finalize_stage(
                stage_id=stage_info.name,
                display_name=display_name,
                status=stage_info.status,
                metrics=tuple(),
                warnings=tuple(),
                errors=tuple(),
                details=tuple(),
            )

        payload = _read_json_object(reader=reader, path=manifest_path)
        if payload is None:
            return self._finalize_stage(
                stage_id=stage_info.name,
                display_name=display_name,
                status=stage_info.status,
                metrics=tuple(),
                warnings=tuple(),
                errors=tuple(),
                details=tuple(),
            )

        sources = _as_list(payload.get("sources"))
        materialized_files = _as_list(payload.get("materialized_files"))
        source_errors = _as_list(payload.get("source_errors"))
        warnings: list[ReportMessage] = []
        for item in source_errors:
            message = _as_dict(item)
            if message is None:
                continue
            warnings.append(
                ReportMessage(
                    code=_as_non_empty_string(message.get("event_name")),
                    message=(
                        _as_non_empty_string(message.get("detail"))
                        or "Input source processing warning."
                    ),
                )
            )

        metrics = (
            ReportMetric(label="Number of input sources", value=str(len(sources))),
            ReportMetric(label="Number of materialized inputs", value=str(len(materialized_files))),
        )
        details: list[str] = []
        duplicates = _as_list(payload.get("skipped_duplicates"))
        if len(duplicates) > 0:
            details.append(f"Skipped duplicate sources: {len(duplicates)}")

        return self._finalize_stage(
            stage_id=stage_info.name,
            display_name=display_name,
            status=stage_info.status,
            metrics=metrics,
            warnings=tuple(warnings),
            errors=tuple(),
            details=tuple(details),
        )

    def _build_input_processing_stage(
        self,
        *,
        reader: JelicaPackageReader,
        stage_info: ResultPackageStageInfo,
        display_name: str,
    ) -> ReportStage:
        manifest_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(INPUT_PROCESSING_MANIFEST_RELATIVE_PATH,),
        )
        if manifest_path is None:
            return self._finalize_stage(
                stage_id=stage_info.name,
                display_name=display_name,
                status=stage_info.status,
                metrics=tuple(),
                warnings=tuple(),
                errors=tuple(),
                details=tuple(),
            )

        payload = _read_json_object(reader=reader, path=manifest_path)
        if payload is None:
            return self._finalize_stage(
                stage_id=stage_info.name,
                display_name=display_name,
                status=stage_info.status,
                metrics=tuple(),
                warnings=tuple(),
                errors=tuple(),
                details=tuple(),
            )

        warnings: list[ReportMessage] = []
        errors: list[ReportMessage] = []
        metrics: tuple[ReportMetric, ...]
        details: list[str] = []

        try:
            parsed_manifest = InputProcessingManifest.model_validate(payload)
            summary = parsed_manifest.dataset_summary
            metrics = (
                ReportMetric(label="Input records", value=str(summary.discovered_record_count)),
                ReportMetric(label="Normalized sequences", value=str(summary.valid_sample_count)),
                ReportMetric(label="Unique sequences", value=str(summary.unique_sequence_count)),
                ReportMetric(
                    label="Rejected/excluded records", value=str(summary.invalid_sample_count)
                ),
            )
            for issue in parsed_manifest.dataset_issues:
                target = errors if "error" in issue.severity.value.lower() else warnings
                target.append(ReportMessage(code=issue.code, message=issue.message))
            for file_summary in parsed_manifest.processed_files:
                for issue in file_summary.validation_issues:
                    target = errors if "error" in issue.severity.value.lower() else warnings
                    target.append(ReportMessage(code=issue.code, message=issue.message))
            details.append(
                "Comparative analysis available: "
                f"{'yes' if summary.comparative_analysis_available else 'no'}"
            )
        except ValidationError:
            summary_payload = _as_dict(payload.get("dataset_summary")) or {}
            metrics = (
                ReportMetric(
                    label="Input records",
                    value=str(
                        _coerce_int(summary_payload.get("discovered_record_count"), default=0)
                    ),
                ),
                ReportMetric(
                    label="Normalized sequences",
                    value=str(_coerce_int(summary_payload.get("valid_sample_count"), default=0)),
                ),
                ReportMetric(
                    label="Unique sequences",
                    value=str(_coerce_int(summary_payload.get("unique_sequence_count"), default=0)),
                ),
                ReportMetric(
                    label="Rejected/excluded records",
                    value=str(_coerce_int(summary_payload.get("invalid_sample_count"), default=0)),
                ),
            )
            for issue_payload in _as_list(payload.get("dataset_issues")):
                issue_data = _as_dict(issue_payload)
                if issue_data is None:
                    continue
                severity = (_as_non_empty_string(issue_data.get("severity")) or "").lower()
                target = errors if "error" in severity else warnings
                target.append(
                    ReportMessage(
                        code=_as_non_empty_string(issue_data.get("code")),
                        message=(
                            _as_non_empty_string(issue_data.get("message")) or "Validation issue."
                        ),
                    )
                )

        return self._finalize_stage(
            stage_id=stage_info.name,
            display_name=display_name,
            status=stage_info.status,
            metrics=metrics,
            warnings=tuple(warnings),
            errors=tuple(errors),
            details=tuple(details),
        )

    def _build_alignment_stage(
        self,
        *,
        reader: JelicaPackageReader,
        stage_info: ResultPackageStageInfo,
        display_name: str,
    ) -> ReportStage:
        manifest_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(ALIGNMENT_MANIFEST_RELATIVE_PATH,),
        )
        if manifest_path is None:
            return self._finalize_stage(
                stage_id=stage_info.name,
                display_name=display_name,
                status=stage_info.status,
                metrics=tuple(),
                warnings=tuple(),
                errors=tuple(),
                details=tuple(),
            )

        payload = _read_json_object(reader=reader, path=manifest_path)
        if payload is None:
            return self._finalize_stage(
                stage_id=stage_info.name,
                display_name=display_name,
                status=stage_info.status,
                metrics=tuple(),
                warnings=tuple(),
                errors=tuple(),
                details=tuple(),
            )

        details: list[str] = []
        try:
            alignment_manifest = AlignmentManifest.model_validate(payload)
            tool_name = (
                alignment_manifest.resolved_engine.value
                if alignment_manifest.resolved_engine is not None
                else alignment_manifest.mode.value
            )
            metrics = (
                ReportMetric(
                    label="Number of aligned sequences",
                    value=str(alignment_manifest.logical_sample_count),
                ),
                ReportMetric(
                    label="Alignment length",
                    value=str(alignment_manifest.alignment_length or 0),
                ),
                ReportMetric(label="Method/tool", value=tool_name),
            )
            if alignment_manifest.outcome.value != "completed":
                details.append(f"Outcome: {alignment_manifest.outcome.value}")
        except ValidationError:
            metrics = (
                ReportMetric(
                    label="Number of aligned sequences",
                    value=str(_coerce_int(payload.get("logical_sample_count"), default=0)),
                ),
                ReportMetric(
                    label="Alignment length",
                    value=str(_coerce_int(payload.get("alignment_length"), default=0)),
                ),
                ReportMetric(
                    label="Method/tool",
                    value=(
                        _as_non_empty_string(payload.get("resolved_engine"))
                        or _as_non_empty_string(payload.get("mode"))
                        or "n/a"
                    ),
                ),
            )
            outcome = _as_non_empty_string(payload.get("outcome"))
            if outcome is not None and outcome != "completed":
                details.append(f"Outcome: {outcome}")

        return self._finalize_stage(
            stage_id=stage_info.name,
            display_name=display_name,
            status=stage_info.status,
            metrics=metrics,
            warnings=tuple(),
            errors=tuple(),
            details=tuple(details),
        )

    def _build_comparative_stage(
        self,
        *,
        reader: JelicaPackageReader,
        stage_info: ResultPackageStageInfo,
        display_name: str,
        distance_summary: _DistancePairSummary,
    ) -> _StageExtraction:
        warnings: list[ReportMessage] = []
        errors: list[ReportMessage] = []
        details: list[str] = []

        manifest_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(COMPARATIVE_ANALYSIS_MANIFEST_RELATIVE_PATH,),
        )
        manifest_payload: dict[str, object] = {}
        if manifest_path is not None:
            loaded_manifest = _read_json_object(reader=reader, path=manifest_path)
            if loaded_manifest is not None:
                manifest_payload = loaded_manifest

        summary_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(DATASET_STATISTICAL_SUMMARY_RELATIVE_PATH,),
        )
        dataset_summary_payload: dict[str, object] = {}
        if summary_path is not None:
            loaded_summary = _read_json_object(reader=reader, path=summary_path)
            if loaded_summary is not None:
                dataset_summary_payload = loaded_summary

        failure_messages = self._load_comparative_failures(reader=reader, stage_info=stage_info)
        errors.extend(failure_messages.values())

        pairwise_items = self._load_pairwise_items(
            reader=reader,
            stage_info=stage_info,
            failure_messages=failure_messages,
            distances_by_pair=distance_summary.distances_by_pair,
        )
        if len(pairwise_items) == 0:
            details.append("No pairwise comparisons are available.")

        sequence_count = _coerce_int(dataset_summary_payload.get("sample_count"), default=None)
        if sequence_count is None:
            sequence_count = _coerce_int(
                _nested_value(manifest_payload, "plan_execution_counts", "unique_sequence_count"),
                default=0,
            )

        total_matches = sum(item.matches or 0 for item in pairwise_items)
        total_substitutions = sum(item.substitutions or 0 for item in pairwise_items)
        total_insertions = sum(item.insertions or 0 for item in pairwise_items)
        total_deletions = sum(item.deletions or 0 for item in pairwise_items)
        identities = [item.identity for item in pairwise_items if item.identity is not None]

        metrics_list = [
            ReportMetric(label="Number of sequences", value=str(sequence_count)),
            ReportMetric(label="Number of pairwise comparisons", value=str(len(pairwise_items))),
            ReportMetric(label="Total matches", value=str(total_matches)),
            ReportMetric(label="Total substitutions", value=str(total_substitutions)),
            ReportMetric(label="Total insertions", value=str(total_insertions)),
            ReportMetric(label="Total deletions", value=str(total_deletions)),
        ]
        if len(identities) > 0:
            minimum = min(identities)
            maximum = max(identities)
            average = sum(identities) / len(identities)
            metrics_list.append(
                ReportMetric(
                    label="Identity summary",
                    value=(
                        f"min={_format_float(minimum)}, max={_format_float(maximum)}, "
                        f"mean={_format_float(average)}"
                    ),
                )
            )

        stage = self._finalize_stage(
            stage_id=stage_info.name,
            display_name=display_name,
            status=stage_info.status,
            metrics=tuple(metrics_list),
            warnings=tuple(warnings),
            errors=tuple(errors),
            details=tuple(details),
        )
        return _StageExtraction(stage=stage, pairwise_items=pairwise_items)

    def _build_distance_matrix_stage(
        self,
        *,
        reader: JelicaPackageReader,
        stage_info: ResultPackageStageInfo,
        display_name: str,
        distance_summary: _DistancePairSummary,
    ) -> ReportStage:
        manifest_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(DISTANCE_MATRIX_MANIFEST_RELATIVE_PATH,),
        )
        payload: dict[str, object] = {}
        if manifest_path is not None:
            loaded = _read_json_object(reader=reader, path=manifest_path)
            if loaded is not None:
                payload = loaded

        matrix_dimensions = _as_list(payload.get("matrix_dimensions"))
        matrix_size_text = "n/a"
        if len(matrix_dimensions) == 2:
            rows = _coerce_int(matrix_dimensions[0], default=0)
            columns = _coerce_int(matrix_dimensions[1], default=0)
            matrix_size_text = f"{rows}x{columns}"

        warnings: list[ReportMessage] = []
        if distance_summary.undefined_pair_count > 0:
            warnings.append(
                ReportMessage(
                    code="undefined_distance_pairs",
                    message=(
                        "Some pairwise distances are undefined because no comparable sites were "
                        "available."
                    ),
                )
            )

        metrics = (
            ReportMetric(label="Matrix size", value=matrix_size_text),
            ReportMetric(
                label="Distance method/model",
                value=_as_non_empty_string(payload.get("model")) or "n/a",
            ),
            ReportMetric(
                label="Minimum distance",
                value=(
                    _format_float(distance_summary.minimum_distance)
                    if distance_summary.minimum_distance is not None
                    else "n/a"
                ),
            ),
            ReportMetric(
                label="Maximum distance",
                value=(
                    _format_float(distance_summary.maximum_distance)
                    if distance_summary.maximum_distance is not None
                    else "n/a"
                ),
            ),
        )

        return self._finalize_stage(
            stage_id=stage_info.name,
            display_name=display_name,
            status=stage_info.status,
            metrics=metrics,
            warnings=tuple(warnings),
            errors=tuple(),
            details=tuple(),
        )

    def _build_phylogenetic_tree_stage(
        self,
        *,
        reader: JelicaPackageReader,
        stage_info: ResultPackageStageInfo,
        display_name: str,
    ) -> ReportStage:
        manifest_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(PHYLOGENETIC_TREE_MANIFEST_RELATIVE_PATH,),
        )
        manifest_payload: dict[str, object] = {}
        if manifest_path is not None:
            loaded = _read_json_object(reader=reader, path=manifest_path)
            if loaded is not None:
                manifest_payload = loaded

        diagnostics_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(TREE_DIAGNOSTICS_RELATIVE_PATH,),
        )
        diagnostics_payload: dict[str, object] = {}
        if diagnostics_path is not None:
            loaded = _read_json_object(reader=reader, path=diagnostics_path)
            if loaded is not None:
                diagnostics_payload = loaded

        tree_payload_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(TREE_JSON_RELATIVE_PATH,),
        )
        tree_payload: dict[str, object] = {}
        if tree_payload_path is not None:
            loaded = _read_json_object(reader=reader, path=tree_payload_path)
            if loaded is not None:
                tree_payload = loaded

        warnings: list[ReportMessage] = []
        warning_entries = _as_list(diagnostics_payload.get("warnings"))
        for entry in warning_entries:
            warning = _as_dict(entry)
            if warning is None:
                continue
            warnings.append(
                ReportMessage(
                    code=_as_non_empty_string(warning.get("code")),
                    message=_as_non_empty_string(warning.get("detail")) or "Tree warning.",
                )
            )

        details: list[str] = []
        diameter = _format_float(
            _coerce_float(diagnostics_payload.get("tree_diameter"), default=0.0)
        )
        zero_diameter = _as_non_empty_string(diagnostics_payload.get("zero_diameter")) or "false"
        diagnostics_text = f"diameter={diameter}, zero_diameter={zero_diameter}"
        newick_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(TREE_ROOTED_NWK_RELATIVE_PATH, TREE_UNROOTED_NWK_RELATIVE_PATH),
        )
        newick_text = (
            _read_text(reader=reader, path=newick_path) if newick_path is not None else None
        )
        if newick_text is not None:
            compact = " ".join(newick_text.split())
            if len(compact) <= 240:
                details.append(f"Newick: {compact}")
            else:
                details.append(f"Newick: {compact[:240]}...")
                details.append("The complete Newick tree is stored in the JELICA package.")

        metrics = (
            ReportMetric(
                label="Construction method",
                value=(
                    _as_non_empty_string(manifest_payload.get("method"))
                    or _as_non_empty_string(tree_payload.get("method"))
                    or "n/a"
                ),
            ),
            ReportMetric(
                label="Rooted/unrooted",
                value=(
                    _as_non_empty_string(tree_payload.get("applied_rooting"))
                    or _as_non_empty_string(manifest_payload.get("applied_rooting"))
                    or "n/a"
                ),
            ),
            ReportMetric(
                label="Number of leaves",
                value=str(
                    _coerce_int(
                        manifest_payload.get("leaf_count"),
                        default=_coerce_int(diagnostics_payload.get("leaf_count"), default=0),
                    )
                ),
            ),
            ReportMetric(
                label="Diagnostics",
                value=diagnostics_text,
            ),
        )

        return self._finalize_stage(
            stage_id=stage_info.name,
            display_name=display_name,
            status=stage_info.status,
            metrics=metrics,
            warnings=tuple(warnings),
            errors=tuple(),
            details=tuple(details),
        )

    def _build_clade_detection_stage(
        self,
        *,
        reader: JelicaPackageReader,
        stage_info: ResultPackageStageInfo,
        display_name: str,
    ) -> ReportStage:
        manifest_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(CLADE_DETECTION_MANIFEST_RELATIVE_PATH,),
        )
        manifest_payload: dict[str, object] = {}
        if manifest_path is not None:
            loaded = _read_json_object(reader=reader, path=manifest_path)
            if loaded is not None:
                manifest_payload = loaded

        inferred_clades_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(INFERRED_CLADES_JSON_RELATIVE_PATH,),
        )
        inferred_payload: dict[str, object] = {}
        if inferred_clades_path is not None:
            loaded = _read_json_object(reader=reader, path=inferred_clades_path)
            if loaded is not None:
                inferred_payload = loaded

        clades = _as_list(inferred_payload.get("clades"))
        details: list[str] = []
        for index, raw_clade in enumerate(clades, start=1):
            clade = _as_dict(raw_clade)
            if clade is None:
                continue
            clade_id = _as_non_empty_string(clade.get("clade_id")) or f"clade-{index}"
            size = _coerce_int(clade.get("leaf_count"), default=0)
            members_payload = _as_list(clade.get("members"))
            member_ids: list[str] = []
            for raw_member in members_payload:
                member = _as_dict(raw_member)
                if member is None:
                    continue
                member_id = (
                    _as_non_empty_string(member.get("sequence_id"))
                    or _as_non_empty_string(member.get("leaf_label"))
                    or "unknown"
                )
                member_ids.append(member_id)
            if len(member_ids) == 0:
                sequence_ids = _as_list(clade.get("sequence_ids"))
                member_ids = [str(item) for item in sequence_ids if isinstance(item, str)]
            details.append(f"Clade ID: {clade_id}")
            details.append(f"Size: {size}")
            details.append(f"Members: {', '.join(member_ids) if member_ids else 'n/a'}")

        metrics_list = [
            ReportMetric(
                label="Number of clades",
                value=str(
                    _coerce_int(
                        inferred_payload.get("clade_count"),
                        default=_coerce_int(manifest_payload.get("clade_count"), default=0),
                    )
                ),
            ),
            ReportMetric(
                label="Maximum within-clade distance threshold",
                value=(
                    _format_float(_coerce_float(inferred_payload.get("max_within_clade_distance")))
                    if inferred_payload.get("max_within_clade_distance") is not None
                    else (
                        _format_float(
                            _coerce_float(manifest_payload.get("max_within_clade_distance"))
                        )
                        if manifest_payload.get("max_within_clade_distance") is not None
                        else "n/a"
                    )
                ),
            ),
            ReportMetric(
                label="Assigned sequences",
                value=str(
                    _coerce_int(
                        inferred_payload.get("coverage_leaf_count"),
                        default=_coerce_int(manifest_payload.get("leaf_count"), default=0),
                    )
                ),
            ),
        ]
        if inferred_payload.get("uncovered_leaf_count") is not None:
            metrics_list.append(
                ReportMetric(
                    label="Unassigned sequences",
                    value=str(_coerce_int(inferred_payload.get("uncovered_leaf_count"), default=0)),
                )
            )
        if len(details) == 0:
            details.append(_NO_STAGE_DETAILS_MESSAGE)

        return self._finalize_stage(
            stage_id=stage_info.name,
            display_name=display_name,
            status=stage_info.status,
            metrics=tuple(metrics_list),
            warnings=tuple(),
            errors=tuple(),
            details=tuple(details),
        )

    def _build_result_package_stage(
        self,
        *,
        manifest: JelicaPackageManifest,
        stage_info: ResultPackageStageInfo,
        display_name: str,
    ) -> ReportStage:
        metrics = (
            ReportMetric(label="Content ID", value=manifest.content_id),
            ReportMetric(label="Format version", value=manifest.format_version),
            ReportMetric(label="Producer version", value=manifest.producer.version),
            ReportMetric(label="Protected artifacts count", value=str(len(manifest.artifacts))),
        )
        return self._finalize_stage(
            stage_id=stage_info.name,
            display_name=display_name,
            status=stage_info.status,
            metrics=metrics,
            warnings=tuple(),
            errors=tuple(),
            details=tuple(),
        )

    def _load_comparative_failures(
        self,
        *,
        reader: JelicaPackageReader,
        stage_info: ResultPackageStageInfo,
    ) -> dict[str, ReportMessage]:
        failures_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(COMPARATIVE_ANALYSIS_FAILURES_RELATIVE_PATH,),
        )
        if failures_path is None:
            return {}

        messages: dict[str, ReportMessage] = {}
        for payload in _iter_jsonl_objects(reader=reader, path=failures_path):
            if payload is None:
                continue
            failure_id = _as_non_empty_string(payload.get("failure_id"))
            if failure_id is None:
                continue
            try:
                failure = ComparativeFailureRecord.model_validate(payload)
                messages[failure_id] = ReportMessage(
                    code=failure.error_code,
                    message=failure.detail,
                )
            except ValidationError:
                messages[failure_id] = ReportMessage(
                    code=_as_non_empty_string(payload.get("error_code")),
                    message=_as_non_empty_string(payload.get("detail"))
                    or "Comparative analysis failure.",
                )
        return messages

    def _load_pairwise_items(
        self,
        *,
        reader: JelicaPackageReader,
        stage_info: ResultPackageStageInfo,
        failure_messages: dict[str, ReportMessage],
        distances_by_pair: dict[tuple[str, str], float | None],
    ) -> tuple[PairwiseReportItem, ...]:
        pairwise_path = _find_artifact_path(
            stage_info=stage_info,
            suffixes=(PAIRWISE_COMPARISON_SUMMARY_RELATIVE_PATH,),
        )
        if pairwise_path is None:
            return tuple()

        result: list[PairwiseReportItem] = []
        for payload in _iter_jsonl_objects(reader=reader, path=pairwise_path):
            if payload is None:
                continue
            try:
                record = SequenceComparisonSummaryRecord.model_validate(payload)
                summary = record.summary
                substitutions = (
                    summary.substitutions.event_count
                    if summary is not None and summary.substitutions.requested
                    else None
                )
                insertions = (
                    summary.insertions.event_count
                    if summary is not None and summary.insertions.requested
                    else None
                )
                deletions = (
                    summary.deletions.event_count
                    if summary is not None and summary.deletions.requested
                    else None
                )
                errors: list[ReportMessage] = []
                if record.status.value == "failed":
                    if record.failure_id is not None and record.failure_id in failure_messages:
                        errors.append(failure_messages[record.failure_id])
                    else:
                        errors.append(
                            ReportMessage(
                                code="comparison_failed",
                                message=_NO_FAILURE_DETAILS_MESSAGE,
                            )
                        )
                pair_key = _pair_key(record.left.sequence_id, record.right.sequence_id)
                result.append(
                    PairwiseReportItem(
                        left_sequence_id=record.left.sequence_id,
                        right_sequence_id=record.right.sequence_id,
                        status=record.status.value,
                        compared_length=summary.comparable_base_count
                        if summary is not None
                        else None,
                        matches=summary.matching_base_count if summary is not None else None,
                        identity=summary.identity_on_comparable_bases
                        if summary is not None
                        else None,
                        substitutions=substitutions,
                        insertions=insertions,
                        deletions=deletions,
                        distance=distances_by_pair.get(pair_key),
                        warnings=tuple(),
                        errors=tuple(errors),
                    )
                )
            except ValidationError:
                left_payload = _as_dict(payload.get("left")) or {}
                right_payload = _as_dict(payload.get("right")) or {}
                summary_payload = _as_dict(payload.get("summary")) or {}
                status = _as_non_empty_string(payload.get("status")) or "completed"
                left_id = _as_non_empty_string(left_payload.get("sequence_id")) or "unknown-left"
                right_id = _as_non_empty_string(right_payload.get("sequence_id")) or "unknown-right"
                pair_key = _pair_key(left_id, right_id)
                fallback_errors: list[ReportMessage] = []
                if status == "failed":
                    failure_id = _as_non_empty_string(payload.get("failure_id"))
                    if failure_id is not None and failure_id in failure_messages:
                        fallback_errors.append(failure_messages[failure_id])
                    else:
                        fallback_errors.append(
                            ReportMessage(
                                code="comparison_failed",
                                message=_NO_FAILURE_DETAILS_MESSAGE,
                            )
                        )
                result.append(
                    PairwiseReportItem(
                        left_sequence_id=left_id,
                        right_sequence_id=right_id,
                        status=status,
                        compared_length=_coerce_int(summary_payload.get("comparable_base_count")),
                        matches=_coerce_int(summary_payload.get("matching_base_count")),
                        identity=_coerce_float(summary_payload.get("identity_on_comparable_bases")),
                        substitutions=_coerce_int(
                            _nested_value(summary_payload, "substitutions", "event_count")
                        ),
                        insertions=_coerce_int(
                            _nested_value(summary_payload, "insertions", "event_count")
                        ),
                        deletions=_coerce_int(
                            _nested_value(summary_payload, "deletions", "event_count")
                        ),
                        distance=distances_by_pair.get(pair_key),
                        warnings=tuple(),
                        errors=tuple(fallback_errors),
                    )
                )
        return tuple(result)

    def _load_distance_pair_summary(
        self,
        *,
        reader: JelicaPackageReader,
        manifest: JelicaPackageManifest,
    ) -> _DistancePairSummary:
        stage = next((entry for entry in manifest.stages if entry.name == "distance_matrix"), None)
        if stage is None:
            return _DistancePairSummary(
                distances_by_pair={},
                minimum_distance=None,
                maximum_distance=None,
                undefined_pair_count=0,
            )
        pairs_path = _find_artifact_path(
            stage_info=stage,
            suffixes=(DISTANCE_PAIRS_JSONL_RELATIVE_PATH,),
        )
        if pairs_path is None:
            return _DistancePairSummary(
                distances_by_pair={},
                minimum_distance=None,
                maximum_distance=None,
                undefined_pair_count=0,
            )

        distances_by_pair: dict[tuple[str, str], float | None] = {}
        minimum_distance: float | None = None
        maximum_distance: float | None = None
        undefined_pair_count = 0

        for payload in _iter_jsonl_objects(reader=reader, path=pairs_path):
            if payload is None:
                continue
            left_id: str | None = None
            right_id: str | None = None
            distance: float | None = None
            state: str | None = None
            try:
                record = DistancePairRecord.model_validate(payload)
                left_id = record.left_sequence_id
                right_id = record.right_sequence_id
                distance = record.distance
                state = record.state.value
            except ValidationError:
                left_id = _as_non_empty_string(payload.get("left_sequence_id"))
                right_id = _as_non_empty_string(payload.get("right_sequence_id"))
                distance = _coerce_float(payload.get("distance"))
                state = _as_non_empty_string(payload.get("state"))

            if left_id is None or right_id is None:
                continue
            distances_by_pair[_pair_key(left_id, right_id)] = distance
            if distance is None or (state is not None and state.startswith("undefined")):
                undefined_pair_count += 1
                continue
            minimum_distance = (
                distance if minimum_distance is None else min(minimum_distance, distance)
            )
            maximum_distance = (
                distance if maximum_distance is None else max(maximum_distance, distance)
            )

        return _DistancePairSummary(
            distances_by_pair=distances_by_pair,
            minimum_distance=minimum_distance,
            maximum_distance=maximum_distance,
            undefined_pair_count=undefined_pair_count,
        )

    def _finalize_stage(
        self,
        *,
        stage_id: str,
        display_name: str,
        status: str,
        metrics: tuple[ReportMetric, ...],
        warnings: tuple[ReportMessage, ...],
        errors: tuple[ReportMessage, ...],
        details: tuple[str, ...],
    ) -> ReportStage:
        normalized_details = [item for item in details if item.strip() != ""]
        normalized_warnings = [item for item in warnings if item.message.strip() != ""]
        normalized_errors = [item for item in errors if item.message.strip() != ""]

        if len(normalized_warnings) == 0 and _status_indicates_warning(status):
            normalized_details.append(_NO_WARNING_DETAILS_MESSAGE)
        if len(normalized_errors) == 0 and _status_indicates_failure(status):
            normalized_details.append(_NO_ERROR_DETAILS_MESSAGE)
        if (
            len(metrics) == 0
            and len(normalized_warnings) == 0
            and len(normalized_errors) == 0
            and len(normalized_details) == 0
        ):
            normalized_details.append(_NO_STAGE_DETAILS_MESSAGE)

        return ReportStage(
            stage_id=stage_id,
            display_name=display_name,
            status=status,
            metrics=metrics,
            warnings=tuple(normalized_warnings),
            errors=tuple(normalized_errors),
            details=tuple(normalized_details),
        )


def build_analysis_report_model(*, package_path: Path | str) -> AnalysisReportModel:
    return AnalysisReportBuilder().build(package_path=package_path)


def _find_artifact_path(
    *,
    stage_info: ResultPackageStageInfo,
    suffixes: tuple[str, ...],
) -> str | None:
    normalized_suffixes = tuple(item.replace("\\", "/") for item in suffixes)
    for artifact_path in stage_info.artifacts:
        normalized = artifact_path.replace("\\", "/")
        for suffix in normalized_suffixes:
            if normalized == suffix or normalized.endswith(f"/{suffix}"):
                return normalized
    return None


def _read_json_object(*, reader: JelicaPackageReader, path: str) -> dict[str, object] | None:
    try:
        return reader.read_json_file(path=path)
    except JelicaPackageReaderError:
        return None


def _read_text(*, reader: JelicaPackageReader, path: str) -> str | None:
    try:
        payload = reader.read_bytes(path=path)
    except JelicaPackageReaderError:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        return None


def _iter_jsonl_objects(
    *, reader: JelicaPackageReader, path: str
) -> tuple[dict[str, object] | None, ...]:
    try:
        with reader.open_entry(path=path) as handle:
            parsed_rows: list[dict[str, object] | None] = []
            for raw_line in handle:
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeError:
                    continue
                if line == "":
                    continue
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    continue
                parsed_rows.append(_as_dict(loaded))
            return tuple(parsed_rows)
    except JelicaPackageReaderError:
        return tuple()


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): casted for key, casted in value.items()}


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return []


def _as_non_empty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    return normalized


def _coerce_int(value: object, *, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return default
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return default
        try:
            return int(stripped)
        except ValueError:
            return default
    return default


def _coerce_float(value: object, *, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            return default
        return numeric
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return default
        try:
            numeric = float(stripped)
        except ValueError:
            return default
        if not math.isfinite(numeric):
            return default
        return numeric
    return default


def _nested_value(root: dict[str, object], *keys: str) -> object:
    current: object = root
    for key in keys:
        nested = _as_dict(current)
        if nested is None or key not in nested:
            return None
        current = nested[key]
    return current


def _pair_key(left_sequence_id: str, right_sequence_id: str) -> tuple[str, str]:
    if left_sequence_id <= right_sequence_id:
        return (left_sequence_id, right_sequence_id)
    return (right_sequence_id, left_sequence_id)


def _status_indicates_warning(status: str) -> bool:
    lowered = status.lower()
    return "warning" in lowered or "partial" in lowered


def _status_indicates_failure(status: str) -> bool:
    lowered = status.lower()
    return any(token in lowered for token in ("failed", "error", "unavailable"))


def _format_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.6f}".rstrip("0").rstrip(".")


__all__ = [
    "AnalysisReportBuildError",
    "AnalysisReportBuildErrorCode",
    "AnalysisReportBuilder",
    "build_analysis_report_model",
]
