import "server-only";

import { createHash } from "node:crypto";
import { lstat, readFile, readdir, realpath, stat } from "node:fs/promises";
import path from "node:path";

import {
  documentationSelectionFallbacks,
  isDocumentationLocale,
  isDocumentationProfile,
  isDocumentationTextSize,
  matchesDocumentationSelection,
  type DocumentationSelection,
} from "../../../../packages/app-platform/src/documentation";

import type {
  DocumentationArtifactResult,
  DocumentationBundle,
  DocumentationHeading,
  DocumentationLoadResult,
  DocumentationManifest,
  DocumentationPage,
  DocumentationPageReference,
  DocumentationRelease,
  DocumentationSearchDocument,
  DocumentationSearchIndex,
  DocumentationSection,
  DocumentationUnavailableReason,
  DocumentationVersion,
} from "./types";

const MANIFEST_FILE = "documentation-manifest.json";
const SEARCH_INDEX_FILE = "search-index.json";
const RELEASE_FILE = "release.json";
const VERSION_FILE = "version.json";
const CHECKSUMS_FILE = "checksums.json";
const SUPPORTED_ARTIFACT_FORMAT_VERSION = 1;
const ARTIFACT_ROUTE = "/documentation-artifacts";
const MAX_DISCOVERY_DEPTH = 8;

const MIME_TYPES: Readonly<Record<string, string>> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".pdf": "application/pdf",
  ".png": "image/png",
};

type LocatedDocumentationBundle = {
  root: string;
  bundle: DocumentationBundle;
};

class DocumentationBundleError extends Error {
  constructor(readonly reason: DocumentationUnavailableReason) {
    super(reason);
    this.name = "DocumentationBundleError";
  }
}

export async function loadDocumentationBundle(
  requested: Partial<DocumentationSelection> = {},
): Promise<DocumentationLoadResult> {
  try {
    const located = await loadLocatedDocumentationBundle(documentationSelection(requested));
    return { status: "available", bundle: located.bundle };
  } catch (error) {
    if (error instanceof DocumentationBundleError) {
      return { status: "unavailable", reason: error.reason };
    }
    return { status: "unavailable", reason: "invalid" };
  }
}

export function findDocumentationPage(
  bundle: DocumentationBundle,
  slug: readonly string[],
): DocumentationPage | undefined {
  if (slug.length !== 1 || !isStableId(slug[0])) {
    return undefined;
  }
  return bundle.manifest.pages.find((page) => page.id === slug[0]);
}

export function documentationArtifactUrl(
  relativePath: string,
  options: { anchor?: string; cacheKey?: string; download?: boolean } = {},
): string {
  const segments = safeRelativePathSegments(relativePath);
  const encodedPath = segments.map((segment) => encodeURIComponent(segment)).join("/");
  const query = new URLSearchParams();
  if (options.cacheKey) {
    if (!/^[A-Za-z0-9._-]{1,200}$/.test(options.cacheKey)) throw new Error("Invalid documentation cache key");
    query.set("artifact", options.cacheKey);
  }
  if (options.download) query.set("download", "1");
  const anchor = options.anchor ? validatedAnchor(options.anchor) : "";
  return `${ARTIFACT_ROUTE}/${encodedPath}${query.size ? `?${query}` : ""}${anchor}`;
}

export function documentationArtifactCacheKey(release: DocumentationRelease): string {
  return [release.sourceHash.toLowerCase(), release.locale, release.profile, release.textSize].join("-");
}

export async function readDocumentationArtifact(
  relativePath: string,
  requested: Partial<DocumentationSelection> = {},
): Promise<DocumentationArtifactResult> {
  try {
    const segments = safeRelativePathSegments(relativePath);
    const extension = path.extname(segments.at(-1) ?? "").toLowerCase();
    const contentType = MIME_TYPES[extension];
    if (!contentType) {
      return { status: "not-found" };
    }

    const located = await loadLocatedDocumentationBundle(documentationSelection(requested));
    const absolutePath = await resolveRegularArtifact(located.root, segments);
    const fileStat = await stat(absolutePath);
    const body = Uint8Array.from(await readFile(absolutePath)).buffer;
    return {
      status: "available",
      artifact: {
        body,
        contentType,
        fileName: segments.at(-1) ?? "artifact",
        size: fileStat.size,
      },
    };
  } catch {
    return { status: "not-found" };
  }
}

