from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jelica_core.comparative_analysis import (
    ComparativeAnalysisManifest,
    ComparativeAnalysisStatus,
    ComparativeFailureRecord,
    ComparisonIdentity,
    ComparisonPlanCounts,
    ComparisonSourceKind,
    DifferenceEventType,
)
from jelica_core.comparative_analysis.aligned_comparator import (
    AlignedDifferenceEvent,
    AlignedSequenceComparator,
)
from jelica_core.comparative_analysis.artifacts import (
    JsonlArtifactWriter,
    SequenceComparisonSummaryRecord,
    materialize_difference_record,
)
from jelica_core.runtime.comparative_analysis_stage import _filtered_summary


def _identity(sample_id: str, value: str) -> ComparisonIdentity:
    return ComparisonIdentity(
        sample_id=sample_id,
        sequence_id="sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest(),
    )


def test_sequence_values_exist_only_in_dedicated_difference_contract() -> None:
    left = _identity("sample-a", "left")
    right = _identity("sample-b", "right")
    event = AlignedDifferenceEvent(
        type=DifferenceEventType.INSERTION,
        msa_column_start=2,
        msa_column_end=3,
        length=2,
        right_start=2,
        right_end=3,
        after_left_position=1,
        before_left_position=2,
    )

    difference = materialize_difference_record(
        left=left,
        right=right,
        source_kinds=(ComparisonSourceKind.EXPLICIT_PAIR,),
        event=event,
        left_aligned_sequence="A--C",
        right_aligned_sequence="ATGC",
    )
    summary = SequenceComparisonSummaryRecord(
        left=left,
        right=right,
        source_kinds=(ComparisonSourceKind.EXPLICIT_PAIR,),
        source_occurrence_count=1,
        status="completed",
        computation_index=0,
        requested_categories=(DifferenceEventType.INSERTION,),
        summary={
            "msa_column_count": 2,
            "both_gap_column_count": 0,
            "comparable_base_count": 1,
            "matching_base_count": 1,
            "substitutions": {"requested": False},
            "insertions": {"requested": True, "event_count": 1, "base_count": 1},
            "deletions": {"requested": False},
            "uncertain_event_count": 0,
            "uncertain_column_count": 0,
            "identity_on_comparable_bases": 1.0,
        },
    )
    failure = ComparativeFailureRecord(
        failure_id="failure-000001",
        category="pairwise_sequence",
        error_code="SEQUENCE_COMPUTATION_FAILED",
        detail="A sequence comparison computation failed.",
        phase="physical_sequence_computation",
        computation_index=0,
        affected_logical_result_count=1,
    )
    manifest = ComparativeAnalysisManifest(
        task_id="task-1",
        job_id="job-1",
        config_hash="0" * 64,
        enabled=True,
        normalized_settings={
            "enabled": True,
            "statistics": {"enabled": True},
            "sequence_differences": {
                "enabled": True,
                "substitutions": False,
                "insertions": True,
                "deletions": False,
            },
            "reference": {"mode": "disabled"},
        },
        status=ComparativeAnalysisStatus.PARTIAL_SUCCESS,
        alignment_mode="prealigned",
        reference_mode="disabled",
        uracil_thymine_equivalent=False,
        requested_difference_categories=(DifferenceEventType.INSERTION,),
        started_at="2026-08-05T00:00:00Z",
        completed_at="2026-08-05T00:00:01Z",
        duration_seconds=1.0,
        plan_counts=ComparisonPlanCounts(
            occurrence_count=1,
            unique_logical_operation_count=1,
            duplicate_occurrence_count=0,
            scan_computation_count=1,
            identical_sequence_projection_count=0,
        ),
        category_execution={
            "statistics": {
                "status": "completed",
                "requested": True,
                "total": 1,
                "completed": 1,
                "successful": 1,
                "failed": 0,
                "available": True,
            },
            "reference_sequence_differences": {
                "status": "not_requested",
                "requested": False,
                "total": 0,
                "completed": 0,
                "successful": 0,
                "failed": 0,
            },
            "pairwise_sequence_differences": {
                "status": "failed",
                "requested": True,
                "total": 1,
                "completed": 1,
                "successful": 0,
                "failed": 1,
            },
        },
        successful_result_count=1,
        failed_result_count=1,
        failure_count=1,
    )

    difference_payload = json.dumps(difference.model_dump(mode="json"), sort_keys=True)
    protected_payload = json.dumps(
        {
            "summary": summary.model_dump(mode="json"),
            "failure": failure.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    assert difference.left_value is None
    assert difference.right_value is not None
    assert difference.right_value in difference_payload
    assert difference.right_value not in protected_payload


@pytest.mark.parametrize(
    ("event", "expected_left", "expected_right"),
    (
        (
            AlignedDifferenceEvent(
                type=DifferenceEventType.SUBSTITUTION,
                msa_column_start=1,
                msa_column_end=1,
                length=1,
                left_start=1,
                left_end=1,
                right_start=1,
                right_end=1,
            ),
            "A",
            "G",
        ),
        (
            AlignedDifferenceEvent(
                type=DifferenceEventType.DELETION,
                msa_column_start=2,
                msa_column_end=2,
                length=1,
                left_start=2,
                left_end=2,
                after_right_position=1,
                before_right_position=2,
            ),
            "C",
            None,
        ),
        (
            AlignedDifferenceEvent(
                type=DifferenceEventType.UNCERTAIN,
                msa_column_start=3,
                msa_column_end=3,
                length=1,
                left_start=3,
                left_end=3,
                right_start=2,
                right_end=2,
            ),
            "T",
            "N",
        ),
    ),
)
def test_specialized_values_are_materialized_for_each_non_insertion_event(
    event: AlignedDifferenceEvent,
    expected_left: str | None,
    expected_right: str | None,
) -> None:
    record = materialize_difference_record(
        left=_identity("sample-a", "left"),
        right=_identity("sample-b", "right"),
        source_kinds=(ComparisonSourceKind.EXPLICIT_PAIR,),
        event=event,
        left_aligned_sequence="ACT",
        right_aligned_sequence="G-N",
    )

    assert record.left_value == expected_left
    assert record.right_value == expected_right


def test_category_filtering_keeps_identity_and_uncertain_counts() -> None:
    left = _identity("sample-a", "first")
    right = _identity("sample-b", "second")
    comparison = AlignedSequenceComparator().compare(
        left_aligned_sequence="AN-C",
        right_aligned_sequence="GTTC",
        left_identity=left,
        right_identity=right,
    )

    filtered = _filtered_summary(
        comparison,
        requested=(DifferenceEventType.INSERTION,),
    )

    assert filtered.substitutions.requested is False
    assert filtered.substitutions.event_count is None
    assert filtered.deletions.requested is False
    assert filtered.insertions.requested is True
    assert filtered.insertions.event_count == comparison.summary.insertion_event_count
    assert filtered.uncertain_event_count == comparison.summary.uncertain_event_count
    assert (
        filtered.identity_on_comparable_bases
        == comparison.summary.identity_on_comparable_bases
    )


def test_jsonl_writer_streams_and_reports_stable_integrity_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "comparative_analysis" / "failures.jsonl"
    writer = JsonlArtifactWriter(
        path,
        relative_path="comparative_analysis/failures.jsonl",
    )
    writer.write({"failure_id": "failure-000001", "count": 1})
    writer.write({"failure_id": "failure-000002", "count": 2})

    metadata = writer.close()

    payload = path.read_bytes()
    assert metadata.record_count == 2
    assert metadata.size_bytes == len(payload)
    assert metadata.sha256 == hashlib.sha256(payload).hexdigest()
    assert [json.loads(line)["count"] for line in payload.splitlines()] == [1, 2]


def test_comparative_manifest_json_round_trip_is_stable() -> None:
    manifest = ComparativeAnalysisManifest(
        task_id="task-1",
        job_id="job-1",
        config_hash="f" * 64,
        enabled=False,
        skipped_reason="comparative_analysis_disabled",
        status=ComparativeAnalysisStatus.COMPLETED,
        alignment_mode="none",
        reference_mode="auto",
        uracil_thymine_equivalent=False,
        started_at="2026-08-05T00:00:00Z",
        completed_at="2026-08-05T00:00:00Z",
        duration_seconds=0.0,
        plan_counts=ComparisonPlanCounts(
            occurrence_count=0,
            unique_logical_operation_count=0,
            duplicate_occurrence_count=0,
            scan_computation_count=0,
            identical_sequence_projection_count=0,
        ),
        category_execution={
            category: {
                "status": "not_requested",
                "requested": False,
                "total": 0,
                "completed": 0,
                "successful": 0,
                "failed": 0,
            }
            for category in (
                "statistics",
                "reference_sequence_differences",
                "pairwise_sequence_differences",
            )
        },
        successful_result_count=0,
        failed_result_count=0,
        failure_count=0,
    )
    payload = manifest.model_dump_json()

    assert ComparativeAnalysisManifest.model_validate_json(payload) == manifest
