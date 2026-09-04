"use client";

import { useState, type FormEvent } from "react";

import { useI18n } from "@/components/I18nProvider";
import { resendVerification } from "@/lib/api/client";
import { isRateLimitedError } from "@/lib/api/errors";

export function ResendVerificationForm({ initialEmail = "" }: Readonly<{ initialEmail?: string }>) {
  const { t } = useI18n();
  const [email, setEmail] = useState(initialEmail);
  const [pending, setPending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(false);
  const [throttled, setThrottled] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true); setSent(false); setError(false); setThrottled(false);
    try { await resendVerification(email.trim()); setSent(true); } catch (caught) { if (isRateLimitedError(caught)) setThrottled(true); else setError(true); } finally { setPending(false); }
  }

  return <form className="form-grid" onSubmit={submit}>
    <label className="input-field"><span>{t("auth.field.email")}</span><input type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required disabled={pending} /></label>
    {sent ? <div className="state-box" role="status">{t("auth.email.sent")}</div> : null}
    {error ? <div className="state-box state-error" role="alert">{t("auth.email.delivery-unavailable")}</div> : null}
    {throttled ? <div className="state-box state-error" role="alert">{t("auth.error.rate-limited")}</div> : null}
    <button className="secondary-button" type="submit" disabled={pending}>{t("auth.email.resend")}</button>
  </form>;
}
