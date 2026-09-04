"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

import { useI18n } from "@/components/I18nProvider";
import { resetPassword } from "@/lib/api/client";
import { toApiClientError } from "@/lib/api/errors";

export function ResetPasswordForm() {
  const router = useRouter();
  const { t } = useI18n();
  const [token, setToken] = useState("");
  const [pending, setPending] = useState(false);
  const [success, setSuccess] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const [failure, setFailure] = useState(false);
  const [throttled, setThrottled] = useState(false);
  const [mismatch, setMismatch] = useState(false);

  useEffect(() => {
    const queryToken = new URLSearchParams(window.location.search).get("token") ?? "";
    if (!queryToken) { setInvalid(true); return; }
    setToken(queryToken);
    window.history.replaceState({}, document.title, "/auth/reset-password");
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const password = String(data.get("password") ?? "");
    const confirmation = String(data.get("confirmation") ?? "");
    setMismatch(password !== confirmation); setInvalid(false); setFailure(false); setThrottled(false);
    if (password !== confirmation) return;
    setPending(true);
    try { await resetPassword(token, password); setToken(""); window.history.replaceState({}, document.title, "/auth/reset-password"); setSuccess(true); } catch (error) { const status = toApiClientError(error).status; setInvalid(status === 400); setThrottled(status === 429); setFailure(status !== 400 && status !== 429); } finally { setPending(false); }
  }

  if (success) return <div className="stack"><div className="state-box" role="status">{t("auth.reset.success")}</div><button type="button" className="primary-button" onClick={() => router.push("/auth/login")}>{t("auth.action.login")}</button></div>;
  if (invalid) return <div className="stack"><div className="state-box state-error" role="alert">{t("auth.reset.invalid")}</div><button type="button" className="secondary-button" onClick={() => router.push("/auth/forgot-password")}>{t("auth.reset.request-new")}</button></div>;

  return <form className="form-grid" onSubmit={submit}>
    <label className="input-field"><span>{t("auth.reset.new-password")}</span><input name="password" type="password" autoComplete="new-password" minLength={8} maxLength={1024} required disabled={pending} /></label>
    <label className="input-field"><span>{t("auth.reset.confirm-password")}</span><input name="confirmation" type="password" autoComplete="new-password" minLength={8} maxLength={1024} required disabled={pending} /></label>
    {mismatch ? <div className="state-box state-error" role="alert">{t("auth.error.password-mismatch")}</div> : null}
    {failure ? <div className="state-box state-error" role="alert">{t("auth.error.password-reset-failed")}</div> : null}
    {throttled ? <div className="state-box state-error" role="alert">{t("auth.error.rate-limited")}</div> : null}
    <button className="primary-button" type="submit" disabled={pending}>{t("auth.reset.submit")}</button>
  </form>;
}
