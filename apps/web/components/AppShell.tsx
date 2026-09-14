"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import {
  formatShellDocumentTitle,
  SHELL_PRIMARY_NAVIGATION,
  SHELL_WEB_APP_NAVIGATION,
  type ShellBreadcrumb,
  type ShellPrimaryNavigationId,
  type ShellWebAppNavigationId,
} from "../../../packages/app-platform/src/shell";
import { ShellPageHeader } from "../../../packages/app-platform/src/shell-ui";
import { AuthControls } from "@/components/auth/AuthControls";
import { useI18n } from "@/components/I18nProvider";
import { LocaleSwitcher } from "@/components/LocaleSwitcher";
import { ThemeSwitcher } from "@/components/ThemeSwitcher";
import type { Locale, TranslationKey, Translator } from "@/lib/i18n";

const PRIMARY_ROUTES: Record<Exclude<ShellPrimaryNavigationId, "web-app">, string> = {
  news: "/news",
  about: "/about",
  download: "/download",
  docs: "/docs",
  support: "/support",
};

const PRIMARY_LABELS: Record<Exclude<ShellPrimaryNavigationId, "web-app">, TranslationKey> = {
  news: "nav.news",
  about: "nav.about",
  download: "nav.download",
  docs: "nav.documentation",
  support: "nav.support",
};

const WEB_APP_ROUTES: Record<ShellWebAppNavigationId, string> = {
  tasks: "/app/tasks",
  projects: "/app/projects",
  results: "/app/results",
};

const WEB_APP_LABELS: Record<ShellWebAppNavigationId, TranslationKey> = {
  tasks: "app.nav.tasks",
  projects: "page.projects.title",
  results: "app.nav.results",
};

type AppShellProps = Readonly<{
  children: ReactNode;
  locales: readonly Locale[];
  version: string;
}>;

export function AppShell({ children, locales, version }: AppShellProps) {
  const pathname = usePathname();
  const { t } = useI18n();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [webAppOpen, setWebAppOpen] = useState(pathname.startsWith("/app"));
  const [authenticated, setAuthenticated] = useState(false);
  const handleAuthenticatedChange = useCallback((value: boolean) => setAuthenticated(value), []);
  const routeContext = useMemo(() => getRouteContext(pathname, t), [pathname, t]);

  useEffect(() => {
    setDrawerOpen(false);
    if (pathname.startsWith("/app")) setWebAppOpen(true);
  }, [pathname]);

  useEffect(() => {
    if (pathname === "/" || routeContext.title) {
      document.title = formatShellDocumentTitle(routeContext.title);
    }
  }, [pathname, routeContext.title]);

  useEffect(() => {
    if (!drawerOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) setDrawerOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [drawerOpen]);

  const webAppActive = pathname.startsWith("/app");

  return (
    <div className="app-shell">
      <aside id="primary-sidebar" className={`app-sidebar${drawerOpen ? " is-open" : ""}`}>
        <div className="sidebar-top">
          <Link href="/" className="brand" aria-label={t("common.action.home")}>
            <Image
              src="/jelica-app-lockup.svg"
              alt="JELICA"
              width={3840}
              height={2048}
              className="brand-logo jelica-app-lockup"
              priority
            />
          </Link>
          <span
            className="sidebar-collapse-affordance"
            aria-label={t("shell.collapse-unavailable")}
            title={t("shell.collapse-unavailable")}
          >
            ‹
          </span>
        </div>

        <nav className="sidebar-navigation" aria-label={t("common.navigation.main-label")}>
          {SHELL_PRIMARY_NAVIGATION.map((item) => {
            if (item === "web-app") {
              return (
                <div key={item} className={`sidebar-navigation-group${webAppActive ? " is-active" : ""}`}>
                  <button
                    type="button"
                    className="sidebar-group-toggle"
                    aria-expanded={webAppOpen}
                    aria-controls="web-app-navigation"
                    onClick={() => setWebAppOpen((value) => !value)}
                  >
                    <span>{t("shell.web-app")}</span>
                    <span aria-hidden="true">{webAppOpen ? "−" : "+"}</span>
                  </button>
                  {webAppOpen ? (
                    <div id="web-app-navigation" className="sidebar-subnavigation">
                      {SHELL_WEB_APP_NAVIGATION.map((child) => {
                        if (child === "projects" && !authenticated) return null;
                        const href = WEB_APP_ROUTES[child];
                        return (
                          <Link key={child} href={href} aria-current={isRouteActive(pathname, href) ? "page" : undefined}>
                            {t(WEB_APP_LABELS[child])}
                          </Link>
                        );
                      })}
                    </div>
                  ) : null}
                </div>
              );
            }
            const href = PRIMARY_ROUTES[item];
            return (
              <Link key={item} href={href} aria-current={isRouteActive(pathname, href) ? "page" : undefined}>
                {t(PRIMARY_LABELS[item])}
              </Link>
            );
          })}
        </nav>

        <div className="sidebar-utilities">
          <ThemeSwitcher />
          <LocaleSwitcher locales={locales} />
          <span className="sidebar-placeholder" aria-label={t("shell.help-placeholder")}>{t("shell.help")}</span>
          <Link href="/settings" aria-current={isRouteActive(pathname, "/settings") ? "page" : undefined}>
            {t("page.settings.title")}
          </Link>
          <AuthControls onAuthenticatedChange={handleAuthenticatedChange} />
          <div className="sidebar-version">{t("shell.version", { version })}</div>
        </div>
      </aside>

      {drawerOpen ? (
        <button
          type="button"
          className="sidebar-scrim"
          aria-label={t("shell.close-navigation")}
          onClick={() => setDrawerOpen(false)}
        />
      ) : null}

      <div className="app-content">
        <ShellPageHeader
          breadcrumbs={routeContext.breadcrumbs}
          breadcrumbLabel={t("breadcrumbs.label")}
          commandLabel={t("shell.command-label")}
          commandPlaceholder={t("shell.command-placeholder")}
          menuControl={<button
            type="button"
            className="sidebar-menu-button"
            aria-controls="primary-sidebar"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen((value) => !value)}
          >
            <span aria-hidden="true">☰</span>
            <span>{t("shell.open-navigation")}</span>
          </button>}
          renderBreadcrumbLink={(item) => <Link href={item.href!}>{item.label}</Link>}
        />
        <main className="app-main">{children}</main>
      </div>
    </div>
  );
}

function isRouteActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function getRouteContext(pathname: string, t: Translator): {
  title: string | null;
  breadcrumbs: ShellBreadcrumb[];
} {
  if (pathname === "/") return { title: null, breadcrumbs: [] };

  const directRoutes: Record<string, TranslationKey> = {
    "/news": "nav.news",
    "/about": "nav.about",
    "/download": "nav.download",
    "/docs": "nav.documentation",
    "/support": "nav.support",
    "/app/support": "nav.support",
    "/settings": "page.settings.title",
    "/app/profile": "page.profile.title",
    "/app/notifications": "page.notifications.title",
    "/license": "public.legal.license-title",
    "/terms": "public.legal.terms-title",
    "/privacy": "public.legal.privacy-title",
  };
  const directKey = directRoutes[pathname];
  if (directKey) {
    const title = t(directKey);
    return { title, breadcrumbs: [{ label: title }] };
  }

  const appSection = SHELL_WEB_APP_NAVIGATION.find((item) => isRouteActive(pathname, WEB_APP_ROUTES[item]));
  if (appSection) {
    const nestedTask = pathname.match(/^\/app\/projects\/[^/]+\/tasks\/([^/]+)/)?.[1];
    const effectiveSection = nestedTask ? "tasks" : appSection;
    const sectionTitle = t(WEB_APP_LABELS[effectiveSection]);
    const breadcrumbs: ShellBreadcrumb[] = [
      { label: t("shell.web-app"), href: "/app/tasks" },
      { label: sectionTitle, href: pathname === WEB_APP_ROUTES[effectiveSection] ? undefined : WEB_APP_ROUTES[effectiveSection] },
    ];
    const segments = pathname.split("/").filter(Boolean);
    const directTask = pathname.match(/^\/app\/tasks\/([^/]+)/)?.[1];
    const directResult = pathname.match(/^\/app\/results\/([^/]+)/)?.[1];
    const project = pathname.match(/^\/app\/projects\/([^/]+)/)?.[1];
    const itemId = nestedTask ?? directTask ?? directResult ?? project ?? null;
    if (itemId && itemId !== "new") breadcrumbs.push({ label: itemId });
    if (segments.at(-1) === "discussion") breadcrumbs.push({ label: t("discussion.title") });
    const title = breadcrumbs.at(-1)?.label ?? sectionTitle;
    return { title, breadcrumbs };
  }

  if (pathname.startsWith("/news/")) {
    return { title: t("nav.news"), breadcrumbs: [{ label: t("nav.news"), href: "/news" }] };
  }
  if (pathname.startsWith("/docs/")) {
    return { title: t("nav.documentation"), breadcrumbs: [{ label: t("nav.documentation"), href: "/docs" }] };
  }

  return { title: null, breadcrumbs: [] };
}
