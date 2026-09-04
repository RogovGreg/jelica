import fs from "node:fs";
import path from "node:path";

import { isSupportedLocale, type Locale } from "@/lib/i18n";

export type NewsItem = {
  slug: string;
  title: string;
  summary: string;
  date: string;
  image?: string;
  content: string;
};

const CONTENT_ROOT = resolveContentRoot();
const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const NEWS_METADATA_KEYS = new Set(["slug", "title", "date", "summary", "image"]);

export function listNews(locale: Locale = "en"): readonly NewsItem[] {
  const englishItems = readNewsLocale("en");
  if (locale === "en") return englishItems;

  const itemsBySlug = new Map(englishItems.map((item) => [item.slug, item]));
  for (const item of readNewsLocale(locale)) itemsBySlug.set(item.slug, item);
  return sortNews([...itemsBySlug.values()]);
}

export function getNewsBySlug(slug: string, locale: Locale = "en"): NewsItem | null {
  const normalized = slug.trim();
  if (!SLUG_PATTERN.test(normalized)) return null;
  return listNews(locale).find((item) => item.slug === normalized) ?? null;
}

export function listLatestNews(limit: number, locale: Locale = "en"): readonly NewsItem[] {
  return listNews(locale).slice(0, Math.max(0, limit));
}

export function parseNewsMarkdown(source: string, fileName = "news article"): NewsItem {
  const { metadata, content } = parseFrontMatter(source, fileName);
  const slug = metadata.slug;
  if (!slug || !SLUG_PATTERN.test(slug)) throw new Error(`Invalid news slug in ${fileName}.`);
  if (!metadata.title || !metadata.summary || !metadata.date) {
    throw new Error(`News metadata is incomplete in ${fileName}.`);
  }
  if (!isIsoDate(metadata.date)) throw new Error(`Invalid news date in ${fileName}.`);
  return {
    slug,
    title: metadata.title,
    summary: metadata.summary,
    date: metadata.date,
    ...(metadata.image ? { image: metadata.image } : {}),
    content,
  };
}

export function resolveNewsPath(locale: string, slug: string): string | null {
  if (!isSupportedLocale(locale) || !SLUG_PATTERN.test(slug)) return null;
  return resolveContentPath("news", locale, `${slug}.md`);
}

function readNewsLocale(locale: Locale): NewsItem[] {
  const localeRoot = resolveContentPath("news", locale);
  if (!localeRoot || !fs.existsSync(localeRoot) || !fs.statSync(localeRoot).isDirectory()) return [];
  return sortNews(
    fs
      .readdirSync(localeRoot, { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name.endsWith(".md"))
      .sort((left, right) => left.name.localeCompare(right.name))
      .map((entry) => {
        const articlePath = path.join(localeRoot, entry.name);
        const item = parseNewsMarkdown(fs.readFileSync(articlePath, "utf8"), articlePath);
        if (item.slug !== entry.name.slice(0, -3)) {
          throw new Error(`News filename and slug differ in ${articlePath}.`);
        }
        return item;
      }),
  );
}

function sortNews(items: NewsItem[]): NewsItem[] {
  return items.sort((left, right) => right.date.localeCompare(left.date) || left.slug.localeCompare(right.slug));
}

function parseFrontMatter(source: string, fileName: string): { metadata: Record<string, string>; content: string } {
  const lines = source.replaceAll("\r\n", "\n").split("\n");
  if (lines[0] !== "---") throw new Error(`News article must start with front matter: ${fileName}.`);
  const end = lines.indexOf("---", 1);
  if (end < 0) throw new Error(`News front matter is not closed: ${fileName}.`);

  const metadata: Record<string, string> = {};
  for (const line of lines.slice(1, end)) {
    const separator = line.indexOf(":");
    if (separator <= 0) throw new Error(`Invalid news metadata line in ${fileName}.`);
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (!NEWS_METADATA_KEYS.has(key) || key in metadata || value === "") {
      throw new Error(`Invalid news metadata key or value in ${fileName}.`);
    }
    metadata[key] = value;
  }
  return { metadata, content: lines.slice(end + 1).join("\n").trim() };
}

function isIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.valueOf()) && date.toISOString().slice(0, 10) === value;
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
