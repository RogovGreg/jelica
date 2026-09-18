export type ResultOverviewSample = Readonly<{
  sample_id: string;
  sequence_id: string | null;
  original_record_id: string | null;
  original_description: string | null;
  source_reference: string;
  validation_status: string;
  eligible_for_analysis: boolean;
  source_length: number | null;
  gc_content: number | null;
  ambiguous_count: number | null;
}>;

export type ResultInputOverview = Readonly<{
  valid_sample_count: number;
  invalid_sample_count: number;
  unique_sequence_count: number;
  duplicate_logical_sample_count: number;
  samples: readonly ResultOverviewSample[];
}>;

export type ResultAlignmentOverview = Readonly<{
  alignment_length: number | null;
  logical_sample_count: number;
  unique_sequence_count: number;
  mode: string;
  engine: string | null;
}>;

export type ResultSequenceReference = Readonly<{
  index: number;
  sequence_id: string;
  logical_sample_ids: readonly string[];
}>;

export type ResultDistancePair = Readonly<{
  schema_version: number;
  left_sequence_id: string;
  right_sequence_id: string;
  left_index: number;
  right_index: number;
  mismatch_count: number;
  comparable_site_count: number;
  excluded_gap_site_count: number;
  excluded_ambiguous_site_count: number;
  distance: number | null;
  state: string;
}>;

export type ResultDistanceOverview = Readonly<{
  model: string;
  expected_pair_count: number;
  processed_pair_count: number;
  defined_distance_count: number;
  undefined_distance_count: number;
  sequence_references: readonly ResultSequenceReference[];
  matrix: readonly (readonly (number | null)[])[];
  pairs: readonly ResultDistancePair[];
}>;

export type ResultTreeNode = Readonly<{
  node_id: string;
  kind: string;
  leaf_label: string | null;
  sequence_index: number | null;
  sequence_id: string | null;
  logical_sample_ids: readonly string[];
}>;

export type ResultTreeEdge = Readonly<{
  parent_id: string;
  child_id: string;
  branch_length: number;
}>;

export type ResultRootedTree = Readonly<{
  rooted: boolean;
  root_id: string | null;
  traversal_root_id: string;
  node_count: number;
  edge_count: number;
  nodes: readonly ResultTreeNode[];
  edges: readonly ResultTreeEdge[];
}>;

export type ResultTreeOverview = Readonly<{
  method: string;
  applied_rooting: string;
  canonical_leaf_order: readonly string[];
  leaf_mappings: readonly Readonly<{
    leaf_label: string;
    sequence_index: number;
    sequence_id: string;
    logical_sample_ids: readonly string[];
  }>[];
  rooted: ResultRootedTree;
}>;

export type TaskResultOverview = Readonly<{
  task_id: string;
  content_id: string;
  input_processing: ResultInputOverview | null;
  alignment: ResultAlignmentOverview | null;
  distance_matrix: ResultDistanceOverview | null;
  phylogenetic_tree: ResultTreeOverview | null;
}>;

export type ResultSampleViewModel = Readonly<{
  sampleId: string;
  sequenceId: string | null;
  displayName: string;
  description: string | null;
  sourceReference: string;
  validationStatus: string;
  eligibleForAnalysis: boolean;
  sourceLength: number | null;
  gcContent: number | null;
  ambiguousCount: number | null;
}>;

export type HeatmapCellViewModel = Readonly<{
  rowIndex: number;
  columnIndex: number;
  distance: number | null;
  intensity: number | null;
  pair: ResultDistancePair | null;
}>;

export type TreeLayoutNode = Readonly<{
  id: string;
  kind: string;
  x: number;
  y: number;
  label: string | null;
  description: string | null;
}>;

export type TreeLayoutEdge = Readonly<{
  parentId: string;
  childId: string;
  parentX: number;
  parentY: number;
  childX: number;
  childY: number;
  branchLength: number;
}>;

