"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { useI18n } from "@/components/I18nProvider";
import { ResendVerificationForm } from "@/components/auth/ResendVerificationForm";
import { verifyEmail } from "@/lib/api/client";
import { toApiClientError } from "@/lib/api/errors";
import type { TranslationKey } from "@/lib/i18n";

type VerifyEmailFormProps = {
  registrationComplete: boolean;
};

export function VerifyEmailForm({
  registrationComplete,
}: VerifyEmailFormProps) {
  const router = useRouter();
  const { t } = useI18n();
  const [pending, setPending] = useState(false);
  const [errorKey, setErrorKey] = useState<TranslationKey | null>(null);
  const [token, setToken] = useState("");

  const submitToken = useCallback(async (value: string) => {
    setPending(true);
    setErrorKey(null);
    try {
      await verifyEmail({ token: value.trim() });
      window.dispatchEvent(new Event("jelica-auth-changed"));
      router.replace("/app/profile");
      router.refresh();
    } catch (error) {
      const apiError = toApiClientError(error);
      setErrorKey(
        apiError.status === 429
          ? "auth.error.rate-limited"
          : [400, 404, 409].includes(apiError.status)
          ? "auth.error.invalid-verification-token"
          : "auth.error.verification-failed",
      );
    } finally {
      setPending(false);
    }
  }, [router]);

  useEffect(() => {
    const queryToken = new URLSearchParams(window.location.search).get("token") ?? "";
    if (!queryToken) return;
    window.history.replaceState({}, document.title, "/auth/verify");
    setToken(queryToken);
    void submitToken(queryToken);
  }, [submitToken]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await submitToken(token);
  }

  return (
    <>
      <form className="form-grid" onSubmit={handleSubmit}>
      {registrationComplete ? (
        <div className="state-box">{t("auth.verify.registration-created")}</div>
      ) : null}
      {errorKey || !pending && token === "" ? <label className="input-field"><span>{t("auth.field.verification-token")}</span><input name="token" type="password" autoComplete="one-time-code" maxLength={1024} value={token} onChange={(event) => setToken(event.target.value)} required disabled={pending} /></label> : <div className="state-box">{t("auth.email.check")}</div>}
      {errorKey ? (
        <div className="state-box state-error" role="alert">
          {t(errorKey)}
        </div>
      ) : null}
      <div className="actions-row">
        <button className="primary-button" type="submit" disabled={pending}>
          {t(pending ? "auth.state.verifying" : "auth.action.verify-email")}
        </button>
        <Link href="/auth/login" className="secondary-button">
          {t("auth.action.login")}
        </Link>
      </div>
      </form>
      {errorKey ? <ResendVerificationForm /> : null}
    </>
  );
}