async function loadLocatedDocumentationBundle(
  selection: DocumentationSelection,
): Promise<LocatedDocumentationBundle> {
  const exactReleaseDirectory = process.env.JELICA_DOCUMENTATION_RELEASE_DIR?.trim();
  const releasesRoot = exactReleaseDirectory || process.env.JELICA_DOCUMENTATION_RELEASES_ROOT?.trim() || path.resolve(process.cwd(), "../../docs/documentation/releases");
  if (!(await isDirectory(releasesRoot))) {
    throw new DocumentationBundleError("missing");
  }

  const root = await resolveReleaseDirectory(releasesRoot);
  const candidates = await discoverReleaseDirectories(root);
  if (candidates.length === 0) {
    throw new DocumentationBundleError("missing");
  }

  let invalidCandidateFound = false;
  const releases: Array<{ directory: string; release: DocumentationRelease }> = [];
  for (const candidate of candidates) {
    try {
      const release = parseRelease(await readJson(path.join(candidate, RELEASE_FILE)));
      releases.push({ directory: candidate, release });
    } catch {
      invalidCandidateFound = true;
    }
  }

  for (const candidateSelection of documentationSelectionFallbacks(selection)) {
    const matches = releases.filter((item) => matchesDocumentationSelection(item.release, candidateSelection));
    if (matches.length > 1) throw new DocumentationBundleError("ambiguous");
    if (matches.length === 1) {
      const selected = matches[0]!;
      const bundle = await readAndValidateBundle(selected.directory, candidateSelection);
      return { root: selected.directory, bundle };
    }
  }
  throw new DocumentationBundleError(invalidCandidateFound ? "invalid" : "missing");
}

export function documentationSelection(
  requested: Partial<DocumentationSelection> = {},
): DocumentationSelection {
  const locale = requested.locale ?? environmentValue("JELICA_DOCUMENTATION_LOCALE", "en");
  const profile = requested.profile ?? environmentValue("JELICA_DOCUMENTATION_PROFILE", "screen");
  const textSize = requested.textSize ?? environmentValue("JELICA_DOCUMENTATION_TEXT_SIZE", "standard");
  const documentationVersion = requested.documentationVersion ?? environmentValue("JELICA_DOCUMENTATION_VERSION", "0.1");
  const rawFormat = requested.artifactFormatVersion ?? Number(environmentValue("JELICA_DOCUMENTATION_ARTIFACT_FORMAT_VERSION", "1"));
  if (!isDocumentationLocale(locale) || !isDocumentationProfile(profile) || !isDocumentationTextSize(textSize) || !Number.isInteger(rawFormat) || rawFormat < 1 || !/^\d+(?:\.\d+){1,2}(?:[-+][0-9A-Za-z.-]+)?$/.test(documentationVersion)) {
    throw new DocumentationBundleError("invalid");
  }
  return { locale, profile, textSize, documentationVersion, artifactFormatVersion: rawFormat };
}

function environmentValue(name: string, fallback: string): string {
  return process.env[name]?.trim() || fallback;
}

async function discoverReleaseDirectories(root: string): Promise<string[]> {
  const releases: string[] = [];
  const pending: Array<{ directory: string; depth: number }> = [{ directory: root, depth: 0 }];

  while (pending.length > 0) {
    const current = pending.pop();
    if (!current) {
      break;
    }

    let entries;
    try {
      entries = await readdir(current.directory, { withFileTypes: true });
    } catch {
      continue;
    }

    if (entries.some((entry) => entry.isFile() && !entry.isSymbolicLink() && entry.name === RELEASE_FILE)) {
      releases.push(current.directory);
      continue;
    }
    if (current.depth >= MAX_DISCOVERY_DEPTH) {
      continue;
    }

    const childDirectories = entries
      .filter((entry) => entry.isDirectory() && !entry.isSymbolicLink())
      .map((entry) => entry.name)
      .sort()
      .reverse();
    for (const name of childDirectories) {
      pending.push({ directory: path.join(current.directory, name), depth: current.depth + 1 });
    }
  }

  return releases.sort();
}

