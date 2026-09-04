from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jelica_core.config import AnalysisKmerStrand
from jelica_core.runtime.input_processing_models import (
    INPUT_PROCESSING_MANIFEST_SCHEMA_VERSION,
    CanonicalBaseCounts,
    InputProcessingCoordinateSystem,
    InputProcessingDatasetSummary,
    InputProcessingFileStatus,
    InputProcessingLogicalSample,
    InputProcessingManifest,
    InputProcessingProcessedFile,
    InputProcessingResolvedReference,
    InputProcessingState,
    InputProcessingUniqueSequence,
    InputProcessingValidationIssue,
    KmerCoordinateRange,
    KmerHit,
    KmerHitsSidecar,
    KmerMatchKind,
    KmerQueryHits,
    KmerQuerySummary,
    LogicalSampleProvenance,
    ReferenceResolutionMethod,
    SampleValidationStatus,
    SequenceFacts,
    SequenceStrand,
    ValidationIssueScope,
    ValidationIssueSeverity,
    sequence_id_digest,
)

_SEQUENCE_ID = "sha256:" + ("a" * 64)
_SECOND_SEQUENCE_ID = "sha256:" + ("b" * 64)


def _sequence_facts(*, sequence_id: str) -> SequenceFacts:
    return SequenceFacts(
        source_length=4,
        ungapped_length=4,
        recognized_nucleotide_count=4,
        symbol_counts={"A": 1, "T": 1, "G": 1, "C": 1},
        canonical_count=4,
        ambiguous_count=0,
        gap_count=0,
        invalid_symbol_count=0,
        invalid_symbol_counts={},
        invalid_positions_truncated=False,
        gc_count=2,
        gc_content_total=0.5,
        resolved_gc_content=0.5,
        expected_gc_count=2.0,
        expected_gc_content=0.5,
        u_count=0,
        sequence_id=sequence_id,
        kmer_summaries=(
            KmerQuerySummary(
                query="ATG",
                definite_match_count=1,
                possible_match_count=0,
                strand=AnalysisKmerStrand.FORWARD,
                hits_path=f"input_processing/kmer_hits/{sequence_id_digest(sequence_id)}.json",
            ),
        ),
    )


def _logical_sample(*, sample_id: str, sequence_id: str) -> InputProcessingLogicalSample:
    return InputProcessingLogicalSample(
        sample_id=sample_id,
        provenance=LogicalSampleProvenance(
            input_manifest_source_reference="sample.fasta",
            materialized_relative_path="inputs/files/0001_sample.fasta",
            record_index=0,
            format_hint=".fasta",
        ),
        original_record_id=f"record-{sample_id}",
        original_description=None,
        validation_status=SampleValidationStatus.VALID,
        validation_issues=(),
        sequence_id=sequence_id,
        eligible_for_analysis=True,
    )


def _processed_file() -> InputProcessingProcessedFile:
    return InputProcessingProcessedFile(
        input_manifest_source_reference="sample.fasta",
        relative_path="inputs/files/0001_sample.fasta",
        format_hint=".fasta",
        status=InputProcessingFileStatus.PROCESSED,
        record_count=2,
        valid_sample_count=2,
        invalid_sample_count=0,
        validation_issues=(
            InputProcessingValidationIssue(
                code="W_DEMO",
                message="demo warning",
                severity=ValidationIssueSeverity.WARNING,
                scope=ValidationIssueScope.FILE,
                path="inputs/files/0001_sample.fasta",
            ),
        ),
    )


