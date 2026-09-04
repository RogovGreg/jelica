import enMessages from "../../../i18n/locales/en/messages.json";
import enNotifications from "../../../i18n/locales/en/notifications.json";
import enReports from "../../../i18n/locales/en/reports.json";
import ruMessages from "../../../i18n/locales/ru/messages.json";
import ruNotifications from "../../../i18n/locales/ru/notifications.json";
import ruReports from "../../../i18n/locales/ru/reports.json";
import srCyrlMessages from "../../../i18n/locales/sr-Cyrl/messages.json";
import srCyrlNotifications from "../../../i18n/locales/sr-Cyrl/notifications.json";
import srCyrlReports from "../../../i18n/locales/sr-Cyrl/reports.json";
import srLatnMessages from "../../../i18n/locales/sr-Latn/messages.json";
import srLatnNotifications from "../../../i18n/locales/sr-Latn/notifications.json";
import srLatnReports from "../../../i18n/locales/sr-Latn/reports.json";
import sourceJson from "../../../i18n/source.json";

export const SUPPORTED_LOCALES = ["en", "ru", "sr-Latn", "sr-Cyrl"] as const;
export type Locale = (typeof SUPPORTED_LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";

export type TranslationKey = keyof typeof sourceJson;
export type TranslationNamespace = "messages" | "reports" | "notifications";
export type TranslationMetadata = {
  verified: boolean;
  verifiedBy: string | null;
  verifiedAt: string | null;
  translatedBy: string | null;
  translatedAt: string | null;
};
export type TranslationEntry = TranslationMetadata & { text: string };
export type SourceEntry = { "default-text": string; context: string };
export type TranslationCatalog = Partial<Record<TranslationKey, TranslationEntry>>;
export type TranslationCatalogs = Record<TranslationNamespace, TranslationCatalog>;
export type LoadedLocale = { locale: Locale; catalogs: TranslationCatalogs };
export type TranslationValues = Readonly<Record<string, string | number>>;
export type Translator = (key: TranslationKey, values?: TranslationValues) => string;

export const sourceCatalog = sourceJson as Record<TranslationKey, SourceEntry>;

const localeCatalogs: Record<Locale, TranslationCatalogs> = {
  en: createCatalogs(enMessages, enReports, enNotifications),
  ru: createCatalogs(ruMessages, ruReports, ruNotifications),
  "sr-Latn": createCatalogs(srLatnMessages, srLatnReports, srLatnNotifications),
  "sr-Cyrl": createCatalogs(srCyrlMessages, srCyrlReports, srCyrlNotifications),
};

export function loadLocale(requestedLocale: string | null | undefined): LoadedLocale {
  const locale = resolveLocale(requestedLocale);
  return { locale, catalogs: localeCatalogs[locale] };
}

export function resolveLocale(requestedLocale: string | null | undefined): Locale {
  if (!requestedLocale) return DEFAULT_LOCALE;
  const normalized = requestedLocale.trim().replaceAll("_", "-").toLowerCase();
  const exact = SUPPORTED_LOCALES.find((locale) => locale.toLowerCase() === normalized);
  if (exact) return exact;
  if (normalized.startsWith("en-")) return "en";
  if (normalized.startsWith("ru-")) return "ru";
  if (normalized.startsWith("sr-latn-")) return "sr-Latn";
  if (normalized.startsWith("sr-cyrl-")) return "sr-Cyrl";
  return DEFAULT_LOCALE;
}

export function isSupportedLocale(value: string): value is Locale {
  return (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

export function createTranslator(requestedLocale: string | null | undefined): Translator {
  const requested = loadLocale(requestedLocale);
  const english = loadLocale(DEFAULT_LOCALE);
  return (key, values) => {
    const namespace = namespaceForKey(key);
    const text =
      entryText(requested.catalogs[namespace][key]) ??
      entryText(english.catalogs[namespace][key]) ??
      sourceCatalog[key]["default-text"] ??
      key;
    return interpolate(text, values);
  };
}

export function translate(
  requestedLocale: string | null | undefined,
  key: TranslationKey,
  values?: TranslationValues,
): string {
  return createTranslator(requestedLocale)(key, values);
}

export function namespaceForKey(key: TranslationKey): TranslationNamespace {
  if (key.startsWith("report.")) return "reports";
  if (key.startsWith("notification.")) return "notifications";
  return "messages";
}

function entryText(entry: TranslationEntry | undefined): string | undefined {
  return entry && entry.text.trim() !== "" ? entry.text : undefined;
}

function interpolate(text: string, values?: TranslationValues): string {
  if (!values) return text;
  return text.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (placeholder, name) => {
    const value = values[name];
    return value === undefined ? placeholder : String(value);
  });
}

function createCatalogs(
  messages: object,
  reports: object,
  notifications: object,
): TranslationCatalogs {
  return {
    messages: messages as TranslationCatalog,
    reports: reports as TranslationCatalog,
    notifications: notifications as TranslationCatalog,
  };
}
