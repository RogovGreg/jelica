import type {
  ProjectComment,
  ProjectCommentReaction,
  ProjectCommentReactionSummary,
  ProjectMemberRole,
  ProjectStatus,
} from "@/types/api";

export type RealtimeUser = { user_id: string; username: string };

export type ProjectDiscussionCommand =
  | { type: "command"; id: string; command: "comment.create"; payload: { body: string } }
  | { type: "command"; id: string; command: "comment.update"; payload: { comment_id: string; body: string } }
  | { type: "command"; id: string; command: "comment.delete"; payload: { comment_id: string } }
  | { type: "command"; id: string; command: "reaction.set"; payload: { comment_id: string; reaction: ProjectCommentReaction } }
  | { type: "command"; id: string; command: "reaction.delete"; payload: { comment_id: string } };

export type ProjectDiscussionTypingCommand = { type: "typing.start" | "typing.stop" };

export type ProjectDiscussionServerMessage =
  | { type: "command.ack"; id: string; command: ProjectDiscussionCommand["command"]; result?: ProjectComment | ProjectCommentReactionSummary | { comment_id: string } }
  | { type: "command.error"; id: string; command: string; error: { code: string; message: string } }
  | { type: "protocol.error"; error: { code: string; message: string } }
  | { type: "comment.created" | "comment.updated"; command_id: string | null; comment: ProjectComment }
  | { type: "comment.deleted"; command_id: string | null; comment_id: string }
  | { type: "reaction.updated" | "reaction.deleted"; command_id: string | null; comment_id: string; support: number; oppose: number }
  | { type: "presence.snapshot"; users: RealtimeUser[] }
  | { type: "presence.joined" | "presence.left"; user: RealtimeUser }
  | { type: "typing.started" | "typing.stopped"; user: RealtimeUser }
  | { type: "member.joined"; user_id: string; username: string; role: ProjectMemberRole }
  | { type: "member.removed"; user_id: string }
  | { type: "member.role_changed"; user_id: string; username: string; role: ProjectMemberRole }
  | { type: "project.ownership_transferred"; previous_owner_user_id: string; new_owner_user_id: string }
  | { type: "project.frozen" | "project.unfrozen"; status: ProjectStatus }
  | { type: "project.deleted" }
  | { type: "access.revoked"; error: { code: "access_revoked"; message: string } };

export type PersistentProjectDiscussionEvent = Extract<
  ProjectDiscussionServerMessage,
  {
    type:
      | "comment.created"
      | "comment.updated"
      | "comment.deleted"
      | "reaction.updated"
      | "reaction.deleted"
      | "member.joined"
      | "member.removed"
      | "member.role_changed"
      | "project.ownership_transferred"
      | "project.frozen"
      | "project.unfrozen"
      | "project.deleted"
      | "access.revoked";
  }
>;

export function isProjectDiscussionServerMessage(value: unknown): value is ProjectDiscussionServerMessage {
  return Boolean(value && typeof value === "object" && "type" in value && typeof (value as { type?: unknown }).type === "string");
}

export function isPersistentProjectDiscussionEvent(
  message: ProjectDiscussionServerMessage,
): message is PersistentProjectDiscussionEvent {
  return PERSISTENT_EVENT_TYPES.has(message.type);
}

const PERSISTENT_EVENT_TYPES = new Set<ProjectDiscussionServerMessage["type"]>([
  "comment.created",
  "comment.updated",
  "comment.deleted",
  "reaction.updated",
  "reaction.deleted",
  "member.joined",
  "member.removed",
  "member.role_changed",
  "project.ownership_transferred",
  "project.frozen",
  "project.unfrozen",
  "project.deleted",
  "access.revoked",
]);