export type PhylogeneticTreeLayout = Readonly<{
  width: number;
  height: number;
  nodes: readonly TreeLayoutNode[];
  edges: readonly TreeLayoutEdge[];
  scaleBarLength: number | null;
  scaleBarPixels: number;
}>;

export function resolveResultSampleName(sample: ResultOverviewSample): string {
  const recordId = sample.original_record_id?.trim();
  if (recordId) return recordId;
  const source = sample.source_reference.trim();
  if (source) return source.replace(/\.(?:fa|fasta|fna|fas)$/i, "");
  return sample.sample_id;
}

export function buildResultSampleViewModels(
  input: ResultInputOverview | null,
): readonly ResultSampleViewModel[] {
  return (input?.samples ?? []).map((sample) => ({
    sampleId: sample.sample_id,
    sequenceId: sample.sequence_id,
    displayName: resolveResultSampleName(sample),
    description: sample.original_description,
    sourceReference: sample.source_reference,
    validationStatus: sample.validation_status,
    eligibleForAnalysis: sample.eligible_for_analysis,
    sourceLength: sample.source_length,
    gcContent: sample.gc_content,
    ambiguousCount: sample.ambiguous_count,
  }));
}

export function resultLabelsBySequence(
  distance: ResultDistanceOverview,
  samples: readonly ResultSampleViewModel[],
): readonly string[] {
  const bySampleId = new Map(samples.map((sample) => [sample.sampleId, sample.displayName]));
  return distance.sequence_references.map((reference) => {
    const names = reference.logical_sample_ids
      .map((sampleId) => bySampleId.get(sampleId))
      .filter((name): name is string => Boolean(name));
    return names.length > 0 ? names.join(", ") : abbreviatedSequenceId(reference.sequence_id);
  });
}

export function buildHeatmapCells(
  distance: ResultDistanceOverview,
): readonly HeatmapCellViewModel[] {
  const pairByIndexes = new Map<string, ResultDistancePair>();
  for (const pair of distance.pairs) {
    pairByIndexes.set(pairIndexKey(pair.left_index, pair.right_index), pair);
  }
  const finiteDistances = distance.matrix
    .flatMap((row) => row)
    .filter((value): value is number => value !== null && Number.isFinite(value));
  const maximum = finiteDistances.length > 0 ? Math.max(...finiteDistances) : 0;
  return distance.matrix.flatMap((row, rowIndex) =>
    row.map((value, columnIndex) => ({
      rowIndex,
      columnIndex,
      distance: value,
      intensity: value === null ? null : maximum > 0 ? value / maximum : 0,
      pair:
        rowIndex === columnIndex
          ? null
          : pairByIndexes.get(pairIndexKey(rowIndex, columnIndex)) ?? null,
    })),
  );
}

