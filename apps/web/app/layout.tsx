import type { Metadata } from "next";
import { cookies } from "next/headers";
import Image from "next/image";
import Link from "next/link";
import Script from "next/script";
import type { ReactNode } from "react";

import { AuthControls } from "@/components/auth/AuthControls";
import { I18nProvider } from "@/components/I18nProvider";
import { LocaleSwitcher } from "@/components/LocaleSwitcher";
import { PublicNavigation } from "@/components/PublicNavigation";
import { ThemeSwitcher } from "@/components/ThemeSwitcher";
import { JELICA_LOCALE_COOKIE } from "@/lib/documentation/request";
import { DEFAULT_LOCALE, isSupportedLocale } from "@/lib/i18n";
import { discoverUiLocales } from "@/lib/i18n/discovery";
import { DevelopmentNoticeModal } from "@/components/DevelopmentNoticeModal";
import { HOME_TITLE, SITE_DESCRIPTION, SITE_NAME, SITE_URL, SOCIAL_IMAGE_PATH } from "@/lib/seo";

import "./globals.css";

const defaultTheme = normalizeTheme(process.env.NEXT_PUBLIC_DEFAULT_THEME);

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: HOME_TITLE,
    template: `%s | ${SITE_NAME}`,
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
  const availableLocales = discoverUiLocales();
  return (
    <html lang={initialLocale} data-theme={defaultTheme} suppressHydrationWarning>
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
      <body>
        <I18nProvider initialLocale={initialLocale}>
          <div className="app-shell">
            <header className="app-header">
              <Link href="/" className="brand">
                <Image
                  src="/jelica-app-lockup.svg"
                  alt="JELICA"
                  width={3840}
                  height={2048}
                  className="brand-logo jelica-app-lockup"
                  priority
                />
              </Link>
              <PublicNavigation />
              <AuthControls />
              <LocaleSwitcher locales={availableLocales} />
              <ThemeSwitcher />
            </header>
            <main className="app-main">{children}</main>
          </div>
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
