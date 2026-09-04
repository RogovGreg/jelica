from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from jelica_core.reporting import (
    PdfReportRenderer,
    ReportStage,
    build_analysis_report_model,
    export_analysis_report_pdf,
)
from jelica_core.result_package import (
    JELICA_PACKAGE_CONFIGURATION_PATH,
    JELICA_PACKAGE_FORMAT,
    JELICA_PACKAGE_FORMAT_VERSION,
    JELICA_PACKAGE_INPUT_MANIFEST_PATH,
    JELICA_PACKAGE_MANIFEST_PATH,
    JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
    JELICA_PACKAGE_TASK_PATH,
    JelicaPackageManifest,
    ResultPackageArtifactInfo,
    ResultPackageProducerInfo,
    ResultPackageStageInfo,
    ResultPackageTaskInfo,
    ResultPackageTaskStatus,
    compute_content_id,
    infer_media_type,
    serialize_stable_json,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _build_full_analysis_package(
    package_path: Path,
    *,
    disable_clade_details: bool = False,
) -> str:
    long_sequence_id = "sequence-" + ("x" * 180)
    sequence_a = "seq-a"
    sequence_b = "seq-b"
    sequence_c = long_sequence_id

    input_manifest_payload = {
        "schema_version": 1,
        "sources": ["sample-a.fasta", "sample-b.fasta", "sample-c.fasta"],
        "materialized_files": [
            {"relative_path": "inputs/sample-a.fasta"},
            {"relative_path": "inputs/sample-b.fasta"},
            {"relative_path": "inputs/sample-c.fasta"},
        ],
        "source_errors": [
            {
                "event_name": "INPUT_DUPLICATES_SKIPPED_EVENT",
                "detail": "One duplicate source was skipped.",
            }
        ],
    }
    input_processing_manifest_payload = {
        "dataset_summary": {
            "discovered_record_count": 4,
            "valid_sample_count": 3,
            "invalid_sample_count": 1,
            "unique_sequence_count": 3,
            "comparative_analysis_available": True,
        },
        "dataset_issues": [
            {
                "code": "input_record_rejected",
                "message": "One input record was rejected.",
                "severity": "warning",
            }
        ],
    }
    alignment_manifest_payload = {
        "logical_sample_count": 3,
        "alignment_length": 1200,
        "resolved_engine": "mafft",
        "mode": "compute",
        "outcome": "completed",
    }
    comparative_manifest_payload = {
        "status": "partial_success",
        "plan_execution_counts": {"unique_sequence_count": 3},
    }
    comparative_dataset_summary_payload = {
        "sample_count": 3,
        "metric_total": 4,
        "metric_successful": 4,
        "metric_failed": 0,
        "metrics": {},
    }
    pairwise_records = [
        {
            "left": {"sample_id": "sample-a", "sequence_id": sequence_a},
            "right": {"sample_id": "sample-b", "sequence_id": sequence_b},
            "status": "completed",
            "summary": {
                "comparable_base_count": 1200,
                "matching_base_count": 1188,
                "identity_on_comparable_bases": 0.99,
                "substitutions": {"event_count": 8},
                "insertions": {"event_count": 2},
                "deletions": {"event_count": 2},
            },
        },
        {
            "left": {"sample_id": "sample-a", "sequence_id": sequence_a},
            "right": {"sample_id": "sample-c", "sequence_id": sequence_c},
            "status": "completed",
            "summary": {
                "comparable_base_count": 1200,
                "matching_base_count": 1176,
                "identity_on_comparable_bases": 0.98,
                "substitutions": {"event_count": 12},
                "insertions": {"event_count": 6},
                "deletions": {"event_count": 6},
            },
        },
        {
            "left": {"sample_id": "sample-b", "sequence_id": sequence_b},
            "right": {"sample_id": "sample-c", "sequence_id": sequence_c},
            "status": "failed",
            "failure_id": "failure-0001",
        },
    ]
    comparative_failures = [
        {
            "failure_id": "failure-0001",
            "error_code": "PAIRWISE_COMPARISON_FAILED",
            "detail": "A pairwise comparison failed.",
            "category": "pairwise_sequence",
            "phase": "pairwise",
        }
    ]
    distance_manifest_payload = {
        "model": "p_distance",
        "matrix_dimensions": [3, 3],
        "status": "partial_success",
    }
    distance_pairs = [
        {
            "left_sequence_id": sequence_a,
            "right_sequence_id": sequence_b,
            "distance": 0.01,
            "state": "defined",
        },
        {
            "left_sequence_id": sequence_a,
            "right_sequence_id": sequence_c,
            "distance": 0.02,
            "state": "defined",
        },
        {
            "left_sequence_id": sequence_b,
            "right_sequence_id": sequence_c,
            "distance": None,
            "state": "undefined_no_comparable_sites",
        },
    ]
    phylogenetic_manifest_payload = {
        "method": "neighbor_joining",
        "applied_rooting": "midpoint",
        "leaf_count": 3,
    }
    phylogenetic_tree_payload = {
        "method": "neighbor_joining",
        "applied_rooting": "midpoint",
    }
    phylogenetic_diagnostics_payload = {
        "leaf_count": 3,
        "tree_diameter": 0.02,
        "zero_diameter": False,
        "warnings": [{"code": "NEGATIVE_BRANCH_CLAMPED", "detail": "Negative branch clamped."}],
    }
    phylogenetic_newick = f"(({sequence_a}:0.01,{sequence_b}:0.01):0.01,{sequence_c}:0.02);"
    clade_manifest_payload = {
        "clade_count": 2,
        "max_within_clade_distance": 0.02,
    }
    inferred_clades_payload = {
        "clade_count": 2,
        "max_within_clade_distance": 0.02,
        "coverage_leaf_count": 3,
        "uncovered_leaf_count": 0,
        "clades": [
            {
                "clade_id": "clade-a",
                "leaf_count": 2,
                "members": [
                    {"sequence_id": sequence_a, "leaf_label": "leaf-a"},
                    {"sequence_id": sequence_b, "leaf_label": "leaf-b"},
                ],
                "sequence_ids": [sequence_a, sequence_b],
            },
            {
                "clade_id": "clade-b",
                "leaf_count": 1,
                "members": [{"sequence_id": sequence_c, "leaf_label": "leaf-c"}],
                "sequence_ids": [sequence_c],
            },
        ],
    }
    result_package_stage_payload = {"stage_id": "result_package", "published": True}

    payloads: dict[str, bytes] = {
        JELICA_PACKAGE_TASK_PATH: (
            b'{"task_id":"task-report","status":"completed","created_at":"2026-08-06T23:48:05Z",'
            b'"completed_at":"2026-08-06T23:58:05Z"}\n'
        ),
        JELICA_PACKAGE_CONFIGURATION_PATH: b'{"analysis":{"alignment":{"mode":"compute"}}}\n',
        JELICA_PACKAGE_INPUT_MANIFEST_PATH: (
            json.dumps(input_manifest_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n"
        ),
        JELICA_PACKAGE_NORMALIZED_FASTA_PATH: (
            f">{sequence_a}\nACGT\n>{sequence_b}\nACGA\n>{sequence_c}\nACGG\n".encode("utf-8")
        ),
        "stages/input_processing/input_processing/input_processing_manifest.json": (
            json.dumps(
                input_processing_manifest_payload, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            + b"\n"
        ),
        "stages/alignment/alignment/alignment_manifest.json": (
            json.dumps(alignment_manifest_payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        ),
        "stages/comparative_analysis/comparative_analysis/comparative_analysis_manifest.json": (
            json.dumps(comparative_manifest_payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        ),
        "stages/comparative_analysis/comparative_analysis/dataset_statistical_summary.json": (
            json.dumps(
                comparative_dataset_summary_payload, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            + b"\n"
        ),
        "stages/comparative_analysis/comparative_analysis/pairwise_comparison_summary.jsonl": (
            "\n".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) for item in pairwise_records
            ).encode("utf-8")
            + b"\n"
        ),
        "stages/comparative_analysis/comparative_analysis/failures.jsonl": (
            "\n".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                for item in comparative_failures
            ).encode("utf-8")
            + b"\n"
        ),
        "stages/distance_matrix/distance_matrix/distance_matrix_manifest.json": (
            json.dumps(distance_manifest_payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        ),
        "stages/distance_matrix/distance_matrix/distance_pairs.jsonl": (
            "\n".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) for item in distance_pairs
            ).encode("utf-8")
            + b"\n"
        ),
        "stages/phylogenetic_tree/phylogenetic_tree/phylogenetic_tree_manifest.json": (
            json.dumps(phylogenetic_manifest_payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        ),
        "stages/phylogenetic_tree/phylogenetic_tree/tree.json": (
            json.dumps(phylogenetic_tree_payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        ),
        "stages/phylogenetic_tree/phylogenetic_tree/tree_diagnostics.json": (
            json.dumps(phylogenetic_diagnostics_payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        ),
        "stages/phylogenetic_tree/phylogenetic_tree/tree_rooted.nwk": phylogenetic_newick.encode(
            "utf-8"
        ),
        "stages/result_package/result_package/result_package_manifest.json": (
            json.dumps(result_package_stage_payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        ),
    }
    if not disable_clade_details:
        payloads["stages/clade_detection/clade_detection/clade_detection_manifest.json"] = (
            json.dumps(clade_manifest_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        payloads["stages/clade_detection/clade_detection/inferred_clades.json"] = (
            json.dumps(inferred_clades_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n"
        )

    stage_artifacts: dict[str, tuple[str, ...]] = {
        "input_acquisition": (JELICA_PACKAGE_INPUT_MANIFEST_PATH,),
        "input_processing": (
            JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
            "stages/input_processing/input_processing/input_processing_manifest.json",
        ),
        "alignment": ("stages/alignment/alignment/alignment_manifest.json",),
        "comparative_analysis": (
            "stages/comparative_analysis/comparative_analysis/comparative_analysis_manifest.json",
            "stages/comparative_analysis/comparative_analysis/dataset_statistical_summary.json",
            "stages/comparative_analysis/comparative_analysis/failures.jsonl",
            "stages/comparative_analysis/comparative_analysis/pairwise_comparison_summary.jsonl",
        ),
        "distance_matrix": (
            "stages/distance_matrix/distance_matrix/distance_matrix_manifest.json",
            "stages/distance_matrix/distance_matrix/distance_pairs.jsonl",
        ),
        "phylogenetic_tree": (
            "stages/phylogenetic_tree/phylogenetic_tree/phylogenetic_tree_manifest.json",
            "stages/phylogenetic_tree/phylogenetic_tree/tree.json",
            "stages/phylogenetic_tree/phylogenetic_tree/tree_diagnostics.json",
            "stages/phylogenetic_tree/phylogenetic_tree/tree_rooted.nwk",
        ),
        "clade_detection": (
            tuple()
            if disable_clade_details
            else (
                "stages/clade_detection/clade_detection/clade_detection_manifest.json",
                "stages/clade_detection/clade_detection/inferred_clades.json",
            )
        ),
        "result_package": ("stages/result_package/result_package/result_package_manifest.json",),
    }

    artifact_stage_map: dict[str, str | None] = {
        JELICA_PACKAGE_TASK_PATH: None,
        JELICA_PACKAGE_CONFIGURATION_PATH: None,
        JELICA_PACKAGE_INPUT_MANIFEST_PATH: "input_acquisition",
        JELICA_PACKAGE_NORMALIZED_FASTA_PATH: "input_processing",
    }
    for stage_id, paths in stage_artifacts.items():
        for artifact_path in paths:
            artifact_stage_map[artifact_path] = stage_id

    protected_artifacts = tuple(
        ResultPackageArtifactInfo(
            path=path,
            stage=artifact_stage_map.get(path),
            media_type=infer_media_type(path),
            size=len(payload),
            sha256=_sha256(payload),
        )
        for path, payload in sorted(payloads.items())
    )
    content_id = compute_content_id(artifacts=protected_artifacts)
    manifest = JelicaPackageManifest(
        format=JELICA_PACKAGE_FORMAT,
        format_version=JELICA_PACKAGE_FORMAT_VERSION,
        content_id=content_id,
        producer=ResultPackageProducerInfo(version="1.0.0-test"),
        package_created_at="2026-08-06T23:59:05Z",
        task=ResultPackageTaskInfo(
            task_id="task-report",
            status=ResultPackageTaskStatus.COMPLETED,
            created_at="2026-08-06T23:48:05Z",
            completed_at="2026-08-06T23:58:05Z",
        ),
        stages=(
            ResultPackageStageInfo(
                name="input_acquisition",
                status="completed_with_warnings",
                artifacts=tuple(sorted(stage_artifacts["input_acquisition"])),
            ),
            ResultPackageStageInfo(
                name="input_processing",
                status="completed_with_warnings",
                artifacts=tuple(sorted(stage_artifacts["input_processing"])),
            ),
            ResultPackageStageInfo(
                name="alignment",
                status="completed",
                artifacts=tuple(sorted(stage_artifacts["alignment"])),
            ),
            ResultPackageStageInfo(
                name="comparative_analysis",
                status="completed_with_warnings",
                artifacts=tuple(sorted(stage_artifacts["comparative_analysis"])),
            ),
            ResultPackageStageInfo(
                name="distance_matrix",
                status="completed_with_warnings",
                artifacts=tuple(sorted(stage_artifacts["distance_matrix"])),
            ),
            ResultPackageStageInfo(
                name="phylogenetic_tree",
                status="completed",
                artifacts=tuple(sorted(stage_artifacts["phylogenetic_tree"])),
            ),
            ResultPackageStageInfo(
                name="clade_detection",
                status="completed",
                artifacts=tuple(sorted(stage_artifacts["clade_detection"])),
            ),
            ResultPackageStageInfo(
                name="result_package",
                status="completed",
                artifacts=tuple(sorted(stage_artifacts["result_package"])),
            ),
        ),
        artifacts=protected_artifacts,
    )

    with zipfile.ZipFile(package_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, payload in sorted(payloads.items()):
            archive.writestr(path, payload)
        archive.writestr(
            JELICA_PACKAGE_MANIFEST_PATH,
            serialize_stable_json(manifest.model_dump(mode="json")).encode("utf-8"),
        )

    return content_id


def _metric_value(stage: ReportStage, label: str) -> str:
    for metric in stage.metrics:
        if metric.label == label:
            return metric.value
    raise AssertionError(f"Metric '{label}' not found")


def test_analysis_report_builder_extracts_stage_data_and_pairwise_comparisons(
    tmp_path: Path,
) -> None:
    package_path = tmp_path / "full-package.jelica"
    _build_full_analysis_package(package_path)

    report_model = build_analysis_report_model(package_path=package_path)

    assert tuple(stage.stage_id for stage in report_model.stages) == (
        "input_acquisition",
        "input_processing",
        "alignment",
        "comparative_analysis",
        "distance_matrix",
        "phylogenetic_tree",
        "clade_detection",
        "result_package",
    )
    assert all(stage.status.strip() != "" for stage in report_model.stages)

    input_stage = next(
        stage for stage in report_model.stages if stage.stage_id == "input_processing"
    )
    assert _metric_value(input_stage, "Input records") == "4"
    assert _metric_value(input_stage, "Normalized sequences") == "3"
    assert _metric_value(input_stage, "Unique sequences") == "3"

    alignment_stage = next(stage for stage in report_model.stages if stage.stage_id == "alignment")
    assert _metric_value(alignment_stage, "Number of aligned sequences") == "3"
    assert _metric_value(alignment_stage, "Alignment length") == "1200"
    assert _metric_value(alignment_stage, "Method/tool") == "mafft"

    comparative_stage = next(
        stage for stage in report_model.stages if stage.stage_id == "comparative_analysis"
    )
    assert _metric_value(comparative_stage, "Number of sequences") == "3"
    assert _metric_value(comparative_stage, "Number of pairwise comparisons") == "3"
    assert _metric_value(comparative_stage, "Total matches") == "2364"

    distance_stage = next(
        stage for stage in report_model.stages if stage.stage_id == "distance_matrix"
    )
    assert _metric_value(distance_stage, "Matrix size") == "3x3"
    assert _metric_value(distance_stage, "Minimum distance") == "0.01"
    assert _metric_value(distance_stage, "Maximum distance") == "0.02"

    tree_stage = next(
        stage for stage in report_model.stages if stage.stage_id == "phylogenetic_tree"
    )
    assert _metric_value(tree_stage, "Construction method") == "neighbor_joining"
    assert _metric_value(tree_stage, "Number of leaves") == "3"

    clade_stage = next(
        stage for stage in report_model.stages if stage.stage_id == "clade_detection"
    )
    assert _metric_value(clade_stage, "Number of clades") == "2"
    assert any(detail.startswith("Clade ID:") for detail in clade_stage.details)
    assert any(detail.startswith("Members:") for detail in clade_stage.details)

    assert len(report_model.pairwise_comparisons) == 3
    assert all(item.left_sequence_id.strip() != "" for item in report_model.pairwise_comparisons)


def test_analysis_report_pdf_renderer_emits_multipage_pdf(tmp_path: Path) -> None:
    package_path = tmp_path / "full-package.jelica"
    _build_full_analysis_package(package_path)
    report_model = build_analysis_report_model(package_path=package_path)

    payload = PdfReportRenderer().render(model=report_model)

    assert payload.startswith(b"%PDF-")
    assert payload.count(b"/Type /Page ") > 1
    assert len(payload) > 8000


def test_analysis_report_export_handles_stage_without_published_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_path = tmp_path / "partial-package.jelica"
    content_id = _build_full_analysis_package(package_path, disable_clade_details=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)

    report_model = build_analysis_report_model(package_path=package_path)
    clade_stage = next(
        stage for stage in report_model.stages if stage.stage_id == "clade_detection"
    )
    assert any(
        detail == "No published details are available for this stage."
        for detail in clade_stage.details
    )

    outcome = export_analysis_report_pdf(source_package_path=package_path, output=None)
    expected_path = cwd / f"jelica-report-{content_id.removeprefix('sha256:')}.pdf"
    assert outcome.output_path == expected_path.resolve(strict=False)
    assert outcome.output_path.is_file()
    assert outcome.output_path.read_bytes().startswith(b"%PDF-")
