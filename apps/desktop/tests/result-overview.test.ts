import assert from "node:assert/strict";
import test from "node:test";

import {
  buildHeatmapCells,
  layoutPhylogeneticTree,
  resolveResultSampleName,
  type ResultDistanceOverview,
  type ResultOverviewSample,
  type ResultTreeOverview,
} from "../../../packages/app-platform/src/result-overview";

const sample = (overrides: Partial<ResultOverviewSample> = {}): ResultOverviewSample => ({
  sample_id: "sample-1",
  sequence_id: "sha256:one",
  original_record_id: "record-one",
  original_description: "Sample one",
  source_reference: "sample-one.fasta",
  validation_status: "valid",
  eligible_for_analysis: true,
  source_length: 100,
  gc_content: 0.5,
  ambiguous_count: 0,
  ...overrides,
});

test("result sample names prefer record ID and fall back to source then sample ID", () => {
  assert.equal(resolveResultSampleName(sample()), "record-one");
  assert.equal(
    resolveResultSampleName(sample({ original_record_id: null })),
    "sample-one",
  );
  assert.equal(
    resolveResultSampleName(
      sample({ original_record_id: null, source_reference: "", sample_id: "sample-fallback" }),
    ),
    "sample-fallback",
  );
});

test("heatmap cells preserve null distances and attach symmetric pair details", () => {
  const distance: ResultDistanceOverview = {
    model: "p_distance",
    expected_pair_count: 1,
    processed_pair_count: 1,
    defined_distance_count: 0,
    undefined_distance_count: 1,
    sequence_references: [
      { index: 0, sequence_id: "seq-a", logical_sample_ids: ["a"] },
      { index: 1, sequence_id: "seq-b", logical_sample_ids: ["b"] },
    ],
    matrix: [[0, null], [null, 0]],
    pairs: [{
      schema_version: 1,
      left_sequence_id: "seq-a",
      right_sequence_id: "seq-b",
      left_index: 0,
      right_index: 1,
      mismatch_count: 0,
      comparable_site_count: 0,
      excluded_gap_site_count: 10,
      excluded_ambiguous_site_count: 90,
      distance: null,
      state: "undefined_no_comparable_sites",
    }],
  };

  const cells = buildHeatmapCells(distance);
  assert.equal(cells.length, 4);
  const upper = cells[1];
  const lower = cells[2];
  assert.ok(upper && lower);
  assert.equal(upper.distance, null);
  assert.equal(upper.intensity, null);
  assert.equal(upper.pair?.state, "undefined_no_comparable_sites");
  assert.equal(lower.pair?.state, "undefined_no_comparable_sites");
});

test("tree layout accumulates branch lengths and averages child positions", () => {
  const tree: ResultTreeOverview = {
    method: "neighbor_joining",
    applied_rooting: "midpoint",
    canonical_leaf_order: ["leaf-a", "leaf-b"],
    leaf_mappings: [],
    rooted: {
      rooted: true,
      root_id: "root",
      traversal_root_id: "root",
      node_count: 3,
      edge_count: 2,
      nodes: [
        { node_id: "root", kind: "root", leaf_label: null, sequence_index: null, sequence_id: null, logical_sample_ids: [] },
        { node_id: "leaf-a", kind: "leaf", leaf_label: "leaf-a", sequence_index: 0, sequence_id: "seq-a", logical_sample_ids: ["a"] },
        { node_id: "leaf-b", kind: "leaf", leaf_label: "leaf-b", sequence_index: 1, sequence_id: "seq-b", logical_sample_ids: ["b"] },
      ],
      edges: [
        { parent_id: "root", child_id: "leaf-a", branch_length: 0.1 },
        { parent_id: "root", child_id: "leaf-b", branch_length: 0.2 },
      ],
    },
  };
  const layout = layoutPhylogeneticTree(tree, [
    { sampleId: "a", sequenceId: "seq-a", displayName: "A", description: null, sourceReference: "a.fa", validationStatus: "valid", eligibleForAnalysis: true, sourceLength: 10, gcContent: 0.5, ambiguousCount: 0 },
    { sampleId: "b", sequenceId: "seq-b", displayName: "B", description: null, sourceReference: "b.fa", validationStatus: "valid", eligibleForAnalysis: true, sourceLength: 10, gcContent: 0.5, ambiguousCount: 0 },
  ]);

  const root = layout.nodes.find((node) => node.id === "root");
  const leafA = layout.nodes.find((node) => node.id === "leaf-a");
  const leafB = layout.nodes.find((node) => node.id === "leaf-b");
  assert.ok(root && leafA && leafB);
  assert.equal(root.y, (leafA.y + leafB.y) / 2);
  assert.ok(leafB.x > leafA.x);
  assert.equal(layout.edges.length, 2);
  assert.ok(layout.scaleBarLength !== null);
});
