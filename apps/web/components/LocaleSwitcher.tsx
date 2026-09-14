"use client";

import { useI18n } from "@/components/I18nProvider";
import { DEFAULT_LOCALE, isSupportedLocale, type Locale } from "@/lib/i18n";
import { JelicaSelect, type JelicaSelectOption } from "../../../packages/app-platform/src/select-ui";

export function LocaleSwitcher({ locales }: Readonly<{ locales: readonly Locale[] }>) {
  const { locale, setLocale, t } = useI18n();

  const onChange = (requestedLocale: Locale) => {
    if (isSupportedLocale(requestedLocale)) {
      setLocale(requestedLocale);
    }
  };

  const options: readonly JelicaSelectOption<Locale>[] = locales.map((supportedLocale) => ({
    value: supportedLocale,
    label: t(localeKey(supportedLocale)),
  }));

  return (
    <JelicaSelect
      label={t("desktop.shell.locale-label")}
      value={locales.includes(locale) ? locale : DEFAULT_LOCALE}
      options={options}
      onValueChange={onChange}
    />
  );
}

function localeKey(locale: Locale) {
  return `locale.${locale}` as const;
}
