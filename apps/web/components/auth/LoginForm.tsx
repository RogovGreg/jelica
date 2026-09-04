"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { useI18n } from "@/components/I18nProvider";
import { login } from "@/lib/api/client";
import { toApiClientError } from "@/lib/api/errors";
import type { TranslationKey } from "@/lib/i18n";

type LoginFormProps = {
  nextPath: string;
};

export function LoginForm({ nextPath }: LoginFormProps) {
  const router = useRouter();
  const { t } = useI18n();
  const [pending, setPending] = useState(false);
  const [errorKey, setErrorKey] = useState<TranslationKey | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setErrorKey(null);

    const formData = new FormData(event.currentTarget);
    try {
      await login({
        identifier: String(formData.get("identifier") ?? "").trim(),
        password: String(formData.get("password") ?? ""),
      });
      window.dispatchEvent(new Event("jelica-auth-changed"));
      router.replace(nextPath);
      router.refresh();
    } catch (error) {
      const apiError = toApiClientError(error);
      setErrorKey(
        apiError.status === 429
          ? "auth.error.rate-limited"
          : apiError.status === 401
          ? "auth.error.invalid-credentials"
          : apiError.status === 403
            ? "auth.error.email-verification-required"
            : "auth.error.login-failed",
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="form-grid" onSubmit={handleSubmit}>
      <label className="input-field">
        <span>{t("auth.field.identifier")}</span>
        <input
          name="identifier"
          autoComplete="username"
          maxLength={320}
          required
          disabled={pending}
        />
      </label>
      <label className="input-field">
        <span>{t("auth.field.password")}</span>
        <input
          name="password"
          type="password"
          autoComplete="current-password"
          maxLength={1024}
          required
          disabled={pending}
        />
      </label>
      {errorKey ? (
        <div className="state-box state-error" role="alert">
          {t(errorKey)}
        </div>
      ) : null}
      <div className="actions-row">
        <button className="primary-button" type="submit" disabled={pending}>
          {t(pending ? "auth.state.logging-in" : "auth.action.login")}
        </button>
        <Link href="/auth/register" className="secondary-button">
          {t("auth.action.register")}
        </Link>
        <Link href="/auth/forgot-password" className="secondary-button">
          {t("auth.action.forgot-password")}
        </Link>
      </div>
    </form>
  );
}
