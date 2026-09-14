import type { ReactNode } from "react";

import { NotificationProvider } from "@/components/notifications/NotificationProvider";
import { NotificationToastViewport } from "@/components/notifications/NotificationToastViewport";

export default function SettingsLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <NotificationProvider>
      <div className="stack">{children}</div>
      <NotificationToastViewport />
    </NotificationProvider>
  );
}
