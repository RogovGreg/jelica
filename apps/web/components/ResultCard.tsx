"use client";

import { useI18n } from "@/components/I18nProvider";
import type { TaskResultLookupResponse } from "@/types/api";

type ResultCardProps = {
  result: TaskResultLookupResponse;
};

export function ResultCard({ result }: ResultCardProps) {
  const { t } = useI18n();
  const resultReference = result.result_reference;

  return (
    <section className="panel result-card">
      <h2 style={{ margin: 0 }}>{t("result.card.title")}</h2>
      <p>
        <strong>{t("result.card.task-id")}</strong> {result.task_id}
      </p>
      <p>
        <strong>{t("result.card.trace-id")}</strong> {result.trace_id ?? "—"}
      </p>
      <p>
        <strong>{t("result.card.task-state")}</strong> {result.state}
      </p>
      <p>
        <strong>{t("result.card.available")}</strong> {result.available ? t("result.card.yes") : t("result.card.no")}
      </p>
      <p>
        <strong>{t("result.card.status-command-id")}</strong> {result.status_command_id}
      </p>
      {resultReference ? (
        <>
          <p>
            <strong>{t("result.card.result-id")}</strong> <code>{resultReference.content_id}</code>
          </p>
          <p>
            <strong>{t("result.card.package-path")}</strong> <code>{resultReference.package_path}</code>
          </p>
          <p>
            <strong>{t("result.card.command-id")}</strong> {resultReference.command_id}
          </p>
        </>
      ) : (
        <p className="muted">
          <strong>{t("result.card.result-id")}</strong> —
        </p>
      )}
      {result.detail ? <div className="state-box">{result.detail}</div> : null}
    </section>
  );
}
