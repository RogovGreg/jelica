"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { useI18n } from "@/components/I18nProvider";
import { createProject } from "@/lib/api/client";
import { toErrorMessage } from "@/lib/api/errors";

export function CreateProjectForm() {
  const { t } = useI18n();
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedName = name.trim();
    if (!normalizedName) {
      setError(t("project.form.required"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const project = await createProject({ name: normalizedName, description: description.trim() || null });
      router.push(`/app/projects/${encodeURIComponent(project.id)}`);
    } catch (requestError) {
      setError(toErrorMessage(requestError));
      setSubmitting(false);
    }
  }

  return (
    <form className="form-grid" onSubmit={handleSubmit}>
      <label className="input-field"><span>{t("project.form.name")}</span><input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} required disabled={submitting} /></label>
      <label className="input-field"><span>{t("project.form.description")}</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={4} disabled={submitting} /></label>
      {error ? <p className="state-box state-error" role="alert">{error}</p> : null}
      <div className="actions-row">
        <button type="submit" className="primary-button" disabled={submitting}>{submitting ? t("project.form.creating") : t("project.action.create")}</button>
        <button type="button" className="secondary-button" onClick={() => router.push("/app/projects")} disabled={submitting}>{t("project.action.cancel")}</button>
      </div>
    </form>
  );
}
