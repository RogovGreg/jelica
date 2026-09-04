"use client";

import { useI18n } from "@/components/I18nProvider";

export function LandingAnchorNav() {
  const { t } = useI18n();
  return (
    <nav className="landing-anchor-nav" aria-label={t("landing.sections.label")}>
      <a href="#statistics">{t("report.section.statistics")}</a>
      <a href="#comparative">{t("landing.section.comparative")}</a>
      <a href="#phylogenetics">{t("landing.section.phylogenetics")}</a>
      <a href="#reproducibility">{t("landing.section.reproducibility")}</a>
      <a href="#platforms">{t("landing.section.platforms")}</a>
      <a href="#technical">{t("landing.section.technical")}</a>
    </nav>
  );
}
