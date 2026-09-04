from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass
from io import StringIO

from Bio import Phylo
from Bio.Phylo.BaseTree import Clade, Tree
from Bio.Phylo.TreeConstruction import DistanceMatrix as BioDistanceMatrix
from Bio.Phylo.TreeConstruction import DistanceTreeConstructor

from jelica_core.config.models import (
    AnalysisDistanceMatrixModel,
    AnalysisPhylogeneticTreeMethod,
    AnalysisPhylogeneticTreeRooting,
)
from jelica_core.distance_matrix import DistanceMatrixResult

from .artifacts import (
    NEGATIVE_BRANCH_POLICY_CLAMP_TO_ZERO,
    PHYLOGENETIC_TREE_RESULT_SCHEMA_VERSION,
    TREE_DIAGNOSTICS_RELATIVE_PATH,
    ZERO_DIAMETER_ROOTING_FALLBACK,
    PhylogeneticTreeConstructionMode,
    PhylogeneticTreeDiagnostics,
    PhylogeneticTreeEdge,
    PhylogeneticTreeLeafMapping,
    PhylogeneticTreeNode,
    PhylogeneticTreeRepresentation,
    PhylogeneticTreeResult,
    PhylogeneticTreeWarning,
    TreeNodeKind,
)

_MATRIX_TOLERANCE = 1e-12
_DISTANCE_TOLERANCE = 1e-9


