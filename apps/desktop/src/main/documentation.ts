import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import {
  documentationSelectionFallbacks,
  isDocumentationLocale,
  isDocumentationProfile,
  isDocumentationTextSize,
  matchesDocumentationSelection,
  type DocumentationBundle,
  type DocumentationPage,
  type DocumentationSearchIndex,
  type DocumentationSelection,
} from "../../../../packages/app-platform/src/documentation";

const FILES = ["documentation-manifest.json", "search-index.json", "release.json", "version.json"] as const;
const MIME: Record<string, string> = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".pdf": "application/pdf", ".png": "image/png" };
const MAX_DISCOVERY_DEPTH = 8;

type LocatedBundle = Readonly<{ root: string; bundle: DocumentationBundle; allowed: ReadonlySet<string>; key: string }>;
export type DocumentationSelectionRequest = Partial<DocumentationSelection>;

export class DocumentationResourceResolver {
  readonly #defaultSelection: DocumentationSelection;
  readonly #releases: readonly LocatedBundle[];
  readonly #byKey = new Map<string, LocatedBundle>();

  constructor(options: { packaged: boolean; appPath: string; environment?: NodeJS.ProcessEnv; cwd?: string }) {
    const env = options.environment ?? process.env;
    this.#defaultSelection = selectionFromEnvironment(env);
    const candidate = options.packaged
      ? path.join(options.appPath, "resources", "documentation")
      : env.JELICA_DOCUMENTATION_RELEASE_DIR?.trim() || path.resolve(options.cwd ?? process.cwd(), "../../docs/documentation/releases");
    let releases: LocatedBundle[] = [];
    try {
      const root = realDirectory(candidate);
      for (const directory of discoverReleaseDirectories(root)) {
        try { releases.push(loadBundle(directory)); } catch { /* Invalid releases remain unavailable. */ }
      }
    } catch {
      releases = [];
    }
    this.#releases = releases;
    const duplicateKeys = new Set<string>();
    for (const release of releases) {
      if (this.#byKey.has(release.key)) duplicateKeys.add(release.key);
      else this.#byKey.set(release.key, release);
    }
    for (const key of duplicateKeys) this.#byKey.delete(key);
  }

  get root(): string | null { return this.locate()?.root ?? null; }
  get bundle(): DocumentationBundle | null { return this.locate()?.bundle ?? null; }
  available(request: DocumentationSelectionRequest = {}): boolean { return this.locate(request) !== null; }
  effectiveBundle(request: DocumentationSelectionRequest = {}): DocumentationBundle | null { return this.locate(request)?.bundle ?? null; }
  page(pageId: string, request: DocumentationSelectionRequest = {}): DocumentationPage | null { return this.locate(request)?.bundle.manifest.pages.find((item) => item.id === pageId) ?? null; }
  searchIndex(request: DocumentationSelectionRequest = {}): DocumentationSearchIndex | null { return this.locate(request)?.bundle.searchIndex ?? null; }
  pdfResource(request: DocumentationSelectionRequest = {}): string | null { const value = this.locate(request)?.bundle.manifest.paths.pdf; return typeof value === "string" ? value : null; }

  resourceUrl(relativePath: string, request: DocumentationSelectionRequest = {}): string | null {
    const release = this.locate(request);
    if (!release || !release.allowed.has(relativePath)) return null;
    const resource = relativePath.split("/").map(encodeURIComponent).join("/");
    return `jelica-doc://artifact/${encodeURIComponent(release.key)}/${resource}`;
  }

  nativePdfPath(request: DocumentationSelectionRequest = {}): string | null {
    const release = this.locate(request);
    const relativePath = release?.bundle.manifest.paths.pdf;
    return release && typeof relativePath === "string" && release.allowed.has(relativePath) ? path.join(release.root, ...relativePath.split("/")) : null;
  }

  serve(requestUrl: string): Response {
    try {
      const url = new URL(requestUrl);
      if (url.hostname !== "artifact") return notFound();
      const encoded = url.pathname.split("/");
      if (encoded.shift() !== "" || encoded.length < 2 || encoded.some((item) => !item)) return notFound();
      const key = decodeURIComponent(encoded.shift()!);
      const release = this.#byKey.get(key);
      const segments = encoded.map((item) => decodeURIComponent(item));
      if (!release || segments.some((item) => !/^[A-Za-z0-9._-]+$/.test(item))) return notFound();
      const relativePath = segments.join("/");
      const contentType = MIME[path.extname(relativePath).toLowerCase()];
      if (!contentType || !release.allowed.has(relativePath)) return notFound();
      const file = path.join(release.root, ...segments);
      const headers: Record<string, string> = { "content-type": contentType, "cache-control": "no-store", "x-content-type-options": "nosniff", "referrer-policy": "no-referrer" };
      if (contentType.startsWith("text/html")) headers["content-security-policy"] = "default-src 'none'; script-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:";
      return new Response(fs.readFileSync(file), { headers });
    } catch {
      return notFound();
    }
  }

  private locate(request: DocumentationSelectionRequest = {}): LocatedBundle | null {
    const selection = mergeSelection(this.#defaultSelection, request);
    if (!selection) return null;
    for (const candidate of documentationSelectionFallbacks(selection)) {
      const matches = this.#releases.filter((item) => matchesDocumentationSelection(item.bundle.release, candidate));
      if (matches.length > 1) return null;
      if (matches.length === 1) return matches[0]!;
    }
    return null;
  }
}

function selectionFromEnvironment(env: NodeJS.ProcessEnv): DocumentationSelection {
  const requested: DocumentationSelectionRequest = {
    documentationVersion: env.JELICA_DOCUMENTATION_VERSION?.trim() || "0.1",
    artifactFormatVersion: Number(env.JELICA_DOCUMENTATION_ARTIFACT_FORMAT_VERSION?.trim() || "1"),
    locale: (env.JELICA_DOCUMENTATION_LOCALE?.trim() || "en") as DocumentationSelection["locale"],
    profile: (env.JELICA_DOCUMENTATION_PROFILE?.trim() || "screen") as DocumentationSelection["profile"],
    textSize: (env.JELICA_DOCUMENTATION_TEXT_SIZE?.trim() || "standard") as DocumentationSelection["textSize"],
  };
  return mergeSelection({ documentationVersion: "0.1", artifactFormatVersion: 1, locale: "en", profile: "screen", textSize: "standard" }, requested) ?? { documentationVersion: "0.1", artifactFormatVersion: 1, locale: "en", profile: "screen", textSize: "standard" };
}

function mergeSelection(base: DocumentationSelection, request: DocumentationSelectionRequest): DocumentationSelection | null {
  const selection = { ...base, ...request };
  return isDocumentationLocale(selection.locale) && isDocumentationProfile(selection.profile) && isDocumentationTextSize(selection.textSize) && Number.isInteger(selection.artifactFormatVersion) && selection.artifactFormatVersion > 0 && /^\d+(?:\.\d+){1,2}(?:[-+][0-9A-Za-z.-]+)?$/.test(selection.documentationVersion) ? selection : null;
}

function discoverReleaseDirectories(root: string): string[] {
  const releases: string[] = [];
  const pending: Array<{ directory: string; depth: number }> = [{ directory: root, depth: 0 }];
  while (pending.length) {
    const current = pending.pop()!;
    const entries = fs.readdirSync(current.directory, { withFileTypes: true });
    if (entries.some((entry) => entry.isFile() && !entry.isSymbolicLink() && entry.name === "release.json")) {
      releases.push(current.directory);
      continue;
    }
    if (current.depth >= MAX_DISCOVERY_DEPTH) continue;
    for (const entry of entries.filter((item) => item.isDirectory() && !item.isSymbolicLink()).sort((a, b) => b.name.localeCompare(a.name))) {
      pending.push({ directory: path.join(current.directory, entry.name), depth: current.depth + 1 });
    }
  }
  return releases.sort();
}

function loadBundle(root: string): LocatedBundle {
  const allowed = verifyChecksums(root);
  const values = Object.fromEntries(FILES.map((name) => [name, JSON.parse(fs.readFileSync(path.join(root, name), "utf8"))])) as Record<string, unknown>;
  const bundle = parseBundle(values);
  for (const relativePath of referencedPaths(bundle)) if (!allowed.has(relativePath)) throw new Error("referenced artifact is not covered by checksums");
  const key = [bundle.release.releaseVersion, `v${bundle.release.artifactFormatVersion}`, bundle.release.locale, bundle.release.profile, bundle.release.textSize, bundle.release.sourceHash].join("--");
  return { root, bundle, allowed, key };
}

function parseBundle(values: Record<string, unknown>): DocumentationBundle {
  const manifest = object(values["documentation-manifest.json"]);
  const searchIndex = object(values["search-index.json"]);
  const release = object(values["release.json"]);
  const version = object(values["version.json"]);
  if (version.artifactFormatVersion !== 1 || release.artifactFormatVersion !== 1 || manifest.schemaVersion !== 1 || searchIndex.schemaVersion !== 1) throw new Error("invalid format metadata");
  for (const field of ["locale", "profile", "textSize"] as const) if (manifest[field] !== release[field]) throw new Error("inconsistent variant metadata");
  if (manifest.locale !== searchIndex.locale || manifest.version !== searchIndex.version || manifest.version !== release.releaseVersion || manifest.version !== version.documentationVersion || manifest.title !== searchIndex.title) throw new Error("inconsistent documentation metadata");
  if (!isDocumentationLocale(string(release.locale)) || !isDocumentationProfile(string(release.profile)) || !isDocumentationTextSize(string(release.textSize)) || !/^[0-9a-f]{64}$/i.test(string(release.sourceHash))) throw new Error("invalid release metadata");
  const pages = array(manifest.pages);
  const pagePaths = new Set(pages.map((value) => safePath(string(object(value).path))));
  const pageIds = pages.map((value) => stableId(string(object(value).id)));
  if (!pages.length || new Set(pageIds).size !== pageIds.length) throw new Error("invalid documentation pages");
  for (const sectionValue of array(manifest.sections)) {
    const section = object(sectionValue);
    stableId(string(section.id));
    for (const reference of array(section.pages)) if (!pagePaths.has(safePath(string(object(reference).path)))) throw new Error("unknown section page");
    for (const headingValue of array(section.headings)) { const heading = object(headingValue); stableId(string(heading.id)); if (!pagePaths.has(safePath(string(heading.path)))) throw new Error("unknown heading page"); }
  }
  const documents = array(searchIndex.documents);
  if (documents.length !== pages.length || documents.some((value) => !pageIds.includes(stableId(string(object(value).id))))) throw new Error("invalid search index");
  return { manifest, searchIndex, release, version } as unknown as DocumentationBundle;
}

function referencedPaths(bundle: DocumentationBundle): Set<string> {
  const paths = new Set<string>(FILES);
  paths.add("checksums.json");
  for (const value of Object.values(bundle.manifest.paths)) if (typeof value === "string") paths.add(safePath(value));
  for (const page of bundle.manifest.pages) paths.add(safePath(page.path));
  for (const section of bundle.manifest.sections) { for (const page of section.pages) paths.add(safePath(page.path)); for (const heading of section.headings) paths.add(safePath(heading.path)); }
  for (const document of bundle.searchIndex.documents) paths.add(safePath(document.path));
  return paths;
}

function verifyChecksums(root: string): Set<string> {
  const manifest = object(JSON.parse(fs.readFileSync(path.join(root, "checksums.json"), "utf8")));
  if (manifest.algorithm !== "SHA-256") throw new Error("unsupported checksum algorithm");
  const listed = new Map<string, { size: number; sha256: string }>();
  for (const value of array(manifest.files)) {
    const entry = object(value); const relative = safePath(string(entry.path)); const size = integer(entry.size); const sha256 = string(entry.sha256).toLowerCase();
    if (size < 0 || !/^[0-9a-f]{64}$/.test(sha256) || listed.has(relative)) throw new Error("invalid checksum entry");
    listed.set(relative, { size, sha256 });
  }
  const files = new Set<string>();
  const walk = (directory: string, prefix: string) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) throw new Error("symbolic links are not allowed");
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name; const file = path.join(directory, entry.name);
      if (entry.isDirectory()) walk(file, relative);
      else if (entry.isFile()) files.add(relative);
      else throw new Error("unsupported release entry");
    }
  };
  walk(root, "");
  const expected = [...files].filter((item) => item !== "checksums.json");
  if (expected.length !== listed.size || expected.some((item) => !listed.has(item))) throw new Error("incomplete checksum inventory");
  for (const [relative, checksum] of listed) {
    const file = path.join(root, ...relative.split("/")); const stats = fs.statSync(file); const digest = createHash("sha256").update(fs.readFileSync(file)).digest("hex");
    if (!stats.isFile() || stats.size !== checksum.size || digest !== checksum.sha256) throw new Error("checksum verification failed");
  }
  return files;
}

function realDirectory(candidate: string): string { const stats = fs.lstatSync(candidate); if (stats.isSymbolicLink() || !stats.isDirectory()) throw new Error("not directory"); return fs.realpathSync(candidate); }
function safePath(value: string): string { const parts = value.split("/"); if (!value || value.includes("\\") || path.posix.isAbsolute(value) || parts.some((item) => !item || item === "." || item === ".." || !/^[A-Za-z0-9._-]+$/.test(item))) throw new Error("invalid path"); return value; }
function stableId(value: string): string { if (!/^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(value)) throw new Error("invalid stable id"); return value; }
function object(value: unknown): Record<string, unknown> { if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("expected object"); return value as Record<string, unknown>; }
function array(value: unknown): unknown[] { if (!Array.isArray(value)) throw new Error("expected array"); return value; }
function string(value: unknown): string { if (typeof value !== "string" || !value.trim()) throw new Error("expected string"); return value; }
function integer(value: unknown): number { if (typeof value !== "number" || !Number.isInteger(value)) throw new Error("expected integer"); return value; }
function notFound(): Response { return new Response("Not found", { status: 404, headers: { "cache-control": "no-store", "x-content-type-options": "nosniff" } }); }
