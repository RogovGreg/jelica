"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useI18n } from "@/components/I18nProvider";
import { getCurrentUser, logout } from "@/lib/api/client";
import { toApiClientError } from "@/lib/api/errors";
import type { AuthUser } from "@/types/api";

type AuthControlsProps = Readonly<{
  onAuthenticatedChange?: (authenticated: boolean) => void;
}>;

export function AuthControls({ onAuthenticatedChange }: AuthControlsProps) {
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
          onAuthenticatedChange?.(true);
        }
      })
      .catch((error) => {
        if (active && toApiClientError(error).status === 401) {
          setUser(null);
          onAuthenticatedChange?.(false);
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
  }, [onAuthenticatedChange, pathname]);

  async function handleLogout() {
    setLoggingOut(true);
    setLogoutFailed(false);
    try {
      await logout();
      setUser(null);
      onAuthenticatedChange?.(false);
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
      <div className="shell-auth-links">
        <Link href="/auth/login" className="secondary-button shell-auth-action">
          {t("auth.action.login")}
        </Link>
        <Link href="/auth/register" className="primary-button shell-auth-action">
          {t("auth.action.register")}
        </Link>
      </div>
    );
  }

  return (
    <div className="shell-account-section">
      <div className="shell-account-row">
        <Link href="/app/profile" className="shell-profile-link">
          <span className="shell-avatar" aria-hidden="true">{user.username.charAt(0).toUpperCase()}</span>
          <span>{user.username}</span>
        </Link>
        <span className="shell-notification-placeholder" aria-label={t("shell.notifications-placeholder")}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" />
          </svg>
        </span>
      </div>
      <button className="shell-logout-button" type="button" onClick={handleLogout} disabled={loggingOut}>
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
