import type {
  NotificationRealtimeMessage,
  WebNotification,
} from "@/types/api";

export type NotificationListFilter = "all" | "unread";

export function installNotificationSnapshot(
  snapshot: readonly WebNotification[],
  bufferedEvents: readonly NotificationRealtimeMessage[],
): WebNotification[] {
  return bufferedEvents.reduce(
    (items, event) => applyNotificationRealtimeEvent(items, event),
    sortNotifications(snapshot),
  );
}

export function applyNotificationRealtimeEvent(
  current: readonly WebNotification[],
  event: NotificationRealtimeMessage,
): WebNotification[] {
  if (event.type === "notification.created") {
    return sortNotifications([
      ...current.filter((item) => item.id !== event.notification.id),
      event.notification,
    ]);
  }
  if (event.type === "notification.read_changed") {
    return current.map((item) =>
      item.id === event.notification_id ? { ...item, read_at: event.read_at } : item,
    );
  }
  return current.map((item) =>
    item.read_at === null ? { ...item, read_at: event.read_at } : item,
  );
}

export function filterNotifications(
  items: readonly WebNotification[],
  listFilter: NotificationListFilter,
  eventId: string,
): WebNotification[] {
  return items.filter(
    (item) =>
      (listFilter === "all" || item.read_at === null) &&
      (eventId === "" || item.event_id === eventId),
  );
}

export function unreadNotificationCount(items: readonly WebNotification[]): number {
  return items.reduce((count, item) => count + (item.read_at === null ? 1 : 0), 0);
}

export function formatUnreadBadge(count: number): string {
  if (count <= 0) return "";
  return count > 99 ? "99+" : String(count);
}

function sortNotifications(items: readonly WebNotification[]): WebNotification[] {
  return [...items].sort(
    (left, right) =>
      right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id),
  );
}
