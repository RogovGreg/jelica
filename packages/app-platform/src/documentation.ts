export const DOCUMENTATION_LOCALES = ["en", "ru", "sr-Latn", "sr-Cyrl"] as const;
export const DOCUMENTATION_PROFILES = ["screen", "print"] as const;
export const DOCUMENTATION_TEXT_SIZES = ["small", "standard", "large"] as const;
export const DEFAULT_DOCUMENTATION_RESULT_LIMIT = 20;

export type DocumentationLocale = (typeof DOCUMENTATION_LOCALES)[number];
export type DocumentationProfile = (typeof DOCUMENTATION_PROFILES)[number];
export type DocumentationTextSize = (typeof DOCUMENTATION_TEXT_SIZES)[number];
export type DocumentationPage = Readonly<{ id: string; title: string; path: string }>;
export type DocumentationHeading = Readonly<{ id: string; title: string; path: string; anchor: string }>;
export type DocumentationPageReference = Readonly<{ path: string; anchor: string }>;
export type DocumentationSection = Readonly<{ id: string; title: string; keywords: readonly string[]; anchor: string; pages: readonly DocumentationPageReference[]; headings: readonly DocumentationHeading[] }>;
export type DocumentationManifest = Readonly<{ schemaVersion: number; locale: string; title: string; subtitle: string; version: string; year: number; profile: string; textSize: string; paths: Readonly<Record<string, string | null>>; sections: readonly DocumentationSection[]; pages: readonly DocumentationPage[] }>;
export type DocumentationSearchDocument = Readonly<{ id: string; title: string; headings: readonly string[]; headingAnchors?: readonly string[]; keywords: readonly string[]; path: string; anchor: string; content: string }>;
export type DocumentationSearchIndex = Readonly<{ schemaVersion: number; locale: string; title: string; version: string; fieldPriority: readonly string[]; documents: readonly DocumentationSearchDocument[] }>;
export type DocumentationRelease = Readonly<{ releaseVersion: string; artifactFormatVersion: number; locale: string; profile: string; textSize: string; generatedAt: string; sourceHash: string }>;
export type DocumentationVersion = Readonly<{ artifactFormatVersion: number; documentationVersion: string }>;
export type DocumentationBundle = Readonly<{ manifest: DocumentationManifest; searchIndex: DocumentationSearchIndex; release: DocumentationRelease; version: DocumentationVersion }>;
export type DocumentationSearchResult = Readonly<{ id: string; title: string; path: string; anchor: string }>;
export type DocumentationSelection = Readonly<{ documentationVersion: string; artifactFormatVersion: number; locale: DocumentationLocale; profile: DocumentationProfile; textSize: DocumentationTextSize }>;
export type DocumentationLocation = Readonly<{ pageId: string | null; anchor: string }>;

export function isDocumentationLocale(value: string): value is DocumentationLocale {
  return DOCUMENTATION_LOCALES.includes(value as DocumentationLocale);
}

export function isDocumentationProfile(value: string): value is DocumentationProfile {
  return DOCUMENTATION_PROFILES.includes(value as DocumentationProfile);
}

export function isDocumentationTextSize(value: string): value is DocumentationTextSize {
  return DOCUMENTATION_TEXT_SIZES.includes(value as DocumentationTextSize);
}

export function documentationSelectionFallbacks(selection: DocumentationSelection): readonly DocumentationSelection[] {
  const candidates: DocumentationSelection[] = [selection];
  const add = (locale: DocumentationLocale, textSize: DocumentationTextSize) => {
    const candidate = { ...selection, locale, textSize };
    if (!candidates.some((item) => sameDocumentationSelection(item, candidate))) candidates.push(candidate);
  };
  add(selection.locale, "standard");
  add("en", "standard");
  return candidates;
}

export function matchesDocumentationSelection(release: DocumentationRelease, selection: DocumentationSelection): boolean {
  return release.releaseVersion === selection.documentationVersion && release.artifactFormatVersion === selection.artifactFormatVersion && release.locale === selection.locale && release.profile === selection.profile && release.textSize === selection.textSize;
}

export function searchDocumentation(index: Pick<DocumentationSearchIndex, "documents">, query: string, limit = DEFAULT_DOCUMENTATION_RESULT_LIMIT): readonly DocumentationSearchResult[] {
  const needle = normalizeSearchText(query);
  if (!needle || !Number.isInteger(limit) || limit <= 0) return [];
  return index.documents
    .map((document, order) => searchMatch(document, needle, order))
    .filter((item): item is NonNullable<typeof item> => item !== null)
    .sort((left, right) => left.priority - right.priority || left.matchKind - right.matchKind || left.order - right.order)
    .slice(0, limit)
    .map(({ document, anchor }) => ({ id: document.id, title: document.title, path: document.path, anchor }));
}

export function preserveDocumentationLocation(manifest: DocumentationManifest, location: DocumentationLocation): DocumentationLocation {
  if (!location.pageId) return { pageId: null, anchor: "" };
  const page = manifest.pages.find((item) => item.id === location.pageId);
  if (!page) return { pageId: null, anchor: "" };
  if (!location.anchor) return { pageId: page.id, anchor: "" };
  const anchors = new Set<string>();
  for (const section of manifest.sections) {
    if (section.pages.some((item) => item.path === page.path)) anchors.add(section.anchor);
    for (const heading of section.headings) if (heading.path === page.path) anchors.add(heading.anchor);
  }
  return { pageId: page.id, anchor: anchors.has(location.anchor) ? location.anchor : "" };
}

export function documentationPageHref(pageId: string, anchor = ""): string {
  if (!/^[A-Za-z0-9._-]+$/.test(pageId) || (anchor && !/^#[A-Za-z0-9._:-]+$/.test(anchor))) throw new Error("Invalid documentation reference");
  return `${encodeURIComponent(pageId)}${anchor}`;
}

function sameDocumentationSelection(left: DocumentationSelection, right: DocumentationSelection): boolean {
  return left.documentationVersion === right.documentationVersion && left.artifactFormatVersion === right.artifactFormatVersion && left.locale === right.locale && left.profile === right.profile && left.textSize === right.textSize;
}

function normalizeSearchText(value: string): string {
  return value.trim().normalize("NFKC").toLocaleLowerCase();
}

function matchKind(value: string, needle: string): number | null {
  const normalized = normalizeSearchText(value);
  if (normalized === needle) return 0;
  if (normalized.startsWith(needle)) return 1;
  return normalized.includes(needle) ? 2 : null;
}

function searchMatch(document: DocumentationSearchDocument, needle: string, order: number) {
  const titleMatch = matchKind(document.title, needle);
  if (titleMatch !== null) return { document, anchor: document.anchor, priority: 0, matchKind: titleMatch, order };
  for (let index = 0; index < document.headings.length; index += 1) {
    const headingMatch = matchKind(document.headings[index] ?? "", needle);
    if (headingMatch !== null) return { document, anchor: document.headingAnchors?.[index] ?? document.anchor, priority: 1, matchKind: headingMatch, order };
  }
  const keywordMatches = document.keywords.map((item) => matchKind(item, needle)).filter((item): item is number => item !== null);
  if (keywordMatches.length) return { document, anchor: document.anchor, priority: 2, matchKind: Math.min(...keywordMatches), order };
  const bodyMatch = matchKind(document.content, needle);
  return bodyMatch === null ? null : { document, anchor: document.anchor, priority: 3, matchKind: bodyMatch, order };
}
