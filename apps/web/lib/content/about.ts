import fs from "node:fs";
import path from "node:path";

import { isSupportedLocale, type Locale } from "@/lib/i18n";

const CONTENT_ROOT = resolveContentRoot();

export function loadAbout(locale: Locale = "en"): string | null {
  const requestedPath = resolveAboutPath(locale);
  const englishPath = resolveAboutPath("en");
  const articlePath = requestedPath && fs.existsSync(requestedPath) ? requestedPath : englishPath;
  if (!articlePath || !fs.existsSync(articlePath) || !fs.statSync(articlePath).isFile()) return null;
  return fs.readFileSync(articlePath, "utf8").replaceAll("\r\n", "\n").trim();
}

export function resolveAboutPath(locale: string): string | null {
  if (!isSupportedLocale(locale)) return null;
  return resolveContentPath("about", `${locale}.md`);
}

function resolveContentRoot(): string {
  const candidates = [path.resolve(process.cwd(), "content"), path.resolve(process.cwd(), "../../content")];
  return candidates.find((candidate) => fs.existsSync(candidate)) ?? candidates[0];
}

function resolveContentPath(...segments: string[]): string | null {
  if (segments.some((segment) => segment === "" || segment === "." || segment === ".." || segment.includes("/") || segment.includes("\\"))) return null;
  const resolved = path.resolve(CONTENT_ROOT, ...segments);
  const rootWithSeparator = `${CONTENT_ROOT}${path.sep}`;
  return resolved.startsWith(rootWithSeparator) ? resolved : null;
}
