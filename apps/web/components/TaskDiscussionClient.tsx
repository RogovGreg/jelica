"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { LoadingState } from "@/components/LoadingState";
import { RestrictedResourceState } from "@/components/RestrictedResourceState";
import { useI18n } from "@/components/I18nProvider";
import { useTaskDiscussionRealtime } from "@/hooks/useTaskDiscussionRealtime";
import { getTaskStatus } from "@/lib/api/client";
import { getTaskDiscussionCanonicalPath } from "@/lib/tasks/routing";
import { canTaskComment } from "@/lib/realtime/taskDiscussion";
import type { AuthUser, TaskDiscussionComment, TaskDiscussionReaction } from "@/types/api";

export function TaskDiscussionClient({ taskId, currentUser, compact = false, routeProjectId }: Readonly<{ taskId: string; currentUser: AuthUser; compact?: boolean; routeProjectId?: string }>) {
  const { t, locale } = useI18n();
  const router = useRouter();
  const [redirecting, setRedirecting] = useState(false);
  const [contextLost, setContextLost] = useState(false);
  const contextHandledRef = useRef(false);
  const handleContextChanged = useCallback(() => {
    if (contextHandledRef.current) return;
    contextHandledRef.current = true;
    setRedirecting(true);
    void getTaskStatus(taskId).then((status) => router.replace(getTaskDiscussionCanonicalPath({ task_id: status.task_id, project_id: status.project_id }))).catch(() => setContextLost(true));
  }, [router, taskId]);
  const realtime = useTaskDiscussionRealtime({ taskId, currentUser, onContextChanged: handleContextChanged });
  const discussion = realtime.discussion;
  const comments = useMemo(() => discussion?.comments ?? [], [discussion?.comments]);
  const members = discussion?.members ?? [];
  const currentRole = members.find((item) => item.user_id === currentUser.id)?.role;
  const connected = realtime.snapshotReady && realtime.connectionStatus === "connected";
  const mutable = connected && discussion?.projectStatus === "active" && canTaskComment(currentRole);
  const ownerAdmin = Boolean(discussion?.metadata.is_task_owner);
  const detached = discussion?.metadata.project_id === null;
  const [draft, setDraft] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<TaskDiscussionComment | null>(null);
  const [clearOpen, setClearOpen] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => { if (realtime.restriction) { setDraft(""); setEditDraft(""); setEditingId(null); } }, [realtime.restriction]);
  useEffect(() => {
    const projectId = discussion?.metadata.project_id;
    const mismatch = (projectId && !routeProjectId) || (projectId && routeProjectId && projectId !== routeProjectId) || (!projectId && Boolean(routeProjectId));
    if (!mismatch || redirecting) return;
    setRedirecting(true);
    router.replace(getTaskDiscussionCanonicalPath({ task_id: taskId, project_id: projectId ?? null }));
  }, [discussion?.metadata.project_id, redirecting, routeProjectId, router, taskId]);

  if (redirecting) return <LoadingState title={t("task.routing.redirecting")} />;
  if (contextLost) return <RestrictedResourceState variant="access-denied" resourceType="task" />;
  if (realtime.restriction) return <RestrictedResourceState variant={realtime.restriction} resourceType="task" />;
  if (!discussion) return <LoadingState title={t("discussion.loading")} />;
  if (!discussion.metadata.available || realtime.unavailable) {
    return <section className={compact ? "panel stack task-discussion-compact" : "panel stack discussion-page"}><h2>{t("discussion.title")}</h2><p className="muted">{t("task.discussion.unavailable")}</p></section>;
  }
  if ((discussion.metadata.project_id && routeProjectId && discussion.metadata.project_id !== routeProjectId) || (discussion.metadata.project_id && !routeProjectId) || (!discussion.metadata.project_id && routeProjectId)) return <LoadingState title={t("task.routing.redirecting")} />;

  const send = (input: Parameters<typeof realtime.sendCommand>[0], key: string, onAck?: () => void) => {
    setBusy(key); setError(null);
    realtime.sendCommand(input, { commentId: "payload" in input && "comment_id" in input.payload ? input.payload.comment_id : undefined, onAck: () => { setBusy(null); onAck?.(); }, onError: (code) => { setBusy(null); setError(code); } });
  };
  const create = () => { const body = draft.trim(); if (!body || !mutable || busy) return; send({ command: "comment.create", payload: { body } }, "create", () => setDraft("")); };
  const save = (commentId: string) => { const body = editDraft.trim(); if (!body || !mutable || busy) return; send({ command: "comment.update", payload: { comment_id: commentId, body } }, "edit", () => { setEditingId(null); setEditDraft(""); }); };
  const canDelete = (comment: TaskDiscussionComment) => ownerAdmin || (comment.author_user_id === currentUser.id && canTaskComment(currentRole)) || currentRole === "supervisor";
  const confirmDelete = () => { if (!deleteTarget || (!mutable && !ownerAdmin) || busy) return; send({ command: "comment.delete", payload: { comment_id: deleteTarget.id } }, "delete", () => setDeleteTarget(null)); };
  const setReaction = (comment: TaskDiscussionComment, reaction: TaskDiscussionReaction) => { if (!mutable || comment.author_user_id === currentUser.id || busy) return; const current = comment.reaction_summary.current_user_reaction; send(current === reaction ? { command: "reaction.delete", payload: { comment_id: comment.id } } : { command: "reaction.set", payload: { comment_id: comment.id, reaction } }, `reaction-${comment.id}`); };
  const clear = () => { if (!ownerAdmin || busy) return; setBusy("clear"); setError(null); realtime.sendCommand({ command: "discussion.clear", payload: {} }, { onAck: () => { setBusy(null); setClearOpen(false); }, onError: (code) => { setBusy(null); setError(code); } }); };

  return <section className={compact ? "panel stack task-discussion-compact" : "stack discussion-page"}>
    {!compact ? <Breadcrumbs items={[{ label: t("breadcrumbs.tasks"), href: "/app/tasks" }, ...(discussion.projectName && discussion.metadata.project_id ? [{ label: discussion.projectName, href: `/app/projects/${encodeURIComponent(discussion.metadata.project_id)}` }, { label: t("discussion.title") }] : [{ label: t("task.label.task-prefix", { task: taskId }), href: `/app/tasks/${encodeURIComponent(taskId)}` }, { label: t("discussion.title") }])]} label={t("breadcrumbs.label")} /> : null}
    <section className="panel discussion-panel">
      <header className="discussion-header"><div><h2>{t("discussion.title")}</h2><span className={`connection-status connection-${realtime.connectionStatus}`} role="status">{t(connectionStatusKey(realtime.connectionStatus))}</span></div><Presence users={realtime.presence} t={t} /></header>
      {detached ? <div className="state-box state-warning">{t("task.discussion.detached")}{ownerAdmin ? t("task.discussion.detached.owner") : ""}</div> : null}
      {discussion.projectStatus === "frozen" ? <div className="state-box state-warning">{t("task.discussion.frozen")}</div> : null}
      {realtime.connectionStatus === "reconnecting" ? <div className="state-box state-warning">{t("task.discussion.reconnecting")}</div> : null}
      {error ? <div className="state-box state-error" role="alert">{error}</div> : null}
      <div className="discussion-feed" ref={feedRef}><ol className="discussion-comment-list" aria-label={t("discussion.messages.label")}>{comments.length ? comments.map((comment) => <li key={comment.id}><article className="discussion-comment"><header className="discussion-comment-header"><div><strong>{comment.author_username}</strong><time dateTime={comment.created_at}>{formatDate(comment.created_at, locale)}</time>{comment.edited_at ? <span className="muted">{t("discussion.comment.edited")}</span> : null}</div><div className="discussion-comment-actions">{comment.author_user_id === currentUser.id && mutable ? <button type="button" className="text-button" onClick={() => { setEditingId(comment.id); setEditDraft(comment.body); }}>{t("project.action.edit")}</button> : null}{canDelete(comment) ? <button type="button" className="text-button danger-text" onClick={() => setDeleteTarget(comment)}>{t("project.action.delete")}</button> : null}</div></header>{editingId === comment.id ? <div className="stack"><textarea className="text-input" value={editDraft} maxLength={10000} onChange={(event) => setEditDraft(event.target.value)} /><div className="actions-row"><button type="button" className="primary-button" disabled={!editDraft.trim() || Boolean(busy)} onClick={() => save(comment.id)}>{t("common.action.save")}</button><button type="button" className="secondary-button" onClick={() => setEditingId(null)}>{t("common.action.cancel")}</button></div></div> : <div className="discussion-comment-body">{comment.body}</div>}<div className="discussion-reactions"><button type="button" aria-label={t("discussion.reaction.support")} aria-pressed={comment.reaction_summary.current_user_reaction === "support"} disabled={!mutable || comment.author_user_id === currentUser.id || Boolean(busy)} onClick={() => setReaction(comment, "support")}><ThumbIcon direction="up" />{comment.reaction_summary.support}</button><button type="button" aria-label={t("discussion.reaction.oppose")} aria-pressed={comment.reaction_summary.current_user_reaction === "oppose"} disabled={!mutable || comment.author_user_id === currentUser.id || Boolean(busy)} onClick={() => setReaction(comment, "oppose")}><ThumbIcon direction="down" />{comment.reaction_summary.oppose}</button></div></article></li>) : <div className="discussion-empty"><h3>{t("discussion.empty.title")}</h3><p className="muted">{t("discussion.empty.readonly")}</p></div>}</ol></div>
      {mutable ? <div className="discussion-composer stack"><label className="input-field"><span>{t("discussion.composer.label")}</span><textarea value={draft} maxLength={10000} rows={4} placeholder={t("discussion.composer.placeholder")} disabled={Boolean(busy)} onChange={(event) => { setDraft(event.target.value); realtime.notifyTyping(); }} onBlur={realtime.stopTyping} /><span className="muted">{t("task.discussion.mentions").replace("{members}", members.map((member) => `@${member.username}`).join(", ") || t("task.discussion.online.none"))}</span></label><div className="discussion-composer-footer"><span className="muted">{draft.length}/10000</span><button type="button" className="primary-button" disabled={!draft.trim() || Boolean(busy)} onClick={create}>{t("discussion.composer.send")}</button></div></div> : <div className="discussion-composer"><div className="state-box">{t("discussion.readonly.viewer")}</div></div>}
      {ownerAdmin && !compact ? <div className="actions-row"><button type="button" className="danger-button" disabled={Boolean(busy)} onClick={() => setClearOpen(true)}>{t("task.discussion.clear.action")}</button></div> : null}
    </section>
    {deleteTarget ? <ConfirmDialog title={t("discussion.delete.title")} description={t(deleteTarget.author_user_id === currentUser.id ? "discussion.delete.description" : "discussion.delete.moderationDescription")} actionLabel={t("project.action.delete")} destructive busy={busy === "delete"} onCancel={() => setDeleteTarget(null)} onConfirm={confirmDelete} /> : null}
    {clearOpen ? <ConfirmDialog title={t("task.discussion.clear.title")} description={t("task.discussion.clear.description")} actionLabel={t("task.discussion.clear.action")} destructive busy={busy === "clear"} onCancel={() => setClearOpen(false)} onConfirm={clear} /> : null}
  </section>;
}

function Presence({ users, t }: Readonly<{ users: Array<{ user_id: string; username: string }>; t: (key: import("@/lib/i18n").TranslationKey) => string }>) { return <div className="discussion-presence" aria-label={t("discussion.presence.label")}><span>{t("task.discussion.online")}</span><strong>{users.length ? users.slice(0, 3).map((user) => user.username).join(", ") : t("task.discussion.online.none")}</strong>{users.length > 3 ? <span>+{users.length - 3}</span> : null}</div>; }
function ThumbIcon({ direction }: Readonly<{ direction: "up" | "down" }>) { return <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className={direction === "down" ? "thumb-down" : undefined}><path d="M7 10v10H3V10h4Zm0 8h9.2a2 2 0 0 0 1.94-1.51l1.5-6A2 2 0 0 0 17.7 8H14l.6-3.1A2.4 2.4 0 0 0 12.25 2L7 10v8Z" /></svg>; }
function formatDate(value: string, locale: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale); }
function connectionStatusKey(status: "connecting" | "connected" | "reconnecting"): import("@/lib/i18n").TranslationKey { return ({ connecting: "discussion.connection.connecting", connected: "discussion.connection.connected", reconnecting: "discussion.connection.reconnecting" } as const)[status]; }
