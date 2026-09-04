"use client";

import { useEffect } from "react";

import { useI18n } from "@/components/I18nProvider";
import { isSupportedLocale } from "@/lib/i18n";

export function ProfileLocaleSync({ language }: Readonly<{ language: string }>) {
  const { setLocale } = useI18n();
  useEffect(() => {
    if (isSupportedLocale(language)) setLocale(language);
  }, [language, setLocale]);
  return null;
}
