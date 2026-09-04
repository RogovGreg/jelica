from __future__ import annotations

import pytest

from jelica_core.clade_detection import (
    CladeDetectionComputationError,
    detect_inferred_clades,
    stable_clade_id,
    validate_published_inferred_clades,
)
from jelica_core.config import (
    AnalysisCladeDetectionMethod,
    AnalysisPhylogeneticTreeMethod,
    AnalysisPhylogeneticTreeRooting,
)
from jelica_core.distance_matrix import (
    DistanceMatrixAggregateCounts,
    DistanceMatrixResult,
    DistanceMatrixSequenceReference,
)
from jelica_core.phylogenetic_tree import (
    PhylogeneticTreeConstructionMode,
    PhylogeneticTreeEdge,
    PhylogeneticTreeLeafMapping,
    PhylogeneticTreeNode,
    PhylogeneticTreeRepresentation,
    PhylogeneticTreeResult,
    TreeNodeKind,
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


def _tree_result(distance_result: DistanceMatrixResult):
    return build_phylogenetic_tree(
        distance_matrix_result=distance_result,
        method=AnalysisPhylogeneticTreeMethod.NEIGHBOR_JOINING,
        rooting=AnalysisPhylogeneticTreeRooting.MIDPOINT,
        input_snapshot_manifest_sha256="a" * 64,
    ).result


def _leaf_mappings(
    distance_result: DistanceMatrixResult,
) -> tuple[PhylogeneticTreeLeafMapping, ...]:
    return tuple(
        PhylogeneticTreeLeafMapping(
            leaf_label=f"leaf_{index + 1:06d}",
            sequence_index=index,
            sequence_id=reference.sequence_id,
            logical_sample_ids=reference.logical_sample_ids,
        )
        for index, reference in enumerate(distance_result.sequence_references)
    )


def _leaf_nodes(
    mappings: tuple[PhylogeneticTreeLeafMapping, ...],
) -> tuple[PhylogeneticTreeNode, ...]:
    return tuple(
        PhylogeneticTreeNode(
            node_id=mapping.leaf_label,
            kind=TreeNodeKind.LEAF,
            leaf_label=mapping.leaf_label,
            sequence_index=mapping.sequence_index,
            sequence_id=mapping.sequence_id,
            logical_sample_ids=mapping.logical_sample_ids,
        )
        for mapping in mappings
    )


def _zero_diameter(matrix: tuple[tuple[float | None, ...], ...]) -> bool:
    for row_index, row in enumerate(matrix):
        for column_index in range(row_index + 1, len(row)):
            value = row[column_index]
            if value is None or value != 0.0:
                return False
    return True


def _star_tree_result(distance_result: DistanceMatrixResult) -> PhylogeneticTreeResult:
    mappings = _leaf_mappings(distance_result)
    leaf_nodes = _leaf_nodes(mappings)
    leaf_labels = tuple(mapping.leaf_label for mapping in mappings)
    leaf_count = len(mappings)
    if leaf_count == 0:
        raise ValueError("distance_result must contain at least one sequence")

    if leaf_count == 1:
        mapping = mappings[0]
        rooted_root_id = "root_singleton"
        unrooted = PhylogeneticTreeRepresentation(
            rooted=False,
            traversal_root_id=mapping.leaf_label,
            node_count=1,
            edge_count=0,
            nodes=(leaf_nodes[0],),
            edges=tuple(),
        )
        rooted = PhylogeneticTreeRepresentation(
            rooted=True,
            root_id=rooted_root_id,
            traversal_root_id=rooted_root_id,
            node_count=2,
            edge_count=1,
            nodes=(
                PhylogeneticTreeNode(node_id=rooted_root_id, kind=TreeNodeKind.ROOT),
                leaf_nodes[0],
            ),
            edges=(
                PhylogeneticTreeEdge(
                    parent_id=rooted_root_id,
                    child_id=mapping.leaf_label,
                    branch_length=0.0,
                ),
            ),
        )
        construction_mode = PhylogeneticTreeConstructionMode.TRIVIAL_SINGLETON
        inference_performed = False
    elif leaf_count == 2:
        unrooted_root_id = "u_root_pair"
        rooted_root_id = "root_pair"
        unrooted = PhylogeneticTreeRepresentation(
            rooted=False,
            traversal_root_id=unrooted_root_id,
            node_count=3,
            edge_count=2,
            nodes=(
                PhylogeneticTreeNode(node_id=unrooted_root_id, kind=TreeNodeKind.INTERNAL),
                *leaf_nodes,
            ),
            edges=tuple(
                PhylogeneticTreeEdge(
                    parent_id=unrooted_root_id,
                    child_id=mapping.leaf_label,
                    branch_length=0.0,
                )
                for mapping in mappings
            ),
        )
        rooted = PhylogeneticTreeRepresentation(
            rooted=True,
            root_id=rooted_root_id,
            traversal_root_id=rooted_root_id,
            node_count=3,
            edge_count=2,
            nodes=(
                PhylogeneticTreeNode(node_id=rooted_root_id, kind=TreeNodeKind.ROOT),
                *leaf_nodes,
            ),
            edges=tuple(
                PhylogeneticTreeEdge(
                    parent_id=rooted_root_id,
                    child_id=mapping.leaf_label,
                    branch_length=0.0,
                )
                for mapping in mappings
            ),
        )
        construction_mode = PhylogeneticTreeConstructionMode.TRIVIAL_PAIR
        inference_performed = False
    else:
        unrooted_root_id = "u_root_star"
        rooted_root_id = "root_star"
        unrooted = PhylogeneticTreeRepresentation(
            rooted=False,
            traversal_root_id=unrooted_root_id,
            node_count=leaf_count + 1,
            edge_count=leaf_count,
            nodes=(
                PhylogeneticTreeNode(node_id=unrooted_root_id, kind=TreeNodeKind.INTERNAL),
                *leaf_nodes,
            ),
            edges=tuple(
                PhylogeneticTreeEdge(
                    parent_id=unrooted_root_id,
                    child_id=mapping.leaf_label,
                    branch_length=0.0,
                )
                for mapping in mappings
            ),
        )
        rooted = PhylogeneticTreeRepresentation(
            rooted=True,
            root_id=rooted_root_id,
            traversal_root_id=rooted_root_id,
            node_count=leaf_count + 1,
            edge_count=leaf_count,
            nodes=(
                PhylogeneticTreeNode(node_id=rooted_root_id, kind=TreeNodeKind.ROOT),
                *leaf_nodes,
            ),
            edges=tuple(
                PhylogeneticTreeEdge(
                    parent_id=rooted_root_id,
                    child_id=mapping.leaf_label,
                    branch_length=0.0,
                )
                for mapping in mappings
            ),
        )
        construction_mode = PhylogeneticTreeConstructionMode.NEIGHBOR_JOINING
        inference_performed = True

    return PhylogeneticTreeResult(
        construction_mode=construction_mode,
        inference_performed=inference_performed,
        requested_rooting=AnalysisPhylogeneticTreeRooting.MIDPOINT,
        applied_rooting="synthetic_rooting",
        input_snapshot_manifest_sha256="a" * 64,
        canonical_leaf_order=leaf_labels,
        leaf_mappings=mappings,
        unrooted=unrooted,
        rooted=rooted,
        raw_negative_branch_count=0,
        normalized_negative_branch_count=0,
        zero_diameter=_zero_diameter(distance_result.matrix),
        node_count=rooted.node_count,
        edge_count=rooted.edge_count,
    )


def _balanced_four_leaf_tree_result(
    distance_result: DistanceMatrixResult,
) -> PhylogeneticTreeResult:
    mappings = _leaf_mappings(distance_result)
    if len(mappings) != 4:
        raise ValueError("balanced helper requires exactly four leaves")
    leaf_nodes = _leaf_nodes(mappings)
    leaf_labels = tuple(mapping.leaf_label for mapping in mappings)

    rooted_root_id = "root_balanced"
    left_id = "node_left"
    right_id = "node_right"
    rooted = PhylogeneticTreeRepresentation(
        rooted=True,
        root_id=rooted_root_id,
        traversal_root_id=rooted_root_id,
        node_count=7,
        edge_count=6,
        nodes=(
            PhylogeneticTreeNode(node_id=rooted_root_id, kind=TreeNodeKind.ROOT),
            PhylogeneticTreeNode(node_id=left_id, kind=TreeNodeKind.INTERNAL),
            PhylogeneticTreeNode(node_id=right_id, kind=TreeNodeKind.INTERNAL),
            *leaf_nodes,
        ),
        edges=(
            PhylogeneticTreeEdge(parent_id=rooted_root_id, child_id=left_id, branch_length=0.0),
            PhylogeneticTreeEdge(parent_id=rooted_root_id, child_id=right_id, branch_length=0.0),
            PhylogeneticTreeEdge(
                parent_id=left_id,
                child_id=mappings[0].leaf_label,
                branch_length=0.0,
            ),
            PhylogeneticTreeEdge(
                parent_id=left_id,
                child_id=mappings[1].leaf_label,
                branch_length=0.0,
            ),
            PhylogeneticTreeEdge(
                parent_id=right_id,
                child_id=mappings[2].leaf_label,
                branch_length=0.0,
            ),
            PhylogeneticTreeEdge(
                parent_id=right_id,
                child_id=mappings[3].leaf_label,
                branch_length=0.0,
            ),
        ),
    )

    unrooted_root_id = "u_root_balanced"
    unrooted_left_id = "u_node_left"
    unrooted_right_id = "u_node_right"
    unrooted = PhylogeneticTreeRepresentation(
        rooted=False,
        traversal_root_id=unrooted_root_id,
        node_count=7,
        edge_count=6,
        nodes=(
            PhylogeneticTreeNode(node_id=unrooted_root_id, kind=TreeNodeKind.INTERNAL),
            PhylogeneticTreeNode(node_id=unrooted_left_id, kind=TreeNodeKind.INTERNAL),
            PhylogeneticTreeNode(node_id=unrooted_right_id, kind=TreeNodeKind.INTERNAL),
            *leaf_nodes,
        ),
        edges=(
            PhylogeneticTreeEdge(
                parent_id=unrooted_root_id,
                child_id=unrooted_left_id,
                branch_length=0.0,
            ),
            PhylogeneticTreeEdge(
                parent_id=unrooted_root_id,
                child_id=unrooted_right_id,
                branch_length=0.0,
            ),
            PhylogeneticTreeEdge(
                parent_id=unrooted_left_id,
                child_id=mappings[0].leaf_label,
                branch_length=0.0,
            ),
            PhylogeneticTreeEdge(
                parent_id=unrooted_left_id,
                child_id=mappings[1].leaf_label,
                branch_length=0.0,
            ),
            PhylogeneticTreeEdge(
                parent_id=unrooted_right_id,
                child_id=mappings[2].leaf_label,
                branch_length=0.0,
            ),
            PhylogeneticTreeEdge(
                parent_id=unrooted_right_id,
                child_id=mappings[3].leaf_label,
                branch_length=0.0,
            ),
        ),
    )
    return PhylogeneticTreeResult(
        construction_mode=PhylogeneticTreeConstructionMode.NEIGHBOR_JOINING,
        inference_performed=True,
        requested_rooting=AnalysisPhylogeneticTreeRooting.MIDPOINT,
        applied_rooting="synthetic_rooting",
        input_snapshot_manifest_sha256="a" * 64,
        canonical_leaf_order=leaf_labels,
        leaf_mappings=mappings,
        unrooted=unrooted,
        rooted=rooted,
        raw_negative_branch_count=0,
        normalized_negative_branch_count=0,
        zero_diameter=_zero_diameter(distance_result.matrix),
        node_count=rooted.node_count,
        edge_count=rooted.edge_count,
    )


def test_singleton_input_produces_singleton_clade_partition() -> None:
    distance_result = _distance_result(((0.0,),))
    tree_result = _star_tree_result(distance_result)

    computation = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.0,
        tree_snapshot_manifest_sha256="4" * 64,
        matrix_snapshot_manifest_sha256="5" * 64,
    )

    assert computation.result.clade_count == 1
    clade = computation.result.clades[0]
    assert clade.is_singleton is True
    assert clade.leaf_count == 1
    assert clade.max_pairwise_distance == pytest.approx(0.0)
    assert clade.sequence_indices == (0,)
    assert clade.logical_sample_ids == ("sample-0",)
    assert clade.clade_id == stable_clade_id((0,))
    assert computation.result.coverage_leaf_count == 1
    assert computation.result.uncovered_leaf_count == 0


