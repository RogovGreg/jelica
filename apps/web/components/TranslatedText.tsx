"use client";

import { useI18n } from "@/components/I18nProvider";
import type { TranslationKey, TranslationValues } from "@/lib/i18n";

type TranslatedTextProps = {
  id: TranslationKey;
  values?: TranslationValues;
};

export function TranslatedText({ id, values }: TranslatedTextProps) {
  const { t } = useI18n();
  return <>{t(id, values)}</>;
}
