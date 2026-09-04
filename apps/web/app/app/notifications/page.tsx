import { NotificationCenter } from "@/components/notifications/NotificationCenter";
import { TranslatedText } from "@/components/TranslatedText";
import { requireCurrentUser } from "@/lib/auth/server";

export default async function NotificationsPage() {
  await requireCurrentUser("/app/notifications");

  return (
    <section className="panel stack">
      <h2 style={{ margin: 0 }}>
        <TranslatedText id="page.notifications.title" />
      </h2>
      <NotificationCenter />
    </section>
  );
}
