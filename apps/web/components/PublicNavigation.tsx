"use client";

import Link from "next/link";

import { useI18n } from "@/components/I18nProvider";

export function PublicNavigation() {
  const { t } = useI18n();
  return (
    <nav className="top-nav" aria-label={t("common.navigation.main-label")}>
      <Link href="/">{t("nav.home")}</Link>
      <Link href="/news">{t("nav.news")}</Link>
      <Link href="/about">{t("nav.about")}</Link>
      <Link href="/download">{t("nav.download")}</Link>
      <Link href="/docs">{t("nav.documentation")}</Link>
      <Link href="/support">{t("nav.support")}</Link>
      <Link href="/app/tasks">{t("nav.run-online")}</Link>
    </nav>
  );
}