class PhylogeneticTreeComputationError(RuntimeError):
    def __init__(self, *, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class PhylogeneticTreeComputation:
    result: PhylogeneticTreeResult
    diagnostics: PhylogeneticTreeDiagnostics
    unrooted_newick: str
    rooted_newick: str


@dataclass(frozen=True, slots=True)
class _MatrixStatistics:
    minimum: float
    maximum: float
    zero_distance_pair_count: int
    zero_diameter: bool


def build_phylogenetic_tree(
    *,
    distance_matrix_result: DistanceMatrixResult,
    method: AnalysisPhylogeneticTreeMethod,
    rooting: AnalysisPhylogeneticTreeRooting,
    input_snapshot_manifest_sha256: str,
) -> PhylogeneticTreeComputation:
    if method is not AnalysisPhylogeneticTreeMethod.NEIGHBOR_JOINING:
        raise PhylogeneticTreeComputationError(
            reason="phylogenetic_method_unsupported",
            detail="Only neighbor_joining is supported in the current phylogenetic stage.",
        )
    if rooting is not AnalysisPhylogeneticTreeRooting.MIDPOINT:
        raise PhylogeneticTreeComputationError(
            reason="phylogenetic_rooting_unsupported",
            detail="Only midpoint rooting is supported in the current phylogenetic stage.",
        )
    if distance_matrix_result.model is not AnalysisDistanceMatrixModel.P_DISTANCE:
        raise PhylogeneticTreeComputationError(
            reason="distance_matrix_model_unsupported",
            detail="Phylogenetic tree requires p_distance matrix inputs.",
        )

    matrix, matrix_stats = _validate_complete_distance_matrix(distance_matrix_result)
    leaf_mappings = _build_leaf_mappings(distance_matrix_result)
    leaf_order = tuple(mapping.leaf_label for mapping in leaf_mappings)
    sequence_count = len(leaf_mappings)
    if sequence_count < 1:
        raise PhylogeneticTreeComputationError(
            reason="distance_matrix_empty",
            detail="Phylogenetic tree requires at least one unique aligned sequence.",
        )
    leaf_mapping_by_label = {mapping.leaf_label: mapping for mapping in leaf_mappings}
    warnings: list[PhylogeneticTreeWarning] = []

    if sequence_count == 1:
        construction_mode = PhylogeneticTreeConstructionMode.TRIVIAL_SINGLETON
        inference_performed = False
        applied_rooting = rooting.value
        unrooted = _build_singleton_unrooted_representation(leaf_mappings[0])
        rooted = _build_singleton_rooted_representation(leaf_mappings[0])
        raw_negative_branch_count = 0
        normalized_negative_branch_count = 0
        minimum_raw_branch_length: float | None = None
    elif sequence_count == 2:
        construction_mode = PhylogeneticTreeConstructionMode.TRIVIAL_PAIR
        inference_performed = False
        applied_rooting = rooting.value
        unrooted, rooted = _build_trivial_pair_representations(
            left=leaf_mappings[0],
            right=leaf_mappings[1],
            pair_distance=matrix[0][1],
        )
        raw_negative_branch_count = 0
        normalized_negative_branch_count = 0
        minimum_raw_branch_length = min(edge.branch_length for edge in unrooted.edges)
    else:
        construction_mode = PhylogeneticTreeConstructionMode.NEIGHBOR_JOINING
        inference_performed = True
        unrooted_tree = _build_neighbor_joining_tree(
            matrix=matrix,
            leaf_labels=leaf_order,
        )
        unrooted = _build_representation_from_biopython_tree(
            tree=unrooted_tree,
            rooted=False,
            internal_prefix="u_node_",
            leaf_mapping_by_label=leaf_mapping_by_label,
            allow_negative_branch_lengths=True,
        )
        raw_branch_lengths = tuple(edge.branch_length for edge in unrooted.edges)
        raw_negative_branch_count = sum(
            1 for branch_length in raw_branch_lengths if branch_length < -_MATRIX_TOLERANCE
        )
        minimum_raw_branch_length = min(raw_branch_lengths) if raw_branch_lengths else None
        if raw_negative_branch_count > 0:
            warnings.append(
                PhylogeneticTreeWarning(
                    code="raw_negative_branch_lengths_detected",
                    detail=(
                        "Neighbor-joining produced negative branch lengths in the unrooted "
                        "raw result."
                    ),
                )
            )

        rooted_tree = copy.deepcopy(unrooted_tree)
        normalized_negative_branch_count = _clamp_negative_branch_lengths(rooted_tree)
        if normalized_negative_branch_count > 0:
            warnings.append(
                PhylogeneticTreeWarning(
                    code="negative_branch_lengths_normalized_for_rooting",
                    detail=(
                        "Negative branch lengths were clamped to zero before midpoint rooting."
                    ),
                )
            )
        applied_rooting = _apply_rooting(
            tree=rooted_tree,
            requested_rooting=rooting,
            zero_diameter=matrix_stats.zero_diameter,
            fallback_leaf_label=leaf_order[0],
        )
        if matrix_stats.zero_diameter:
            warnings.append(
                PhylogeneticTreeWarning(
                    code="zero_diameter_distance_matrix",
                    detail=(
                        "Input distance matrix has zero diameter; deterministic rooting fallback "
                        "was applied."
                        if applied_rooting == ZERO_DIAMETER_ROOTING_FALLBACK
                        else "Input distance matrix has zero diameter."
                    ),
                )
            )
        rooted = _build_representation_from_biopython_tree(
            tree=rooted_tree,
            rooted=True,
            internal_prefix="node_",
            leaf_mapping_by_label=leaf_mapping_by_label,
            allow_negative_branch_lengths=False,
        )

    unrooted_newick = serialize_tree_newick(unrooted)
    rooted_newick = serialize_tree_newick(rooted)
    _validate_newick_roundtrip(
        newick=unrooted_newick,
        tree=unrooted,
        artifact_path_label="tree_unrooted.nwk",
    )
    _validate_newick_roundtrip(
        newick=rooted_newick,
        tree=rooted,
        artifact_path_label="tree_rooted.nwk",
    )

    rooted_pairwise_distances = _pairwise_leaf_distances(rooted)
    diagnostics = PhylogeneticTreeDiagnostics(
        leaf_count=sequence_count,
        internal_node_count=sum(
            1 for node in rooted.nodes if node.kind is not TreeNodeKind.LEAF
        ),
        edge_count=rooted.edge_count,
        input_matrix_dimensions=(sequence_count, sequence_count),
        input_distance_min=matrix_stats.minimum,
        input_distance_max=matrix_stats.maximum,
        tree_diameter=max(rooted_pairwise_distances.values(), default=0.0),
        zero_distance_pair_count=matrix_stats.zero_distance_pair_count,
        zero_diameter=matrix_stats.zero_diameter,
        raw_negative_branch_count=raw_negative_branch_count,
        minimum_raw_branch_length=minimum_raw_branch_length,
        normalized_negative_branch_count=normalized_negative_branch_count,
        requested_rooting=rooting,
        applied_rooting=applied_rooting,
        construction_mode=construction_mode,
        warnings=tuple(warnings),
    )
    result = PhylogeneticTreeResult(
        schema_version=PHYLOGENETIC_TREE_RESULT_SCHEMA_VERSION,
        method=method,
        construction_mode=construction_mode,
        inference_performed=inference_performed,
        requested_rooting=rooting,
        applied_rooting=applied_rooting,
        negative_branch_policy=NEGATIVE_BRANCH_POLICY_CLAMP_TO_ZERO,
        input_distance_model=distance_matrix_result.model,
        input_snapshot_manifest_sha256=input_snapshot_manifest_sha256,
        canonical_leaf_order=leaf_order,
        leaf_mappings=leaf_mappings,
        unrooted=unrooted,
        rooted=rooted,
        raw_negative_branch_count=raw_negative_branch_count,
        minimum_raw_branch_length=minimum_raw_branch_length,
        normalized_negative_branch_count=normalized_negative_branch_count,
        zero_diameter=matrix_stats.zero_diameter,
        node_count=rooted.node_count,
        edge_count=rooted.edge_count,
    )
    return PhylogeneticTreeComputation(
        result=result,
        diagnostics=diagnostics,
        unrooted_newick=unrooted_newick,
        rooted_newick=rooted_newick,
    )


def _build_leaf_mappings(
    distance_matrix_result: DistanceMatrixResult,
) -> tuple[PhylogeneticTreeLeafMapping, ...]:
    mappings: list[PhylogeneticTreeLeafMapping] = []
    for index, reference in enumerate(distance_matrix_result.sequence_references):
        if reference.index != index:
            raise PhylogeneticTreeComputationError(
                reason="distance_matrix_sequence_order_invalid",
                detail="Distance-matrix sequence references are not in canonical index order.",
            )
        mappings.append(
            PhylogeneticTreeLeafMapping(
                leaf_label=f"leaf_{index + 1:06d}",
                sequence_index=index,
                sequence_id=reference.sequence_id,
                logical_sample_ids=reference.logical_sample_ids,
            )
        )
    return tuple(mappings)


def _validate_complete_distance_matrix(
    distance_matrix_result: DistanceMatrixResult,
) -> tuple[list[list[float]], _MatrixStatistics]:
    sequence_count = len(distance_matrix_result.sequence_references)
    matrix = distance_matrix_result.matrix
    if len(matrix) != sequence_count:
        raise PhylogeneticTreeComputationError(
            reason="distance_matrix_dimensions_invalid",
            detail="Distance-matrix row count does not match canonical sequence references.",
        )

    if distance_matrix_result.undefined_distance_count != 0:
        raise PhylogeneticTreeComputationError(
            reason="distance_matrix_incomplete",
            detail="Distance matrix contains undefined pair distances.",
        )

    numeric_matrix: list[list[float]] = []
    off_diagonal_values: list[float] = []
    zero_distance_pair_count = 0

    for row_index, row in enumerate(matrix):
        if len(row) != sequence_count:
            raise PhylogeneticTreeComputationError(
                reason="distance_matrix_dimensions_invalid",
                detail="Distance matrix must be square.",
            )
        numeric_row: list[float] = []
        for column_index, value in enumerate(row):
            if value is None:
                if row_index != column_index:
                    raise PhylogeneticTreeComputationError(
                        reason="distance_matrix_incomplete",
                        detail="Distance matrix contains undefined pair distances.",
                    )
                raise PhylogeneticTreeComputationError(
                    reason="distance_matrix_invalid",
                    detail="Distance-matrix diagonal must be explicitly zero.",
                )
            numeric_value = float(value)
            if not math.isfinite(numeric_value):
                raise PhylogeneticTreeComputationError(
                    reason="distance_matrix_invalid",
                    detail="Distance matrix contains a non-finite value.",
                )
            if numeric_value < -_MATRIX_TOLERANCE or numeric_value > 1.0 + _MATRIX_TOLERANCE:
                raise PhylogeneticTreeComputationError(
                    reason="distance_matrix_invalid",
                    detail="Distance matrix values must stay within [0, 1].",
                )
            if row_index == column_index:
                if not math.isclose(
                    numeric_value,
                    0.0,
                    rel_tol=0.0,
                    abs_tol=_MATRIX_TOLERANCE,
                ):
                    raise PhylogeneticTreeComputationError(
                        reason="distance_matrix_invalid",
                        detail="Distance-matrix diagonal must be zero.",
                    )
                numeric_value = 0.0
            numeric_row.append(numeric_value)
        numeric_matrix.append(numeric_row)

    for row_index in range(sequence_count):
        for column_index in range(row_index + 1, sequence_count):
            value = numeric_matrix[row_index][column_index]
            mirror = numeric_matrix[column_index][row_index]
            if not math.isclose(value, mirror, rel_tol=0.0, abs_tol=_MATRIX_TOLERANCE):
                raise PhylogeneticTreeComputationError(
                    reason="distance_matrix_invalid",
                    detail="Distance matrix must be symmetric.",
                )
            if math.isclose(value, 0.0, rel_tol=0.0, abs_tol=_MATRIX_TOLERANCE):
                zero_distance_pair_count += 1
            off_diagonal_values.append(value)

    if len(off_diagonal_values) == 0:
        minimum = 0.0
        maximum = 0.0
        zero_diameter = True
    else:
        minimum = min(off_diagonal_values)
        maximum = max(off_diagonal_values)
        zero_diameter = all(
            math.isclose(value, 0.0, rel_tol=0.0, abs_tol=_MATRIX_TOLERANCE)
            for value in off_diagonal_values
        )

    return (
        numeric_matrix,
        _MatrixStatistics(
            minimum=minimum,
            maximum=maximum,
            zero_distance_pair_count=zero_distance_pair_count,
            zero_diameter=zero_diameter,
        ),
    )


def _build_neighbor_joining_tree(
    *,
    matrix: list[list[float]],
    leaf_labels: tuple[str, ...],
) -> Tree:
    lower_triangle: list[list[float]] = []
    for row_index, row in enumerate(matrix):
        lower_triangle.append([row[column_index] for column_index in range(row_index)] + [0.0])
    try:
        distance_matrix = BioDistanceMatrix(
            names=list(leaf_labels),
            matrix=lower_triangle,
        )
        tree = DistanceTreeConstructor().nj(distance_matrix)
    except Exception as error:
        raise PhylogeneticTreeComputationError(
            reason="neighbor_joining_failed",
            detail="Neighbor-joining construction failed for the published distance matrix.",
        ) from error
    tree.rooted = False
    return tree


def _clamp_negative_branch_lengths(tree: Tree) -> int:
    normalized_count = 0
    for parent in tree.find_clades(order="preorder"):
        for child in parent.clades:
            length = _normalize_branch_length(child.branch_length)
            if length < -_MATRIX_TOLERANCE:
                child.branch_length = 0.0
                normalized_count += 1
            else:
                child.branch_length = 0.0 if abs(length) <= _MATRIX_TOLERANCE else length
    return normalized_count


def _apply_rooting(
    *,
    tree: Tree,
    requested_rooting: AnalysisPhylogeneticTreeRooting,
    zero_diameter: bool,
    fallback_leaf_label: str,
) -> str:
    if requested_rooting is not AnalysisPhylogeneticTreeRooting.MIDPOINT:
        raise PhylogeneticTreeComputationError(
            reason="phylogenetic_rooting_unsupported",
            detail="Only midpoint rooting is supported.",
        )
    if zero_diameter:
        outgroup = _find_terminal_by_name(tree, fallback_leaf_label)
        try:
            tree.root_with_outgroup(outgroup)
        except Exception as error:
            raise PhylogeneticTreeComputationError(
                reason="rooting_zero_diameter_failed",
                detail="Deterministic zero-diameter rooting fallback failed.",
            ) from error
        tree.rooted = True
        return ZERO_DIAMETER_ROOTING_FALLBACK
    try:
        tree.root_at_midpoint()
    except Exception as error:
        raise PhylogeneticTreeComputationError(
            reason="midpoint_rooting_failed",
            detail="Midpoint rooting failed for the normalized neighbor-joining tree.",
        ) from error
    tree.rooted = True
    return requested_rooting.value


def _find_terminal_by_name(tree: Tree, leaf_label: str) -> Clade:
    for clade in tree.get_terminals():
        if clade.name == leaf_label:
            return clade
    raise PhylogeneticTreeComputationError(
        reason="tree_leaf_missing",
        detail="Tree rooting fallback could not locate a canonical leaf label.",
    )


def _build_representation_from_biopython_tree(
    *,
    tree: Tree,
    rooted: bool,
    internal_prefix: str,
    leaf_mapping_by_label: dict[str, PhylogeneticTreeLeafMapping],
    allow_negative_branch_lengths: bool,
) -> PhylogeneticTreeRepresentation:
    leaf_index_by_label = {
        mapping.leaf_label: mapping.sequence_index for mapping in leaf_mapping_by_label.values()
    }
    descendant_cache: dict[int, tuple[int, ...]] = {}

    def descendant_indexes(clade: Clade) -> tuple[int, ...]:
        cache_key = id(clade)
        if cache_key in descendant_cache:
            return descendant_cache[cache_key]
        if clade.is_terminal():
            if clade.name is None or clade.name not in leaf_index_by_label:
                raise PhylogeneticTreeComputationError(
                    reason="tree_leaf_invalid",
                    detail="Tree contains a leaf outside canonical distance-matrix order.",
                )
            indexes = (leaf_index_by_label[clade.name],)
        else:
            accumulator: list[int] = []
            for child in clade.clades:
                accumulator.extend(descendant_indexes(child))
            if len(accumulator) == 0:
                raise PhylogeneticTreeComputationError(
                    reason="tree_internal_node_invalid",
                    detail="Tree contains an internal node without descendant leaves.",
                )
            indexes = tuple(sorted(accumulator))
        descendant_cache[cache_key] = indexes
        return indexes

    def ordered_children(clade: Clade) -> tuple[Clade, ...]:
        return tuple(
            sorted(
                clade.clades,
                key=lambda child: (
                    min(descendant_indexes(child)),
                    descendant_indexes(child),
                ),
            )
        )

    visited_node_ids: set[str] = set()
    nodes: list[PhylogeneticTreeNode] = []
    edges: list[PhylogeneticTreeEdge] = []

    def node_id_for_clade(clade: Clade, *, is_root: bool) -> str:
        if clade.is_terminal():
            if clade.name is None:
                raise PhylogeneticTreeComputationError(
                    reason="tree_leaf_invalid",
                    detail="Tree contains an unnamed leaf.",
                )
            return clade.name
        digest = _stable_digest(descendant_indexes(clade))
        if rooted and is_root:
            return f"root_{digest}"
        return f"{internal_prefix}{digest}"

    def walk(clade: Clade, *, is_root: bool) -> str:
        node_id = node_id_for_clade(clade, is_root=is_root)
        if node_id in visited_node_ids:
            raise PhylogeneticTreeComputationError(
                reason="tree_structure_invalid",
                detail="Tree node identifiers are not unique for canonical serialization.",
            )
        visited_node_ids.add(node_id)

        if clade.is_terminal():
            assert clade.name is not None
            mapping = leaf_mapping_by_label.get(clade.name)
            if mapping is None:
                raise PhylogeneticTreeComputationError(
                    reason="tree_leaf_invalid",
                    detail="Tree leaf mapping is inconsistent with canonical references.",
                )
            nodes.append(
                PhylogeneticTreeNode(
                    node_id=node_id,
                    kind=TreeNodeKind.LEAF,
                    leaf_label=mapping.leaf_label,
                    sequence_index=mapping.sequence_index,
                    sequence_id=mapping.sequence_id,
                    logical_sample_ids=mapping.logical_sample_ids,
                )
            )
            return node_id

        nodes.append(
            PhylogeneticTreeNode(
                node_id=node_id,
                kind=TreeNodeKind.ROOT if rooted and is_root else TreeNodeKind.INTERNAL,
            )
        )
        for child in ordered_children(clade):
            child_id = walk(child, is_root=False)
            branch_length = _normalize_branch_length(child.branch_length)
            if not allow_negative_branch_lengths and branch_length < -_MATRIX_TOLERANCE:
                raise PhylogeneticTreeComputationError(
                    reason="tree_branch_length_negative_after_normalization",
                    detail="Rooted tree still contains negative branch lengths after normalization.",
                )
            if abs(branch_length) <= _MATRIX_TOLERANCE:
                branch_length = 0.0
            edges.append(
                PhylogeneticTreeEdge(
                    parent_id=node_id,
                    child_id=child_id,
                    branch_length=branch_length,
                )
            )
        return node_id

    traversal_root_id = walk(tree.root, is_root=True)
    return PhylogeneticTreeRepresentation(
        rooted=rooted,
        root_id=traversal_root_id if rooted else None,
        traversal_root_id=traversal_root_id,
        node_count=len(nodes),
        edge_count=len(edges),
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


def _build_singleton_unrooted_representation(
    mapping: PhylogeneticTreeLeafMapping,
) -> PhylogeneticTreeRepresentation:
    return PhylogeneticTreeRepresentation(
        rooted=False,
        traversal_root_id=mapping.leaf_label,
        node_count=1,
        edge_count=0,
        nodes=(
            PhylogeneticTreeNode(
                node_id=mapping.leaf_label,
                kind=TreeNodeKind.LEAF,
                leaf_label=mapping.leaf_label,
                sequence_index=mapping.sequence_index,
                sequence_id=mapping.sequence_id,
                logical_sample_ids=mapping.logical_sample_ids,
            ),
        ),
        edges=tuple(),
    )


def _build_singleton_rooted_representation(
    mapping: PhylogeneticTreeLeafMapping,
) -> PhylogeneticTreeRepresentation:
    root_id = f"root_{_stable_digest((mapping.sequence_index,))}"
    return PhylogeneticTreeRepresentation(
        rooted=True,
        root_id=root_id,
        traversal_root_id=root_id,
        node_count=2,
        edge_count=1,
        nodes=(
            PhylogeneticTreeNode(node_id=root_id, kind=TreeNodeKind.ROOT),
            PhylogeneticTreeNode(
                node_id=mapping.leaf_label,
                kind=TreeNodeKind.LEAF,
                leaf_label=mapping.leaf_label,
                sequence_index=mapping.sequence_index,
                sequence_id=mapping.sequence_id,
                logical_sample_ids=mapping.logical_sample_ids,
            ),
        ),
        edges=(
            PhylogeneticTreeEdge(
                parent_id=root_id,
                child_id=mapping.leaf_label,
                branch_length=0.0,
            ),
        ),
    )


def _build_trivial_pair_representations(
    *,
    left: PhylogeneticTreeLeafMapping,
    right: PhylogeneticTreeLeafMapping,
    pair_distance: float,
) -> tuple[PhylogeneticTreeRepresentation, PhylogeneticTreeRepresentation]:
    branch_length = 0.0 if abs(pair_distance) <= _MATRIX_TOLERANCE else pair_distance / 2.0
    unrooted_root_id = f"u_node_{_stable_digest((left.sequence_index, right.sequence_index))}"
    rooted_root_id = f"root_{_stable_digest((left.sequence_index, right.sequence_index))}"
    leaf_nodes = (
        PhylogeneticTreeNode(
            node_id=left.leaf_label,
            kind=TreeNodeKind.LEAF,
            leaf_label=left.leaf_label,
            sequence_index=left.sequence_index,
            sequence_id=left.sequence_id,
            logical_sample_ids=left.logical_sample_ids,
        ),
        PhylogeneticTreeNode(
            node_id=right.leaf_label,
            kind=TreeNodeKind.LEAF,
            leaf_label=right.leaf_label,
            sequence_index=right.sequence_index,
            sequence_id=right.sequence_id,
            logical_sample_ids=right.logical_sample_ids,
        ),
    )
    unrooted = PhylogeneticTreeRepresentation(
        rooted=False,
        traversal_root_id=unrooted_root_id,
        node_count=3,
        edge_count=2,
        nodes=(
            PhylogeneticTreeNode(node_id=unrooted_root_id, kind=TreeNodeKind.INTERNAL),
            *leaf_nodes,
        ),
        edges=(
            PhylogeneticTreeEdge(
                parent_id=unrooted_root_id,
                child_id=left.leaf_label,
                branch_length=branch_length,
            ),
            PhylogeneticTreeEdge(
                parent_id=unrooted_root_id,
                child_id=right.leaf_label,
                branch_length=branch_length,
            ),
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
        edges=(
            PhylogeneticTreeEdge(
                parent_id=rooted_root_id,
                child_id=left.leaf_label,
                branch_length=branch_length,
            ),
            PhylogeneticTreeEdge(
                parent_id=rooted_root_id,
                child_id=right.leaf_label,
                branch_length=branch_length,
            ),
        ),
    )
    return unrooted, rooted


def _stable_digest(values: tuple[int, ...]) -> str:
    payload = ",".join(str(value) for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _normalize_branch_length(raw_value: float | None) -> float:
    if raw_value is None:
        return 0.0
    value = float(raw_value)
    if not math.isfinite(value):
        raise PhylogeneticTreeComputationError(
            reason="tree_branch_length_invalid",
            detail="Tree contains a non-finite branch length.",
        )
    return value


def serialize_tree_newick(tree: PhylogeneticTreeRepresentation) -> str:
    nodes_by_id = {node.node_id: node for node in tree.nodes}
    children_by_parent: dict[str, list[str]] = {}
    branch_length_by_child: dict[str, float] = {}
    for edge in tree.edges:
        children_by_parent.setdefault(edge.parent_id, []).append(edge.child_id)
        branch_length_by_child[edge.child_id] = edge.branch_length

    min_descendant_cache: dict[str, int] = {}

    def min_descendant_index(node_id: str) -> int:
        if node_id in min_descendant_cache:
            return min_descendant_cache[node_id]
        node = nodes_by_id[node_id]
        if node.kind is TreeNodeKind.LEAF:
            assert node.sequence_index is not None
            min_descendant_cache[node_id] = node.sequence_index
            return node.sequence_index
        children = children_by_parent.get(node_id, [])
        if len(children) == 0:
            raise PhylogeneticTreeComputationError(
                reason="tree_structure_invalid",
                detail="Internal tree node has no children.",
            )
        value = min(min_descendant_index(child_id) for child_id in children)
        min_descendant_cache[node_id] = value
        return value

    def render(node_id: str) -> str:
        node = nodes_by_id[node_id]
        if node.kind is TreeNodeKind.LEAF:
            assert node.leaf_label is not None
            return node.leaf_label
        children = sorted(
            children_by_parent.get(node_id, []),
            key=lambda child_id: (
                min_descendant_index(child_id),
                child_id,
            ),
        )
        parts = [
            f"{render(child_id)}:{_format_branch_length(branch_length_by_child[child_id])}"
            for child_id in children
        ]
        return f"({','.join(parts)})"

    return f"{render(tree.traversal_root_id)};"


def _format_branch_length(value: float) -> str:
    normalized = 0.0 if abs(value) <= _MATRIX_TOLERANCE else value
    text = f"{normalized:.12f}".rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        return "0"
    return text


def _pairwise_leaf_distances(tree: PhylogeneticTreeRepresentation) -> dict[tuple[str, str], float]:
    adjacency: dict[str, list[tuple[str, float]]] = {}
    nodes_by_id = {node.node_id: node for node in tree.nodes}
    leaf_ids_by_label: dict[str, str] = {}
    for node in tree.nodes:
        if node.kind is TreeNodeKind.LEAF:
            assert node.leaf_label is not None
            leaf_ids_by_label[node.leaf_label] = node.node_id
    for edge in tree.edges:
        adjacency.setdefault(edge.parent_id, []).append((edge.child_id, edge.branch_length))
        adjacency.setdefault(edge.child_id, []).append((edge.parent_id, edge.branch_length))

    ordered_leaf_labels = sorted(
        leaf_ids_by_label,
        key=lambda label: (
            nodes_by_id[leaf_ids_by_label[label]].sequence_index,
            label,
        ),
    )
    distances: dict[tuple[str, str], float] = {}
    for left_index, left_label in enumerate(ordered_leaf_labels):
        start_node_id = leaf_ids_by_label[left_label]
        distance_by_node = _distance_from_node(start_node_id, adjacency)
        for right_label in ordered_leaf_labels[left_index + 1 :]:
            target_node_id = leaf_ids_by_label[right_label]
            distance = distance_by_node.get(target_node_id)
            if distance is None:
                raise PhylogeneticTreeComputationError(
                    reason="tree_structure_invalid",
                    detail="Tree representation is disconnected.",
                )
            distances[(left_label, right_label)] = distance
    return distances


def _distance_from_node(
    start_node_id: str,
    adjacency: dict[str, list[tuple[str, float]]],
) -> dict[str, float]:
    distances = {start_node_id: 0.0}
    stack = [(start_node_id, None)]
    while stack:
        node_id, parent_id = stack.pop()
        base = distances[node_id]
        for neighbor_id, branch_length in adjacency.get(node_id, ()):
            if neighbor_id == parent_id:
                continue
            distance = base + branch_length
            distances[neighbor_id] = distance
            stack.append((neighbor_id, node_id))
    return distances


def _validate_newick_roundtrip(
    *,
    newick: str,
    tree: PhylogeneticTreeRepresentation,
    artifact_path_label: str,
) -> None:
    leaf_labels = tuple(
        node.leaf_label for node in tree.nodes if node.kind is TreeNodeKind.LEAF and node.leaf_label
    )
    expected_leaf_set = set(leaf_labels)
    try:
        parsed = Phylo.read(StringIO(newick), "newick")
    except Exception as error:
        raise PhylogeneticTreeComputationError(
            reason="newick_invalid",
            detail=f"Serialized {artifact_path_label} is not parseable as Newick.",
        ) from error
    observed_leaf_set = {
        terminal.name
        for terminal in parsed.get_terminals()
        if terminal.name is not None and terminal.name != ""
    }
    if observed_leaf_set != expected_leaf_set:
        raise PhylogeneticTreeComputationError(
            reason="newick_leaf_mismatch",
            detail=f"Serialized {artifact_path_label} leaf labels do not match canonical labels.",
        )
    expected_distances = _pairwise_leaf_distances(tree)
    for (left_label, right_label), expected_distance in expected_distances.items():
        observed_distance = float(parsed.distance(left_label, right_label))
        if not math.isclose(
            observed_distance,
            expected_distance,
            rel_tol=_DISTANCE_TOLERANCE,
            abs_tol=_DISTANCE_TOLERANCE,
        ):
            raise PhylogeneticTreeComputationError(
                reason="newick_distance_mismatch",
                detail=(
                    f"Serialized {artifact_path_label} does not preserve pairwise leaf path "
                    "distances."
                ),
            )


__all__ = [
    "TREE_DIAGNOSTICS_RELATIVE_PATH",
    "PhylogeneticTreeComputation",
    "PhylogeneticTreeComputationError",
    "build_phylogenetic_tree",
    "serialize_tree_newick",
]
