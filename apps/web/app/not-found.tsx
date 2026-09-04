"use client";

import Link from "next/link";

import { useI18n } from "@/components/I18nProvider";

export default function NotFoundPage() {
  const { t } = useI18n();
  return (
    <section className="panel stack" role="alert">
      <h1 style={{ margin: 0 }}>{t("common.error.not-found")}</h1>
      <div className="state-box">{t("common.error.not-found-description")}</div>
      <div className="actions-row">
        <Link href="/" className="primary-button">
          {t("common.action.home")}
        </Link>
        <Link href="/app/tasks" className="secondary-button">
          {t("common.action.open-app")}
        </Link>
      </div>
    </section>
  );
}
