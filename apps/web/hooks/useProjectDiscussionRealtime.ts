"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { getProject, getProjectComments, getProjectMembers } from "@/lib/api/client";
import { isResourceUnavailableError } from "@/lib/api/errors";
import {
  applyPersistentDiscussionEvent,
  canComment,
  installDiscussionSnapshot,
  isReactionSummary,
  parseProjectDiscussionMessage,
  updateCurrentUserReaction,
  type ProjectDiscussionState,
} from "@/lib/realtime/projectDiscussion";
import {
  isPersistentProjectDiscussionEvent,
  type PersistentProjectDiscussionEvent,
  type ProjectDiscussionCommand,
  type ProjectDiscussionServerMessage,
  type RealtimeUser,
} from "@/lib/realtime/types";
import type { AuthUser, Project } from "@/types/api";

export type DiscussionConnectionStatus = "connecting" | "connected" | "reconnecting";
export type DiscussionRestriction = "access-denied" | "resource-unavailable";
export type DiscussionCommandInput =
  | Omit<Extract<ProjectDiscussionCommand, { command: "comment.create" }>, "id" | "type">
  | Omit<Extract<ProjectDiscussionCommand, { command: "comment.update" }>, "id" | "type">
  | Omit<Extract<ProjectDiscussionCommand, { command: "comment.delete" }>, "id" | "type">
  | Omit<Extract<ProjectDiscussionCommand, { command: "reaction.set" }>, "id" | "type">
  | Omit<Extract<ProjectDiscussionCommand, { command: "reaction.delete" }>, "id" | "type">;

type PendingCommand = {
  command: ProjectDiscussionCommand["command"];
  commentId?: string;
  onAck?: () => void;
  onError?: (code: string) => void;
};

type SendOptions = Omit<PendingCommand, "command">;

