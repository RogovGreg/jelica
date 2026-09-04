import type { NotificationRealtimeMessage, WebNotification } from "@/types/api";

export function parseNotificationRealtimeMessage(
  raw: string,
): NotificationRealtimeMessage | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || typeof value.type !== "string") return null;
    if (value.type === "notification.created" && isWebNotification(value.notification)) {
      return { type: value.type, notification: value.notification };
    }
    if (
      value.type === "notification.read_changed" &&
      typeof value.notification_id === "string" &&
      (value.read_at === null || typeof value.read_at === "string")
    ) {
      return {
        type: value.type,
        notification_id: value.notification_id,
        read_at: value.read_at,
      };
    }
    if (value.type === "notifications.all_read" && typeof value.read_at === "string") {
      return { type: value.type, read_at: value.read_at };
    }
    return null;
  } catch {
    return null;
  }
}

function isWebNotification(value: unknown): value is WebNotification {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === "string" &&
    typeof value.event_id === "string" &&
    typeof value.category === "string" &&
    (value.actor_username === null || typeof value.actor_username === "string") &&
    (value.resource === null || isNotificationResource(value.resource)) &&
    typeof value.created_at === "string" &&
    (value.read_at === null || typeof value.read_at === "string") &&
    (value.target_path === null || typeof value.target_path === "string")
  );
}

function isNotificationResource(value: unknown): boolean {
  if (!isRecord(value) || typeof value.kind !== "string") return false;
  return ["task", "project", "project_tasks", "project_discussion", "task_discussion", "invitation"].includes(
    value.kind,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
