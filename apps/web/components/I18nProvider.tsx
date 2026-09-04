"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { usePathname, useRouter } from "next/navigation";

import {
  createTranslator,
  DEFAULT_LOCALE,
  resolveLocale,
  type Locale,
  type Translator,
} from "@/lib/i18n";
import { getCurrentUser, updateCurrentUserPreferences } from "@/lib/api/client";

const STORAGE_KEY = "jelica-web-locale";
const DOCUMENTATION_SWITCH_COOKIE = "jelica-doc-locale-switch";

type I18nContextValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: Translator;
};

const I18nContext = createContext<I18nContextValue | null>(null);

type I18nProviderProps = Readonly<{
  children: ReactNode;
  initialLocale?: Locale;
}>;

export function I18nProvider({
  children,
  initialLocale = DEFAULT_LOCALE,
}: I18nProviderProps) {
  const pathname = usePathname();
  const router = useRouter();
  const [locale, setLocaleState] = useState<Locale>(() => resolveLocale(initialLocale));

  useEffect(() => {
    let active = true;
    const syncGuestCache = () => {
      const storedLocale = window.localStorage.getItem(STORAGE_KEY);
      const requestedLocale = storedLocale || window.navigator.language || initialLocale;
      return resolveLocale(requestedLocale);
    };
    const applyLocale = (nextLocale: Locale) => {
      setLocaleState(nextLocale);
      applyDocumentLocale(nextLocale);
      document.cookie = `${STORAGE_KEY}=${encodeURIComponent(nextLocale)}; Path=/; Max-Age=31536000; SameSite=Lax`;
    };
    const cachedLocale = syncGuestCache();
    applyLocale(cachedLocale);
    if (cachedLocale !== initialLocale) router.refresh();

    const syncFromServer = () => {
      void getCurrentUser()
        .then((user) => {
          if (!active) return;
          const serverLocale = resolveLocale(user.language);
          const needsRefresh = serverLocale !== cachedLocale;
          window.localStorage.setItem(STORAGE_KEY, serverLocale);
          applyLocale(serverLocale);
          if (needsRefresh) router.refresh();
        })
        .catch(() => undefined);
    };

    syncFromServer();
    window.addEventListener("jelica-auth-changed", syncFromServer);
    return () => {
      active = false;
      window.removeEventListener("jelica-auth-changed", syncFromServer);
    };
  }, [initialLocale, router]);

  const setLocale = useCallback((nextLocale: Locale) => {
    const resolvedLocale = resolveLocale(nextLocale);
    window.localStorage.setItem(STORAGE_KEY, resolvedLocale);
    if (pathname.startsWith("/docs/") && pathname !== "/docs/download") {
      document.cookie = `${DOCUMENTATION_SWITCH_COOKIE}=1; Path=/docs; Max-Age=60; SameSite=Lax`;
    }
    document.cookie = `${STORAGE_KEY}=${encodeURIComponent(resolvedLocale)}; Path=/; Max-Age=31536000; SameSite=Lax`;
    setLocaleState(resolvedLocale);
    applyDocumentLocale(resolvedLocale);
    router.refresh();
    void updateCurrentUserPreferences({ language: resolvedLocale })
      .then((user) => {
        const confirmedLocale = resolveLocale(user.language);
        window.localStorage.setItem(STORAGE_KEY, confirmedLocale);
        setLocaleState(confirmedLocale);
        applyDocumentLocale(confirmedLocale);
      })
      .catch(() => undefined);
  }, [pathname, router]);

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale,
      t: createTranslator(locale),
    }),
    [locale, setLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (!value) {
    throw new Error("useI18n must be used within I18nProvider");
  }
  return value;
}

function applyDocumentLocale(locale: Locale) {
  document.documentElement.lang = locale;
}
