import type {
  Project,
  ProjectCommentListItem,
  ProjectCommentReaction,
  ProjectCommentReactionSummary,
  ProjectMember,
  ProjectMemberRole,
} from "@/types/api";

import type { PersistentProjectDiscussionEvent, ProjectDiscussionServerMessage } from "./types";
import { isProjectDiscussionServerMessage } from "./types";

export type DiscussionMember = Pick<ProjectMember, "user_id" | "username" | "role">;
export type ProjectDiscussionState = {
  project: Project;
  members: DiscussionMember[];
  comments: ProjectCommentListItem[];
};

export function parseProjectDiscussionMessage(raw: string): ProjectDiscussionServerMessage | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    return isProjectDiscussionServerMessage(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function installDiscussionSnapshot(
  project: Project,
  members: ProjectMember[],
  comments: ProjectCommentListItem[],
  bufferedEvents: PersistentProjectDiscussionEvent[],
): ProjectDiscussionState {
  return bufferedEvents.reduce(
    (state, event) => applyPersistentDiscussionEvent(state, event),
    {
      project,
      members: members.map(({ user_id, username, role }) => ({ user_id, username, role })),
      comments: sortComments(comments),
    },
  );
}

export function applyPersistentDiscussionEvent(
  state: ProjectDiscussionState,
  event: PersistentProjectDiscussionEvent,
): ProjectDiscussionState {
  if (event.type === "comment.created" || event.type === "comment.updated") {
    const existing = state.comments.find((comment) => comment.id === event.comment.id);
    const merged: ProjectCommentListItem = {
      ...event.comment,
      reaction_summary: existing?.reaction_summary ?? emptyReactionSummary(),
    };
    return {
      ...state,
      comments: sortComments([
        ...state.comments.filter((comment) => comment.id !== merged.id),
        merged,
      ]),
    };
  }
  if (event.type === "comment.deleted") {
    return {
      ...state,
      comments: state.comments.filter((comment) => comment.id !== event.comment_id),
    };
  }
  if (event.type === "reaction.updated" || event.type === "reaction.deleted") {
    return {
      ...state,
      comments: state.comments.map((comment) =>
        comment.id === event.comment_id
          ? {
              ...comment,
              reaction_summary: {
                ...comment.reaction_summary,
                support: event.support,
                oppose: event.oppose,
              },
            }
          : comment,
      ),
    };
  }
  if (event.type === "member.joined") {
    return {
      ...state,
      members: upsertMember(state.members, {
        user_id: event.user_id,
        username: event.username,
        role: event.role,
      }),
    };
  }
  if (event.type === "member.removed") {
    return {
      ...state,
      members: state.members.filter((member) => member.user_id !== event.user_id),
    };
  }
  if (event.type === "member.role_changed") {
    return {
      ...state,
      members: upsertMember(state.members, {
        user_id: event.user_id,
        username: event.username,
        role: event.role,
      }),
    };
  }
  if (event.type === "project.ownership_transferred") {
    return {
      ...state,
      project: { ...state.project, owner_user_id: event.new_owner_user_id },
    };
  }
  if (event.type === "project.frozen" || event.type === "project.unfrozen") {
    return { ...state, project: { ...state.project, status: event.status } };
  }
  return state;
}

export function updateCurrentUserReaction(
  state: ProjectDiscussionState,
  commentId: string,
  reaction: ProjectCommentReaction | null,
): ProjectDiscussionState {
  return {
    ...state,
    comments: state.comments.map((comment) =>
      comment.id === commentId
        ? {
            ...comment,
            reaction_summary: {
              ...comment.reaction_summary,
              current_user_reaction: reaction,
            },
          }
        : comment,
    ),
  };
}

export function isReactionSummary(value: unknown): value is ProjectCommentReactionSummary {
  if (!value || typeof value !== "object") return false;
  const summary = value as Partial<ProjectCommentReactionSummary>;
  return (
    typeof summary.support === "number" &&
    typeof summary.oppose === "number" &&
    (summary.current_user_reaction === null ||
      summary.current_user_reaction === "support" ||
      summary.current_user_reaction === "oppose")
  );
}

export function canComment(role: ProjectMemberRole | null | undefined): boolean {
  return role === "commenter" || role === "member" || role === "supervisor";
}

function emptyReactionSummary(): ProjectCommentReactionSummary {
  return { support: 0, oppose: 0, current_user_reaction: null };
}

function sortComments(comments: ProjectCommentListItem[]): ProjectCommentListItem[] {
  return [...comments].sort(
    (left, right) =>
      left.created_at.localeCompare(right.created_at) || left.id.localeCompare(right.id),
  );
}

function upsertMember(
  members: DiscussionMember[],
  member: DiscussionMember,
): DiscussionMember[] {
  return [...members.filter((item) => item.user_id !== member.user_id), member].sort(
    (left, right) =>
      left.username.localeCompare(right.username, undefined, { sensitivity: "base" }) ||
      left.user_id.localeCompare(right.user_id),
  );
}