async function readAndValidateBundle(
  root: string,
  selection: DocumentationSelection,
): Promise<DocumentationBundle> {
  try {
    const [manifestValue, searchValue, releaseValue, versionValue] = await Promise.all([
      readReleaseJson(root, MANIFEST_FILE),
      readReleaseJson(root, SEARCH_INDEX_FILE),
      readReleaseJson(root, RELEASE_FILE),
      readReleaseJson(root, VERSION_FILE),
    ]);

    const bundle: DocumentationBundle = {
      manifest: parseManifest(manifestValue),
      searchIndex: parseSearchIndex(searchValue),
      release: parseRelease(releaseValue),
      version: parseVersion(versionValue),
    };
    validateBundleConsistency(bundle, selection);
    await validateRequiredArtifactFiles(root, bundle);
    await validateChecksums(root);
    return bundle;
  } catch (error) {
    if (error instanceof DocumentationBundleError) {
      throw error;
    }
    throw new DocumentationBundleError("invalid");
  }
}

async function readReleaseJson(root: string, fileName: string): Promise<unknown> {
  const filePath = await resolveRegularArtifact(root, [fileName]);
  return readJson(filePath);
}

async function readJson(filePath: string): Promise<unknown> {
  return JSON.parse(await readFile(filePath, "utf8")) as unknown;
}

function parseManifest(value: unknown): DocumentationManifest {
  const object = requiredObject(value);
  const pathsObject = requiredObject(object.paths);
  const paths: Record<string, string | null> = {};
  for (const [key, entry] of Object.entries(pathsObject)) {
    if (entry !== null && typeof entry !== "string") {
      throw new Error("Invalid documentation path");
    }
    if (typeof entry === "string") {
      safeRelativePathSegments(entry);
    }
    paths[key] = entry;
  }

  const year = requiredInteger(object.year);
  if (year < 1000 || year > 9999) {
    throw new Error("Invalid documentation year");
  }

  return {
    schemaVersion: requiredInteger(object.schemaVersion),
    locale: requiredString(object.locale),
    title: requiredString(object.title),
    subtitle: requiredString(object.subtitle),
    version: requiredString(object.version),
    year,
    profile: requiredString(object.profile),
    textSize: requiredString(object.textSize),
    paths,
    sections: requiredArray(object.sections).map(parseSection),
    pages: requiredArray(object.pages).map(parsePage),
  };
}

async function validateRequiredArtifactFiles(
  root: string,
  bundle: DocumentationBundle,
): Promise<void> {
  const relativePaths = new Set<string>();
  for (const value of Object.values(bundle.manifest.paths)) {
    if (typeof value === "string") {
      relativePaths.add(value);
    }
  }
  for (const page of bundle.manifest.pages) {
    relativePaths.add(page.path);
  }
  for (const section of bundle.manifest.sections) {
    for (const page of section.pages) {
      relativePaths.add(page.path);
    }
    for (const heading of section.headings) {
      relativePaths.add(heading.path);
    }
  }
  for (const document of bundle.searchIndex.documents) {
    relativePaths.add(document.path);
  }

  await Promise.all(
    [...relativePaths].map((relativePath) =>
      resolveRegularArtifact(root, safeRelativePathSegments(relativePath)),
    ),
  );
}

function parseSection(value: unknown): DocumentationSection {
  const object = requiredObject(value);
  return {
    id: requiredStableId(object.id),
    title: requiredString(object.title),
    keywords: stringArray(object.keywords),
    anchor: requiredAnchor(object.anchor),
    pages: requiredArray(object.pages).map(parsePageReference),
    headings: requiredArray(object.headings).map(parseHeading),
  };
}