export function useProjectDiscussionRealtime({
  projectId,
  initialProject,
  currentUser,
}: {
  projectId: string;
  initialProject: Project;
  currentUser: AuthUser;
}) {
  const initialState: ProjectDiscussionState = {
    project: initialProject,
    members: [],
    comments: [],
  };
  const [discussion, setDiscussion] = useState<ProjectDiscussionState | null>(initialState);
  const [connectionStatus, setConnectionStatus] =
    useState<DiscussionConnectionStatus>("connecting");
  const [snapshotReady, setSnapshotReady] = useState(false);
  const [restriction, setRestriction] = useState<DiscussionRestriction | null>(null);
  const [presence, setPresence] = useState<RealtimeUser[]>([]);
  const [typingUsers, setTypingUsers] = useState<RealtimeUser[]>([]);
  const [protocolError, setProtocolError] = useState<string | null>(null);

  const discussionRef = useRef<ProjectDiscussionState | null>(initialState);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);
  const pendingRef = useRef(new Map<string, PendingCommand>());
  const bufferRef = useRef<PersistentProjectDiscussionEvent[]>([]);
  const bufferingRef = useRef(false);
  const mountedRef = useRef(true);
  const terminalRef = useRef(false);
  const deliberateCloseRef = useRef(false);
  const hasSnapshotRef = useRef(false);
  const lastTypingStartRef = useRef(0);

  const replaceDiscussion = useCallback((next: ProjectDiscussionState | null) => {
    discussionRef.current = next;
    setDiscussion(next);
  }, []);

  const clearPending = useCallback((code: string) => {
    for (const pending of pendingRef.current.values()) pending.onError?.(code);
    pendingRef.current.clear();
  }, []);

  const enterRestrictedState = useCallback(
    (nextRestriction: DiscussionRestriction) => {
      terminalRef.current = true;
      deliberateCloseRef.current = true;
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
      clearPending(nextRestriction === "access-denied" ? "access_revoked" : "resource_unavailable");
      bufferRef.current = [];
      bufferingRef.current = false;
      setPresence([]);
      setTypingUsers([]);
      setSnapshotReady(false);
      replaceDiscussion(null);
      setRestriction(nextRestriction);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000);
    },
    [clearPending, replaceDiscussion],
  );

  const applyPersistentEvent = useCallback(
    (event: PersistentProjectDiscussionEvent) => {
      if (event.type === "access.revoked") {
        enterRestrictedState("access-denied");
        return;
      }
      if (event.type === "project.deleted") {
        enterRestrictedState("resource-unavailable");
        return;
      }
      const current = discussionRef.current;
      if (!current) return;
      const next = applyPersistentDiscussionEvent(current, event);
      replaceDiscussion(next);
      if (
        (event.type === "member.role_changed" &&
          event.user_id === currentUser.id &&
          !canComment(event.role)) ||
        event.type === "project.frozen"
      ) {
        const socket = socketRef.current;
        if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "typing.stop" }));
      }
    },
    [currentUser.id, enterRestrictedState, replaceDiscussion],
  );

  const handleServerMessage = useCallback(
    (message: ProjectDiscussionServerMessage) => {
      if (message.type === "command.ack") {
        const pending = pendingRef.current.get(message.id);
        if (!pending) return;
        if (
          pending.commentId &&
          (pending.command === "reaction.set" || pending.command === "reaction.delete") &&
          isReactionSummary(message.result)
        ) {
          const current = discussionRef.current;
          if (current) {
            replaceDiscussion(
              updateCurrentUserReaction(
                current,
                pending.commentId,
                message.result.current_user_reaction,
              ),
            );
          }
        }
        pendingRef.current.delete(message.id);
        pending.onAck?.();
        return;
      }
      if (message.type === "command.error") {
        const pending = pendingRef.current.get(message.id);
        pendingRef.current.delete(message.id);
        pending?.onError?.(message.error.code);
        return;
      }
      if (message.type === "protocol.error") {
        setProtocolError(message.error.code);
        return;
      }
      if (message.type === "presence.snapshot") {
        setPresence(uniqueUsers(message.users));
        return;
      }
      if (message.type === "presence.joined") {
        setPresence((users) => uniqueUsers([...users, message.user]));
        return;
      }
      if (message.type === "presence.left") {
        setPresence((users) => users.filter((user) => user.user_id !== message.user.user_id));
        setTypingUsers((users) =>
          users.filter((user) => user.user_id !== message.user.user_id),
        );
        return;
      }
      if (message.type === "typing.started") {
        if (message.user.user_id !== currentUser.id) {
          setTypingUsers((users) => uniqueUsers([...users, message.user]));
        }
        return;
      }
      if (message.type === "typing.stopped") {
        setTypingUsers((users) =>
          users.filter((user) => user.user_id !== message.user.user_id),
        );
        return;
      }
      if (isPersistentProjectDiscussionEvent(message)) {
        if (message.type === "access.revoked" || message.type === "project.deleted") {
          applyPersistentEvent(message);
        } else if (bufferingRef.current) {
          bufferRef.current.push(message);
        } else {
          applyPersistentEvent(message);
        }
      }
    },
    [applyPersistentEvent, currentUser.id, replaceDiscussion],
  );

  useEffect(() => {
    mountedRef.current = true;
    terminalRef.current = false;
    deliberateCloseRef.current = false;

    const installFreshSnapshot = async (socket: WebSocket) => {
      try {
        const [project, members, comments] = await Promise.all([
          getProject(projectId),
          getProjectMembers(projectId),
          getProjectComments(projectId),
        ]);
        if (!mountedRef.current || socket !== socketRef.current || terminalRef.current) return;
        const buffered = bufferRef.current.splice(0);
        const next = installDiscussionSnapshot(project, members.items, comments.items, buffered);
        bufferingRef.current = false;
        hasSnapshotRef.current = true;
        replaceDiscussion(next);
        reconnectAttemptRef.current = 0;
        setSnapshotReady(true);
        setConnectionStatus("connected");
      } catch (error) {
        if (!mountedRef.current || socket !== socketRef.current || terminalRef.current) return;
        if (isResourceUnavailableError(error)) {
          enterRestrictedState("access-denied");
          return;
        }
        socket.close();
      }
    };

    const installReadOnlySnapshot = async () => {
      if (hasSnapshotRef.current || terminalRef.current) return;
      try {
        const [project, members, comments] = await Promise.all([
          getProject(projectId),
          getProjectMembers(projectId),
          getProjectComments(projectId),
        ]);
        if (!mountedRef.current || terminalRef.current || hasSnapshotRef.current) return;
        replaceDiscussion(installDiscussionSnapshot(project, members.items, comments.items, []));
        hasSnapshotRef.current = true;
      } catch (error) {
        if (isResourceUnavailableError(error)) enterRestrictedState("access-denied");
      }
    };

    const scheduleReconnect = () => {
      if (!mountedRef.current || terminalRef.current || reconnectTimerRef.current !== null) return;
      const delays = [1000, 2000, 4000, 8000, 10000];
      const delay = delays[Math.min(reconnectAttemptRef.current, delays.length - 1)];
      reconnectAttemptRef.current += 1;
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null;
        connect();
      }, delay);
    };

    const verifyUnavailableClose = async () => {
      try {
        await getProject(projectId);
        scheduleReconnect();
      } catch (error) {
        if (isResourceUnavailableError(error)) enterRestrictedState("access-denied");
        else scheduleReconnect();
      }
    };

    const connect = () => {
      if (!mountedRef.current || terminalRef.current) return;
      deliberateCloseRef.current = false;
      setConnectionStatus(reconnectAttemptRef.current === 0 ? "connecting" : "reconnecting");
      setSnapshotReady(false);
      setPresence([]);
      setTypingUsers([]);
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(
        `${protocol}//${window.location.host}/api/projects/${encodeURIComponent(projectId)}/realtime`,
      );
      socketRef.current = socket;
      socket.onopen = () => {
        if (!mountedRef.current || socket !== socketRef.current || terminalRef.current) return;
        bufferRef.current = [];
        bufferingRef.current = true;
        void installFreshSnapshot(socket);
      };
      socket.onmessage = (event) => {
        if (typeof event.data !== "string") return;
        const message = parseProjectDiscussionMessage(event.data);
        if (message) handleServerMessage(message);
      };
      socket.onclose = (event) => {
        if (socket === socketRef.current) socketRef.current = null;
        if (!mountedRef.current || terminalRef.current || deliberateCloseRef.current) return;
        bufferingRef.current = false;
        bufferRef.current = [];
        setSnapshotReady(false);
        setConnectionStatus("reconnecting");
        setPresence([]);
        setTypingUsers([]);
        clearPending("realtime_unavailable");
        if (event.code === 4403) {
          enterRestrictedState("access-denied");
          return;
        }
        if (event.code === 4401) {
          terminalRef.current = true;
          window.location.reload();
          return;
        }
        if (event.code === 4404) {
          void verifyUnavailableClose();
          return;
        }
        void installReadOnlySnapshot();
        scheduleReconnect();
      };
    };

    connect();
    return () => {
      mountedRef.current = false;
      deliberateCloseRef.current = true;
      terminalRef.current = true;
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
      clearPending("realtime_unavailable");
      bufferRef.current = [];
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "typing.stop" }));
        socket.close(1000);
      } else if (socket && socket.readyState < WebSocket.CLOSING) {
        socket.close(1000);
      }
    };
  }, [
    clearPending,
    enterRestrictedState,
    handleServerMessage,
    projectId,
    replaceDiscussion,
  ]);

  const sendCommand = useCallback(
    (input: DiscussionCommandInput, options: SendOptions = {}): string | null => {
      const socket = socketRef.current;
      if (
        !snapshotReady ||
        connectionStatus !== "connected" ||
        socket?.readyState !== WebSocket.OPEN
      ) {
        options.onError?.("realtime_unavailable");
        return null;
      }
      const id = crypto.randomUUID();
      const command = { type: "command", id, ...input } as ProjectDiscussionCommand;
      pendingRef.current.set(id, { command: input.command, ...options });
      socket.send(JSON.stringify(command));
      return id;
    },
    [connectionStatus, snapshotReady],
  );

  const notifyTyping = useCallback(() => {
    const socket = socketRef.current;
    const current = discussionRef.current;
    const role = current?.members.find((member) => member.user_id === currentUser.id)?.role;
    if (
      !snapshotReady ||
      current?.project.status !== "active" ||
      !canComment(role) ||
      socket?.readyState !== WebSocket.OPEN
    ) {
      return;
    }
    const now = Date.now();
    if (now - lastTypingStartRef.current < 2500) return;
    lastTypingStartRef.current = now;
    socket.send(JSON.stringify({ type: "typing.start" }));
  }, [currentUser.id, snapshotReady]);

  const stopTyping = useCallback(() => {
    const socket = socketRef.current;
    lastTypingStartRef.current = 0;
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "typing.stop" }));
    }
  }, []);

  return {
    discussion,
    connectionStatus,
    snapshotReady,
    restriction,
    presence,
    typingUsers,
    protocolError,
    sendCommand,
    notifyTyping,
    stopTyping,
  };
}

function uniqueUsers(users: RealtimeUser[]): RealtimeUser[] {
  return [...new Map(users.map((user) => [user.user_id, user])).values()].sort(
    (left, right) =>
      left.username.localeCompare(right.username, undefined, { sensitivity: "base" }) ||
      left.user_id.localeCompare(right.user_id),
  );
}
