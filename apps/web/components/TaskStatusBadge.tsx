"use client";

import { useI18n } from "@/components/I18nProvider";
import { normalizeTaskState } from "@/types/api";
import { TaskStatusBadge as SharedTaskStatusBadge } from "../../../packages/app-platform/src/task-ui";

type TaskStatusBadgeProps = {
  status: string;
};

export function TaskStatusBadge({ status }: TaskStatusBadgeProps) {
  const { t } = useI18n();
  const normalized = normalizeTaskState(status);
  const labels: Record<string, string> = { created: t("task.status.created"), queued: t("task.status.queued"), running: t("task.status.running"), waiting: t("task.status.waiting"), paused: t("task.status.paused"), pausing: t("task.status.pausing"), resuming: t("task.status.resuming"), completed: t("task.status.completed"), failed: t("task.status.failed"), interrupted: t("task.status.interrupted"), cancelled: t("task.status.cancelled") };
  return <SharedTaskStatusBadge status={normalized} labels={labels} />;
}
