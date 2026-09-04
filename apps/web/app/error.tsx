"use client";

import Link from "next/link";

import { useI18n } from "@/components/I18nProvider";

type RootErrorPageProps = {
  error: Error & { digest?: string };
  reset: () => void;
};

export default function RootErrorPage({ error, reset }: RootErrorPageProps) {
  const { t } = useI18n();
  return (
    <section className="panel stack" role="alert">
      <h1 style={{ margin: 0 }}>{t("common.error.unexpected-title")}</h1>
      <div className="state-box state-error">
        {t("common.error.unexpected-description")}
      </div>
      <div className="actions-row">
        <button type="button" className="primary-button" onClick={reset}>
          {t("common.action.retry-now")}
        </button>
        <Link href="/" className="secondary-button">
          {t("common.action.home")}
        </Link>
        <Link href="/app/tasks" className="secondary-button">
          {t("common.action.open-app")}
        </Link>
      </div>
    </section>
  );
}
