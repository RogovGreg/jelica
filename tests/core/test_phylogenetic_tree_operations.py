from __future__ import annotations

from Bio.Phylo.BaseTree import Clade, Tree
import pytest

from jelica_core.config import (
    AnalysisPhylogeneticTreeMethod,
    AnalysisPhylogeneticTreeRooting,
)
from jelica_core.distance_matrix import (
    DistanceMatrixAggregateCounts,
    DistanceMatrixResult,
    DistanceMatrixSequenceReference,
)
from jelica_core.phylogenetic_tree import (
    ZERO_DIAMETER_ROOTING_FALLBACK,
    PhylogeneticTreeComputationError,
    PhylogeneticTreeConstructionMode,
    build_phylogenetic_tree,
)


def _distance_result(
    matrix: tuple[tuple[float | None, ...], ...],
) -> DistanceMatrixResult:
    size = len(matrix)
    expected_pairs = (size * (size - 1)) // 2
    undefined_pairs = 0
    for row_index in range(size):
        for column_index in range(row_index + 1, size):
            if matrix[row_index][column_index] is None:
                undefined_pairs += 1
    return DistanceMatrixResult(
        sequence_references=tuple(
            DistanceMatrixSequenceReference(
                index=index,
                sequence_id=f"seq-{index}",
                logical_sample_ids=(f"sample-{index}",),
            )
            for index in range(size)
        ),
        matrix=matrix,
        unique_sequence_count=size,
        expected_pair_count=expected_pairs,
        processed_pair_count=expected_pairs,
        defined_distance_count=expected_pairs - undefined_pairs,
        undefined_distance_count=undefined_pairs,
        aggregate_counts=DistanceMatrixAggregateCounts(
            mismatch_count_sum=0,
            comparable_site_count_sum=0,
            excluded_gap_site_count_sum=0,
            excluded_ambiguous_site_count_sum=0,
        ),
    )


def _build(result: DistanceMatrixResult):
    return build_phylogenetic_tree(
        distance_matrix_result=result,
        method=AnalysisPhylogeneticTreeMethod.NEIGHBOR_JOINING,
        rooting=AnalysisPhylogeneticTreeRooting.MIDPOINT,
        input_snapshot_manifest_sha256="a" * 64,
    )


def test_singleton_matrix_produces_trivial_singleton_tree() -> None:
    computation = _build(_distance_result(((0.0,),)))

    assert (
        computation.result.construction_mode
        is PhylogeneticTreeConstructionMode.TRIVIAL_SINGLETON
    )
    assert computation.result.inference_performed is False
    assert computation.result.unrooted.rooted is False
    assert computation.result.rooted.rooted is True
    assert computation.unrooted_newick == "leaf_000001;"


def test_two_taxa_matrix_produces_trivial_pair_tree() -> None:
    computation = _build(_distance_result(((0.0, 0.4), (0.4, 0.0))))

    assert computation.result.construction_mode is PhylogeneticTreeConstructionMode.TRIVIAL_PAIR
    assert computation.result.inference_performed is False
    assert tuple(edge.branch_length for edge in computation.result.rooted.edges) == (
        pytest.approx(0.2),
        pytest.approx(0.2),
    )


def test_neighbor_joining_ties_are_deterministic_for_same_input() -> None:
    matrix = (
        (0.0, 0.5, 0.5, 0.5),
        (0.5, 0.0, 0.5, 0.5),
        (0.5, 0.5, 0.0, 0.5),
        (0.5, 0.5, 0.5, 0.0),
    )
    result = _distance_result(matrix)

    first = _build(result)
    second = _build(result)

    assert (
        first.result.construction_mode
        is PhylogeneticTreeConstructionMode.NEIGHBOR_JOINING
    )
    assert first.result.inference_performed is True
    assert first.unrooted_newick == second.unrooted_newick
    assert first.rooted_newick == second.rooted_newick
    assert first.result.model_dump(mode="json") == second.result.model_dump(mode="json")


def test_zero_diameter_matrix_uses_deterministic_rooting_fallback() -> None:
    computation = _build(
        _distance_result(
            (
                (0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0),
            )
        )
    )

    assert computation.result.zero_diameter is True
    assert computation.result.applied_rooting == ZERO_DIAMETER_ROOTING_FALLBACK
    assert any(
        warning.code == "zero_diameter_distance_matrix"
        for warning in computation.diagnostics.warnings
    )


def test_incomplete_distance_matrix_is_rejected() -> None:
    with pytest.raises(PhylogeneticTreeComputationError) as error_info:
        _build(_distance_result(((0.0, None), (None, 0.0))))

    assert error_info.value.reason == "distance_matrix_incomplete"


def test_negative_raw_branch_lengths_are_clamped_only_for_rooted_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_neighbor_joining_tree(*, matrix, leaf_labels):  # type: ignore[no-untyped-def]
        root = Clade()
        root.clades = [
            Clade(name=leaf_labels[0], branch_length=-0.25),
            Clade(
                branch_length=0.05,
                clades=[
                    Clade(name=leaf_labels[1], branch_length=0.1),
                    Clade(name=leaf_labels[2], branch_length=0.2),
                ],
            ),
        ]
        return Tree(root=root, rooted=False)

    monkeypatch.setattr(
        "jelica_core.phylogenetic_tree.operations._build_neighbor_joining_tree",
        _fake_neighbor_joining_tree,
    )

    computation = _build(
        _distance_result(
            (
                (0.0, 0.2, 0.3),
                (0.2, 0.0, 0.4),
                (0.3, 0.4, 0.0),
            )
        )
    )

    assert computation.result.raw_negative_branch_count == 1
    assert computation.result.normalized_negative_branch_count == 1
    assert any(
        edge.branch_length < 0 for edge in computation.result.unrooted.edges
    )
    assert all(
        edge.branch_length >= 0 for edge in computation.result.rooted.edges
    )
