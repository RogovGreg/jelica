"use client";

import { useRouter } from "next/navigation";

import { useI18n } from "@/components/I18nProvider";
import { useNotifications } from "@/components/notifications/NotificationProvider";
import {
  isSafeNotificationTarget,
  notificationEventDescription,
  notificationEventLabel,
} from "@/lib/notifications/presentation";

export function NotificationToastViewport() {
  const router = useRouter();
  const { t } = useI18n();
  const { toasts, dismissToast, markReadState } = useNotifications();

  async function open(toastId: string, notificationId: string, targetPath: string) {
    try {
      await markReadState(notificationId, true);
    } catch {
      // Resource navigation remains useful even if the read-state request failed.
    }
    dismissToast(toastId);
    router.push(targetPath);
  }

  return (
    <aside className="notification-toast-viewport" aria-live="polite" aria-relevant="additions">
      {toasts.map(({ id, notification }) => {
        const target = isSafeNotificationTarget(notification.target_path)
          ? notification.target_path
          : null;
        return (
          <section className="notification-toast" role="status" key={id}>
            <div className="stack" style={{ gap: "0.3rem" }}>
              <strong>{notificationEventLabel(notification.event_id, t)}</strong>
              <span>{notificationEventDescription(notification, t)}</span>
            </div>
            <div className="notification-toast-actions">
              {target ? (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => void open(id, notification.id, target)}
                >
                  {t("notification.action.open")}
                </button>
              ) : null}
              <button type="button" className="text-button" onClick={() => dismissToast(id)}>
                {t("notification.action.dismiss")}
              </button>
            </div>
          </section>
        );
      })}
    </aside>
  );
}
