"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { useI18n } from "@/components/I18nProvider";

type AnalysisServiceUnavailableLink = {
  href: string;
  label: ReactNode;
};

type AnalysisServiceUnavailableProps = {
  title?: ReactNode;
  description?: ReactNode;
  retryLabel?: ReactNode;
  onRetry?: () => void;
  fallbackLinks?: readonly AnalysisServiceUnavailableLink[];
};

export function AnalysisServiceUnavailable({
  title,
  description,
  retryLabel,
  onRetry,
  fallbackLinks,
}: AnalysisServiceUnavailableProps) {
  const { t } = useI18n();
  const resolvedTitle = title ?? t("common.error.service-unavailable");
  const resolvedDescription = description ?? t("common.error.service-unavailable");
  const resolvedRetryLabel = retryLabel ?? t("common.action.retry");
  const resolvedFallbackLinks = fallbackLinks ?? [
    { href: "/app/tasks", label: t("common.action.open-tasks") },
    { href: "/app/support", label: t("nav.support") },
  ];
  return (
    <section className="panel stack" role="alert">
      <h1 style={{ margin: 0 }}>{resolvedTitle}</h1>
      <div className="state-box state-warning">{resolvedDescription}</div>
      <div className="actions-row">
        {onRetry ? (
          <button type="button" className="primary-button" onClick={onRetry}>
            {resolvedRetryLabel}
          </button>
        ) : null}
        {resolvedFallbackLinks.map((link) => (
          <Link key={link.href} href={link.href} className="secondary-button">
            {link.label}
          </Link>
        ))}
      </div>
    </section>
  );
}
