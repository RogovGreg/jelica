"use client";

import Link from "next/link";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorState } from "@/components/ErrorState";
import { useI18n } from "@/components/I18nProvider";
import { LoadingState } from "@/components/LoadingState";
import { ProjectNavigation } from "@/components/ProjectNavigation";
import { RestrictedResourceState } from "@/components/RestrictedResourceState";
import {
  type DiscussionCommandInput,
  useProjectDiscussionRealtime,
} from "@/hooks/useProjectDiscussionRealtime";
import { getProject } from "@/lib/api/client";
import { isResourceUnavailableError, toErrorMessage } from "@/lib/api/errors";
import { canComment, type DiscussionMember } from "@/lib/realtime/projectDiscussion";
import type { TranslationKey } from "@/lib/i18n";
import type {
  AuthUser,
  Project,
  ProjectCommentListItem,
  ProjectCommentReaction,
} from "@/types/api";

export function ProjectDiscussionClient({
  projectId,
  currentUser,
  compact = false,
  initialProject,
  onRestriction,
}: Readonly<{ projectId: string; currentUser: AuthUser; compact?: boolean; initialProject?: Project; onRestriction?: (variant: "access-denied" | "resource-unavailable") => void }>) {
  const { t } = useI18n();
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resourceUnavailable, setResourceUnavailable] = useState(false);

  useEffect(() => {
    if (initialProject) {
      setProject(initialProject);
      return;
    }
    let active = true;
    getProject(projectId)
      .then((loaded) => {
        if (active) setProject(loaded);
      })
      .catch((requestError) => {
        if (!active) return;
        if (isResourceUnavailableError(requestError)) setResourceUnavailable(true);
        else setError(toErrorMessage(requestError));
      });
    return () => {
      active = false;
    };
  }, [initialProject, projectId]);

  useEffect(() => {
    if (resourceUnavailable) onRestriction?.("resource-unavailable");
  }, [onRestriction, resourceUnavailable]);

  if (resourceUnavailable && !compact) {
    return <RestrictedResourceState variant="resource-unavailable" resourceType="project" />;
  }
  if (!project && !error) return <LoadingState title={t("common.state.loading")} />;
  if (error || !project) {
    if (compact) return <section className="panel stack discussion-compact"><h2 style={{ margin: 0 }}>{t("discussion.title")}</h2><div className="state-box state-warning">{error ?? t("project.error.details-unavailable")}</div></section>;
    return (
      <ErrorState
        title={t("project.error.unavailable")}
        description={error ?? t("project.error.details-unavailable")}
      >
        <Link href="/app/projects" className="secondary-button">
          {t("project.action.backToProjects")}
        </Link>
      </ErrorState>
    );
  }
  return (
    <ProjectDiscussionSession
      projectId={projectId}
      currentUser={currentUser}
      initialProject={project}
      compact={compact}
      onRestriction={onRestriction}
    />
  );
}

