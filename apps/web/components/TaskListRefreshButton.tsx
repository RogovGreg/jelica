"use client";

import { useRouter } from "next/navigation";

import { useI18n } from "@/components/I18nProvider";

export function TaskListRefreshButton() {
  const router = useRouter();
  const { t } = useI18n();
  return <button type="button" className="secondary-button" onClick={() => router.refresh()}>{t("task.list.refresh")}</button>;
}