def test_manifest_round_trip_and_schema_version() -> None:
    sample_a = _logical_sample(sample_id="sample-a", sequence_id=_SEQUENCE_ID)
    sample_b = _logical_sample(sample_id="sample-b", sequence_id=_SEQUENCE_ID)
    unique_sequence = InputProcessingUniqueSequence(
        sequence_id=_SEQUENCE_ID,
        sequence_artifact_path=f"input_processing/sequences/{sequence_id_digest(_SEQUENCE_ID)}.fasta",
        facts=_sequence_facts(sequence_id=_SEQUENCE_ID),
        logical_sample_ids=("sample-a", "sample-b"),
        kmer_hits_path=f"input_processing/kmer_hits/{sequence_id_digest(_SEQUENCE_ID)}.json",
    )
    manifest = InputProcessingManifest(
        schema_version=1,
        task_id="task-1",
        job_id="job-1",
        config_revision_path="configs/000001.json",
        config_hash="c" * 64,
        generated_at="2026-08-02T12:30:17Z",
        processing_state=InputProcessingState.COMPLETED,
        processed_files=(_processed_file(),),
        logical_samples=(sample_a, sample_b),
        unique_sequences=(unique_sequence,),
        dataset_summary=InputProcessingDatasetSummary(
            discovered_record_count=2,
            valid_sample_count=2,
            invalid_sample_count=0,
            unique_sequence_count=1,
            duplicate_logical_sample_count=1,
            comparative_analysis_available=True,
        ),
    )

    payload = manifest.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False)
    restored = InputProcessingManifest.model_validate(json.loads(encoded))

    assert manifest.schema_version == INPUT_PROCESSING_MANIFEST_SCHEMA_VERSION
    assert restored.model_dump(mode="json") == payload
    assert restored.unique_sequences[0].logical_sample_ids == ("sample-a", "sample-b")
    expected_counts = CanonicalBaseCounts(A=1, C=1, G=1, T=1, U=0)
    assert restored.unique_sequences[0].facts.base_counts.definite == expected_counts
    assert restored.unique_sequences[0].facts.base_counts.potential == expected_counts


def test_manifest_supports_optional_resolved_reference() -> None:
    manifest = InputProcessingManifest(
        schema_version=1,
        task_id="task-1",
        job_id="job-1",
        config_revision_path="configs/000001.json",
        config_hash="d" * 64,
        generated_at="2026-08-02T12:30:17Z",
        processing_state=InputProcessingState.COMPLETED,
        processed_files=(),
        logical_samples=(),
        unique_sequences=(),
        dataset_summary=InputProcessingDatasetSummary(
            discovered_record_count=0,
            valid_sample_count=0,
            invalid_sample_count=0,
            unique_sequence_count=0,
            duplicate_logical_sample_count=0,
            comparative_analysis_available=False,
        ),
        resolved_reference=InputProcessingResolvedReference(
            selector="data/alignment.afa::NC_045512.2",
            sample_id="sample-ref",
            sequence_id=_SEQUENCE_ID,
            source_relative_path="inputs/files/0001_alignment.afa",
            record_id="NC_045512.2",
            resolution_method=ReferenceResolutionMethod.FILE_PATH_AND_RECORD_ID,
        ),
    )

    assert manifest.resolved_reference is not None
    assert manifest.resolved_reference.selector == "data/alignment.afa::NC_045512.2"


def test_logical_sample_rejects_same_sample_and_sequence_ids() -> None:
    with pytest.raises(ValidationError):
        _logical_sample(sample_id=_SEQUENCE_ID, sequence_id=_SEQUENCE_ID)


def test_unique_sequence_can_be_shared_by_multiple_logical_samples() -> None:
    sample_a = _logical_sample(sample_id="sample-a", sequence_id=_SECOND_SEQUENCE_ID)
    sample_b = _logical_sample(sample_id="sample-b", sequence_id=_SECOND_SEQUENCE_ID)
    unique_sequence = InputProcessingUniqueSequence(
        sequence_id=_SECOND_SEQUENCE_ID,
        sequence_artifact_path=(
            f"input_processing/sequences/{sequence_id_digest(_SECOND_SEQUENCE_ID)}.fasta"
        ),
        facts=_sequence_facts(sequence_id=_SECOND_SEQUENCE_ID),
        logical_sample_ids=(sample_a.sample_id, sample_b.sample_id),
    )

    assert unique_sequence.logical_sample_ids == ("sample-a", "sample-b")
    assert sample_a.sample_id != unique_sequence.sequence_id
    assert sample_b.sample_id != unique_sequence.sequence_id