function ProjectDiscussionSession({
  projectId,
  currentUser,
  initialProject,
  compact,
  onRestriction,
}: Readonly<{ projectId: string; currentUser: AuthUser; initialProject: Project; compact: boolean; onRestriction?: (variant: "access-denied" | "resource-unavailable") => void }>) {
  const { t, locale } = useI18n();
  const realtime = useProjectDiscussionRealtime({ projectId, initialProject, currentUser });
  useEffect(() => { if (realtime.restriction) onRestriction?.(realtime.restriction); }, [onRestriction, realtime.restriction]);
  const [draft, setDraft] = useState("");
  const [createPending, setCreatePending] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [editPending, setEditPending] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ProjectCommentListItem | null>(null);
  const [deletePending, setDeletePending] = useState(false);
  const [reactionPending, setReactionPending] = useState<Set<string>>(new Set());
  const [commandError, setCommandError] = useState<TranslationKey | null>(null);
  const [newMessages, setNewMessages] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const initialScrollRef = useRef(false);
  const previousCommentCountRef = useRef(0);
  const scrollAfterOwnRef = useRef(false);

  const discussion = realtime.discussion;
  const project = discussion?.project;
  const members = discussion?.members ?? [];
  const comments = useMemo(() => discussion?.comments ?? [], [discussion?.comments]);
  const currentRole = members.find((member) => member.user_id === currentUser.id)?.role;
  const connected = realtime.connectionStatus === "connected" && realtime.snapshotReady;
  const mutationEnabled =
    connected && project?.status === "active" && canComment(currentRole);

  useEffect(() => {
    if (!realtime.restriction) return;
    setDraft("");
    setEditDraft("");
    setEditingId(null);
    setDeleteTarget(null);
    setReactionPending(new Set());
    setCommandError(null);
  }, [realtime.restriction]);

  useEffect(() => {
    const feed = feedRef.current;
    if (!feed || !realtime.snapshotReady) return;
    const previousCount = previousCommentCountRef.current;
    const hasNewComment = comments.length > previousCount;
    previousCommentCountRef.current = comments.length;
    if (!initialScrollRef.current) {
      feed.scrollTop = feed.scrollHeight;
      initialScrollRef.current = true;
      return;
    }
    if (!hasNewComment) return;
    if (nearBottomRef.current || scrollAfterOwnRef.current) {
      feed.scrollTo({ top: feed.scrollHeight, behavior: "smooth" });
      setNewMessages(false);
      scrollAfterOwnRef.current = false;
    } else {
      setNewMessages(true);
    }
  }, [comments, realtime.snapshotReady]);

  if (realtime.restriction) {
    return (
      <RestrictedResourceState variant={realtime.restriction} resourceType="project" />
    );
  }
  if (!discussion || !project) return <LoadingState title={t("common.state.loading")} />;

  function showCommandError(code: string) {
    setCommandError(commandErrorKey(code));
  }

  function createComment() {
    const body = draft.trim();
    if (!body || !mutationEnabled || createPending) return;
    setCommandError(null);
    setCreatePending(true);
    realtime.sendCommand(
      { command: "comment.create", payload: { body } },
      {
        onAck: () => {
          setCreatePending(false);
          setDraft("");
          scrollAfterOwnRef.current = true;
          realtime.stopTyping();
        },
        onError: (code) => {
          setCreatePending(false);
          showCommandError(code);
        },
      },
    );
  }

  function saveEdit(commentId: string) {
    const body = editDraft.trim();
    if (!body || !mutationEnabled || editPending) return;
    setEditPending(true);
    setCommandError(null);
    realtime.sendCommand(
      { command: "comment.update", payload: { comment_id: commentId, body } },
      {
        onAck: () => {
          setEditPending(false);
          setEditingId(null);
          setEditDraft("");
          realtime.stopTyping();
        },
        onError: (code) => {
          setEditPending(false);
          showCommandError(code);
        },
      },
    );
  }

  function confirmDelete() {
    if (!deleteTarget || !mutationEnabled || deletePending) return;
    setDeletePending(true);
    setCommandError(null);
    realtime.sendCommand(
      { command: "comment.delete", payload: { comment_id: deleteTarget.id } },
      {
        onAck: () => {
          setDeletePending(false);
          setDeleteTarget(null);
        },
        onError: (code) => {
          setDeletePending(false);
          showCommandError(code);
        },
      },
    );
  }

  function changeReaction(comment: ProjectCommentListItem, reaction: ProjectCommentReaction) {
    if (!mutationEnabled || comment.author_user_id === currentUser.id) return;
    setReactionPending((pending) => new Set(pending).add(comment.id));
    setCommandError(null);
    const input: DiscussionCommandInput =
      comment.reaction_summary.current_user_reaction === reaction
        ? { command: "reaction.delete", payload: { comment_id: comment.id } }
        : {
            command: "reaction.set",
            payload: { comment_id: comment.id, reaction },
          };
    realtime.sendCommand(input, {
      commentId: comment.id,
      onAck: () =>
        setReactionPending((pending) => {
          const next = new Set(pending);
          next.delete(comment.id);
          return next;
        }),
      onError: (code) => {
        setReactionPending((pending) => {
          const next = new Set(pending);
          next.delete(comment.id);
          return next;
        });
        showCommandError(code);
      },
    });
  }

  return (
    <section className={`stack discussion-page${compact ? " discussion-compact" : ""}`}>
      {!compact ? <Breadcrumbs
        items={[
          { label: t("breadcrumbs.projects"), href: "/app/projects" },
          {
            label: project.name,
            href: `/app/projects/${encodeURIComponent(projectId)}`,
          },
          { label: t("project.navigation.discussion") },
        ]}
        label={t("breadcrumbs.label")}
      /> : null}
      {!compact ? <header className="panel project-overview-header">
        <div className="project-card-heading">
          <div>
            <h1>{project.name}</h1>
            <p className="muted">{t("discussion.subtitle")}</p>
          </div>
          <span className={`status-badge project-status-${project.status}`}>
            {t(
              project.status === "active"
                ? "project.status.active"
                : "project.status.frozen",
            )}
          </span>
        </div>
        {!compact && project.status === "frozen" ? (
          <div className="state-box state-warning" role="status">
            {t("project.status.banner")}
          </div>
        ) : null}
        <ProjectNavigation projectId={projectId} active="discussion" />
      </header> : null}

      <section className="panel discussion-panel" aria-labelledby="discussion-title">
        <header className="discussion-header">
          <div>
            <h2 id="discussion-title">{t("discussion.title")}</h2>
            <span
              className={`connection-status connection-${realtime.connectionStatus}`}
              role="status"
            >
              {t(connectionStatusKey(realtime.connectionStatus))}
            </span>
          </div>
          {compact ? <Link href={`/app/projects/${encodeURIComponent(projectId)}/discussion`} className="secondary-button">{t("task.discussion.open")}</Link> : null}
          <Presence users={realtime.presence} />
        </header>

        {commandError ? (
          <div className="state-box state-error" role="alert">
            {t(commandError)}
          </div>
        ) : null}
        {realtime.protocolError ? (
          <div className="state-box state-error" role="alert">
            {t("discussion.error.protocol")}
          </div>
        ) : null}

        <div
          className="discussion-feed"
          ref={feedRef}
          onScroll={(event) => {
            const element = event.currentTarget;
            nearBottomRef.current =
              element.scrollHeight - element.scrollTop - element.clientHeight < 80;
            if (nearBottomRef.current) setNewMessages(false);
          }}
        >
          {!realtime.snapshotReady && !comments.length ? (
            <LoadingState title={t("discussion.loading")} />
          ) : comments.length === 0 ? (
            <div className="discussion-empty">
              <h3>{t("discussion.empty.title")}</h3>
              <p className="muted">
                {t(
                  canComment(currentRole)
                    ? "discussion.empty.description"
                    : "discussion.empty.readonly",
                )}
              </p>
            </div>
          ) : (
            <ol className="discussion-comment-list" aria-label={t("discussion.messages.label")}>
              {comments.map((comment) => {
                const own = comment.author_user_id === currentUser.id;
                const canDelete =
                  mutationEnabled && (own || currentRole === "supervisor");
                return (
                  <li key={comment.id}>
                    <article className="discussion-comment">
                      <header className="discussion-comment-header">
                        <div>
                          <strong>{comment.author_username}</strong>
                          <time dateTime={comment.created_at}>
                            {formatDate(comment.created_at, locale)}
                          </time>
                          {comment.edited_at ? (
                            <span className="muted">{t("discussion.comment.edited")}</span>
                          ) : null}
                        </div>
                        <div className="discussion-comment-actions">
                          {own && mutationEnabled ? (
                            <button
                              type="button"
                              className="text-button"
                              onClick={() => {
                                setEditingId(comment.id);
                                setEditDraft(comment.body);
                                setCommandError(null);
                              }}
                            >
                              {t("project.action.edit")}
                            </button>
                          ) : null}
                          {canDelete ? (
                            <button
                              type="button"
                              className="text-button danger-text"
                              onClick={() => setDeleteTarget(comment)}
                            >
                              {t("project.action.delete")}
                            </button>
                          ) : null}
                        </div>
                      </header>
                      {editingId === comment.id ? (
                        <div className="discussion-edit stack">
                          <MentionTextarea
                            id={`edit-${comment.id}`}
                            label={t("discussion.edit.label")}
                            placeholder={t("discussion.composer.placeholder")}
                            value={editDraft}
                            members={members}
                            currentUserId={currentUser.id}
                            disabled={!mutationEnabled || editPending}
                            onChange={setEditDraft}
                            onTyping={realtime.notifyTyping}
                            onStopTyping={realtime.stopTyping}
                            onSubmit={() => saveEdit(comment.id)}
                          />
                          <div className="actions-row">
                            <button
                              type="button"
                              className="primary-button"
                              disabled={!editDraft.trim() || editPending || !mutationEnabled}
                              onClick={() => saveEdit(comment.id)}
                            >
                              {t("common.action.save")}
                            </button>
                            <button
                              type="button"
                              className="secondary-button"
                              disabled={editPending}
                              onClick={() => {
                                setEditingId(null);
                                setEditDraft("");
                                realtime.stopTyping();
                              }}
                            >
                              {t("common.action.cancel")}
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="discussion-comment-body">
                          {renderCommentBody(comment)}
                        </div>
                      )}
                      <ReactionControls
                        comment={comment}
                        disabled={
                          !mutationEnabled ||
                          own ||
                          reactionPending.has(comment.id)
                        }
                        onReaction={(reaction) => changeReaction(comment, reaction)}
                      />
                    </article>
                  </li>
                );
              })}
            </ol>
          )}
        </div>

        {newMessages ? (
          <button
            type="button"
            className="new-messages-button"
            onClick={() => {
              const feed = feedRef.current;
              if (feed) feed.scrollTo({ top: feed.scrollHeight, behavior: "smooth" });
              nearBottomRef.current = true;
              setNewMessages(false);
            }}
          >
            {t("discussion.newMessages")}
          </button>
        ) : null}

        <div className="discussion-composer stack">
          {currentRole === "viewer" ? (
            <div className="state-box" role="status">
              {t("discussion.readonly.viewer")}
            </div>
          ) : null}
          {realtime.connectionStatus === "reconnecting" ? (
            <div className="state-box state-warning" role="status">
              {t("discussion.reconnecting.description")}
            </div>
          ) : null}
          {canComment(currentRole) ? (
            <>
              <MentionTextarea
                id="discussion-composer"
                label={t("discussion.composer.label")}
                placeholder={t("discussion.composer.placeholder")}
                value={draft}
                members={members}
                currentUserId={currentUser.id}
                disabled={!mutationEnabled || createPending}
                onChange={setDraft}
                onTyping={realtime.notifyTyping}
                onStopTyping={realtime.stopTyping}
                onSubmit={createComment}
              />
              <div className="discussion-composer-footer">
                <Typing users={realtime.typingUsers} />
                <span className="muted">{draft.length}/10000</span>
                <button
                  type="button"
                  className="primary-button"
                  disabled={!draft.trim() || draft.length > 10000 || createPending || !mutationEnabled}
                  onClick={createComment}
                >
                  {t("discussion.composer.send")}
                </button>
              </div>
            </>
          ) : null}
        </div>
      </section>

      {deleteTarget ? (
        <ConfirmDialog
          title={t("discussion.delete.title")}
          description={t(
            deleteTarget.author_user_id === currentUser.id
              ? "discussion.delete.description"
              : "discussion.delete.moderationDescription",
          )}
          actionLabel={t("project.action.delete")}
          destructive
          busy={deletePending}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={confirmDelete}
        />
      ) : null}
    </section>
  );
}

