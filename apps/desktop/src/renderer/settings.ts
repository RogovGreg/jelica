import type { Locale } from "../../../../packages/app-platform/src/i18n";
import { INTERFACE_SCALE_STEPS, parseInterfaceScale, type InterfaceScale } from "../../../../packages/app-platform/src/theme";

export const SCALE_STEPS = INTERFACE_SCALE_STEPS;
export type Scale = InterfaceScale;
export function parseScale(value: string | null | undefined): Scale { return parseInterfaceScale(value); }
export function parseTheme(value: string | null | undefined): "system" | "light" | "dark" | "mono" { return value === "light" || value === "dark" || value === "mono" ? value : "system"; }
export function parseLocale(value: string | null | undefined, fallback: Locale): Locale { return value === "en" || value === "ru" || value === "sr-Latn" || value === "sr-Cyrl" ? value : fallback; }