function parsePageReference(value: unknown): DocumentationPageReference {
  const object = requiredObject(value);
  return {
    path: requiredRelativePath(object.path),
    anchor: requiredAnchor(object.anchor),
  };
}

function parseHeading(value: unknown): DocumentationHeading {
  const object = requiredObject(value);
  return {
    id: requiredStableId(object.id),
    title: requiredString(object.title),
    path: requiredRelativePath(object.path),
    anchor: requiredAnchor(object.anchor),
  };
}

function parsePage(value: unknown): DocumentationPage {
  const object = requiredObject(value);
  return {
    id: requiredStableId(object.id),
    title: requiredString(object.title),
    path: requiredRelativePath(object.path),
  };
}

function parseSearchIndex(value: unknown): DocumentationSearchIndex {
  const object = requiredObject(value);
  return {
    schemaVersion: requiredInteger(object.schemaVersion),
    locale: requiredString(object.locale),
    title: requiredString(object.title),
    version: requiredString(object.version),
    fieldPriority: stringArray(object.fieldPriority),
    documents: requiredArray(object.documents).map(parseSearchDocument),
  };
}

function parseSearchDocument(value: unknown): DocumentationSearchDocument {
  const object = requiredObject(value);
  const headingAnchors = object.headingAnchors === undefined ? undefined : requiredArray(object.headingAnchors).map(requiredAnchor);
  return {
    id: requiredStableId(object.id),
    title: requiredString(object.title),
    headings: stringArray(object.headings),
    ...(headingAnchors ? { headingAnchors } : {}),
    keywords: stringArray(object.keywords),
    path: requiredRelativePath(object.path),
    anchor: requiredAnchor(object.anchor),
    content: requiredString(object.content),
  };
}

function parseRelease(value: unknown): DocumentationRelease {
  const object = requiredObject(value);
  const sourceHash = requiredString(object.sourceHash);
  if (!/^[0-9a-f]{64}$/i.test(sourceHash)) {
    throw new Error("Invalid source hash");
  }
  return {
    releaseVersion: requiredString(object.releaseVersion),
    artifactFormatVersion: requiredInteger(object.artifactFormatVersion),
    locale: requiredString(object.locale),
    profile: requiredString(object.profile),
    textSize: requiredString(object.textSize),
    generatedAt: requiredString(object.generatedAt),
    sourceHash,
  };
}

function parseVersion(value: unknown): DocumentationVersion {
  const object = requiredObject(value);
  return {
    artifactFormatVersion: requiredInteger(object.artifactFormatVersion),
    documentationVersion: requiredString(object.documentationVersion),
  };
}

