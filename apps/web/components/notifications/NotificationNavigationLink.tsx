"use client";

import Link from "next/link";

import { useI18n } from "@/components/I18nProvider";
import { useNotifications } from "@/components/notifications/NotificationProvider";
import { formatUnreadBadge } from "@/lib/notifications/state";

export function NotificationNavigationLink() {
  const { t } = useI18n();
  const { badgeVisible, unreadCount } = useNotifications();
  return (
    <Link href="/app/notifications" className="notification-nav-link">
      <span>{t("page.notifications.title")}</span>
      {badgeVisible ? (
        <span
          className="notification-nav-badge"
          aria-label={t("notification.badge.unread").replace("{count}", String(unreadCount))}
        >
          {formatUnreadBadge(unreadCount)}
        </span>
      ) : null}
    </Link>
  );
}
