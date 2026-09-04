"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import { useI18n } from "@/components/I18nProvider";
import { createSupportRequest } from "@/lib/api/client";
import { toLocalizedErrorMessage } from "@/lib/api/errors";
import type { SupportRequestCreatePayload, SupportRequestResponse } from "@/types/api";

export function SupportRequestForm() {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [subject, setSubject] = useState("");
  const [message, setMessage] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [createdRequest, setCreatedRequest] = useState<SupportRequestResponse | null>(null);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitError(null);
    setCreatedRequest(null);

    const payload: SupportRequestCreatePayload = {
      name: name.trim(),
      email: email.trim(),
      subject: subject.trim(),
      message: message.trim(),
    };
    const validationError = validatePayload(payload, t);
    if (validationError) {
      setSubmitError(validationError);
      return;
    }

    setIsSubmitting(true);
    try {
      const created = await createSupportRequest(payload);
      setCreatedRequest(created);
      setMessage("");
    } catch (error) {
      setSubmitError(toLocalizedErrorMessage(error, t));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <section className="stack">
      <form className="form-grid" onSubmit={onSubmit}>
        <label className="input-field">
          <span>{t("support.field.name")}</span>
          <input
            name="name"
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={t("support.placeholder.name")}
          />
        </label>

        <label className="input-field">
          <span>{t("support.field.email")}</span>
          <input
            name="email"
            required
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder={t("support.placeholder.email")}
          />
        </label>

        <label className="input-field">
          <span>{t("support.field.subject")}</span>
          <input
            name="subject"
            required
            value={subject}
            onChange={(event) => setSubject(event.target.value)}
            placeholder={t("support.placeholder.subject")}
          />
        </label>

        <label className="input-field">
          <span>{t("support.field.message")}</span>
          <textarea
            name="message"
            required
            rows={8}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder={t("support.placeholder.message")}
          />
        </label>

        {submitError ? <div className="state-box state-error">{submitError}</div> : null}

        <div className="actions-row">
          <button type="submit" className="primary-button" disabled={isSubmitting}>
            {isSubmitting ? t("support.action.submitting") : t("support.action.send")}
          </button>
        </div>
      </form>

      {createdRequest ? (
        <section className="panel stack">
          <h2 style={{ margin: 0 }}>{t("support.created.title")}</h2>
          <p>
            <strong>{t("support.created.id")}</strong> <code>{createdRequest.id}</code>
          </p>
          <p>
            <strong>{t("support.created.status")}</strong> {createdRequest.status}
          </p>
          <p>
            <strong>{t("support.created.created-at")}</strong> {formatDateTime(createdRequest.created_at)}
          </p>
        </section>
      ) : null}
    </section>
  );
}

function validatePayload(payload: SupportRequestCreatePayload, t: ReturnType<typeof useI18n>["t"]): string | null {
  if (payload.name === "") {
    return t("support.validation.name-required");
  }
  if (payload.email === "") {
    return t("support.validation.email-required");
  }
  if (!payload.email.includes("@")) {
    return t("support.validation.email-invalid");
  }
  if (payload.subject === "") {
    return t("support.validation.subject-required");
  }
  if (payload.message === "") {
    return t("support.validation.message-required");
  }
  return null;
}

function formatDateTime(rawValue: string): string {
  const value = new Date(rawValue);
  if (Number.isNaN(value.getTime())) {
    return rawValue;
  }
  return value.toLocaleString();
}
