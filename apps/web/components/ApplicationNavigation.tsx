"use client";

import Link from "next/link";

import { NotificationNavigationLink } from "@/components/notifications/NotificationNavigationLink";
import { useI18n } from "@/components/I18nProvider";
import { TranslatedText } from "@/components/TranslatedText";

export function ApplicationNavigation() {
  const { t } = useI18n();
  return (
    <nav className="app-nav" aria-label={t("common.navigation.application-label")}>
      <Link href="/app/tasks"><TranslatedText id="app.nav.tasks" /></Link>
      <Link href="/app/results"><TranslatedText id="app.nav.results" /></Link>
      <Link href="/app/support"><TranslatedText id="nav.support" /></Link>
      <Link href="/app/projects"><TranslatedText id="page.projects.title" /></Link>
      <NotificationNavigationLink />
      <Link href="/app/profile"><TranslatedText id="page.profile.title" /></Link>
      <Link href="/app/settings"><TranslatedText id="page.settings.title" /></Link>
    </nav>
  );
}
