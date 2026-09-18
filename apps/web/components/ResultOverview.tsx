"use client";

import type { CSSProperties } from "react";

import {
  buildHeatmapCells,
  buildResultSampleViewModels,
  layoutPhylogeneticTree,
  resultLabelsBySequence,
  type ResultDistancePair,
} from "../../../packages/app-platform/src/result-overview";
import { useI18n } from "@/components/I18nProvider";
import type { TaskResultOverview } from "@/types/api";

type ResultOverviewProps = {
  overview: TaskResultOverview | null;
};

type HeatmapCellStyle = CSSProperties & {
  "--heat-intensity": string;
};

export function ResultOverview({ overview }: ResultOverviewProps) {
  const { t } = useI18n();

  if (!overview) {
    return (
      <section className="panel result-overview-unavailable">
        <h2>{t("result.overview.unavailable-title")}</h2>
        <p className="muted">{t("result.overview.unavailable-description")}</p>
      </section>
    );
  }

  const samples = buildResultSampleViewModels(overview.input_processing);
  const distance = overview.distance_matrix;
  const tree = overview.phylogenetic_tree;
  const heatmapCells = distance ? buildHeatmapCells(distance) : [];
  const distanceLabels = distance ? resultLabelsBySequence(distance, samples) : [];
  const treeLayout = tree ? layoutPhylogeneticTree(tree, samples) : null;
  const summary = [
    {
      label: t("result.overview.summary.sequences"),
      value:
        overview.alignment?.unique_sequence_count
        ?? overview.input_processing?.unique_sequence_count
        ?? null,
    },
    {
      label: t("result.overview.summary.alignment-length"),
      value: overview.alignment?.alignment_length ?? null,
    },
    {
      label: t("result.overview.summary.tree-method"),
      value: tree ? humanizeIdentifier(tree.method) : null,
    },
    {
      label: t("result.overview.summary.distance-model"),
      value: distance ? humanizeIdentifier(distance.model) : null,
    },
    {
      label: t("result.overview.summary.pairs"),
      value: distance?.processed_pair_count ?? null,
    },
  ];

  return (
    <div className="result-overview stack">
      <section aria-labelledby="result-summary-title">
        <div className="result-section-heading">
          <div>
            <h2 id="result-summary-title">{t("result.overview.title")}</h2>
            <p className="muted">{t("result.overview.description")}</p>
          </div>
        </div>
        <div className="result-summary-grid">
          {summary.map((item) => (
            <article className="panel result-summary-card" key={item.label}>
              <span>{item.label}</span>
              <strong>{item.value ?? t("result.overview.unavailable-value")}</strong>
            </article>
          ))}
        </div>
      </section>

      <section className="panel result-visualization" aria-labelledby="result-tree-title">
        <div className="result-section-heading">
          <div>
            <h2 id="result-tree-title">{t("result.overview.tree.title")}</h2>
            <p className="muted">
              {tree
                ? t("result.overview.tree.description", {
                    method: humanizeIdentifier(tree.method),
                    rooting: humanizeIdentifier(tree.applied_rooting),
                  })
                : t("result.overview.tree.unavailable")}
            </p>
          </div>
        </div>
        {tree && treeLayout ? (
          <div className="result-tree-scroll">
            <svg
              className="result-tree"
              viewBox={`0 0 ${treeLayout.width} ${treeLayout.height}`}
              role="img"
              aria-label={t("result.overview.tree.accessible-label")}
            >
              <title>{t("result.overview.tree.accessible-label")}</title>
              {treeLayout.edges.map((edge) => (
                <g key={`${edge.parentId}-${edge.childId}`}>
                  <title>
                    {t("result.overview.tree.branch-tooltip", {
                      length: formatDistance(edge.branchLength),
                    })}
                  </title>
                  <line
                    className="result-tree-branch"
                    x1={edge.parentX}
                    y1={edge.parentY}
                    x2={edge.parentX}
                    y2={edge.childY}
                  />
                  <line
                    className="result-tree-branch"
                    x1={edge.parentX}
                    y1={edge.childY}
                    x2={edge.childX}
                    y2={edge.childY}
                  />
                </g>
              ))}
              {treeLayout.nodes.map((node) => (
                <g key={node.id}>
                  <title>{node.description ?? node.label ?? node.id}</title>
                  <circle
                    className={`result-tree-node result-tree-node-${node.kind}`}
                    cx={node.x}
                    cy={node.y}
                    r={node.kind === "leaf" ? 4 : 3}
                  />
                  {node.label ? (
                    <text className="result-tree-label" x={node.x + 10} y={node.y + 4}>
                      {node.label}
                    </text>
                  ) : null}
                </g>
              ))}
              {treeLayout.scaleBarLength !== null ? (
                <g className="result-tree-scale">
                  <line
                    x1={24}
                    y1={treeLayout.height - 22}
                    x2={24 + treeLayout.scaleBarPixels}
                    y2={treeLayout.height - 22}
                  />
                  <text x={24} y={treeLayout.height - 7}>
                    {formatDistance(treeLayout.scaleBarLength)}
                  </text>
                </g>
              ) : null}
            </svg>
          </div>
        ) : (
          <UnavailableBlock />
        )}
      </section>

      <section className="panel result-visualization" aria-labelledby="result-heatmap-title">
        <div className="result-section-heading">
          <div>
            <h2 id="result-heatmap-title">{t("result.overview.heatmap.title")}</h2>
            <p className="muted">
              {distance
                ? t("result.overview.heatmap.description", {
                    model: humanizeIdentifier(distance.model),
                  })
                : t("result.overview.heatmap.unavailable")}
            </p>
          </div>
        </div>
        {distance ? (
          <div className="result-heatmap-scroll">
            <table className="result-heatmap">
              <caption className="sr-only">{t("result.overview.heatmap.accessible-label")}</caption>
              <thead>
                <tr>
                  <th scope="col" aria-label={t("result.overview.sample")} />
                  {distanceLabels.map((label, index) => (
                    <th scope="col" title={label} key={`${index}-${label}`}>
                      <span>{label}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {distanceLabels.map((rowLabel, rowIndex) => (
                  <tr key={`${rowIndex}-${rowLabel}`}>
                    <th scope="row" title={rowLabel}>{rowLabel}</th>
                    {heatmapCells
                      .filter((cell) => cell.rowIndex === rowIndex)
                      .map((cell) => {
                        const title = heatmapTooltip(
                          rowLabel,
                          distanceLabels[cell.columnIndex] ?? "",
                          cell.distance,
                          cell.pair,
                          t,
                        );
                        const style: HeatmapCellStyle = {
                          "--heat-intensity": `${(cell.intensity ?? 0) * 78}%`,
                        };
                        return (
                          <td
                            className={cell.distance === null ? "is-undefined" : ""}
                            style={style}
                            title={title}
                            aria-label={title}
                            tabIndex={0}
                            key={cell.columnIndex}
                          >
                            {cell.distance === null ? "—" : formatDistance(cell.distance)}
                          </td>
                        );
                      })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <UnavailableBlock />
        )}
      </section>

      <section className="panel result-samples" aria-labelledby="result-samples-title">
        <div className="result-section-heading">
          <div>
            <h2 id="result-samples-title">{t("result.overview.samples.title")}</h2>
            <p className="muted">{t("result.overview.samples.description")}</p>
          </div>
        </div>
        {overview.input_processing ? (
          <div className="table-scroll">
            <table className="task-table result-sample-table">
              <thead>
                <tr>
                  <th scope="col">{t("result.overview.sample")}</th>
                  <th scope="col">{t("result.overview.samples.length")}</th>
                  <th scope="col">{t("result.overview.samples.gc")}</th>
                  <th scope="col">{t("result.overview.samples.ambiguous")}</th>
                  <th scope="col">{t("result.overview.samples.status")}</th>
                </tr>
              </thead>
              <tbody>
                {samples.map((sample) => (
                  <tr key={sample.sampleId}>
                    <td title={sample.description ?? sample.sourceReference}>
                      <strong>{sample.displayName}</strong>
                      <span className="muted result-sample-source">{sample.sourceReference}</span>
                    </td>
                    <td>{sample.sourceLength?.toLocaleString() ?? "—"}</td>
                    <td>{sample.gcContent === null ? "—" : formatPercent(sample.gcContent)}</td>
                    <td>{sample.ambiguousCount?.toLocaleString() ?? "—"}</td>
                    <td>{humanizeIdentifier(sample.validationStatus)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <UnavailableBlock />
        )}
      </section>
    </div>
  );
}

function UnavailableBlock() {
  const { t } = useI18n();
  return <div className="state-box">{t("result.overview.block-unavailable")}</div>;
}

function formatDistance(value: number): string {
  if (value === 0) return "0";
  return value.toPrecision(4);
}

function formatPercent(value: number): string {
  return new Intl.NumberFormat(undefined, {
    style: "percent",
    minimumFractionDigits: 1,
    maximumFractionDigits: 2,
  }).format(value);
}

function humanizeIdentifier(value: string): string {
  const normalized = value.replaceAll("_", " ").trim();
  return normalized.length > 0
    ? `${normalized.charAt(0).toUpperCase()}${normalized.slice(1)}`
    : value;
}

type Translate = ReturnType<typeof useI18n>["t"];

function heatmapTooltip(
  left: string,
  right: string,
  distance: number | null,
  pair: ResultDistancePair | null,
  t: Translate,
): string {
  if (left === right) return t("result.overview.heatmap.self-tooltip", { sample: left });
  if (distance === null) {
    return t("result.overview.heatmap.undefined-tooltip", { left, right });
  }
  if (!pair) {
    return t("result.overview.heatmap.distance-tooltip", {
      left,
      right,
      distance: formatDistance(distance),
    });
  }
  return t("result.overview.heatmap.pair-tooltip", {
    left,
    right,
    distance: formatDistance(distance),
    mismatches: pair.mismatch_count,
    comparable: pair.comparable_site_count,
    gaps: pair.excluded_gap_site_count,
    ambiguous: pair.excluded_ambiguous_site_count,
  });
}
