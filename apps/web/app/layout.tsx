import type { Metadata } from "next";
import { cookies } from "next/headers";
import Script from "next/script";
import type { ReactNode } from "react";

import packageMetadata from "../package.json";
import { AppShell } from "@/components/AppShell";
import { I18nProvider } from "@/components/I18nProvider";
import { JELICA_LOCALE_COOKIE } from "@/lib/documentation/request";
import { DEFAULT_LOCALE, isSupportedLocale } from "@/lib/i18n";
import { DevelopmentNoticeModal } from "@/components/DevelopmentNoticeModal";
import { HOME_TITLE, SITE_DESCRIPTION, SITE_NAME, SITE_URL, SOCIAL_IMAGE_PATH } from "@/lib/seo";

import "./globals.css";

const defaultTheme = normalizeTheme(process.env.NEXT_PUBLIC_DEFAULT_THEME);

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: SITE_NAME,
    template: `${SITE_NAME} | %s`,
  },
  applicationName: "JELICA Web",
  description: SITE_DESCRIPTION,
  icons: {
    icon: [
      { url: "/favicon.svg", type: "image/svg+xml" },
      { url: "/favicon.ico", sizes: "any" },
    ],
    apple: "/apple-touch-icon.png",
  },
  openGraph: {
    title: HOME_TITLE,
    description: SITE_DESCRIPTION,
    type: "website",
    siteName: SITE_NAME,
    url: SITE_URL,
    images: [{ url: SOCIAL_IMAGE_PATH, width: 1200, height: 630, alt: HOME_TITLE }],
  },
  twitter: {
    card: "summary_large_image",
    title: HOME_TITLE,
    description: SITE_DESCRIPTION,
    images: [SOCIAL_IMAGE_PATH],
  },
};

type RootLayoutProps = Readonly<{
  children: ReactNode;
}>;

export default function RootLayout({ children }: RootLayoutProps) {
  const storedLocale = cookies().get(JELICA_LOCALE_COOKIE)?.value;
  const initialLocale = storedLocale && isSupportedLocale(storedLocale) ? storedLocale : DEFAULT_LOCALE;
  const availableLocales = [DEFAULT_LOCALE] as const;
  return (
    <html lang={initialLocale} data-theme={defaultTheme} suppressHydrationWarning>
      <head>
        <Script id="jelica-preferences-bootstrap" strategy="beforeInteractive">
          {`(() => {
  try {
    const storedTheme = window.localStorage.getItem("jelica-web-theme");
    if (["system", "light", "dark", "mono"].includes(storedTheme || "")) {
      document.documentElement.dataset.theme = storedTheme;
    }
    const storedScale = Number(window.localStorage.getItem("jelica-web-scale"));
    if ([80, 100, 125, 150].includes(storedScale)) {
      document.documentElement.style.setProperty("--ui-scale", String(storedScale / 100));
    }
  } catch {
    // The server-provided defaults remain the fallback when browser storage is unavailable.
  }
})();`}
        </Script>
      </head>
      <body>
        <I18nProvider initialLocale={initialLocale}>
          <AppShell locales={availableLocales} version={packageMetadata.version}>
            {children}
          </AppShell>
          <DevelopmentNoticeModal />
        </I18nProvider>
      </body>
    </html>
  );
}

function normalizeTheme(rawTheme: string | undefined): "system" | "light" | "dark" | "mono" {
  if (rawTheme === "system") {
    return "system";
  }
  if (rawTheme === "dark") {
    return "dark";
  }
  if (rawTheme === "mono") {
    return "mono";
  }
  return "light";
}