function Presence({ users }: Readonly<{ users: { user_id: string; username: string }[] }>) {
  const { t } = useI18n();
  const visible = users.slice(0, 3);
  const overflow = users.length - visible.length;
  return (
    <div className="discussion-presence" aria-label={t("discussion.presence.label")}>
      <span>{t("discussion.presence.online")}</span>
      <strong>{visible.length ? visible.map((user) => user.username).join(", ") : t("discussion.presence.none")}</strong>
      {overflow > 0 ? <span>+{overflow}</span> : null}
    </div>
  );
}

function Typing({ users }: Readonly<{ users: { user_id: string; username: string }[] }>) {
  const { t } = useI18n();
  if (!users.length) return <span className="discussion-typing" aria-live="polite" />;
  return (
    <span className="discussion-typing" aria-live="polite">
      {t("discussion.typing")} {users.map((user) => user.username).join(", ")}
    </span>
  );
}

function ReactionControls({
  comment,
  disabled,
  onReaction,
}: Readonly<{
  comment: ProjectCommentListItem;
  disabled: boolean;
  onReaction: (reaction: ProjectCommentReaction) => void;
}>) {
  const { t } = useI18n();
  return (
    <div className="discussion-reactions" aria-label={t("discussion.reactions.label")}>
      <button
        type="button"
        aria-label={t("discussion.reaction.support")}
        aria-pressed={comment.reaction_summary.current_user_reaction === "support"}
        disabled={disabled}
        onClick={() => onReaction("support")}
      >
        <ThumbIcon direction="up" />
        <span>{comment.reaction_summary.support}</span>
      </button>
      <button
        type="button"
        aria-label={t("discussion.reaction.oppose")}
        aria-pressed={comment.reaction_summary.current_user_reaction === "oppose"}
        disabled={disabled}
        onClick={() => onReaction("oppose")}
      >
        <ThumbIcon direction="down" />
        <span>{comment.reaction_summary.oppose}</span>
      </button>
    </div>
  );
}

