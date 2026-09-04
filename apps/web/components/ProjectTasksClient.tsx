"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorState } from "@/components/ErrorState";
import { LoadingState } from "@/components/LoadingState";
import { ProjectNavigation } from "@/components/ProjectNavigation";
import { TaskListView } from "@/components/TaskListView";
import { RestrictedResourceState } from "@/components/RestrictedResourceState";
import { getProject, getProjectTasks } from "@/lib/api/client";
import { isResourceUnavailableError, toErrorMessage } from "@/lib/api/errors";
import { useI18n } from "@/components/I18nProvider";
import type { Project, ProjectTask } from "@/types/api";
import { normalizeTaskState } from "@/types/api";

export function ProjectTasksClient({ projectId }: Readonly<{ projectId: string }>) {
  const { t } = useI18n();
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<ProjectTask[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resourceUnavailable, setResourceUnavailable] = useState(false);

  useEffect(() => {
    let projectLoaded = false;
    getProject(projectId)
      .then((loadedProject) => { projectLoaded = true; setProject(loadedProject); return getProjectTasks(projectId); })
      .then((loadedTasks) => setTasks(loadedTasks.items))
      .catch((requestError) => { if (!projectLoaded && isResourceUnavailableError(requestError)) setResourceUnavailable(true); else setError(toErrorMessage(requestError)); });
  }, [projectId]);

  if (resourceUnavailable) return <RestrictedResourceState variant="resource-unavailable" resourceType="project" />;
  if (!project && !error) return <LoadingState title={t("common.state.loading")} />;
  if (error || !project) return <ErrorState title={t("project.error.unavailable")} description={error ?? t("project.error.details-unavailable")}><Link href="/app/projects" className="secondary-button">{t("project.overview.back")}</Link></ErrorState>;
  const loadedTasks = tasks ?? [];
  const counts = countStates(loadedTasks);

  return <section className="stack">
    <Breadcrumbs items={[{ label: t("breadcrumbs.projects"), href: "/app/projects" }, { label: project.name, href: `/app/projects/${encodeURIComponent(projectId)}` }, { label: t("project.navigation.tasks") }]} label={t("breadcrumbs.label")} />
    <header className="panel project-overview-header">
      <div className="project-card-heading"><div><h1>{project.name}</h1><p className="muted">{t("project.tasks.subtitle")}</p></div><span className={`status-badge project-status-${project.status}`}>{t(project.status === "active" ? "project.status.active" : "project.status.frozen")}</span></div>
      {project.status === "frozen" ? <div className="state-box state-warning" role="status">{t("project.status.banner")}</div> : null}
      <ProjectNavigation projectId={projectId} active="tasks" />
    </header>
    <section className="panel stack"><div className="project-task-summary" aria-label={t("project.tasks.summary")}><div><strong>{loadedTasks.length}</strong><span>{t("project.tasks.total")}</span></div>{counts.map(([state, count]) => <div key={state}><strong>{count}</strong><span>{stateLabel(state, t)}</span></div>)}</div></section>
    <section className="panel stack"><p className="muted" style={{ margin: 0 }}>{t("task.list.latest-known")}</p><div className="actions-row"><Link href={`/app/projects/${encodeURIComponent(projectId)}`} className="secondary-button">{t("project.tasks.back-overview")}</Link></div><TaskListView tasks={loadedTasks} linkResolver={(task) => `/app/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(task.task_id)}`} emptyLabel={t("project.tasks.empty")} /></section>
  </section>;
}

function countStates(tasks: ProjectTask[]) { const counts = new Map<string, number>(); tasks.forEach((task) => { const state = normalizeTaskState(task.state); counts.set(state, (counts.get(state) ?? 0) + 1); }); return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b)); }
function stateLabel(state: string, t: (key: import("@/lib/i18n").TranslationKey) => string) {
  const keys: Record<string, import("@/lib/i18n").TranslationKey> = {
    created: "task.status.created", queued: "task.status.queued", running: "task.status.running", waiting: "task.status.waiting",
    paused: "task.status.paused", pausing: "task.status.pausing", resuming: "task.status.resuming",
    completed: "task.status.completed", failed: "task.status.failed", interrupted: "task.status.interrupted",
    cancelled: "task.status.cancelled",
  };
  return keys[state] ? t(keys[state]) : state;
}
