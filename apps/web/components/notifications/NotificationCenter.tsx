"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { useI18n } from "@/components/I18nProvider";
import { useNotifications } from "@/components/notifications/NotificationProvider";
import {
  isSafeNotificationTarget,
  notificationCategoryLabel,
  notificationEventDescription,
  notificationEventLabel,
} from "@/lib/notifications/presentation";
import {
  filterNotifications,
  type NotificationListFilter,
} from "@/lib/notifications/state";
import type { Locale } from "@/lib/i18n";
import type { WebNotification } from "@/types/api";
import { activeNotificationEventCatalog } from "../../../../packages/app-platform/src/notification-events";

const WEB_EVENTS = activeNotificationEventCatalog.filter(
  (event) => event.scope === "web" || event.scope === "both",
);

export function NotificationCenter() {
  const router = useRouter();
  const { locale, t } = useI18n();
  const {
    notifications,
    preferences,
    loading,
    loadFailed,
    reload,
    markReadState,
    markAllRead,
  } = useNotifications();
  const [listFilter, setListFilter] = useState<NotificationListFilter>("all");
  const [eventFilter, setEventFilter] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionFailed, setActionFailed] = useState(false);
  const visible = filterNotifications(notifications, listFilter, eventFilter);
  const groupedEvents = useMemo(() => groupEvents(), []);
  const globallyDisabled = preferences !== null && !preferences.enabled;

  async function updateReadState(notificationId: string, read: boolean) {
    setBusy(notificationId);
    setActionFailed(false);
    try {
      await markReadState(notificationId, read);
    } catch {
      setActionFailed(true);
    } finally {
      setBusy(null);
    }
  }

  async function openNotification(notification: WebNotification, target: string) {
    setBusy(notification.id);
    setActionFailed(false);
    try {
      await markReadState(notification.id, true);
    } catch {
      setActionFailed(true);
    } finally {
      setBusy(null);
      router.push(target);
    }
  }

  async function markEverythingRead() {
    setBusy("all");
    setActionFailed(false);
    try {
      await markAllRead();
    } catch {
      setActionFailed(true);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="stack notification-center">
      {globallyDisabled ? (
        <div className="state-box state-warning" role="status">
          {t("notification.center.globally-disabled")}
        </div>
      ) : null}
      <div className="notification-center-toolbar">
        <div className="notification-filter-tabs" role="group" aria-label={t("notification.filter.status")}>
          <button
            type="button"
            className={listFilter === "all" ? "active" : undefined}
            aria-pressed={listFilter === "all"}
            onClick={() => setListFilter("all")}
          >
            {t("notification.filter.all")}
          </button>
          <button
            type="button"
            className={listFilter === "unread" ? "active" : undefined}
            aria-pressed={listFilter === "unread"}
            onClick={() => setListFilter("unread")}
          >
            {t("notification.filter.unread")}
          </button>
        </div>
        <label className="input-field notification-event-filter" htmlFor="notification-event-filter">
          <span>{t("notification.filter.event")}</span>
          <select
            id="notification-event-filter"
            value={eventFilter}
            onChange={(event) => setEventFilter(event.target.value)}
          >
            <option value="">{t("notification.filter.any-event")}</option>
            {groupedEvents.map(([category, events]) => (
              <optgroup key={category} label={notificationCategoryLabel(category, t)}>
                {events.map((event) => (
                  <option key={event.id} value={event.id}>
                    {notificationEventLabel(event.id, t)}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="secondary-button"
          disabled={busy !== null || !notifications.some((item) => item.read_at === null)}
          onClick={() => void markEverythingRead()}
        >
          {busy === "all" ? t("common.state.loading") : t("notification.action.mark-all-read")}
        </button>
      </div>

      {actionFailed ? (
        <div className="state-box state-error" role="alert">
          {t("notification.center.action-failed")}
        </div>
      ) : null}
      {loadFailed ? (
        <div className="state-box state-error stack" role="alert">
          <span>{t("notification.center.load-failed")}</span>
          <button type="button" className="secondary-button" onClick={() => void reload()}>
            {t("common.action.retry")}
          </button>
        </div>
      ) : loading && notifications.length === 0 ? (
        <div className="state-box" aria-busy="true">
          {t("common.state.loading")}
        </div>
      ) : visible.length > 0 ? (
        <ol className="notification-list" aria-label={t("notification.center.list-label")}>
          {visible.map((notification) => (
            <NotificationRow
              key={notification.id}
              notification={notification}
              locale={locale}
              busy={busy === notification.id}
              onReadState={updateReadState}
              onOpen={openNotification}
            />
          ))}
        </ol>
      ) : (
        <div className="state-box">
          {emptyState(notifications, listFilter, eventFilter, t)}
        </div>
      )}
    </div>
  );
}

function NotificationRow({
  notification,
  locale,
  busy,
  onReadState,
  onOpen,
}: Readonly<{
  notification: WebNotification;
  locale: Locale;
  busy: boolean;
  onReadState: (notificationId: string, read: boolean) => Promise<void>;
  onOpen: (notification: WebNotification, target: string) => Promise<void>;
}>) {
  const { t } = useI18n();
  const unread = notification.read_at === null;
  const target = isSafeNotificationTarget(notification.target_path)
    ? notification.target_path
    : null;
  return (
    <li>
      <article className={`notification-row${unread ? " notification-row-unread" : ""}`}>
        <button
          type="button"
          className="notification-row-main"
          disabled={busy}
          onClick={() => void onReadState(notification.id, true)}
          aria-label={
            unread
              ? t("notification.action.mark-read")
              : notificationEventLabel(notification.event_id, t)
          }
        >
          <span className="notification-row-heading">
            <strong>{notificationEventLabel(notification.event_id, t)}</strong>
            <span className="notification-read-state">
              {t(unread ? "notification.state.unread" : "notification.state.read")}
            </span>
          </span>
          <span>{notificationEventDescription(notification, t)}</span>
          <span className="notification-row-context muted">
            {notification.actor_username ? `${notification.actor_username} · ` : ""}
            {notification.resource?.display_name ?? notificationCategoryLabel(notification.category, t)}
          </span>
          <time className="muted" dateTime={notification.created_at}>
            {formatTimestamp(notification.created_at, locale)}
          </time>
        </button>
        <div className="notification-row-actions">
          {target ? (
            <button
              type="button"
              className="secondary-button"
              disabled={busy}
              onClick={() => void onOpen(notification, target)}
            >
              {t("notification.action.open")}
            </button>
          ) : null}
          <button
            type="button"
            className="text-button"
            disabled={busy}
            onClick={() => void onReadState(notification.id, !unread)}
          >
            {t(unread ? "notification.action.mark-read" : "notification.action.mark-unread")}
          </button>
        </div>
      </article>
    </li>
  );
}

function groupEvents() {
  const groups = new Map<string, typeof WEB_EVENTS[number][]>();
  WEB_EVENTS.forEach((event) => groups.set(event.category, [...(groups.get(event.category) ?? []), event]));
  return [...groups.entries()];
}

function emptyState(
  notifications: readonly WebNotification[],
  listFilter: NotificationListFilter,
  eventFilter: string,
  t: ReturnType<typeof useI18n>["t"],
): string {
  if (notifications.length === 0) return t("notification.empty.none-yet");
  if (eventFilter && !notifications.some((item) => item.event_id === eventFilter)) {
    return t("notification.empty.no-event-match");
  }
  if (listFilter === "unread") return t("notification.empty.no-unread");
  return t("notification.empty.no-event-match");
}

function formatTimestamp(value: string, locale: Locale): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}
