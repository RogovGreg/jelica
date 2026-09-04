"use client";

import type { ReactNode } from "react";

import { useI18n } from "@/components/I18nProvider";

type LoadingStateProps = {
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
};

export function LoadingState({ title, description, children }: LoadingStateProps) {
  const { t } = useI18n();
  return (
    <section className="panel stack" aria-live="polite">
      <h1 style={{ margin: 0 }}>{title}</h1>
      <div className="state-box">{t("common.state.loading")}</div>
      {description ? <p className="muted">{description}</p> : null}
      {children}
    </section>
  );
}
