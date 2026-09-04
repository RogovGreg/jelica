"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { useI18n } from "@/components/I18nProvider";
import { ResendVerificationForm } from "@/components/auth/ResendVerificationForm";
import { registerUser } from "@/lib/api/client";
import { toApiClientError } from "@/lib/api/errors";
import type { TranslationKey } from "@/lib/i18n";

export function RegisterForm() {
  const { t } = useI18n();
  const [pending, setPending] = useState(false);
  const [errorKey, setErrorKey] = useState<TranslationKey | null>(null);
  const [registeredEmail, setRegisteredEmail] = useState<string | null>(null);
  const [verificationToken, setVerificationToken] = useState<string | null>(null);
  const [deliveryFailed, setDeliveryFailed] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setErrorKey(null);

    const formData = new FormData(event.currentTarget);
    try {
      const result = await registerUser({
        username: String(formData.get("username") ?? "").trim(),
        email: String(formData.get("email") ?? "").trim(),
        password: String(formData.get("password") ?? ""),
      });
      setRegisteredEmail(String(formData.get("email") ?? "").trim());
      setVerificationToken(result.verification_token ?? null);
      setDeliveryFailed(Boolean(result.email_delivery_failed));
    } catch (error) {
      const apiError = toApiClientError(error);
      setErrorKey(
        apiError.status === 429
          ? "auth.error.rate-limited"
          : apiError.status === 409
          ? "auth.error.account-exists"
          : "auth.error.registration-failed",
      );
    } finally {
      setPending(false);
    }
  }

  if (registeredEmail !== null) {
    return <section className="stack"><div className="state-box" role="status">{deliveryFailed ? t("auth.email.delivery-failed") : t("auth.email.check")}</div><ResendVerificationForm initialEmail={registeredEmail} /><div className="actions-row">{verificationToken ? <Link href={`/auth/verify?token=${encodeURIComponent(verificationToken)}`} className="secondary-button">{t("auth.verify.title")}</Link> : null}<Link href="/auth/login" className="secondary-button">{t("auth.action.login")}</Link></div></section>;
  }

  return (
    <form className="form-grid" onSubmit={handleSubmit}>
      <label className="input-field">
        <span>{t("auth.field.username")}</span>
        <input
          name="username"
          autoComplete="username"
          maxLength={64}
          required
          disabled={pending}
        />
      </label>
      <label className="input-field">
        <span>{t("auth.field.email")}</span>
        <input
          name="email"
          type="email"
          autoComplete="email"
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
          autoComplete="new-password"
          minLength={8}
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
          {t(pending ? "auth.state.registering" : "auth.action.register")}
        </button>
        <Link href="/auth/login" className="secondary-button">
          {t("auth.action.login")}
        </Link>
      </div>
    </form>
  );
}