function validateBundleConsistency(
  bundle: DocumentationBundle,
  selection: DocumentationSelection,
): void {
  const { manifest, searchIndex, release, version } = bundle;
  const formatVersion = version.artifactFormatVersion;
  if (
    formatVersion !== SUPPORTED_ARTIFACT_FORMAT_VERSION ||
    manifest.schemaVersion !== formatVersion ||
    searchIndex.schemaVersion !== formatVersion ||
    release.artifactFormatVersion !== formatVersion
  ) {
    throw new Error("Unsupported or inconsistent artifact format");
  }

  if (
    manifest.locale !== searchIndex.locale ||
    manifest.locale !== release.locale ||
    manifest.locale !== selection.locale ||
    manifest.profile !== release.profile ||
    manifest.profile !== selection.profile ||
    manifest.textSize !== release.textSize ||
    manifest.textSize !== selection.textSize
  ) {
    throw new Error("Inconsistent documentation variant");
  }

  if (
    manifest.version !== searchIndex.version ||
    manifest.version !== release.releaseVersion ||
    manifest.version !== version.documentationVersion ||
    manifest.title !== searchIndex.title
  ) {
    throw new Error("Inconsistent documentation metadata");
  }

  requireManifestPath(manifest, "html", ".html");
  requireManifestPath(manifest, "pdf", ".pdf");
  const searchPath = requireManifestPath(manifest, "search", ".json");
  const versionPath = requireManifestPath(manifest, "version", ".json");
  if (searchPath !== SEARCH_INDEX_FILE || versionPath !== VERSION_FILE) {
    throw new Error("Inconsistent metadata paths");
  }

  ensureUnique(manifest.pages.map((page) => page.id));
  ensureUnique(manifest.pages.map((page) => page.path));
  ensureUnique(manifest.sections.map((section) => section.id));
  ensureUnique(searchIndex.documents.map((document) => document.id));

  if (manifest.pages.length === 0 || manifest.sections.length === 0) {
    throw new Error("Documentation navigation is empty");
  }
  const pagesById = new Map(manifest.pages.map((page) => [page.id, page]));
  const pagePaths = new Set(manifest.pages.map((page) => page.path));
  for (const section of manifest.sections) {
    if (section.pages.length === 0) {
      throw new Error("Documentation section has no pages");
    }
    for (const page of section.pages) {
      if (!pagePaths.has(page.path)) {
        throw new Error("Documentation section references an unknown page");
      }
    }
    for (const heading of section.headings) {
      if (!pagePaths.has(heading.path)) {
        throw new Error("Documentation heading references an unknown page");
      }
    }
  }

  if (searchIndex.documents.length !== manifest.pages.length) {
    throw new Error("Search index does not match documentation pages");
  }
  for (const document of searchIndex.documents) {
    const page = pagesById.get(document.id);
    if (!page || page.title !== document.title || page.path !== document.path) {
      throw new Error("Search document does not match its page");
    }
    const section = manifest.sections.find((item) => item.id === document.id);
    if (!section || document.headings.length !== section.headings.length || document.headings.some((heading, index) => heading !== section.headings[index]?.title)) {
      throw new Error("Search headings do not match documentation navigation");
    }
    if (document.headingAnchors && (document.headingAnchors.length !== section.headings.length || document.headingAnchors.some((anchor, index) => anchor !== section.headings[index]?.anchor))) {
      throw new Error("Search heading anchors do not match documentation navigation");
    }
  }
}

async function validateChecksums(root: string): Promise<void> {
  const value = requiredObject(await readReleaseJson(root, CHECKSUMS_FILE));
  if (value.algorithm !== "SHA-256") throw new Error("Unsupported documentation checksum algorithm");
  const entries = requiredArray(value.files);
  const listed = new Map<string, { size: number; sha256: string }>();
  for (const rawEntry of entries) {
    const entry = requiredObject(rawEntry);
    const relativePath = requiredRelativePath(entry.path);
    const size = requiredInteger(entry.size);
    const sha256 = requiredString(entry.sha256).toLowerCase();
    if (size < 0 || !/^[0-9a-f]{64}$/.test(sha256) || listed.has(relativePath)) throw new Error("Invalid documentation checksum entry");
    listed.set(relativePath, { size, sha256 });
  }

  const files = await listReleaseFiles(root);
  const expected = files.filter((item) => item !== CHECKSUMS_FILE).sort();
  if (expected.length !== listed.size || expected.some((item) => !listed.has(item))) throw new Error("Documentation checksums do not cover the release");
  await Promise.all(expected.map(async (relativePath) => {
    const absolutePath = await resolveRegularArtifact(root, safeRelativePathSegments(relativePath));
    const body = await readFile(absolutePath);
    const checksum = listed.get(relativePath)!;
    if (body.byteLength !== checksum.size || createHash("sha256").update(body).digest("hex") !== checksum.sha256) throw new Error("Documentation checksum verification failed");
  }));
}

