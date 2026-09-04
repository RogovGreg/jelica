import type { ProjectMember, ProjectStatus, TaskDiscussionComment, TaskDiscussionMetadata, TaskDiscussionReaction, TaskDiscussionReactionSummary } from "@/types/api";

export type TaskDiscussionMember = Pick<ProjectMember, "user_id" | "username" | "role">;
export type TaskDiscussionState = {
  metadata: TaskDiscussionMetadata;
  comments: TaskDiscussionComment[];
  members: TaskDiscussionMember[];
  projectName: string | null;
  projectStatus: ProjectStatus | null;
};

export type TaskDiscussionCommand =
  | { type: "command"; id: string; command: "comment.create"; payload: { body: string } }
  | { type: "command"; id: string; command: "comment.update"; payload: { comment_id: string; body: string } }
  | { type: "command"; id: string; command: "comment.delete"; payload: { comment_id: string } }
  | { type: "command"; id: string; command: "reaction.set"; payload: { comment_id: string; reaction: TaskDiscussionReaction } }
  | { type: "command"; id: string; command: "reaction.delete"; payload: { comment_id: string } }
  | { type: "command"; id: string; command: "discussion.clear"; payload: Record<string, never> };

export type TaskDiscussionServerMessage =
  | { type: "command.ack"; id: string; command: TaskDiscussionCommand["command"]; result?: TaskDiscussionComment | TaskDiscussionReactionSummary | Record<string, never> }
  | { type: "command.error"; id: string; command: string; error: { code: string; message: string } }
  | { type: "protocol.error"; error: { code: string; message: string } }
  | { type: "comment.created" | "comment.updated"; command_id: string | null; comment: TaskDiscussionComment }
  | { type: "comment.deleted"; command_id: string | null; comment_id: string }
  | { type: "reaction.updated" | "reaction.deleted"; command_id: string | null; comment_id: string; support: number; oppose: number }
  | { type: "discussion.cleared"; command_id: string | null }
  | { type: "presence.snapshot"; users: Array<{ user_id: string; username: string }> }
  | { type: "presence.joined" | "presence.left"; user: { user_id: string; username: string } }
  | { type: "typing.started" | "typing.stopped"; user: { user_id: string; username: string } }
  | { type: "member.joined"; user_id: string; username: string; role: TaskDiscussionMember["role"] }
  | { type: "member.removed"; user_id: string }
  | { type: "member.role_changed"; user_id: string; username: string; role: TaskDiscussionMember["role"] }
  | { type: "project.frozen" | "project.unfrozen"; status: ProjectStatus }
  | { type: "task.context_changed"; task_id: string; project_id: string | null }
  | { type: "access.revoked"; error: { code: "access_revoked"; message: string } };

export type TaskDiscussionPersistentEvent = Exclude<Extract<TaskDiscussionServerMessage, { type: string }>,
  { type: "command.ack" | "command.error" | "protocol.error" | "presence.snapshot" | "presence.joined" | "presence.left" | "typing.started" | "typing.stopped" | "task.context_changed" }>;

export function parseTaskDiscussionMessage(raw: string): TaskDiscussionServerMessage | null {
  try {
    const value: unknown = JSON.parse(raw);
    return isTaskDiscussionMessage(value) ? value : null;
  } catch {
    return null;
  }
}

function isTaskDiscussionMessage(value: unknown): value is TaskDiscussionServerMessage {
  return Boolean(value && typeof value === "object" && "type" in value && typeof (value as { type?: unknown }).type === "string");
}

export function installTaskDiscussionSnapshot(
  metadata: TaskDiscussionMetadata,
  comments: TaskDiscussionComment[],
  members: TaskDiscussionMember[],
  projectName: string | null,
  projectStatus: ProjectStatus | null,
  events: TaskDiscussionPersistentEvent[],
): TaskDiscussionState {
  return events.reduce(applyTaskDiscussionEvent, {
    metadata,
    comments: sortComments(comments),
    members: sortMembers(members),
    projectName,
    projectStatus,
  });
}

export function applyTaskDiscussionEvent(state: TaskDiscussionState, event: TaskDiscussionPersistentEvent): TaskDiscussionState {
  switch (event.type) {
    case "comment.created":
    case "comment.updated":
      return { ...state, comments: sortComments([...state.comments.filter((item) => item.id !== event.comment.id), event.comment]) };
    case "comment.deleted":
      return { ...state, comments: state.comments.filter((item) => item.id !== event.comment_id) };
    case "reaction.updated":
    case "reaction.deleted":
      return { ...state, comments: state.comments.map((item) => item.id === event.comment_id ? { ...item, reaction_summary: { ...item.reaction_summary, support: event.support, oppose: event.oppose } } : item) };
    case "discussion.cleared":
      return { ...state, comments: [] };
    case "member.joined":
    case "member.role_changed":
      return { ...state, members: sortMembers([...state.members.filter((item) => item.user_id !== event.user_id), { user_id: event.user_id, username: event.username, role: event.role }]) };
    case "member.removed":
      return { ...state, members: state.members.filter((item) => item.user_id !== event.user_id) };
    case "project.frozen":
    case "project.unfrozen":
      return { ...state, projectStatus: event.status };
    default:
      return state;
  }
}

export function canTaskComment(role: TaskDiscussionMember["role"] | null | undefined): boolean {
  return role === "commenter" || role === "member" || role === "supervisor";
}

export function isReactionSummary(value: unknown): value is TaskDiscussionReactionSummary {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<TaskDiscussionReactionSummary>;
  return typeof item.support === "number" && typeof item.oppose === "number" && (item.current_user_reaction === null || item.current_user_reaction === "support" || item.current_user_reaction === "oppose");
}

function sortComments(items: TaskDiscussionComment[]): TaskDiscussionComment[] {
  return [...items].sort((left, right) => left.created_at.localeCompare(right.created_at) || left.id.localeCompare(right.id));
}
function sortMembers(items: TaskDiscussionMember[]): TaskDiscussionMember[] {
  return [...items].sort((left, right) => left.username.localeCompare(right.username, undefined, { sensitivity: "base" }) || left.user_id.localeCompare(right.user_id));
}
