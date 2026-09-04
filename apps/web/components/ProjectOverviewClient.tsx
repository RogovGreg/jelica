"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useCallback, useEffect, useState } from "react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorState } from "@/components/ErrorState";
import { useI18n } from "@/components/I18nProvider";
import { LoadingState } from "@/components/LoadingState";
import { ProjectNavigation } from "@/components/ProjectNavigation";
import { RestrictedResourceState } from "@/components/RestrictedResourceState";
import { ProjectDiscussionClient } from "@/components/ProjectDiscussionClient";
import { deleteProject, getProject, getProjectHistory, getProjectMembers, getProjectTasks, leaveProject, updateProject } from "@/lib/api/client";
import { isResourceUnavailableError, toErrorMessage } from "@/lib/api/errors";
import type { TranslationKey, Translator } from "@/lib/i18n";
import type { AuthUser, Project, ProjectHistoryEvent, ProjectMember, ProjectTask } from "@/types/api";
import { normalizeTaskState } from "@/types/api";

type ProjectOverviewClientProps = Readonly<{ projectId: string; currentUser: AuthUser }>;
type DialogAction = "freeze" | "unfreeze" | "leave" | "delete";

export function ProjectOverviewClient({ projectId, currentUser }: ProjectOverviewClientProps) {
  const { t } = useI18n();
  const [project, setProject] = useState<Project | null>(null);
  const [members, setMembers] = useState<ProjectMember[] | null>(null);
  const [tasks, setTasks] = useState<ProjectTask[] | null>(null);
  const [history, setHistory] = useState<ProjectHistoryEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [secondaryErrors, setSecondaryErrors] = useState<string[]>([]);
  const [dialog, setDialog] = useState<DialogAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [resourceUnavailable, setResourceUnavailable] = useState(false);
  const [discussionRestriction, setDiscussionRestriction] = useState<"access-denied" | "resource-unavailable" | null>(null);

  const loadOverview = useCallback(async () => {
    setError(null);
    try {
      const loadedProject = await getProject(projectId);
      setProject(loadedProject);
      const results = await Promise.allSettled([getProjectTasks(projectId), getProjectMembers(projectId), getProjectHistory(projectId)]);
      const failures: string[] = [];
      if (results[0].status === "fulfilled") setTasks(results[0].value.items); else failures.push(toErrorMessage(results[0].reason));
      if (results[1].status === "fulfilled") setMembers(results[1].value.items); else failures.push(toErrorMessage(results[1].reason));
      if (results[2].status === "fulfilled") setHistory(results[2].value.items); else failures.push(toErrorMessage(results[2].reason));
      setSecondaryErrors(failures);
    } catch (requestError) {
      if (isResourceUnavailableError(requestError)) setResourceUnavailable(true);
      else setError(toErrorMessage(requestError));
    }
  }, [projectId]);

  useEffect(() => { void loadOverview(); }, [loadOverview]);

  const currentMember = members?.find((member) => member.user_id === currentUser.id);
  const canAdminister = currentMember?.role === "supervisor" || project?.owner_user_id === currentUser.id;
  const canLeave = project?.owner_user_id !== currentUser.id;
  const handleDiscussionRestriction = useCallback((variant: "access-denied" | "resource-unavailable") => {
    setDiscussionRestriction(variant);
  }, []);

  async function performAction() {
    if (!dialog) return;
    setBusy(true);
    try {
      if (dialog === "delete") { await deleteProject(projectId); window.location.assign("/app/projects"); return; }
      if (dialog === "leave") { await leaveProject(projectId); window.location.assign("/app/projects"); return; }
      const nextStatus = dialog === "freeze" ? "frozen" : "active";
      setProject(await updateProject(projectId, { status: nextStatus }));
      setDialog(null);
      setHistory((await getProjectHistory(projectId)).items);
    } catch (requestError) {
      setError(toErrorMessage(requestError));
      setBusy(false);
    }
    setBusy(false);
  }

  if (resourceUnavailable) return <RestrictedResourceState variant="resource-unavailable" resourceType="project" />;
  if (discussionRestriction) return <RestrictedResourceState variant={discussionRestriction} resourceType="project" />;
  if (!project && !error) return <LoadingState title={t("common.state.loading")} />;
  if (error || !project) return <ErrorState title={t("project.error.unavailable")} description={error ?? t("project.error.details-unavailable")}><Link href="/app/projects" className="secondary-button">{t("project.overview.back")}</Link></ErrorState>;

  return <section className="stack">
    <div className="actions-row"><Link href="/app/projects" className="secondary-button">← {t("project.overview.back")}</Link></div>
    <header className="panel project-overview-header">
      {editing ? <EditProjectForm project={project} busy={busy} onCancel={() => setEditing(false)} onSave={async (changes) => { setBusy(true); try { setProject(await updateProject(projectId, changes)); setEditing(false); } catch (requestError) { setError(toErrorMessage(requestError)); } finally { setBusy(false); } }} /> : <>
        <div className="project-card-heading"><div><h1>{project.name}</h1>{project.description ? <p className="muted">{project.description}</p> : null}</div><span className={`status-badge project-status-${project.status}`}>{t(project.status === "active" ? "project.status.active" : "project.status.frozen")}</span></div>
        {project.status === "frozen" ? <div className="state-box state-warning" role="status">{t("project.status.banner")}</div> : null}
        <ProjectNavigation projectId={projectId} active="overview" />
        <div className="actions-row">
          {canAdminister ? <button type="button" className="secondary-button" onClick={() => setEditing(true)}>{t("project.action.edit")}</button> : null}
          {canAdminister ? <button type="button" className="secondary-button" onClick={() => setDialog(project.status === "active" ? "freeze" : "unfreeze")}>{t(project.status === "active" ? "project.action.freeze" : "project.action.unfreeze")}</button> : null}
          {canLeave ? <button type="button" className="secondary-button" onClick={() => setDialog("leave")}>{t("project.action.leave")}</button> : <span className="muted">{t("project.action.transfer-first")}</span>}
          {project.owner_user_id === currentUser.id ? <button type="button" className="danger-button" onClick={() => setDialog("delete")}>{t("project.action.delete")}</button> : null}
        </div>
      </>}
    </header>
    {secondaryErrors.length ? <div className="state-box state-warning" role="status">{t("project.error.details-unavailable")} {secondaryErrors[0]}</div> : null}
    <div className="project-summary-grid">
      <SummaryCard title={t("project.overview.info")}><dl className="profile-details"><div><dt>{t("project.overview.created-at")}</dt><dd>{formatDate(project.created_at)}</dd></div><div><dt>{t("project.overview.created-by")}</dt><dd>{findUsername(members, project.created_by_user_id) ?? project.created_by_user_id}</dd></div><div><dt>{t("project.overview.owner")}</dt><dd>{findUsername(members, project.owner_user_id) ?? project.owner_user_id}</dd></div></dl></SummaryCard>
      <SummaryCard title={t("project.overview.tasks")}>{tasks ? <><strong className="summary-number">{tasks.length}</strong><span className="muted">{t("project.overview.total")}</span><div className="summary-breakdown">{countStates(tasks).map(([state, count]) => <span key={state}>{state}: {count}</span>)}</div><Link href={`/app/projects/${projectId}/tasks`} className="secondary-button">{t("project.overview.view-tasks")}</Link></> : <span className="muted">{t("project.error.details-unavailable")}</span>}</SummaryCard>
      <SummaryCard title={t("project.overview.members")}>{members ? <><strong className="summary-number">{members.length}</strong><span className="muted">{t("project.overview.total")}</span><div className="summary-breakdown">{["viewer", "commenter", "member", "supervisor"].map((role) => <span key={role}>{role}: {members.filter((member) => member.role === role).length}</span>)}</div><Link href={`/app/projects/${projectId}/members`} className="secondary-button">{t("project.overview.view-members")}</Link></> : <span className="muted">{t("project.error.details-unavailable")}</span>}</SummaryCard>
    </div>
    <section className="panel stack"><h2 style={{ margin: 0 }}>{t("project.activity")}</h2>{history === null ? <span className="muted">{t("project.error.details-unavailable")}</span> : history.length === 0 ? <span className="muted">{t("project.activity.empty")}</span> : <div className="activity-timeline">{[...history].reverse().map((event) => <div className="activity-event" key={event.id}><span className="activity-marker" aria-hidden="true" /><div><strong>{eventLabel(event, t)}</strong><span className="muted">{formatDate(event.occurred_at)}</span></div></div>)}</div>}</section>
    <ProjectDiscussionClient projectId={projectId} currentUser={currentUser} compact initialProject={project} onRestriction={handleDiscussionRestriction} />
    {dialog ? <ConfirmDialog title={dialogTitle(dialog, t)} description={dialogDescription(dialog, t)} actionLabel={dialogAction(dialog, t)} destructive={dialog === "delete"} busy={busy} onCancel={() => setDialog(null)} onConfirm={performAction} /> : null}
  </section>;
}

