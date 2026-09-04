"use client";

import { useState, type FormEvent } from "react";

import { useI18n } from "@/components/I18nProvider";
import { requestPasswordReset } from "@/lib/api/client";
import { isRateLimitedError } from "@/lib/api/errors";

export function ForgotPasswordForm() {
  const { t } = useI18n();
  const [pending, setPending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(false);
  const [throttled, setThrottled] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setPending(true); setSent(false); setError(false); setThrottled(false);
    const email = String(new FormData(event.currentTarget).get("email") ?? "").trim();
    try { await requestPasswordReset(email); setSent(true); } catch (caught) { if (isRateLimitedError(caught)) setThrottled(true); else setError(true); } finally { setPending(false); }
  }

  return <form className="form-grid" onSubmit={submit}>
    <label className="input-field"><span>{t("auth.field.email")}</span><input name="email" type="email" autoComplete="email" required disabled={pending} /></label>
    {sent ? <div className="state-box" role="status">{t("auth.forgot.sent")}</div> : null}
    {error ? <div className="state-box state-error" role="alert">{t("auth.error.password-reset-failed")}</div> : null}
    {throttled ? <div className="state-box state-error" role="alert">{t("auth.error.rate-limited")}</div> : null}
    <button className="primary-button" type="submit" disabled={pending}>{t("auth.forgot.submit")}</button>
  </form>;
}