function ThumbIcon({ direction }: Readonly<{ direction: "up" | "down" }>) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={direction === "down" ? "thumb-down" : undefined}
    >
      <path d="M7 10v10H3V10h4Zm0 8h9.2a2 2 0 0 0 1.94-1.51l1.5-6A2 2 0 0 0 17.7 8H14l.6-3.1A2.4 2.4 0 0 0 12.25 2L7 10v8Z" />
    </svg>
  );
}

function MentionTextarea({
  id,
  label,
  placeholder,
  value,
  members,
  currentUserId,
  disabled,
  onChange,
  onTyping,
  onStopTyping,
  onSubmit,
}: Readonly<{
  id: string;
  label: string;
  placeholder: string;
  value: string;
  members: DiscussionMember[];
  currentUserId: string;
  disabled: boolean;
  onChange: (value: string) => void;
  onTyping: () => void;
  onStopTyping: () => void;
  onSubmit: () => void;
}>) {
  const { t } = useI18n();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [mention, setMention] = useState<MentionRange | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const candidates = useMemo(() => {
    if (!mention) return [];
    const prefix = mention.prefix.toLocaleLowerCase();
    return members
      .filter(
        (member) =>
          member.user_id !== currentUserId &&
          member.username.toLocaleLowerCase().startsWith(prefix),
      )
      .slice(0, 8);
  }, [currentUserId, members, mention]);
  const listId = `${id}-mentions`;

  function updateMention(cursor: number | null) {
    const next = cursor === null ? null : findMentionRange(value, cursor);
    setMention(next);
    setActiveIndex(0);
  }

  function choose(member: DiscussionMember) {
    if (!mention) return;
    const suffix = value.slice(mention.end);
    const separator = suffix !== "" && /^\s/.test(suffix) ? "" : " ";
    const inserted = `${value.slice(0, mention.start)}@${member.username}${separator}${suffix}`;
    const cursor = mention.start + member.username.length + 1 + separator.length;
    onChange(inserted);
    setMention(null);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(cursor, cursor);
    });
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      onSubmit();
      return;
    }
    if (!mention) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setMention(null);
      return;
    }
    if (!candidates.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) =>
        event.key === "ArrowDown"
          ? (index + 1) % candidates.length
          : (index - 1 + candidates.length) % candidates.length,
      );
      return;
    }
    if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      choose(candidates[activeIndex]);
    }
  }

  return (
    <label className="input-field mention-field" htmlFor={id}>
      <span>{label}</span>
      <textarea
        id={id}
        ref={textareaRef}
        value={value}
        maxLength={10000}
        rows={4}
        placeholder={placeholder}
        disabled={disabled}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={Boolean(mention)}
        aria-controls={mention ? listId : undefined}
        aria-activedescendant={
          mention && candidates[activeIndex]
            ? `${listId}-${candidates[activeIndex].user_id}`
            : undefined
        }
        aria-autocomplete="list"
        onChange={(event) => {
          onChange(event.target.value);
          setMention(findMentionRange(event.target.value, event.target.selectionStart));
          setActiveIndex(0);
          onTyping();
        }}
        onClick={(event) => updateMention(event.currentTarget.selectionStart)}
        onKeyUp={(event) => {
          if (!["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(event.key)) {
            updateMention(event.currentTarget.selectionStart);
          }
        }}
        onKeyDown={handleKeyDown}
        onBlur={() => {
          window.setTimeout(() => setMention(null), 120);
          onStopTyping();
        }}
      />
      {mention ? (
        <div className="mention-picker" id={listId} role="listbox" aria-label={t("discussion.mentions.label")}>
          {candidates.length ? (
            candidates.map((member, index) => (
              <button
                type="button"
                role="option"
                aria-selected={index === activeIndex}
                className={index === activeIndex ? "active" : undefined}
                id={`${listId}-${member.user_id}`}
                key={member.user_id}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => choose(member)}
              >
                @{member.username}
              </button>
            ))
          ) : (
            <span className="muted">{t("discussion.mentions.noMatches")}</span>
          )}
        </div>
      ) : null}
    </label>
  );
}