async function listReleaseFiles(root: string): Promise<string[]> {
  const result: string[] = [];
  const pending: Array<{ absolute: string; relative: string }> = [{ absolute: root, relative: "" }];
  while (pending.length) {
    const current = pending.pop()!;
    const entries = await readdir(current.absolute, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.isSymbolicLink()) throw new Error("Symbolic links are not allowed in documentation releases");
      const relative = current.relative ? `${current.relative}/${entry.name}` : entry.name;
      const absolute = path.join(current.absolute, entry.name);
      if (entry.isDirectory()) pending.push({ absolute, relative });
      else if (entry.isFile()) result.push(relative);
      else throw new Error("Unsupported documentation release entry");
    }
  }
  return result;
}

function requireManifestPath(
  manifest: DocumentationManifest,
  key: string,
  extension: string,
): string {
  const value = manifest.paths[key];
  if (typeof value !== "string" || path.extname(value).toLowerCase() !== extension) {
    throw new Error("Required documentation artifact is missing");
  }
  return value;
}

function requiredObject(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Expected an object");
  }
  return value as Record<string, unknown>;
}

function requiredArray(value: unknown): unknown[] {
  if (!Array.isArray(value)) {
    throw new Error("Expected an array");
  }
  return value;
}

function requiredString(value: unknown): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error("Expected a non-empty string");
  }
  return value;
}

function requiredInteger(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new Error("Expected an integer");
  }
  return value;
}

function stringArray(value: unknown): string[] {
  const strings = requiredArray(value).map(requiredString);
  ensureUnique(strings);
  return strings;
}

function requiredStableId(value: unknown): string {
  const id = requiredString(value);
  if (!isStableId(id)) {
    throw new Error("Invalid stable identifier");
  }
  return id;
}

function isStableId(value: string | undefined): value is string {
  return typeof value === "string" && /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(value);
}

function requiredAnchor(value: unknown): string {
  return validatedAnchor(requiredString(value));
}

function validatedAnchor(value: string): string {
  if (!/^#[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(value)) {
    throw new Error("Invalid documentation anchor");
  }
  return value;
}

function requiredRelativePath(value: unknown): string {
  const relativePath = requiredString(value);
  safeRelativePathSegments(relativePath);
  return relativePath;
}

function safeRelativePathSegments(relativePath: string): string[] {
  if (
    relativePath === "" ||
    relativePath.includes("\\") ||
    relativePath.includes("\0") ||
    path.posix.isAbsolute(relativePath)
  ) {
    throw new Error("Invalid documentation path");
  }
  const segments = relativePath.split("/");
  if (
    segments.some(
      (segment) =>
        segment === "" ||
        segment === "." ||
        segment === ".." ||
        !/^[A-Za-z0-9._-]+$/.test(segment),
    )
  ) {
    throw new Error("Invalid documentation path");
  }
  return segments;
}

function ensureUnique(values: readonly string[]): void {
  if (new Set(values).size !== values.length) {
    throw new Error("Expected unique values");
  }
}

async function resolveRegularArtifact(root: string, segments: readonly string[]): Promise<string> {
  const rootRealPath = await realpath(root);
  let currentPath = rootRealPath;
  for (const segment of segments) {
    currentPath = path.join(currentPath, segment);
    const entry = await lstat(currentPath);
    if (entry.isSymbolicLink()) {
      throw new Error("Symbolic links are not allowed in documentation releases");
    }
  }

  const artifactRealPath = await realpath(currentPath);
  if (!isWithinRoot(rootRealPath, artifactRealPath)) {
    throw new Error("Documentation artifact escapes its release root");
  }
  const artifactStat = await stat(artifactRealPath);
  if (!artifactStat.isFile()) {
    throw new Error("Documentation artifact is not a regular file");
  }
  return artifactRealPath;
}

async function resolveReleaseDirectory(directory: string): Promise<string> {
  const entry = await lstat(directory);
  if (entry.isSymbolicLink() || !entry.isDirectory()) {
    throw new DocumentationBundleError("invalid");
  }
  return realpath(directory);
}

function isWithinRoot(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

async function isDirectory(candidate: string): Promise<boolean> {
  try {
    return (await lstat(candidate)).isDirectory();
  } catch {
    return false;
  }
}