function SummaryCard({ title, children }: Readonly<{ title: string; children: ReactNode }>) { return <section className="panel summary-card"><h2>{title}</h2><div className="stack">{children}</div></section>; }
function EditProjectForm({ project, busy, onCancel, onSave }: Readonly<{ project: Project; busy: boolean; onCancel: () => void; onSave: (changes: { name: string; description: string | null }) => Promise<void> }>) {
  const { t } = useI18n();
  const [name, setName] = useState(project.name); const [description, setDescription] = useState(project.description ?? "");
  return <form className="form-grid" onSubmit={(event) => { event.preventDefault(); if (name.trim()) void onSave({ name: name.trim(), description: description.trim() || null }); }}><label className="input-field"><span>{t("project.form.name")}</span><input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} required disabled={busy} /></label><label className="input-field"><span>{t("project.form.description")}</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={3} disabled={busy} /></label><div className="actions-row"><button type="submit" className="primary-button" disabled={busy}>{t("project.action.save")}</button><button type="button" className="secondary-button" onClick={onCancel} disabled={busy}>{t("project.action.cancel")}</button></div></form>;
}
function findUsername(members: ProjectMember[] | null, id: string) { return members?.find((member) => member.user_id === id)?.username; }
function countStates(tasks: ProjectTask[]) { const counts = new Map<string, number>(); tasks.forEach((task) => { const state = normalizeTaskState(task.state); counts.set(state, (counts.get(state) ?? 0) + 1); }); return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b)); }
function eventLabel(event: ProjectHistoryEvent, t: Translator) { const labels: Record<string, TranslationKey> = { project_created: "project.activity.created", project_updated: "project.activity.updated", project_frozen: "project.activity.frozen", project_unfrozen: "project.activity.unfrozen", member_joined: "project.activity.member-joined", member_removed: "project.activity.member-removed", member_role_changed: "project.activity.role-changed", ownership_transferred: "project.activity.ownership-transferred", task_attached: "project.activity.task-attached", task_detached: "project.activity.task-detached" }; const key = labels[event.event_type]; return key ? t(key) : t("project.activity"); }
function formatDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString(); }
function dialogTitle(action: DialogAction, t: Translator) { return t(({ freeze: "project.action.freeze.confirm", unfreeze: "project.action.unfreeze.confirm", leave: "project.action.leave.confirm", delete: "project.action.delete.confirm" } as const)[action]); }
function dialogDescription(action: DialogAction, t: Translator) { return t(({ freeze: "project.action.freeze.description", unfreeze: "project.action.unfreeze.description", leave: "project.action.leave.description", delete: "project.action.delete.description" } as const)[action]); }
function dialogAction(action: DialogAction, t: Translator) { return t(({ freeze: "project.action.freeze", unfreeze: "project.action.unfreeze", leave: "project.action.leave", delete: "project.action.delete" } as const)[action]); }
