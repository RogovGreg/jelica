"use client";

import type { ReactNode } from "react";

export type TaskStatusLabels = Readonly<Record<string, ReactNode>>;

export function normalizeTaskState(rawState: string): string {
  return rawState.trim().toLowerCase();
}

export function TaskStatusBadge({ status, labels, unknownLabel = "unknown" }: { status: string; labels?: TaskStatusLabels; unknownLabel?: ReactNode }) {
  const normalized = normalizeTaskState(status);
  const label = labels?.[normalized] ?? (normalized || unknownLabel);
  return <span className={`status-badge ${taskStatusClassName(normalized)}`}>{label}</span>;
}

export function TaskProgress({ value, ariaLabel, fallback = "—" }: { value: number | null; ariaLabel?: string; fallback?: ReactNode }) {
  return value === null ? <>{fallback}</> : <progress max={100} value={value} aria-label={ariaLabel} />;
}

function taskStatusClassName(status: string): string {
  if (["created", "queued"].includes(status)) return "status-neutral";
  if (["running", "waiting", "pausing", "resuming"].includes(status)) return "status-progress";
  if (status === "paused") return "status-paused";
  if (status === "completed") return "status-completed";
  if (["failed", "interrupted"].includes(status)) return "status-failed";
  if (status === "cancelled") return "status-cancelled";
  return "status-default";
}