def test_two_leaf_input_selects_root_clade_when_distance_within_threshold() -> None:
    distance_result = _distance_result(((0.0, 0.2), (0.2, 0.0)))
    tree_result = _star_tree_result(distance_result)

    computation = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.2,
        tree_snapshot_manifest_sha256="6" * 64,
        matrix_snapshot_manifest_sha256="7" * 64,
    )

    assert computation.result.clade_count == 1
    clade = computation.result.clades[0]
    assert clade.source_node_id == tree_result.rooted.root_id
    assert clade.sequence_indices == (0, 1)
    assert clade.max_pairwise_distance == pytest.approx(0.2)
    assert tuple(item.ordinal for item in computation.result.clades) == (1,)


def test_two_leaf_input_splits_into_singletons_when_distance_exceeds_threshold() -> None:
    distance_result = _distance_result(((0.0, 0.3), (0.3, 0.0)))
    tree_result = _star_tree_result(distance_result)

    computation = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.2,
        tree_snapshot_manifest_sha256="8" * 64,
        matrix_snapshot_manifest_sha256="9" * 64,
    )

    assert computation.result.clade_count == 2
    assert tuple(clade.sequence_indices for clade in computation.result.clades) == (
        (0,),
        (1,),
    )
    assert all(clade.is_singleton for clade in computation.result.clades)
    assert all(
        clade.source_node_id != tree_result.rooted.root_id
        for clade in computation.result.clades
    )
    assert computation.result.coverage_leaf_count == 2
    assert computation.result.uncovered_leaf_count == 0