type MentionRange = { start: number; end: number; prefix: string };

function findMentionRange(value: string, cursor: number): MentionRange | null {
  let start = cursor - 1;
  while (start >= 0 && isMentionCharacter(value[start])) start -= 1;
  if (start < 0 || value[start] !== "@") return null;
  if (start > 0 && (value[start - 1] === "@" || isMentionCharacter(value[start - 1]))) {
    return null;
  }
  return { start, end: cursor, prefix: value.slice(start + 1, cursor) };
}

function renderCommentBody(comment: ProjectCommentListItem): ReactNode[] {
  const usernames = new Map(
    comment.mentions.map((mention) => [mention.username.toLocaleLowerCase(), mention.username]),
  );
  if (!usernames.size) return [comment.body];
  const nodes: ReactNode[] = [];
  let plainStart = 0;
  let index = 0;
  while (index < comment.body.length) {
    if (
      comment.body[index] !== "@" ||
      (index > 0 &&
        (comment.body[index - 1] === "@" || isMentionCharacter(comment.body[index - 1])))
    ) {
      index += 1;
      continue;
    }
    let end = index + 1;
    while (end < comment.body.length && isMentionCharacter(comment.body[end])) end += 1;
    const username = comment.body.slice(index + 1, end);
    if (!usernames.has(username.toLocaleLowerCase())) {
      index = end;
      continue;
    }
    if (plainStart < index) nodes.push(comment.body.slice(plainStart, index));
    nodes.push(
      <span className="discussion-mention" key={`${index}-${username}`}>
        @{username}
      </span>,
    );
    plainStart = end;
    index = end;
  }
  if (plainStart < comment.body.length) nodes.push(comment.body.slice(plainStart));
  return nodes;
}

function isMentionCharacter(character: string): boolean {
  return /[\p{L}\p{N}_-]/u.test(character);
}

function formatDate(value: string, locale: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale);
}

function connectionStatusKey(status: "connecting" | "connected" | "reconnecting"): TranslationKey {
  return {
    connecting: "discussion.connection.connecting",
    connected: "discussion.connection.connected",
    reconnecting: "discussion.connection.reconnecting",
  }[status] as TranslationKey;
}

function commandErrorKey(code: string): TranslationKey {
  const keys: Record<string, TranslationKey> = {
    validation_error: "discussion.error.validation",
    forbidden: "discussion.error.forbidden",
    project_frozen: "discussion.error.frozen",
    comment_not_found: "discussion.error.commentNotFound",
    reaction_not_allowed: "discussion.error.selfReaction",
    resource_unavailable: "discussion.error.resourceUnavailable",
    access_revoked: "discussion.error.accessRevoked",
    malformed_command: "discussion.error.protocol",
    unsupported_command: "discussion.error.protocol",
    realtime_unavailable: "discussion.error.realtimeUnavailable",
  };
  return keys[code] ?? "discussion.error.generic";
}
