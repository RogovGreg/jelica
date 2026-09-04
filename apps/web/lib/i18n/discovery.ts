import fs from "node:fs";
import path from "node:path";

import { isSupportedLocale, SUPPORTED_LOCALES, type Locale } from "./index";

const LOCALE_ROOT_CANDIDATES = [
  path.resolve(process.cwd(), "i18n/locales"),
  path.resolve(process.cwd(), "../../i18n/locales"),
];

export function discoverUiLocales(): readonly Locale[] {
  const root = LOCALE_ROOT_CANDIDATES.find((candidate) => fs.existsSync(candidate));
  if (!root) return [];
  return discoverUiLocalesFromRoot(root);
}

export function discoverUiLocalesFromRoot(root: string): readonly Locale[] {
  const resolvedRoot = path.resolve(root);
  if (!LOCALE_ROOT_CANDIDATES.includes(resolvedRoot)) {
    return [];
  }
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(resolvedRoot, { withFileTypes: true });
  } catch {
    return [];
  }
  const present = new Set(
    entries
      .filter((entry) => entry.isDirectory() && isSupportedLocale(entry.name))
      .map((entry) => entry.name),
  );
  return SUPPORTED_LOCALES.filter((locale) => present.has(locale));
}