export function layoutPhylogeneticTree(
  tree: ResultTreeOverview,
  samples: readonly ResultSampleViewModel[],
): PhylogeneticTreeLayout {
  const width = 900;
  const left = 24;
  const plotWidth = 590;
  const top = 24;
  const rowHeight = 52;
  const bottom = 52;
  const nodesById = new Map(tree.rooted.nodes.map((node) => [node.node_id, node]));
  const childrenById = new Map<string, ResultTreeEdge[]>();
  for (const edge of tree.rooted.edges) {
    const children = childrenById.get(edge.parent_id) ?? [];
    children.push(edge);
    childrenById.set(edge.parent_id, children);
  }
  const leafIds = tree.canonical_leaf_order.filter((id) => nodesById.has(id));
  const yById = new Map<string, number>(
    leafIds.map((id, index) => [id, top + index * rowHeight]),
  );
  const distanceById = new Map<string, number>([[tree.rooted.traversal_root_id, 0]]);
  const depthById = new Map<string, number>([[tree.rooted.traversal_root_id, 0]]);

  const visitDistances = (nodeId: string): void => {
    const parentDistance = distanceById.get(nodeId) ?? 0;
    const parentDepth = depthById.get(nodeId) ?? 0;
    for (const edge of childrenById.get(nodeId) ?? []) {
      distanceById.set(edge.child_id, parentDistance + Math.max(0, edge.branch_length));
      depthById.set(edge.child_id, parentDepth + 1);
      visitDistances(edge.child_id);
    }
  };
  visitDistances(tree.rooted.traversal_root_id);

  const resolveY = (nodeId: string): number => {
    const existing = yById.get(nodeId);
    if (existing !== undefined) return existing;
    const childYs = (childrenById.get(nodeId) ?? []).map((edge) => resolveY(edge.child_id));
    const resolved =
      childYs.length > 0
        ? childYs.reduce((sum, value) => sum + value, 0) / childYs.length
        : top + leafIds.length * rowHeight;
    yById.set(nodeId, resolved);
    return resolved;
  };
  resolveY(tree.rooted.traversal_root_id);

  const maximumDistance = Math.max(0, ...distanceById.values());
  const maximumDepth = Math.max(1, ...depthById.values());
  const xFor = (nodeId: string): number => {
    if (maximumDistance > 0) {
      return left + ((distanceById.get(nodeId) ?? 0) / maximumDistance) * plotWidth;
    }
    return left + ((depthById.get(nodeId) ?? 0) / maximumDepth) * plotWidth;
  };
  const samplesById = new Map(samples.map((sample) => [sample.sampleId, sample]));
  const layoutNodes = tree.rooted.nodes.map((node) => {
    const nodeSamples = node.logical_sample_ids
      .map((sampleId) => samplesById.get(sampleId))
      .filter((candidate): candidate is ResultSampleViewModel => Boolean(candidate));
    return {
      id: node.node_id,
      kind: node.kind,
      x: xFor(node.node_id),
      y: resolveY(node.node_id),
      label:
        nodeSamples.length > 0
          ? nodeSamples.map((sample) => sample.displayName).join(", ")
          : node.kind === "leaf"
            ? node.leaf_label
            : null,
      description:
        nodeSamples
          .map((sample) => sample.description)
          .filter((description): description is string => Boolean(description))
          .join("\n") || null,
    };
  });
  const positionedById = new Map(layoutNodes.map((node) => [node.id, node]));
  const layoutEdges = tree.rooted.edges.flatMap((edge) => {
    const parent = positionedById.get(edge.parent_id);
    const child = positionedById.get(edge.child_id);
    return parent && child
      ? [{
          parentId: edge.parent_id,
          childId: edge.child_id,
          parentX: parent.x,
          parentY: parent.y,
          childX: child.x,
          childY: child.y,
          branchLength: edge.branch_length,
        }]
      : [];
  });
  const scaleBarLength = maximumDistance > 0 ? niceScaleLength(maximumDistance / 4) : null;

  return {
    width,
    height: Math.max(160, top + Math.max(1, leafIds.length - 1) * rowHeight + bottom),
    nodes: layoutNodes,
    edges: layoutEdges,
    scaleBarLength,
    scaleBarPixels: scaleBarLength === null ? 0 : (scaleBarLength / maximumDistance) * plotWidth,
  };
}

function pairIndexKey(left: number, right: number): string {
  return left < right ? `${left}:${right}` : `${right}:${left}`;
}

function abbreviatedSequenceId(sequenceId: string): string {
  const digest = sequenceId.includes(":") ? sequenceId.split(":").at(-1) ?? sequenceId : sequenceId;
  return digest.length > 12 ? `${digest.slice(0, 12)}…` : digest;
}

function niceScaleLength(value: number): number {
  if (!(value > 0) || !Number.isFinite(value)) return 0;
  const exponent = Math.floor(Math.log10(value));
  const magnitude = 10 ** exponent;
  const normalized = value / magnitude;
  const step = normalized >= 5 ? 5 : normalized >= 2 ? 2 : 1;
  return step * magnitude;
}
