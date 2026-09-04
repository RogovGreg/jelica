"use client";

import { useEffect, useState } from "react";

import { useI18n } from "@/components/I18nProvider";
import { getCurrentUser, getProjects } from "@/lib/api/client";
import type { Project } from "@/types/api";

const STATES = ["created", "queued", "running", "waiting", "pausing", "paused", "resuming", "completed", "failed", "interrupted", "cancelled"] as const;

export function TaskListFilters({ initialOwner, initialProject, initialProjectId, initialState }: Readonly<{ initialOwner?: string; initialProject?: string; initialProjectId?: string; initialState?: string }>) {
  const { t } = useI18n();
  const [authenticated, setAuthenticated] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState(initialProjectId ?? "");
  useEffect(() => { void getCurrentUser().then(() => { setAuthenticated(true); return getProjects(); }).then((response) => setProjects(response.items)).catch(() => setAuthenticated(false)); }, []);

  return <form method="get" className="task-list-filters" aria-label={t("task.list.filters")}>
    {authenticated ? <>
      <label className="input-field"><span>{t("task.list.owner")}</span><select name="owner" defaultValue={initialOwner ?? ""}><option value="">{t("task.list.all-visible")}</option><option value="me">{t("task.list.mine")}</option></select></label>
      <label className="input-field"><span>{t("task.list.project")}</span><select name="project_id" value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}><option value="">{t("task.list.all-projects")}</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
      <label className="checkbox-field"><input type="checkbox" name="project" value="none" defaultChecked={initialProject === "none"} disabled={Boolean(selectedProjectId)} /><span>{t("task.list.no-project")}</span></label>
    </> : null}
    <label className="input-field"><span>{t("task.list.state")}</span><select name="state" defaultValue={initialState ?? ""}><option value="">{t("task.list.all-states")}</option>{STATES.map((state) => <option key={state} value={state}>{stateLabel(state, t)}</option>)}</select></label>
    <button type="submit" className="secondary-button">{t("task.list.apply-filters")}</button>
  </form>;
}

function stateLabel(state: (typeof STATES)[number], t: ReturnType<typeof useI18n>["t"]): string {
  const key = `task.status.${state}` as Parameters<typeof t>[0];
  return t(key);
}
