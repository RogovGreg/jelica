"use client";

import { useTheme, type UiTheme } from "@/hooks/useTheme";
import { useI18n } from "@/components/I18nProvider";

const THEMES: readonly UiTheme[] = ["system", "light", "dark", "mono"];
const DEFAULT_THEME = normalizeTheme(process.env.NEXT_PUBLIC_DEFAULT_THEME);

export function ThemeSwitcher() {
  const { theme, setTheme } = useTheme(DEFAULT_THEME);
  const { t } = useI18n();
  return (
    <div className="theme-switcher" role="group" aria-label={t("theme.selector-label")}>
      {THEMES.map((item) => (
        <button
          key={item}
          type="button"
          className={item === theme ? "active" : undefined}
          onClick={() => setTheme(item)}
        >
          {t(themeKey(item))}
        </button>
      ))}
    </div>
  );
}

function themeKey(theme: UiTheme) {
  return `theme.label.${theme}` as const;
}

function normalizeTheme(rawTheme: string | undefined): UiTheme {
  if (rawTheme === "system" || rawTheme === "dark" || rawTheme === "mono") {
    return rawTheme;
  }
  return "light";
}
