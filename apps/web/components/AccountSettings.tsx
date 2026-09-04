"use client";

import { useEffect, useState } from "react";

import { useI18n } from "@/components/I18nProvider";
import { InterfaceScaleSwitcher } from "@/components/InterfaceScaleSwitcher";
import { LocaleSwitcher } from "@/components/LocaleSwitcher";
import { NotificationSettings } from "@/components/notifications/NotificationSettings";
import { ThemeSwitcher } from "@/components/ThemeSwitcher";
import { getAuthSessions, revokeAuthSession, revokeOtherAuthSessions } from "@/lib/api/client";
import type { Locale } from "@/lib/i18n";
import type { AuthSessionSummary, AuthUser } from "@/types/api";

export function AccountSettings({ user, locales }: Readonly<{ user: AuthUser | null; locales: readonly Locale[] }>) {
  const { locale, t } = useI18n();
  const [sessions, setSessions] = useState<AuthSessionSummary[] | null>(null);
  const [sessionError, setSessionError] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    let active = true;
    getAuthSessions().then((result) => active && setSessions(result.items)).catch(() => active && setSessionError(true));
    return () => { active = false; };
  }, [user]);

  async function revoke(id: string) {
    setBusy(id);
    try { await revokeAuthSession(id); setSessions((items) => items?.filter((item) => item.id !== id) ?? items); }
    catch { setSessionError(true); }
    finally { setBusy(null); }
  }

  async function revokeOthers() {
    setBusy("others");
    try { await revokeOtherAuthSessions(); setSessions((items) => items?.filter((item) => item.current) ?? items); }
    catch { setSessionError(true); }
    finally { setBusy(null); }
  }

  return <div className="stack">
    <section className="panel stack">
      <h2 style={{ margin: 0 }}>{t("settings.appearance")}</h2>
      <LocaleSwitcher locales={locales} />
      <ThemeSwitcher />
      <InterfaceScaleSwitcher />
      <p className="muted">{t(user ? "settings.appearance-account" : "settings.appearance-local")}</p>
    </section>
    {user ? <NotificationSettings /> : null}
    {user ? <section className="panel stack">
      <h2 style={{ margin: 0 }}>{t("settings.security")}</h2>
      <h3 style={{ margin: 0 }}>{t("settings.active-sessions")}</h3>
      {sessionError ? <p className="auth-error" role="alert">{t("settings.sessions-error")}</p> : null}
      {!sessions ? <p className="muted" aria-busy="true">{t("common.state.loading")}</p> : sessions.length === 1 ? <p className="muted">{t("settings.no-other-sessions")}</p> : <ul className="session-list">{sessions.map((session) => <li key={session.id} className="session-row"><div><strong>{session.current ? t("settings.current-session") : t("settings.active-sessions")}</strong><div className="muted">{t("settings.last-active")}: {formatTimestamp(session.last_used_at, locale)} · {t("settings.expires")}: {formatTimestamp(session.expires_at, locale)}</div></div>{session.current ? null : <button type="button" className="secondary-button" disabled={busy !== null} onClick={() => void revoke(session.id)}>{busy === session.id ? t("common.state.loading") : t("settings.sign-out-session")}</button>}</li>)}</ul>}
      {sessions && sessions.some((session) => !session.current) ? <button type="button" className="danger-button" disabled={busy !== null} onClick={() => void revokeOthers()}>{busy === "others" ? t("common.state.loading") : t("settings.sign-out-other-sessions")}</button> : null}
    </section> : null}
  </div>;
}

function formatTimestamp(value: string, locale: Locale): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}
