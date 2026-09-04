import Link from "next/link";
import type { ReactNode } from "react";
import type { Metadata } from "next";

import { TranslatedText } from "@/components/TranslatedText";

export const metadata: Metadata = {
  title: { default: "Account", template: "%s | JELICA" },
  robots: { index: false, follow: false },
};

type AuthLayoutProps = Readonly<{
  children: ReactNode;
}>;

export default function AuthLayout({ children }: AuthLayoutProps) {
  return (
    <section className="stack">
      <header className="panel stack" style={{ gap: "0.65rem" }}>
        <h1 id="auth-navigation-title" style={{ margin: 0 }}>
          <TranslatedText id="auth.navigation.title" />
        </h1>
        <p className="muted" style={{ margin: 0 }}>
          <TranslatedText id="auth.navigation.description" />
        </p>
        <nav className="app-nav" aria-labelledby="auth-navigation-title">
          <Link href="/auth/login">
            <TranslatedText id="auth.action.login" />
          </Link>
          <Link href="/auth/register">
            <TranslatedText id="auth.action.register" />
          </Link>
        </nav>
      </header>
      {children}
    </section>
  );
}
