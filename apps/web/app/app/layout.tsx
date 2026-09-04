import type { ReactNode } from "react";
import type { Metadata } from "next";

import { ApplicationNavigation } from "@/components/ApplicationNavigation";
import { NotificationProvider } from "@/components/notifications/NotificationProvider";
import { NotificationToastViewport } from "@/components/notifications/NotificationToastViewport";
import { TranslatedText } from "@/components/TranslatedText";

export const metadata: Metadata = {
  title: { default: "JELICA Web", template: "%s | JELICA" },
  robots: { index: false, follow: false },
};

type ApplicationLayoutProps = Readonly<{
  children: ReactNode;
}>;

export default function ApplicationLayout({ children }: ApplicationLayoutProps) {
  return (
    <NotificationProvider>
      <section className="app-shell-area stack">
        <header className="panel stack" style={{ gap: "0.7rem" }}>
          <div>
            <h1 style={{ margin: 0 }}><TranslatedText id="app.shell.title" /></h1>
            <p className="muted" style={{ margin: "0.35rem 0 0" }}>
              <TranslatedText id="app.shell.description" />
            </p>
          </div>
          <ApplicationNavigation />
        </header>
        <div className="stack">{children}</div>
        <footer className="panel app-shell-footer">
          <span><TranslatedText id="app.shell.footer" /></span>
        </footer>
      </section>
      <NotificationToastViewport />
    </NotificationProvider>
  );
}