def test_multifurcation_considers_all_cross_child_pairs() -> None:
    distance_result = _distance_result(
        (
            (0.0, 0.1, 0.2),
            (0.1, 0.0, 0.7),
            (0.2, 0.7, 0.0),
        )
    )
    tree_result = _star_tree_result(distance_result)

    high_threshold = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.8,
        tree_snapshot_manifest_sha256="a" * 64,
        matrix_snapshot_manifest_sha256="b" * 64,
    )
    low_threshold = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.6,
        tree_snapshot_manifest_sha256="a" * 64,
        matrix_snapshot_manifest_sha256="b" * 64,
    )

    assert high_threshold.result.clade_count == 1
    assert high_threshold.result.clades[0].max_pairwise_distance == pytest.approx(0.7)
    assert low_threshold.result.clade_count == 3
    assert tuple(clade.sequence_indices for clade in low_threshold.result.clades) == (
        (0,),
        (1,),
        (2,),
    )


def test_clade_ids_and_order_are_deterministic_for_same_inputs() -> None:
    distance_result = _distance_result(
        (
            (0.0, 0.1, 0.9, 0.9),
            (0.1, 0.0, 0.9, 0.9),
            (0.9, 0.9, 0.0, 0.2),
            (0.9, 0.9, 0.2, 0.0),
        )
    )
    tree_result = _balanced_four_leaf_tree_result(distance_result)

    first = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.2,
        tree_snapshot_manifest_sha256="c" * 64,
        matrix_snapshot_manifest_sha256="d" * 64,
    )
    second = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.2,
        tree_snapshot_manifest_sha256="c" * 64,
        matrix_snapshot_manifest_sha256="d" * 64,
    )

    assert tuple(clade.clade_id for clade in first.result.clades) == tuple(
        clade.clade_id for clade in second.result.clades
    )
    assert tuple(clade.ordinal for clade in first.result.clades) == tuple(
        clade.ordinal for clade in second.result.clades
    )
    assert first.result.canonical_clade_order == second.result.canonical_clade_order


