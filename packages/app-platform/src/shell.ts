export const SHELL_PRIMARY_NAVIGATION = [
  "download",
  "web-app",
  "docs",
  "support",
  "news",
  "about",
] as const;

export const SHELL_WEB_APP_NAVIGATION = ["tasks", "projects", "results"] as const;

export type ShellPrimaryNavigationId = (typeof SHELL_PRIMARY_NAVIGATION)[number];
export type ShellWebAppNavigationId = (typeof SHELL_WEB_APP_NAVIGATION)[number];

export type ShellBreadcrumb = Readonly<{
  label: string;
  href?: string;
}>;

export function formatShellDocumentTitle(pageTitle?: string | null): string {
  const title = pageTitle?.trim();
  return title ? `JELICA | ${title}` : "JELICA";
}

export function isEditableCommandTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return Boolean(
    target.closest(
      'input, textarea, select, [contenteditable=""], [contenteditable="true"], [role="textbox"]',
    ),
  );
}
