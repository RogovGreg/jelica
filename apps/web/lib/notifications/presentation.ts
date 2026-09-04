import { sourceCatalog, type TranslationKey, type Translator } from "@/lib/i18n";
import type { NotificationResource, WebNotification } from "@/types/api";

export function notificationEventLabel(eventId: string, t: Translator): string {
  const key = translationKey(`notification.event.${eventId}.label`);
  return key ? t(key) : eventId;
}

export function notificationEventDescription(
  notification: WebNotification,
  t: Translator,
): string {
  const key = translationKey(`notification.event.${notification.event_id}.description`);
  const template = key ? t(key) : notificationEventLabel(notification.event_id, t);
  return template
    .replaceAll("{actor}", notification.actor_username ?? t("notification.actor.system"))
    .replaceAll(
      "{resource}",
      notification.resource?.display_name ?? t("notification.resource.fallback"),
    );
}

export function notificationCategoryLabel(category: string, t: Translator): string {
  const key = translationKey(`notification.category.${category}`);
  return key ? t(key) : category;
}

export function isSafeNotificationTarget(target: string | null): target is string {
  if (!target || !target.startsWith("/app") || target.startsWith("//")) return false;
  if (target.includes("\\") || /[\u0000-\u001f\u007f]/.test(target)) return false;
  try {
    const parsed = new URL(target, "https://jelica.invalid");
    return (
      parsed.origin === "https://jelica.invalid" &&
      (parsed.pathname === "/app" || parsed.pathname.startsWith("/app/")) &&
      parsed.username === "" &&
      parsed.password === ""
    );
  } catch {
    return false;
  }
}

export function notificationMatchesCurrentContext(
  notification: WebNotification,
  pathname: string,
): boolean {
  const resource = notification.resource;
  if (!resource) return false;
  const context = parseApplicationContext(pathname);
  if (resource.kind === "task_discussion") {
    return context.taskId === resource.task_id && context.section === "discussion";
  }
  if (resource.kind === "task") {
    return context.taskId === resource.task_id && context.section === "task";
  }
  if (!resource.project_id || context.projectId !== resource.project_id) return false;
  if (resource.kind === "project_discussion") return context.section === "discussion";
  if (resource.kind === "project_tasks") return context.section === "tasks";
  return resource.kind === "project";
}

function parseApplicationContext(pathname: string): {
  projectId: string | null;
  taskId: string | null;
  section: string | null;
} {
  const segments = pathname.split("/").filter(Boolean).map(safeDecode);
  if (segments[0] !== "app") return { projectId: null, taskId: null, section: null };
  if (segments[1] === "tasks" && segments[2]) {
    return {
      projectId: null,
      taskId: segments[2],
      section: segments[3] === "discussion" ? "discussion" : "task",
    };
  }
  if (segments[1] === "projects" && segments[2]) {
    const taskIndex = segments[3] === "tasks" && segments[4] ? 4 : -1;
    return {
      projectId: segments[2],
      taskId: taskIndex === 4 ? segments[4] : null,
      section: segments[3] ?? "project",
    };
  }
  return { projectId: null, taskId: null, section: null };
}

function translationKey(value: string): TranslationKey | null {
  return Object.hasOwn(sourceCatalog, value) ? (value as TranslationKey) : null;
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function notificationResourceIdentity(
  resource: NotificationResource | null,
): string | null {
  if (!resource) return null;
  return `${resource.kind}:${resource.project_id ?? ""}:${resource.task_id ?? ""}`;
}
