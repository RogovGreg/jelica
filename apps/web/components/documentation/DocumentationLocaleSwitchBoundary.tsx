"use client";

import { useEffect } from "react";

const DOCUMENTATION_SWITCH_COOKIE = "jelica-doc-locale-switch";

type DocumentationLocaleSwitchBoundaryProps = Readonly<{
  anchors?: readonly string[];
}>;

export function DocumentationLocaleSwitchBoundary({
  anchors,
}: DocumentationLocaleSwitchBoundaryProps) {
  useEffect(() => {
    const switchedLocale = document.cookie
      .split(";")
      .some((cookie) => cookie.trim() === `${DOCUMENTATION_SWITCH_COOKIE}=1`);
    if (!switchedLocale) {
      return;
    }

    if (window.location.hash && anchors && !anchors.includes(window.location.hash)) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    }
    document.cookie = `${DOCUMENTATION_SWITCH_COOKIE}=; Path=/docs; Max-Age=0; SameSite=Lax`;
  }, [anchors]);

  return null;
}
