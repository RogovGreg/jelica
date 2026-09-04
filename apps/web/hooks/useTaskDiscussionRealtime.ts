"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { getProject, getProjectMembers, getTaskDiscussion, getTaskDiscussionComments } from "@/lib/api/client";
import { isResourceUnavailableError } from "@/lib/api/errors";
import { applyTaskDiscussionEvent, canTaskComment, installTaskDiscussionSnapshot, isReactionSummary, parseTaskDiscussionMessage, type TaskDiscussionCommand, type TaskDiscussionPersistentEvent, type TaskDiscussionServerMessage, type TaskDiscussionState } from "@/lib/realtime/taskDiscussion";
import type { AuthUser } from "@/types/api";

export type TaskDiscussionConnectionStatus = "connecting" | "connected" | "reconnecting";
export type TaskDiscussionRestriction = "access-denied" | "resource-unavailable";
export type TaskDiscussionCommandInput = Omit<Extract<TaskDiscussionCommand, { command: "comment.create" | "comment.update" | "comment.delete" | "reaction.set" | "reaction.delete" | "discussion.clear" }>, "id" | "type">;
type Pending = { command: TaskDiscussionCommand["command"]; commentId?: string; onAck?: () => void; onError?: (code: string) => void };

export function useTaskDiscussionRealtime({ taskId, currentUser, onContextChanged }: { taskId: string; currentUser: AuthUser; onContextChanged?: () => void }) {
  const [discussion, setDiscussion] = useState<TaskDiscussionState | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<TaskDiscussionConnectionStatus>("connecting");
  const [snapshotReady, setSnapshotReady] = useState(false);
  const [restriction, setRestriction] = useState<TaskDiscussionRestriction | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [presence, setPresence] = useState<Array<{ user_id: string; username: string }>>([]);
  const [typingUsers, setTypingUsers] = useState<Array<{ user_id: string; username: string }>>([]);
  const [protocolError, setProtocolError] = useState(false);
  const discussionRef = useRef<TaskDiscussionState | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const pendingRef = useRef(new Map<string, Pending>());
  const reconnectRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const terminalRef = useRef(false);
  const mountedRef = useRef(true);
  const bufferingRef = useRef(false);
  const bufferRef = useRef<TaskDiscussionPersistentEvent[]>([]);
  const lastTypingRef = useRef(0);
  const availabilityRef = useRef<boolean | null>(null);

  const replace = useCallback((next: TaskDiscussionState | null) => { discussionRef.current = next; setDiscussion(next); }, []);
  const clearPending = useCallback((code: string) => { for (const item of pendingRef.current.values()) item.onError?.(code); pendingRef.current.clear(); }, []);
  const restrict = useCallback((kind: TaskDiscussionRestriction) => {
    terminalRef.current = true;
    clearPending(kind === "access-denied" ? "access_revoked" : "resource_unavailable");
    setRestriction(kind); setSnapshotReady(false); setPresence([]); setTypingUsers([]); replace(null);
    const socket = socketRef.current; socketRef.current = null; if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000);
  }, [clearPending, replace]);

  useEffect(() => {
    mountedRef.current = true; terminalRef.current = false; setRestriction(null); setUnavailable(false); setProtocolError(false);
    availabilityRef.current = null;
    let cancelled = false;
    let connectSocket: (() => void) | null = null;
    const loadSnapshot = async (socket?: WebSocket) => {
      try {
        const metadata = await getTaskDiscussion(taskId);
        if (cancelled || terminalRef.current) return;
        if (!metadata.available) {
          availabilityRef.current = false;
          setUnavailable(true); setSnapshotReady(true); setConnectionStatus("connected");
          replace(installTaskDiscussionSnapshot(metadata, [], [], null, null, []));
          return;
        }
        availabilityRef.current = true;
        const [comments, projectData] = await Promise.all([
          getTaskDiscussionComments(taskId),
          metadata.project_id ? Promise.all([getProject(metadata.project_id), getProjectMembers(metadata.project_id)]) : Promise.resolve(null),
        ]);
        if (cancelled || terminalRef.current) return;
        const project = projectData ? projectData[0] : null;
        const members = projectData ? projectData[1].items : [];
        const buffered = bufferRef.current.splice(0);
        replace(installTaskDiscussionSnapshot(metadata, comments.items, members, project?.name ?? null, project?.status ?? null, buffered));
        bufferingRef.current = false; setSnapshotReady(true); setConnectionStatus("connected"); reconnectRef.current = 0;
        if (!socket) connectSocket?.();
      } catch (error) {
        if (cancelled || terminalRef.current) return;
        if (isResourceUnavailableError(error)) { restrict("resource-unavailable"); return; }
        if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
      }
    };
    const scheduleReconnect = () => {
      if (cancelled || terminalRef.current || reconnectTimerRef.current !== null) return;
      const delay = [1000, 2000, 4000, 8000, 10000][Math.min(reconnectRef.current, 4)]; reconnectRef.current += 1;
      reconnectTimerRef.current = window.setTimeout(() => { reconnectTimerRef.current = null; connectSocket?.(); }, delay);
    };
    const handleMessage = (message: TaskDiscussionServerMessage) => {
      if (message.type === "presence.snapshot") { setPresence(unique(message.users)); return; }
      if (message.type === "presence.joined") { setPresence((items) => unique([...items, message.user])); return; }
      if (message.type === "presence.left") { setPresence((items) => items.filter((item) => item.user_id !== message.user.user_id)); setTypingUsers((items) => items.filter((item) => item.user_id !== message.user.user_id)); return; }
      if (message.type === "typing.started") { if (message.user.user_id !== currentUser.id) setTypingUsers((items) => unique([...items, message.user])); return; }
      if (message.type === "typing.stopped") { setTypingUsers((items) => items.filter((item) => item.user_id !== message.user.user_id)); return; }
      if (message.type === "protocol.error") { setProtocolError(true); return; }
      if (message.type === "command.ack") {
        const item = pendingRef.current.get(message.id); if (!item) return;
        if (item.commentId && isReactionSummary(message.result)) replace(discussionRef.current ? { ...discussionRef.current, comments: discussionRef.current.comments.map((comment) => comment.id === item.commentId ? { ...comment, reaction_summary: message.result as TaskDiscussionState["comments"][number]["reaction_summary"] } : comment) } : null);
        pendingRef.current.delete(message.id); item.onAck?.(); return;
      }
      if (message.type === "command.error") { const item = pendingRef.current.get(message.id); pendingRef.current.delete(message.id); item?.onError?.(message.error.code); return; }
      if (message.type === "access.revoked") { restrict("access-denied"); return; }
      if (message.type === "task.context_changed") {
        terminalRef.current = true;
        clearPending("context_changed");
        bufferingRef.current = false;
        bufferRef.current = [];
        setSnapshotReady(false); setPresence([]); setTypingUsers([]); replace(null);
        const socket = socketRef.current; socketRef.current = null;
        if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000);
        onContextChanged?.();
        return;
      }
      if (message.type === "discussion.cleared" || message.type.startsWith("comment.") || message.type.startsWith("reaction.") || message.type.startsWith("member.") || message.type.startsWith("project.")) {
        if (bufferingRef.current) bufferRef.current.push(message as TaskDiscussionPersistentEvent); else if (discussionRef.current) replace(applyTaskDiscussionEvent(discussionRef.current, message as TaskDiscussionPersistentEvent));
      }
    };
    connectSocket = () => {
      if (cancelled || terminalRef.current || unavailable || availabilityRef.current === false || socketRef.current) return;
      setConnectionStatus(reconnectRef.current === 0 ? "connecting" : "reconnecting"); setSnapshotReady(false);
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(`${protocol}//${window.location.host}/api/tasks/${encodeURIComponent(taskId)}/realtime`); socketRef.current = socket;
      socket.onopen = () => { bufferingRef.current = true; bufferRef.current = []; void loadSnapshot(socket); };
      socket.onmessage = (event) => { if (typeof event.data === "string") { const message = parseTaskDiscussionMessage(event.data); if (message) handleMessage(message); } };
      socket.onclose = (event) => {
        if (socketRef.current === socket) socketRef.current = null;
        if (cancelled || terminalRef.current) return;
        clearPending("realtime_unavailable"); bufferingRef.current = false; setSnapshotReady(false); setConnectionStatus("reconnecting"); setPresence([]); setTypingUsers([]);
        if (event.code === 4403) { restrict("access-denied"); return; }
        if (event.code === 4404) { restrict("resource-unavailable"); return; }
        if (event.code === 4410) { onContextChanged?.(); return; }
        if (event.code === 4401) { window.location.reload(); return; }
        scheduleReconnect();
      };
    };
    void loadSnapshot();
    const timer = window.setTimeout(() => { if (availabilityRef.current === true && !terminalRef.current) connectSocket?.(); }, 500);
    return () => { cancelled = true; mountedRef.current = false; terminalRef.current = true; window.clearTimeout(timer); if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current); clearPending("realtime_unavailable"); const socket = socketRef.current; socketRef.current = null; if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000); };
  }, [clearPending, currentUser.id, restrict, replace, taskId, onContextChanged]);

  const sendCommand = useCallback((input: Omit<TaskDiscussionCommand, "id" | "type">, options: Omit<Pending, "command"> = {}): string | null => {
    const socket = socketRef.current; if (!snapshotReady || connectionStatus !== "connected" || socket?.readyState !== WebSocket.OPEN) { options.onError?.("realtime_unavailable"); return null; }
    const id = crypto.randomUUID(); pendingRef.current.set(id, { command: input.command, ...options }); socket.send(JSON.stringify({ type: "command", id, ...input })); return id;
  }, [connectionStatus, snapshotReady]);
  const notifyTyping = useCallback(() => { const socket = socketRef.current; const state = discussionRef.current; const role = state?.members.find((member) => member.user_id === currentUser.id)?.role; if (!socket || socket.readyState !== WebSocket.OPEN || !canTaskComment(role) || state?.projectStatus !== "active") return; if (Date.now() - lastTypingRef.current < 2500) return; lastTypingRef.current = Date.now(); socket.send(JSON.stringify({ type: "typing.start" })); }, [currentUser.id]);
  const stopTyping = useCallback(() => { lastTypingRef.current = 0; if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: "typing.stop" })); }, []);
  return { discussion, connectionStatus, snapshotReady, restriction, unavailable, presence, typingUsers, protocolError, sendCommand, notifyTyping, stopTyping };
}

function unique(items: Array<{ user_id: string; username: string }>) { return [...new Map(items.map((item) => [item.user_id, item])).values()].sort((a, b) => a.username.localeCompare(b.username, undefined, { sensitivity: "base" }) || a.user_id.localeCompare(b.user_id)); }
