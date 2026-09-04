"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useI18n } from "@/components/I18nProvider";
import { getCurrentUser, logout } from "@/lib/api/client";
import { toApiClientError } from "@/lib/api/errors";
import type { AuthUser } from "@/types/api";

export function AuthControls() {
  const pathname = usePathname();
  const router = useRouter();
  const { t } = useI18n();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutFailed, setLogoutFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    getCurrentUser()
      .then((currentUser) => {
        if (active) {
          setUser(currentUser);
        }
      })
      .catch((error) => {
        if (active && toApiClientError(error).status === 401) {
          setUser(null);
        }
      })
      .finally(() => {
        if (active) {
          setLoading(false);
        }
      });

    return () => {
      active = false;
    };
  }, [pathname]);

  async function handleLogout() {
    setLoggingOut(true);
    setLogoutFailed(false);
    try {
      await logout();
      setUser(null);
      window.dispatchEvent(new Event("jelica-auth-changed"));
      router.refresh();
    } catch {
      setLogoutFailed(true);
    } finally {
      setLoggingOut(false);
    }
  }

  if (loading) {
    return <div className="auth-controls" aria-busy="true" />;
  }

  if (!user) {
    return (
      <div className="auth-controls">
        <Link href="/auth/login">{t("auth.action.login")}</Link>
        <Link href="/auth/register">{t("auth.action.register")}</Link>
      </div>
    );
  }

  return (
    <div className="auth-controls">
      <Link href="/app/profile">{user.username}</Link>
      <button type="button" onClick={handleLogout} disabled={loggingOut}>
        {t(loggingOut ? "auth.state.signing-out" : "auth.action.logout")}
      </button>
      {logoutFailed ? (
        <span className="auth-error" role="alert">
          {t("auth.error.logout-failed")}
        </span>
      ) : null}
    </div>
  );
}
