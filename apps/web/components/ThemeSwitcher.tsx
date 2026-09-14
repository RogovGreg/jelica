"use client";

import Image from "next/image";

import { useTheme, type UiTheme } from "@/hooks/useTheme";
import { useI18n } from "@/components/I18nProvider";
import { JelicaSelect, type JelicaSelectOption } from "../../../packages/app-platform/src/select-ui";

const THEMES: readonly UiTheme[] = ["system", "light", "dark", "mono"];
const DEFAULT_THEME = normalizeTheme(process.env.NEXT_PUBLIC_DEFAULT_THEME);
const THEME_ICONS: Record<UiTheme, string> = {
  system: "/theme-system.svg",
  light: "/theme-light.svg",
  dark: "/theme-dark.svg",
  mono: "/theme-monochrome.svg",
};

export function ThemeSwitcher() {
  const { theme, setTheme } = useTheme(DEFAULT_THEME);
  const { t } = useI18n();
  const options: readonly JelicaSelectOption<UiTheme>[] = THEMES.map((item) => ({
    value: item,
    label: t(themeKey(item)),
    icon: <Image src={THEME_ICONS[item]} alt="" width={20} height={20} />,
  }));

  return (
    <JelicaSelect
      label={t("desktop.shell.theme-label")}
      value={theme}
      options={options}
      onValueChange={setTheme}
    />
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