def test_threshold_selects_maximal_subtrees() -> None:
    distance_result = _distance_result(
        (
            (0.0, 0.1, 0.9, 0.9),
            (0.1, 0.0, 0.9, 0.9),
            (0.9, 0.9, 0.0, 0.2),
            (0.9, 0.9, 0.2, 0.0),
        )
    )
    tree_result = _tree_result(distance_result)

    computation = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.2,
        tree_snapshot_manifest_sha256="b" * 64,
        matrix_snapshot_manifest_sha256="c" * 64,
    )

    assert tuple(clade.sequence_indices for clade in computation.result.clades) == (
        (0, 1),
        (2, 3),
    )
    assert tuple(clade.ordinal for clade in computation.result.clades) == (1, 2)
    assert all(clade.within_threshold for clade in computation.result.clades)


def test_threshold_zero_produces_singleton_partition() -> None:
    distance_result = _distance_result(
        (
            (0.0, 0.3, 0.8),
            (0.3, 0.0, 0.7),
            (0.8, 0.7, 0.0),
        )
    )
    tree_result = _tree_result(distance_result)

    computation = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.0,
        tree_snapshot_manifest_sha256="d" * 64,
        matrix_snapshot_manifest_sha256="e" * 64,
    )

    assert computation.result.clade_count == 3
    assert all(clade.is_singleton for clade in computation.result.clades)
    assert tuple(clade.sequence_indices for clade in computation.result.clades) == (
        (0,),
        (1,),
        (2,),
    )


