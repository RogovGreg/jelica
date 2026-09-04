"use client";

import type { ChangeEvent } from "react";

import { useI18n } from "@/components/I18nProvider";
import { isSupportedLocale, type Locale } from "@/lib/i18n";

export function LocaleSwitcher({ locales }: Readonly<{ locales: readonly Locale[] }>) {
  const { locale, setLocale, t } = useI18n();

  const onChange = (event: ChangeEvent<HTMLSelectElement>) => {
    const requestedLocale = event.target.value;
    if (isSupportedLocale(requestedLocale)) {
      setLocale(requestedLocale);
    }
  };

  return (
    <select
      className="locale-switcher"
      aria-label={t("desktop.shell.locale-label")}
      value={locale}
      onChange={onChange}
    >
      {locales.map((supportedLocale) => (
        <option key={supportedLocale} value={supportedLocale}>
          {t(localeKey(supportedLocale))}
        </option>
      ))}
    </select>
  );
}

function localeKey(locale: Locale) {
  return `locale.${locale}` as const;
}
