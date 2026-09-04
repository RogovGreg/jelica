"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { useI18n } from "@/components/I18nProvider";
import { LoadingState } from "@/components/LoadingState";
import { acceptInvitation, declineInvitation, getProjects, getReceivedInvitations } from "@/lib/api/client";
import { toErrorMessage } from "@/lib/api/errors";
import type { Translator } from "@/lib/i18n";
import type { Project, ProjectInvitation } from "@/types/api";

type ProjectsClientProps = Readonly<{ initialTab?: "projects" | "invitations" }>;

export function ProjectsClient({ initialTab = "projects" }: ProjectsClientProps) {
  const { t } = useI18n();
  const [tab, setTab] = useState(initialTab);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [invitations, setInvitations] = useState<ProjectInvitation[] | null>(null);
  const [invitationError, setInvitationError] = useState<string | null>(null);
  const [loadingInvitations, setLoadingInvitations] = useState(false);

  useEffect(() => {
    getProjects().then((response) => setProjects(response.items)).catch((error) => setProjectError(toErrorMessage(error)));
  }, []);

  useEffect(() => {
    if (tab !== "invitations" || invitations !== null || loadingInvitations) return;
    setLoadingInvitations(true);
    getReceivedInvitations()
      .then((response) => setInvitations(response.items.filter((item) => item.status === "pending")))
      .catch((error) => setInvitationError(toErrorMessage(error)))
      .finally(() => setLoadingInvitations(false));
  }, [invitations, loadingInvitations, tab]);

  async function refreshProjects() {
    try {
      setProjects((await getProjects()).items);
      setProjectError(null);
    } catch (error) {
      setProjectError(toErrorMessage(error));
    }
  }

  async function resolveInvitation(invitation: ProjectInvitation, action: "accept" | "decline") {
    try {
      if (action === "accept") await acceptInvitation(invitation.invitation_id);
      else await declineInvitation(invitation.invitation_id);
      setInvitations((items) => items?.filter((item) => item.invitation_id !== invitation.invitation_id) ?? []);
      if (action === "accept") await refreshProjects();
    } catch (error) {
      setInvitationError(toErrorMessage(error));
    }
  }

  return (
    <section className="stack">
      <header className="projects-page-header">
        <div>
          <h1 style={{ margin: 0 }}>{t("project.tab.projects")}</h1>
          <p className="muted" style={{ margin: "0.35rem 0 0" }}>{t("project.page.subtitle")}</p>
        </div>
        {tab === "projects" ? <Link href="/app/projects/new" className="primary-button">{t("project.action.create")}</Link> : null}
      </header>
      <div className="project-tabs" role="tablist" aria-label={t("project.navigation.sections")}>
        <button type="button" role="tab" aria-selected={tab === "projects"} className={tab === "projects" ? "active" : undefined} onClick={() => setTab("projects")}>{t("project.tab.projects")}</button>
        <button type="button" role="tab" aria-selected={tab === "invitations"} className={tab === "invitations" ? "active" : undefined} onClick={() => setTab("invitations")}>{t("project.tab.invitations")}</button>
      </div>
      {tab === "projects" ? (
        projectError ? <ErrorState title={t("common.state.error")} description={projectError} /> : projects === null ? <LoadingState title={t("common.state.loading")} /> : projects.length === 0 ? <EmptyState title={t("project.empty")} description={t("project.empty.description")} /> : (
          <div className="project-card-grid">{projects.map((project) => <ProjectCard key={project.id} project={project} t={t} />)}</div>
        )
      ) : (
        loadingInvitations && invitations === null ? <LoadingState title={t("common.state.loading")} /> : invitationError ? <ErrorState title={t("common.state.error")} description={invitationError} /> : invitations?.length === 0 ? <EmptyState title={t("project.invitation.empty")} description={t("project.invitation.empty.description")} /> : (
          <div className="stack">{invitations?.map((invitation) => <InvitationCard key={invitation.invitation_id} invitation={invitation} onResolve={resolveInvitation} t={t} />)}</div>
        )
      )}
    </section>
  );
}

function ProjectCard({ project, t }: Readonly<{ project: Project; t: Translator }>) {
  return <Link href={`/app/projects/${encodeURIComponent(project.id)}`} className="project-card panel">
    <div className="project-card-heading"><h2>{project.name}</h2><span className={`status-badge project-status-${project.status}`}>{t(project.status === "active" ? "project.status.active" : "project.status.frozen")}</span></div>
    {project.description ? <p className="muted project-card-description">{project.description}</p> : null}
  </Link>;
}

function InvitationCard({ invitation, onResolve, t }: Readonly<{ invitation: ProjectInvitation; onResolve: (invitation: ProjectInvitation, action: "accept" | "decline") => Promise<void>; t: Translator }>) {
  const [busy, setBusy] = useState(false);
  async function resolve(action: "accept" | "decline") {
    setBusy(true);
    await onResolve(invitation, action);
    setBusy(false);
  }
  return <article className="panel invitation-card">
    <div><h2>{invitation.project_name}</h2><p className="muted">{t("project.invitation.invited-by")} {invitation.inviter_username} · {invitation.role}</p><p className="muted">{t("project.invitation.expires")} {formatDate(invitation.expires_at)}</p></div>
    <div className="actions-row"><button type="button" className="primary-button" disabled={busy || invitation.status === "expired"} onClick={() => resolve("accept")}>{t("project.action.accept")}</button><button type="button" className="secondary-button" disabled={busy} onClick={() => resolve("decline")}>{t("project.action.decline")}</button></div>
  </article>;
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
