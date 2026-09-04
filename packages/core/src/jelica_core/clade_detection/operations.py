from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from jelica_core.config.models import (
    AnalysisCladeDetectionMethod,
    AnalysisDistanceMatrixModel,
)
from jelica_core.distance_matrix import DistanceMatrixResult
from jelica_core.phylogenetic_tree import (
    PhylogeneticTreeResult,
    TreeNodeKind,
)

from .artifacts import (
    CLADE_DISTANCE_TOLERANCE,
    CLADE_INTERPRETATION_SCOPE_CURRENT_ROOTED_TREE,
    CLADE_SELECTION_POLICY_MAXIMAL_MONOPHYLETIC_SUBTREES,
    CladeAssignmentRecord,
    InferredClade,
    InferredCladeMember,
    InferredCladeMembershipRecord,
    InferredCladesResult,
    stable_clade_id,
)


class CladeDetectionComputationError(RuntimeError):
    def __init__(self, *, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class CladeDetectionComputation:
    result: InferredCladesResult
    membership_records: tuple[InferredCladeMembershipRecord, ...]
    assignment_records: tuple[CladeAssignmentRecord, ...]


@dataclass(frozen=True, slots=True)
class _LeafInfo:
    node_id: str
    leaf_label: str
    sequence_index: int
    sequence_id: str
    logical_sample_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TreeStructure:
    root_id: str
    children_by_parent: dict[str, tuple[str, ...]]
    parent_by_node: dict[str, str]
    node_ids: tuple[str, ...]
    preorder_node_ids: tuple[str, ...]
    postorder_node_ids: tuple[str, ...]
    leaf_by_node_id: dict[str, _LeafInfo]
    leaf_by_sequence_index: dict[int, _LeafInfo]


@dataclass(frozen=True, slots=True)
class _NodeMetrics:
    node_id: str
    parent_id: str | None
    sequence_indices: tuple[int, ...]
    leaf_labels: tuple[str, ...]
    max_pairwise_distance: float
    eligible: bool


NodeProgressCallback = Callable[[int, int, int, int], None]


def detect_inferred_clades(
    *,
    phylogenetic_tree_result: PhylogeneticTreeResult,
    distance_matrix_result: DistanceMatrixResult,
    method: AnalysisCladeDetectionMethod,
    max_within_clade_distance: float,
    tree_snapshot_manifest_sha256: str,
    matrix_snapshot_manifest_sha256: str,
    float_tolerance: float = CLADE_DISTANCE_TOLERANCE,
    control_check: Callable[[], None] | None = None,
    control_check_interval: int = 4096,
    node_progress_callback: NodeProgressCallback | None = None,
) -> CladeDetectionComputation:
    _validate_threshold(
        value=max_within_clade_distance,
        reason="clade_threshold_invalid",
    )
    if not math.isfinite(float_tolerance) or float_tolerance <= 0.0:
        raise CladeDetectionComputationError(
            reason="clade_tolerance_invalid",
            detail="Clade detection requires a finite positive float tolerance.",
        )
    if control_check_interval <= 0:
        raise CladeDetectionComputationError(
            reason="clade_control_interval_invalid",
            detail="Control-check interval must be a positive integer.",
        )
    if method is not AnalysisCladeDetectionMethod.MAX_PAIRWISE_DISTANCE:
        raise CladeDetectionComputationError(
            reason="clade_method_unsupported",
            detail="Only max_pairwise_distance clade detection is supported.",
        )
    if distance_matrix_result.model is not AnalysisDistanceMatrixModel.P_DISTANCE:
        raise CladeDetectionComputationError(
            reason="distance_matrix_model_unsupported",
            detail="Clade detection requires p_distance matrix inputs.",
        )
    if distance_matrix_result.undefined_distance_count != 0:
        raise CladeDetectionComputationError(
            reason="distance_matrix_incomplete",
            detail="Clade detection requires a completed distance matrix without null values.",
        )
    if not phylogenetic_tree_result.rooted.rooted:
        raise CladeDetectionComputationError(
            reason="tree_rooted_representation_missing",
            detail="Clade detection requires a rooted tree representation.",
        )

    tree = _build_tree_structure(phylogenetic_tree_result)
    matrix = _load_complete_distance_matrix(distance_matrix_result)
    _validate_leaf_mapping_against_matrix(
        tree=tree,
        phylogenetic_tree_result=phylogenetic_tree_result,
        distance_matrix_result=distance_matrix_result,
    )

    expected_pair_count = (len(tree.leaf_by_sequence_index) * (len(tree.leaf_by_sequence_index) - 1)) // 2
    processed_pairs = 0
    processed_nodes = 0
    total_nodes = len(tree.node_ids)
    metrics_by_node: dict[str, _NodeMetrics] = {}

    for node_id in tree.postorder_node_ids:
        if control_check is not None and processed_nodes % control_check_interval == 0:
            control_check()
        leaf_info = tree.leaf_by_node_id.get(node_id)
        if leaf_info is not None:
            metric = _NodeMetrics(
                node_id=node_id,
                parent_id=tree.parent_by_node.get(node_id),
                sequence_indices=(leaf_info.sequence_index,),
                leaf_labels=(leaf_info.leaf_label,),
                max_pairwise_distance=0.0,
                eligible=True,
            )
            metrics_by_node[node_id] = metric
            processed_nodes += 1
            if node_progress_callback is not None:
                node_progress_callback(
                    processed_nodes,
                    total_nodes,
                    processed_pairs,
                    expected_pair_count,
                )
            continue

        child_ids = tree.children_by_parent.get(node_id, tuple())
        if len(child_ids) == 0:
            raise CladeDetectionComputationError(
                reason="tree_internal_without_children",
                detail="Rooted tree internal node has no children.",
            )
        child_metrics = [
            metrics_by_node[child_id]
            for child_id in sorted(
                child_ids,
                key=lambda value: (
                    metrics_by_node[value].sequence_indices[0],
                    metrics_by_node[value].sequence_indices,
                    value,
                ),
            )
        ]
        sequence_indices = tuple(
            index for child in child_metrics for index in child.sequence_indices
        )
        sequence_indices = tuple(sorted(sequence_indices))
        leaf_labels = tuple(
            tree.leaf_by_sequence_index[index].leaf_label for index in sequence_indices
        )
        max_pairwise_distance = max(
            child.max_pairwise_distance for child in child_metrics
        )

        for left_child_index, left_child in enumerate(child_metrics):
            for right_child in child_metrics[left_child_index + 1 :]:
                for left_index in left_child.sequence_indices:
                    for right_index in right_child.sequence_indices:
                        if control_check is not None:
                            processed_pairs += 1
                            if processed_pairs % control_check_interval == 0:
                                control_check()
                        distance = matrix[left_index][right_index]
                        if distance is None:
                            raise CladeDetectionComputationError(
                                reason="distance_matrix_incomplete",
                                detail=(
                                    "Clade detection encountered an undefined pair distance "
                                    "while processing rooted subtrees."
                                ),
                            )
                        numeric_distance = float(distance)
                        if numeric_distance > max_pairwise_distance:
                            max_pairwise_distance = numeric_distance
                        if control_check is None:
                            processed_pairs += 1

        metric = _NodeMetrics(
            node_id=node_id,
            parent_id=tree.parent_by_node.get(node_id),
            sequence_indices=sequence_indices,
            leaf_labels=leaf_labels,
            max_pairwise_distance=max_pairwise_distance,
            eligible=max_pairwise_distance <= (max_within_clade_distance + float_tolerance),
        )
        metrics_by_node[node_id] = metric
        processed_nodes += 1
        if node_progress_callback is not None:
            node_progress_callback(
                processed_nodes,
                total_nodes,
                processed_pairs,
                expected_pair_count,
            )

    if processed_pairs != expected_pair_count:
        raise CladeDetectionComputationError(
            reason="pairwise_metric_coverage_invalid",
            detail="Node metric computation did not cover each unordered leaf pair exactly once.",
        )

    selected_node_ids = _select_maximal_eligible_nodes(
        root_id=tree.root_id,
        tree=tree,
        metrics_by_node=metrics_by_node,
    )
    _validate_selected_partition(
        tree=tree,
        matrix=matrix,
        metrics_by_node=metrics_by_node,
        selected_node_ids=selected_node_ids,
        threshold=max_within_clade_distance,
        tolerance=float_tolerance,
    )

    inferred_clades = _build_inferred_clades(
        selected_node_ids=selected_node_ids,
        tree=tree,
        distance_matrix_result=distance_matrix_result,
        metrics_by_node=metrics_by_node,
        threshold=max_within_clade_distance,
    )
    membership_records = tuple(
        InferredCladeMembershipRecord(
            clade_id=clade.clade_id,
            ordinal=clade.ordinal,
            source_node_id=clade.source_node_id,
            leaf_count=clade.leaf_count,
            logical_sample_count=clade.logical_sample_count,
            is_singleton=clade.is_singleton,
            leaf_labels=clade.leaf_labels,
            sequence_indices=clade.sequence_indices,
            sequence_ids=clade.sequence_ids,
            logical_sample_ids=clade.logical_sample_ids,
            max_pairwise_distance=clade.max_pairwise_distance,
            max_within_clade_distance=clade.max_within_clade_distance,
            within_threshold=clade.within_threshold,
        )
        for clade in inferred_clades
    )
    assignment_records = _build_assignment_records(inferred_clades)

    clade_sizes = tuple(clade.leaf_count for clade in inferred_clades)
    result = InferredCladesResult(
        method=method,
        selection_policy=CLADE_SELECTION_POLICY_MAXIMAL_MONOPHYLETIC_SUBTREES,
        interpretation_scope=CLADE_INTERPRETATION_SCOPE_CURRENT_ROOTED_TREE,
        max_within_clade_distance=max_within_clade_distance,
        float_tolerance=float_tolerance,
        input_distance_model=distance_matrix_result.model,
        tree_snapshot_manifest_sha256=tree_snapshot_manifest_sha256,
        matrix_snapshot_manifest_sha256=matrix_snapshot_manifest_sha256,
        requested_rooting=phylogenetic_tree_result.requested_rooting,
        applied_rooting=phylogenetic_tree_result.applied_rooting,
        canonical_leaf_count=len(tree.leaf_by_sequence_index),
        canonical_clade_order=tuple(clade.clade_id for clade in inferred_clades),
        clades=inferred_clades,
        clade_count=len(inferred_clades),
        singleton_clade_count=sum(1 for clade in inferred_clades if clade.is_singleton),
        multi_leaf_clade_count=sum(1 for clade in inferred_clades if not clade.is_singleton),
        minimum_clade_size=min(clade_sizes),
        maximum_clade_size=max(clade_sizes),
        coverage_leaf_count=sum(clade_sizes),
        uncovered_leaf_count=0,
        partition_validated=True,
    )
    return CladeDetectionComputation(
        result=result,
        membership_records=membership_records,
        assignment_records=assignment_records,
    )


def validate_published_inferred_clades(
    *,
    phylogenetic_tree_result: PhylogeneticTreeResult,
    distance_matrix_result: DistanceMatrixResult,
    result: InferredCladesResult,
    membership_records: tuple[InferredCladeMembershipRecord, ...],
    assignment_records: tuple[CladeAssignmentRecord, ...],
) -> None:
    expected = detect_inferred_clades(
        phylogenetic_tree_result=phylogenetic_tree_result,
        distance_matrix_result=distance_matrix_result,
        method=result.method,
        max_within_clade_distance=result.max_within_clade_distance,
        tree_snapshot_manifest_sha256=result.tree_snapshot_manifest_sha256,
        matrix_snapshot_manifest_sha256=result.matrix_snapshot_manifest_sha256,
        float_tolerance=result.float_tolerance,
    )
    if expected.result.model_dump(mode="json") != result.model_dump(mode="json"):
        raise CladeDetectionComputationError(
            reason="clade_result_semantic_mismatch",
            detail=(
                "Published inferred_clades.json is inconsistent with rooted tree, distance "
                "matrix, threshold, or deterministic clade ordering."
            ),
        )
    if tuple(record.model_dump(mode="json") for record in expected.membership_records) != tuple(
        record.model_dump(mode="json") for record in membership_records
    ):
        raise CladeDetectionComputationError(
            reason="clade_memberships_mismatch",
            detail="Published clade_memberships.jsonl is inconsistent with inferred clade result.",
        )
    if tuple(record.model_dump(mode="json") for record in expected.assignment_records) != tuple(
        record.model_dump(mode="json") for record in assignment_records
    ):
        raise CladeDetectionComputationError(
            reason="clade_assignments_mismatch",
            detail="Published clade_assignments.tsv is inconsistent with inferred clade result.",
        )


def _build_tree_structure(phylogenetic_tree_result: PhylogeneticTreeResult) -> _TreeStructure:
    rooted = phylogenetic_tree_result.rooted
    root_id = rooted.root_id
    if root_id is None:
        raise CladeDetectionComputationError(
            reason="tree_root_id_missing",
            detail="Rooted tree representation must define root_id.",
        )
    nodes_by_id = {node.node_id: node for node in rooted.nodes}
    if root_id not in nodes_by_id:
        raise CladeDetectionComputationError(
            reason="tree_root_id_unknown",
            detail="Rooted tree root_id does not reference a known node.",
        )

    parent_by_node: dict[str, str] = {}
    children_by_parent: dict[str, list[str]] = {}
    for edge in rooted.edges:
        if edge.child_id in parent_by_node:
            raise CladeDetectionComputationError(
                reason="tree_parent_assignment_invalid",
                detail="Rooted tree child node has multiple parents.",
            )
        parent_by_node[edge.child_id] = edge.parent_id
        children_by_parent.setdefault(edge.parent_id, []).append(edge.child_id)

    if root_id in parent_by_node:
        raise CladeDetectionComputationError(
            reason="tree_root_has_parent",
            detail="Rooted tree root node unexpectedly has an incoming edge.",
        )

    leaf_by_node_id: dict[str, _LeafInfo] = {}
    leaf_by_sequence_index: dict[int, _LeafInfo] = {}
    for node in rooted.nodes:
        if node.kind is not TreeNodeKind.LEAF:
            continue
        if (
            node.leaf_label is None
            or node.sequence_index is None
            or node.sequence_id is None
        ):
            raise CladeDetectionComputationError(
                reason="tree_leaf_mapping_missing",
                detail="Rooted tree leaf nodes must define label, sequence index, and sequence ID.",
            )
        leaf = _LeafInfo(
            node_id=node.node_id,
            leaf_label=node.leaf_label,
            sequence_index=node.sequence_index,
            sequence_id=node.sequence_id,
            logical_sample_ids=node.logical_sample_ids,
        )
        leaf_by_node_id[node.node_id] = leaf
        if node.sequence_index in leaf_by_sequence_index:
            raise CladeDetectionComputationError(
                reason="tree_leaf_index_duplicate",
                detail="Rooted tree leaf sequence_index values must be unique.",
            )
        leaf_by_sequence_index[node.sequence_index] = leaf

    preorder: list[str] = []
    postorder: list[str] = []
    visited: set[str] = set()

    def walk(node_id: str) -> None:
        if node_id in visited:
            raise CladeDetectionComputationError(
                reason="tree_cycle_detected",
                detail="Rooted tree traversal detected a cycle.",
            )
        visited.add(node_id)
        preorder.append(node_id)
        for child_id in children_by_parent.get(node_id, []):
            walk(child_id)
        postorder.append(node_id)

    walk(root_id)
    if len(visited) != len(nodes_by_id):
        raise CladeDetectionComputationError(
            reason="tree_disconnected",
            detail="Rooted tree traversal did not reach all nodes.",
        )
    for leaf in leaf_by_node_id.values():
        if len(children_by_parent.get(leaf.node_id, [])) != 0:
            raise CladeDetectionComputationError(
                reason="tree_leaf_has_children",
                detail="Rooted tree leaf nodes must not have children.",
            )

    return _TreeStructure(
        root_id=root_id,
        children_by_parent={
            node_id: tuple(children)
            for node_id, children in children_by_parent.items()
        },
        parent_by_node=parent_by_node,
        node_ids=tuple(node.node_id for node in rooted.nodes),
        preorder_node_ids=tuple(preorder),
        postorder_node_ids=tuple(postorder),
        leaf_by_node_id=leaf_by_node_id,
        leaf_by_sequence_index=leaf_by_sequence_index,
    )


def _load_complete_distance_matrix(
    distance_matrix_result: DistanceMatrixResult,
) -> tuple[tuple[float | None, ...], ...]:
    matrix = distance_matrix_result.matrix
    sequence_count = len(distance_matrix_result.sequence_references)
    if len(matrix) != sequence_count:
        raise CladeDetectionComputationError(
            reason="distance_matrix_dimensions_invalid",
            detail="Distance-matrix row count does not match sequence references.",
        )
    for index, reference in enumerate(distance_matrix_result.sequence_references):
        if reference.index != index:
            raise CladeDetectionComputationError(
                reason="distance_matrix_index_invalid",
                detail="Distance-matrix sequence references must follow canonical index order.",
            )
    for row_index, row in enumerate(matrix):
        if len(row) != sequence_count:
            raise CladeDetectionComputationError(
                reason="distance_matrix_dimensions_invalid",
                detail="Distance matrix must be square.",
            )
        for column_index, value in enumerate(row):
            if row_index == column_index:
                if value is None or not math.isclose(
                    float(value), 0.0, rel_tol=0.0, abs_tol=0.0
                ):
                    raise CladeDetectionComputationError(
                        reason="distance_matrix_diagonal_invalid",
                        detail="Distance-matrix diagonal values must be explicit zeros.",
                    )
                continue
            if value is None:
                raise CladeDetectionComputationError(
                    reason="distance_matrix_incomplete",
                    detail="Distance matrix contains undefined pair distances.",
                )
            numeric = float(value)
            if not math.isfinite(numeric) or numeric < 0.0 or numeric > 1.0:
                raise CladeDetectionComputationError(
                    reason="distance_matrix_value_invalid",
                    detail="Distance matrix values must be finite and within [0, 1].",
                )
    return matrix


def _validate_leaf_mapping_against_matrix(
    *,
    tree: _TreeStructure,
    phylogenetic_tree_result: PhylogeneticTreeResult,
    distance_matrix_result: DistanceMatrixResult,
) -> None:
    expected_leaf_count = len(distance_matrix_result.sequence_references)
    if len(tree.leaf_by_sequence_index) != expected_leaf_count:
        raise CladeDetectionComputationError(
            reason="leaf_count_mismatch",
            detail="Rooted tree leaf count does not match distance-matrix dimensions.",
        )
    expected_indexes = set(range(expected_leaf_count))
    if set(tree.leaf_by_sequence_index) != expected_indexes:
        raise CladeDetectionComputationError(
            reason="leaf_index_range_invalid",
            detail="Rooted tree leaf sequence indexes do not match matrix index range.",
        )
    if len(phylogenetic_tree_result.leaf_mappings) != expected_leaf_count:
        raise CladeDetectionComputationError(
            reason="leaf_mapping_count_invalid",
            detail="Tree leaf mapping count does not match distance-matrix dimensions.",
        )
    for mapping in phylogenetic_tree_result.leaf_mappings:
        leaf = tree.leaf_by_sequence_index.get(mapping.sequence_index)
        if leaf is None:
            raise CladeDetectionComputationError(
                reason="leaf_mapping_unknown_index",
                detail="Tree leaf mapping references an unknown matrix index.",
            )
        if (
            leaf.leaf_label != mapping.leaf_label
            or leaf.sequence_id != mapping.sequence_id
            or leaf.logical_sample_ids != mapping.logical_sample_ids
        ):
            raise CladeDetectionComputationError(
                reason="leaf_mapping_inconsistent",
                detail="Tree rooted leaf metadata is inconsistent with tree leaf mappings.",
            )
        reference = distance_matrix_result.sequence_references[mapping.sequence_index]
        if (
            reference.sequence_id != mapping.sequence_id
            or reference.logical_sample_ids != mapping.logical_sample_ids
        ):
            raise CladeDetectionComputationError(
                reason="matrix_mapping_inconsistent",
                detail=(
                    "Tree leaf mappings are inconsistent with distance-matrix sequence "
                    "references."
                ),
            )


def _select_maximal_eligible_nodes(
    *,
    root_id: str,
    tree: _TreeStructure,
    metrics_by_node: dict[str, _NodeMetrics],
) -> tuple[str, ...]:
    selected: list[str] = []

    def visit(node_id: str) -> None:
        metric = metrics_by_node[node_id]
        if metric.eligible:
            selected.append(node_id)
            return
        child_ids = tree.children_by_parent.get(node_id, tuple())
        for child_id in sorted(
            child_ids,
            key=lambda value: (
                metrics_by_node[value].sequence_indices[0],
                metrics_by_node[value].sequence_indices,
                value,
            ),
        ):
            visit(child_id)

    visit(root_id)
    return tuple(selected)


def _validate_selected_partition(
    *,
    tree: _TreeStructure,
    matrix: tuple[tuple[float | None, ...], ...],
    metrics_by_node: dict[str, _NodeMetrics],
    selected_node_ids: tuple[str, ...],
    threshold: float,
    tolerance: float,
) -> None:
    if len(selected_node_ids) == 0:
        raise CladeDetectionComputationError(
            reason="clade_partition_empty",
            detail="Clade detection selected no clades.",
        )
    selected_set = set(selected_node_ids)
    if len(selected_set) != len(selected_node_ids):
        raise CladeDetectionComputationError(
            reason="clade_partition_duplicate_node",
            detail="Clade detection selected duplicate source nodes.",
        )

    coverage: set[int] = set()
    for node_id in selected_node_ids:
        metric = metrics_by_node[node_id]
        parent_id = metric.parent_id
        if parent_id is not None and metrics_by_node[parent_id].eligible:
            raise CladeDetectionComputationError(
                reason="clade_partition_not_maximal",
                detail="A selected clade has an eligible ancestor and is not maximal.",
            )
        node_indexes = set(metric.sequence_indices)
        if coverage.intersection(node_indexes):
            raise CladeDetectionComputationError(
                reason="clade_partition_overlap",
                detail="Selected inferred clades overlap in sequence-index membership.",
            )
        coverage.update(node_indexes)
        if metric.max_pairwise_distance > threshold + tolerance:
            raise CladeDetectionComputationError(
                reason="clade_threshold_violation",
                detail="Selected inferred clade exceeds max_within_clade_distance threshold.",
            )
        recomputed_maximum = _max_pairwise_distance(metric.sequence_indices, matrix)
        if not math.isclose(
            metric.max_pairwise_distance,
            recomputed_maximum,
            rel_tol=0.0,
            abs_tol=tolerance,
        ):
            raise CladeDetectionComputationError(
                reason="clade_max_distance_mismatch",
                detail="Selected inferred clade has inconsistent maximum pairwise distance.",
            )

    expected_coverage = set(range(len(tree.leaf_by_sequence_index)))
    if coverage != expected_coverage:
        raise CladeDetectionComputationError(
            reason="clade_partition_incomplete",
            detail="Selected inferred clades do not fully cover canonical leaf indexes.",
        )


def _build_inferred_clades(
    *,
    selected_node_ids: tuple[str, ...],
    tree: _TreeStructure,
    distance_matrix_result: DistanceMatrixResult,
    metrics_by_node: dict[str, _NodeMetrics],
    threshold: float,
) -> tuple[InferredClade, ...]:
    unordered: list[tuple[int, tuple[int, ...], str, InferredClade]] = []
    seen_clade_ids: set[str] = set()
    for node_id in selected_node_ids:
        metric = metrics_by_node[node_id]
        sequence_indices = metric.sequence_indices
        members: list[InferredCladeMember] = []
        logical_sample_ids: list[str] = []
        sequence_ids: list[str] = []
        leaf_labels: list[str] = []
        for sequence_index in sequence_indices:
            leaf = tree.leaf_by_sequence_index[sequence_index]
            reference = distance_matrix_result.sequence_references[sequence_index]
            if reference.sequence_id != leaf.sequence_id:
                raise CladeDetectionComputationError(
                    reason="leaf_sequence_reference_mismatch",
                    detail="Tree leaf sequence metadata is inconsistent with matrix references.",
                )
            if reference.logical_sample_ids != leaf.logical_sample_ids:
                raise CladeDetectionComputationError(
                    reason="leaf_logical_mapping_mismatch",
                    detail="Tree leaf logical-sample metadata is inconsistent with matrix references.",
                )
            members.append(
                InferredCladeMember(
                    leaf_label=leaf.leaf_label,
                    sequence_index=sequence_index,
                    sequence_id=reference.sequence_id,
                    logical_sample_ids=reference.logical_sample_ids,
                )
            )
            leaf_labels.append(leaf.leaf_label)
            sequence_ids.append(reference.sequence_id)
            logical_sample_ids.extend(reference.logical_sample_ids)

        clade_id = stable_clade_id(sequence_indices)
        if clade_id in seen_clade_ids:
            raise CladeDetectionComputationError(
                reason="clade_id_collision",
                detail="Deterministic clade IDs are not unique for selected memberships.",
            )
        seen_clade_ids.add(clade_id)

        inferred = InferredClade(
            clade_id=clade_id,
            ordinal=1,
            source_node_id=node_id,
            leaf_count=len(sequence_indices),
            logical_sample_count=len(logical_sample_ids),
            is_singleton=len(sequence_indices) == 1,
            leaf_labels=tuple(leaf_labels),
            sequence_indices=sequence_indices,
            sequence_ids=tuple(sequence_ids),
            logical_sample_ids=tuple(logical_sample_ids),
            members=tuple(members),
            max_pairwise_distance=metric.max_pairwise_distance,
            max_within_clade_distance=threshold,
            within_threshold=True,
        )
        unordered.append((sequence_indices[0], sequence_indices, node_id, inferred))

    ordered_models = [
        item[3]
        for item in sorted(
            unordered,
            key=lambda value: (value[0], value[1], value[2]),
        )
    ]
    return tuple(
        model.model_copy(update={"ordinal": index})
        for index, model in enumerate(ordered_models, start=1)
    )


def _build_assignment_records(
    clades: tuple[InferredClade, ...],
) -> tuple[CladeAssignmentRecord, ...]:
    records: list[CladeAssignmentRecord] = []
    for clade in clades:
        for member in clade.members:
            records.append(
                CladeAssignmentRecord(
                    clade_ordinal=clade.ordinal,
                    clade_id=clade.clade_id,
                    leaf_label=member.leaf_label,
                    sequence_index=member.sequence_index,
                    sequence_id=member.sequence_id,
                    logical_sample_ids=member.logical_sample_ids,
                    clade_leaf_count=clade.leaf_count,
                    clade_max_pairwise_distance=clade.max_pairwise_distance,
                    max_within_clade_distance=clade.max_within_clade_distance,
                )
            )
    return tuple(records)


def _max_pairwise_distance(
    sequence_indices: tuple[int, ...],
    matrix: tuple[tuple[float | None, ...], ...],
) -> float:
    if len(sequence_indices) <= 1:
        return 0.0
    maximum = 0.0
    for left_position, left_index in enumerate(sequence_indices):
        for right_index in sequence_indices[left_position + 1 :]:
            value = matrix[left_index][right_index]
            if value is None:
                raise CladeDetectionComputationError(
                    reason="distance_matrix_incomplete",
                    detail="Distance matrix contains undefined pair distances.",
                )
            numeric = float(value)
            if numeric > maximum:
                maximum = numeric
    return maximum


def _validate_threshold(*, value: object, reason: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CladeDetectionComputationError(
            reason=reason,
            detail="Clade detection threshold must be a finite number in [0, 1].",
        )
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0 or numeric > 1.0:
        raise CladeDetectionComputationError(
            reason=reason,
            detail="Clade detection threshold must be a finite number in [0, 1].",
        )


__all__ = [
    "CLADE_DISTANCE_TOLERANCE",
    "CladeDetectionComputation",
    "CladeDetectionComputationError",
    "NodeProgressCallback",
    "detect_inferred_clades",
    "validate_published_inferred_clades",
]
