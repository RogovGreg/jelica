import "server-only";

import { cookies } from "next/headers";

import { isDocumentationLocale, type DocumentationLocale } from "../../../../packages/app-platform/src/documentation";
import { documentationTextSizeForScale, parseInterfaceScale, type DocumentationPresentationTextSize } from "../../../../packages/app-platform/src/theme";

export const JELICA_LOCALE_COOKIE = "jelica-web-locale";
export const JELICA_DOCUMENTATION_SWITCH_COOKIE = "jelica-doc-locale-switch";
export const JELICA_INTERFACE_SCALE_COOKIE = "jelica-web-scale";

export function requestedDocumentationLocale(): DocumentationLocale {
  const value = cookies().get(JELICA_LOCALE_COOKIE)?.value;
  return value && isDocumentationLocale(value) ? value : "en";
}

export function documentationLocaleSwitchRequested(): boolean {
  return cookies().get(JELICA_DOCUMENTATION_SWITCH_COOKIE)?.value === "1";
}

export function requestedDocumentationTextSize(): DocumentationPresentationTextSize {
  const scale = parseInterfaceScale(cookies().get(JELICA_INTERFACE_SCALE_COOKIE)?.value);
  return documentationTextSizeForScale(scale);
}
