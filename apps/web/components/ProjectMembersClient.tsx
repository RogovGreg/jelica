"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { useI18n } from "@/components/I18nProvider";
import { LoadingState } from "@/components/LoadingState";
import { ProjectNavigation } from "@/components/ProjectNavigation";
import { RestrictedResourceState } from "@/components/RestrictedResourceState";
import { createProjectInvitation, getProject, getProjectMembers, listInvitationCandidates, listProjectInvitations, removeProjectMember, revokeProjectInvitation, transferProjectOwnership, updateProjectMemberRole } from "@/lib/api/client";
import { isResourceUnavailableError, toErrorMessage } from "@/lib/api/errors";
import type { AuthUser, InvitationCandidate, Project, ProjectInvitation, ProjectMember, ProjectMemberRole } from "@/types/api";

type Props = Readonly<{ projectId: string; currentUser: AuthUser }>;
type ConfirmAction = { kind: "remove" | "transfer" | "revoke"; userId?: string; invitationId?: string; username?: string };

export function ProjectMembersClient({ projectId, currentUser }: Props) {
  const { t } = useI18n();
  const [project, setProject] = useState<Project | null>(null);
  const [members, setMembers] = useState<ProjectMember[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState<"members" | "invitations">("members");
  const [invitations, setInvitations] = useState<ProjectInvitation[] | null>(null);
  const [invitationError, setInvitationError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [resourceUnavailable, setResourceUnavailable] = useState(false);

  useEffect(() => {
    let projectLoaded = false;
    getProject(projectId)
      .then((loadedProject) => { projectLoaded = true; setProject(loadedProject); return getProjectMembers(projectId); })
      .then((loadedMembers) => setMembers(loadedMembers.items))
      .catch((requestError) => { if (!projectLoaded && isResourceUnavailableError(requestError)) setResourceUnavailable(true); else setError(toErrorMessage(requestError)); });
  }, [projectId]);

  const currentMember = members?.find((member) => member.user_id === currentUser.id);
  const canAdminister = currentMember?.role === "supervisor" || project?.owner_user_id === currentUser.id;
  const visibleMembers = useMemo(() => [...(members ?? [])].filter((member) => member.username.toLowerCase().includes(search.trim().toLowerCase())).sort((a, b) => a.username.localeCompare(b.username, undefined, { sensitivity: "base" }) || a.user_id.localeCompare(b.user_id)), [members, search]);

  useEffect(() => {
    if (!canAdminister && tab === "invitations") setTab("members");
  }, [canAdminister, tab]);

  useEffect(() => {
    if (tab !== "invitations" || invitations !== null || !canAdminister) return;
    listProjectInvitations(projectId).then((response) => setInvitations(response.items.filter((item) => item.status === "pending"))).catch((requestError) => setInvitationError(toErrorMessage(requestError)));
  }, [canAdminister, invitations, projectId, tab]);

  async function refreshMembers() { setMembers((await getProjectMembers(projectId)).items); }
  async function applyRole(userId: string, role: ProjectMemberRole) { setBusy(true); setMutationError(null); try { await updateProjectMemberRole(projectId, userId, role); await refreshMembers(); } catch (requestError) { setMutationError(toErrorMessage(requestError)); } finally { setBusy(false); } }
  async function performConfirm() {
    if (!confirm) return;
    setBusy(true); setMutationError(null);
    try {
      if (confirm.kind === "remove" && confirm.userId) await removeProjectMember(projectId, confirm.userId);
      if (confirm.kind === "transfer" && confirm.userId) { setProject(await transferProjectOwnership(projectId, confirm.userId)); }
      if (confirm.kind === "revoke" && confirm.invitationId) await revokeProjectInvitation(projectId, confirm.invitationId);
      if (confirm.kind !== "revoke") await refreshMembers();
      else setInvitations((items) => items?.filter((item) => item.invitation_id !== confirm.invitationId) ?? []);
      setConfirm(null);
    } catch (requestError) { setMutationError(toErrorMessage(requestError)); }
    setBusy(false);
  }

  if (resourceUnavailable) return <RestrictedResourceState variant="resource-unavailable" resourceType="project" />;
  if (!project && !error) return <LoadingState title={t("common.state.loading")} />;
  if (error || !project) return <ErrorState title={t("project.error.unavailable")} description={error ?? t("project.error.details-unavailable")} />;
  return <section className="stack">
    <div className="actions-row"><Link href={`/app/projects/${projectId}`} className="secondary-button">← {t("project.overview.back")}</Link></div>
    <header className="panel project-overview-header"><div className="project-card-heading"><div><h1>{project.name}</h1><p className="muted">{t(project.status === "frozen" ? "project.status.banner" : "project.page.subtitle")}</p></div><span className={`status-badge project-status-${project.status}`}>{t(project.status === "active" ? "project.status.active" : "project.status.frozen")}</span></div>{project.status === "frozen" ? <div className="state-box state-warning" role="status">{t("project.status.banner")}</div> : null}<ProjectNavigation projectId={projectId} active="members" /></header>
    {canAdminister ? <div className="project-tabs" role="tablist"><button type="button" role="tab" aria-selected={tab === "members"} className={tab === "members" ? "active" : undefined} onClick={() => setTab("members")}>{t("members.tab.members")}</button><button type="button" role="tab" aria-selected={tab === "invitations"} className={tab === "invitations" ? "active" : undefined} onClick={() => setTab("invitations")}>{t("members.tab.invitations")}</button></div> : null}
    {mutationError ? <div className="state-box state-error" role="alert">{mutationError}</div> : null}
    {tab === "members" ? <MembersPanel members={visibleMembers} allMembers={members ?? []} search={search} onSearch={setSearch} canAdminister={Boolean(canAdminister)} currentUserId={currentUser.id} ownerId={project.owner_user_id} busy={busy} onRoleChange={applyRole} onConfirm={setConfirm} t={t} /> : <InvitationsPanel projectId={projectId} project={project} invitations={invitations} error={invitationError} busy={busy} onCreated={(invitation) => setInvitations((items) => [invitation, ...(items ?? [])])} onRevoke={(invitation) => setConfirm({ kind: "revoke", invitationId: invitation.invitation_id, username: invitation.invited_username })} t={t} />}
    {confirm ? <ConfirmDialog title={confirmTitle(confirm, t)} description={confirmDescription(confirm, t)} actionLabel={confirm.kind === "remove" ? t("members.action.remove") : confirm.kind === "transfer" ? t("members.action.transfer") : t("members.action.revoke")} destructive={confirm.kind === "remove"} busy={busy} onCancel={() => setConfirm(null)} onConfirm={performConfirm} /> : null}
  </section>;
}

function MembersPanel({ members, allMembers, search, onSearch, canAdminister, currentUserId, ownerId, busy, onRoleChange, onConfirm, t }: Readonly<{ members: ProjectMember[]; allMembers: ProjectMember[]; search: string; onSearch: (value: string) => void; canAdminister: boolean; currentUserId: string; ownerId: string; busy: boolean; onRoleChange: (userId: string, role: ProjectMemberRole) => Promise<void>; onConfirm: (action: ConfirmAction) => void; t: (key: import("@/lib/i18n").TranslationKey) => string }>) {
  return <section className="panel stack"><div className="members-toolbar"><label className="input-field"><span>{t("members.search")}</span><input value={search} onChange={(event) => onSearch(event.target.value)} placeholder={t("members.search.placeholder")} /></label><strong>{members.length}/{allMembers.length}</strong></div>{members.length === 0 ? <div className="state-box">{t("members.empty.filter")}</div> : <div className="table-scroll"><table className="task-table members-table"><thead><tr><th>{t("members.username")}</th><th>{t("members.role")}</th><th>{t("members.joined")}</th><th>{t("members.email")}</th><th>{t("members.actions")}</th></tr></thead><tbody>{members.map((member) => <MemberRow key={member.user_id} member={member} isYou={member.user_id === currentUserId} isOwner={member.user_id === ownerId} canAdminister={canAdminister} canTransfer={canAdminister && currentUserId === ownerId} busy={busy} onRoleChange={onRoleChange} onConfirm={onConfirm} t={t} />)}</tbody></table></div>}</section>;
}

function MemberRow({ member, isYou, isOwner, canAdminister, canTransfer, busy, onRoleChange, onConfirm, t }: Readonly<{ member: ProjectMember; isYou: boolean; isOwner: boolean; canAdminister: boolean; canTransfer: boolean; busy: boolean; onRoleChange: (userId: string, role: ProjectMemberRole) => Promise<void>; onConfirm: (action: ConfirmAction) => void; t: (key: import("@/lib/i18n").TranslationKey) => string }>) {
  const [role, setRole] = useState(member.role);
  useEffect(() => setRole(member.role), [member.role]);
  return <tr><td><strong>{member.username}</strong>{isYou ? <span className="member-badge">{t("members.you")}</span> : null}{isOwner ? <span className="member-badge">{t("members.owner")}</span> : null}</td><td>{canAdminister ? <select aria-label={`${t("members.role")} ${member.username}`} value={role} onChange={(event) => setRole(event.target.value as ProjectMemberRole)} disabled={busy || isOwner}><option value="viewer">{t("members.role.viewer")}</option><option value="commenter">{t("members.role.commenter")}</option><option value="member">{t("members.role.member")}</option><option value="supervisor">{t("members.role.supervisor")}</option></select> : roleLabel(member.role, t)}</td><td>{formatDate(member.joined_at)}</td><td><a href={`mailto:${member.email}`}>{member.email}</a></td><td><div className="actions-row">{canAdminister && role !== member.role && !isOwner ? <button type="button" className="secondary-button" onClick={() => void onRoleChange(member.user_id, role)} disabled={busy}>{t("members.action.apply")}</button> : null}{canAdminister && !isYou && !isOwner ? <button type="button" className="danger-button" onClick={() => onConfirm({ kind: "remove", userId: member.user_id, username: member.username })} disabled={busy}>{t("members.action.remove")}</button> : null}{canTransfer && !isOwner ? <button type="button" className="secondary-button" onClick={() => onConfirm({ kind: "transfer", userId: member.user_id, username: member.username })} disabled={busy}>{t("members.action.transfer")}</button> : null}</div></td></tr>;
}

function InvitationsPanel({ projectId, project, invitations, error, busy, onCreated, onRevoke, t }: Readonly<{ projectId: string; project: Project; invitations: ProjectInvitation[] | null; error: string | null; busy: boolean; onCreated: (invitation: ProjectInvitation) => void; onRevoke: (invitation: ProjectInvitation) => void; t: (key: import("@/lib/i18n").TranslationKey) => string }>) {
  const [query, setQuery] = useState(""); const [selected, setSelected] = useState<InvitationCandidate | null>(null); const [role, setRole] = useState<ProjectMemberRole>("member"); const [candidates, setCandidates] = useState<InvitationCandidate[]>([]); const [searching, setSearching] = useState(false); const [formError, setFormError] = useState<string | null>(null); const [sending, setSending] = useState(false);
  useEffect(() => { if (!query.trim() || selected) { setCandidates([]); return; } const timer = window.setTimeout(() => { setSearching(true); listInvitationCandidates(projectId, query.trim()).then((response) => setCandidates(response.items)).catch((requestError) => setFormError(toErrorMessage(requestError))).finally(() => setSearching(false)); }, 250); return () => window.clearTimeout(timer); }, [projectId, query, selected]);
  async function send() { if (!selected) return; setSending(true); setFormError(null); try { const invitation = await createProjectInvitation(projectId, selected.user_id, role); onCreated(invitation); setSelected(null); setQuery(""); setRole("member"); } catch (requestError) { setFormError(toErrorMessage(requestError)); } finally { setSending(false); } }
  return <section className="stack"><div className="panel stack"><h2 style={{ margin: 0 }}>{t("members.invite.title")}</h2><label className="input-field"><span>{t("members.username")}</span><input value={selected?.username ?? query} onChange={(event) => { setSelected(null); setQuery(event.target.value); }} placeholder={t("members.search.placeholder")} disabled={sending || project.status === "frozen"} /></label>{searching ? <span className="muted">{t("common.state.loading")}</span> : candidates.length ? <div className="candidate-list">{candidates.map((candidate) => <button type="button" key={candidate.user_id} onClick={() => { setSelected(candidate); setCandidates([]); }}>{candidate.username}</button>)}</div> : null}<label className="input-field"><span>{t("members.role")}</span><select value={role} onChange={(event) => setRole(event.target.value as ProjectMemberRole)} disabled={sending || project.status === "frozen"}><option value="viewer">{t("members.role.viewer")}</option><option value="commenter">{t("members.role.commenter")}</option><option value="member">{t("members.role.member")}</option><option value="supervisor">{t("members.role.supervisor")}</option></select></label>{project.status === "frozen" ? <div className="state-box state-warning">{t("members.invite.frozen")}</div> : null}{formError ? <div className="state-box state-error" role="alert">{formError}</div> : null}<button type="button" className="primary-button" onClick={() => void send()} disabled={!selected || sending || project.status === "frozen"}>{t("members.invite.send")}</button></div><div className="panel stack"><h2 style={{ margin: 0 }}>{t("members.invite.pending")}</h2>{error ? <ErrorState title={t("common.state.error")} description={error} /> : invitations === null ? <LoadingState title={t("common.state.loading")} /> : invitations.length === 0 ? <EmptyState title={t("project.invitation.empty")} description={t("project.invitation.empty.description")} /> : invitations.map((invitation) => <div className="pending-invitation" key={invitation.invitation_id}><div><strong>{invitation.invited_username}</strong><span className="muted">{roleLabel(invitation.role, t)} · {formatDate(invitation.invited_at)} – {formatDate(invitation.expires_at)}</span></div><button type="button" className="danger-button" onClick={() => onRevoke(invitation)} disabled={busy}>{t("members.action.revoke")}</button></div>)}</div></section>;
}

function confirmTitle(action: ConfirmAction, t: (key: import("@/lib/i18n").TranslationKey) => string) { return t(({ remove: "members.confirm.remove.title", transfer: "members.confirm.transfer.title", revoke: "members.confirm.revoke.title" } as const)[action.kind]); }
function confirmDescription(action: ConfirmAction, t: (key: import("@/lib/i18n").TranslationKey) => string) { return t(({ remove: "members.confirm.remove.description", transfer: "members.confirm.transfer.description", revoke: "members.confirm.revoke.description" } as const)[action.kind]); }
function formatDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString(); }
function roleLabel(role: ProjectMemberRole, t: (key: import("@/lib/i18n").TranslationKey) => string) { return t(({ viewer: "members.role.viewer", commenter: "members.role.commenter", member: "members.role.member", supervisor: "members.role.supervisor" } as const)[role]); }
