"use client";

import { useEffect, useState } from "react";

import { getCurrentUser, updateCurrentUserPreferences } from "@/lib/api/client";

const STORAGE_KEY = "jelica-web-theme";

export type UiTheme = "system" | "light" | "dark" | "mono";

export function useTheme(initialTheme: UiTheme) {
  const [theme, setThemeState] = useState<UiTheme>(initialTheme);

  useEffect(() => {
    let active = true;
    const cached = readCachedTheme(initialTheme);
    applyTheme(cached);
    setThemeState(cached);

    const syncFromServer = () => {
      void getCurrentUser()
        .then((user) => {
          if (!active) return;
          applyTheme(user.theme);
          window.localStorage.setItem(STORAGE_KEY, user.theme);
          setThemeState(user.theme);
        })
        .catch(() => undefined);
    };

    syncFromServer();
    window.addEventListener("jelica-auth-changed", syncFromServer);
    return () => {
      active = false;
      window.removeEventListener("jelica-auth-changed", syncFromServer);
    };
  }, [initialTheme]);

  const setTheme = (value: UiTheme) => {
    applyTheme(value);
    window.localStorage.setItem(STORAGE_KEY, value);
    setThemeState(value);
    void updateCurrentUserPreferences({ theme: value })
      .then((user) => {
        applyTheme(user.theme);
        window.localStorage.setItem(STORAGE_KEY, user.theme);
        setThemeState(user.theme);
      })
      .catch(() => undefined);
  };

  return {
    theme,
    setTheme,
  };
}

function readCachedTheme(fallback: UiTheme): UiTheme {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "system" || stored === "light" || stored === "dark" || stored === "mono"
    ? stored
    : fallback;
}

function applyTheme(theme: UiTheme) {
  document.documentElement.setAttribute("data-theme", theme);
}