def test_kmer_hits_sidecar_preserves_query_identity_order_and_coordinates() -> None:
    sidecar = KmerHitsSidecar(
        sequence_id=_SEQUENCE_ID,
        query_summaries=(
            KmerQuerySummary(
                query="ATG",
                definite_match_count=1,
                possible_match_count=1,
                strand=AnalysisKmerStrand.BOTH,
            ),
            KmerQuerySummary(
                query="RY",
                definite_match_count=0,
                possible_match_count=0,
                strand=AnalysisKmerStrand.FORWARD,
            ),
        ),
        queries=(
            KmerQueryHits(
                query="ATG",
                strand=AnalysisKmerStrand.BOTH,
                hits=(
                    KmerHit(
                        match_kind=KmerMatchKind.DEFINITE,
                        strand=SequenceStrand.PLUS,
                        sequence_range=KmerCoordinateRange(start=0, end=3),
                        alignment_range=KmerCoordinateRange(start=5, end=8),
                    ),
                    KmerHit(
                        match_kind=KmerMatchKind.POSSIBLE,
                        strand=SequenceStrand.MINUS,
                        sequence_range=KmerCoordinateRange(start=7, end=10),
                    ),
                ),
            ),
            KmerQueryHits(
                query="RY",
                strand=AnalysisKmerStrand.FORWARD,
            ),
        ),
    )

    payload = sidecar.model_dump(mode="json")
    restored = KmerHitsSidecar.model_validate(json.loads(json.dumps(payload)))

    assert [item["query"] for item in payload["query_summaries"]] == ["ATG", "RY"]
    assert [item["query"] for item in payload["queries"]] == ["ATG", "RY"]
    assert all(
        "kmer" not in item
        for collection in (payload["query_summaries"], payload["queries"])
        for item in collection
    )
    assert [item.query for item in restored.queries] == ["ATG", "RY"]
    assert [item.query for item in restored.query_summaries] == ["ATG", "RY"]
    assert restored.query_summaries[0].definite_match_count == 1
    assert restored.query_summaries[0].possible_match_count == 1
    assert restored.queries[0].strand is AnalysisKmerStrand.BOTH
    assert restored.queries[1].strand is AnalysisKmerStrand.FORWARD
    assert (
        restored.coordinate_system
        == InputProcessingCoordinateSystem.ZERO_BASED_END_EXCLUSIVE
    )
    assert (
        restored.queries[0].coordinate_system
        == InputProcessingCoordinateSystem.ZERO_BASED_END_EXCLUSIVE
    )
    assert restored.queries[0].hits[0].sequence_range.start == 0
    assert restored.queries[0].hits[0].sequence_range.end == 3


@pytest.mark.parametrize("collection_name", ("query_summaries", "queries"))
def test_kmer_hits_sidecar_rejects_legacy_result_without_query_identity(
    collection_name: str,
) -> None:
    sidecar = KmerHitsSidecar(
        sequence_id=_SEQUENCE_ID,
        query_summaries=(
            KmerQuerySummary(
                query="ATG",
                definite_match_count=0,
                possible_match_count=0,
                strand=AnalysisKmerStrand.FORWARD,
            ),
        ),
        queries=(
            KmerQueryHits(
                query="ATG",
                strand=AnalysisKmerStrand.FORWARD,
            ),
        ),
    )
    legacy_payload = sidecar.model_dump(mode="json")
    del legacy_payload[collection_name][0]["query"]

    with pytest.raises(ValidationError):
        KmerHitsSidecar.model_validate(legacy_payload)


@pytest.mark.parametrize(
    "path_value",
    (
        "/absolute/path.fasta",
        "../escape/path.fasta",
    ),
)
def test_artifact_paths_must_be_relative(path_value: str) -> None:
    with pytest.raises(ValidationError):
        InputProcessingUniqueSequence(
            sequence_id=_SEQUENCE_ID,
            sequence_artifact_path=path_value,
            facts=_sequence_facts(sequence_id=_SEQUENCE_ID),
            logical_sample_ids=("sample-a",),
        )


def test_sequence_facts_contract_does_not_include_u_to_t_replacements_field() -> None:
    facts = _sequence_facts(sequence_id=_SEQUENCE_ID)
    payload = facts.model_dump(mode="json")

    assert "u_count" in payload
    assert "u_to_t_replacements" not in payload


def test_sequence_facts_uses_gc_content_total_field_name() -> None:
    facts = _sequence_facts(sequence_id=_SEQUENCE_ID)
    payload = facts.model_dump(mode="json")

    assert "gc_content_total" in payload
    assert "gc_content" not in payload