def test_threshold_one_can_select_root_clade() -> None:
    distance_result = _distance_result(
        (
            (0.0, 0.3, 0.8),
            (0.3, 0.0, 0.7),
            (0.8, 0.7, 0.0),
        )
    )
    tree_result = _tree_result(distance_result)

    computation = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=1.0,
        tree_snapshot_manifest_sha256="f" * 64,
        matrix_snapshot_manifest_sha256="1" * 64,
    )

    assert computation.result.clade_count == 1
    assert computation.result.clades[0].sequence_indices == (0, 1, 2)


def test_validate_published_detects_assignment_mismatch() -> None:
    distance_result = _distance_result(
        (
            (0.0, 0.1, 0.9, 0.9),
            (0.1, 0.0, 0.9, 0.9),
            (0.9, 0.9, 0.0, 0.2),
            (0.9, 0.9, 0.2, 0.0),
        )
    )
    tree_result = _tree_result(distance_result)

    computation = detect_inferred_clades(
        phylogenetic_tree_result=tree_result,
        distance_matrix_result=distance_result,
        method=AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE,
        max_within_clade_distance=0.2,
        tree_snapshot_manifest_sha256="2" * 64,
        matrix_snapshot_manifest_sha256="3" * 64,
    )

    first = computation.assignment_records[0]
    tampered_assignments = (
        first.model_copy(update={"clade_id": "clade_invalid"}),
        *computation.assignment_records[1:],
    )

    with pytest.raises(CladeDetectionComputationError) as error_info:
        validate_published_inferred_clades(
            phylogenetic_tree_result=tree_result,
            distance_matrix_result=distance_result,
            result=computation.result,
            membership_records=computation.membership_records,
            assignment_records=tampered_assignments,
        )

    assert error_info.value.reason == "clade_assignments_mismatch"
