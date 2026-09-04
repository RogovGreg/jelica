"use client";

import Link from "next/link";

import { useI18n } from "@/components/I18nProvider";
import { TaskStatusBadge } from "@/components/TaskStatusBadge";
import { getTaskCanonicalPath } from "@/lib/tasks/routing";
import { normalizeTaskState, type ProjectTask, type TaskListItem } from "@/types/api";

type TaskRow = TaskListItem | ProjectTask;

export function TaskListView({ tasks, linkResolver = getTaskCanonicalPath, emptyLabel }: Readonly<{ tasks: TaskRow[]; linkResolver?: (task: TaskRow) => string; emptyLabel?: string }>) {
  const { t } = useI18n();
  if (tasks.length === 0) return <div className="state-box">{emptyLabel ?? t("task.list.empty")}</div>;
  return <div className="table-scroll"><table className="task-table project-task-table" aria-label={t("page.tasks.title")}>
    <thead><tr><th>{t("task.list.task-id")}</th><th>{t("task.list.project")}</th><th>{t("task.list.trace-id")}</th><th>{t("task.list.state")}</th><th>{t("task.list.stage")}</th><th>{t("task.list.progress")}</th><th>{t("task.list.updated")}</th><th>{t("task.list.source")}</th><th>{t("task.list.owner")}</th><th>{t("task.list.actions")}</th></tr></thead>
    <tbody>{tasks.map((task) => <tr key={task.task_id}>
      <td data-label={t("task.list.task-id")}><Link href={linkResolver(task)}>{"name" in task ? task.name || task.task_id : task.task_id}</Link><div className="muted task-id-subtitle">{task.task_id}</div></td>
      <td data-label={t("task.list.project")}>{task.project_id ?? "—"}</td>
      <td data-label={t("task.list.trace-id")}>{"trace_id" in task ? task.trace_id ?? "—" : "—"}</td>
      <td data-label={t("task.list.state")}><TaskStatusBadge status={task.state} /></td>
      <td data-label={t("task.list.stage")}>{"current_stage" in task ? task.current_stage ?? "—" : "—"}</td>
      <td data-label={t("task.list.progress")}>{"progress" in task && task.progress != null ? `${task.progress}%` : "—"}</td>
      <td data-label={t("task.list.updated")}>{"updated_at" in task ? formatDateTime(task.updated_at) : "—"}</td>
      <td data-label={t("task.list.source")}>{"state_source" in task ? task.state_source + (task.authoritative ? "" : " (" + t("task.list.cached") + ")") + (task.stale_state ? " (" + t("task.list.stale") + ")" : "") : "—"}</td>
      <td data-label={t("task.list.owner")}>{"owner_user_id" in task && task.owner_user_id ? <span title={task.owner_user_id}>{t("task.list.owner-present")}</span> : "—"}</td>
      <td data-label={t("task.list.actions")}><div className="actions-row"><Link href={linkResolver(task)} className="secondary-button">{t("task.list.open")}</Link>{normalizeTaskState(task.state) === "completed" ? <Link href={`/app/results/${encodeURIComponent(task.task_id)}`} className="secondary-button">{t("task.list.open-result")}</Link> : null}</div></td>
    </tr>)}</tbody>
  </table></div>;
}

function formatDateTime(rawValue: string): string { const value = new Date(rawValue); return Number.isNaN(value.getTime()) ? rawValue : value.toLocaleString(); }
