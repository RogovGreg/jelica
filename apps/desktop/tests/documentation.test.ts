import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { DocumentationResourceResolver } from "../src/main/documentation";
import { preserveDocumentationLocation, searchDocumentation } from "../../../packages/app-platform/src/documentation";

test("documentation resolver loads the validated release and serves only known resources", () => {
  const resolver = new DocumentationResourceResolver({ packaged: false, appPath: "/tmp", cwd: process.cwd() });
  assert.equal(resolver.available(), true);
  assert.ok(resolver.bundle?.manifest.pages.length);
  const page = resolver.bundle!.manifest.pages[0]!;
  const resource = resolver.resourceUrl(page.path);
  assert.ok(resource?.startsWith("jelica-doc://artifact/"));
  assert.equal(resolver.resourceUrl("../../etc/passwd"), null);
  assert.equal(resolver.resourceUrl("not-registered.html"), null);
  const response = resolver.serve(resource!); assert.equal(response.status, 200);
});

test("packaged resolver uses application-owned staged resources", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "jelica-packaged-"));
  fs.cpSync(path.resolve(process.cwd(), "../../docs/documentation/releases/0.1/format-v1/en/screen-standard"), path.join(root, "resources/documentation"), { recursive: true });
  const resolver = new DocumentationResourceResolver({ packaged: true, appPath: root });
  assert.equal(resolver.available(), true);
  assert.equal(resolver.root?.includes("docs/documentation"), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test("missing or invalid packaged documentation is controlled unavailable", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "jelica-docs-"));
  const resolver = new DocumentationResourceResolver({ packaged: true, appPath: root });
  assert.equal(resolver.available(), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test("documentation search contract maps to semantic page and anchor", () => {
  const results = searchDocumentation({ documents: [{ id: "intro", title: "Introduction", headings: ["Setup"], headingAnchors: ["#setup"], keywords: [], path: "html/intro.html", anchor: "#intro", content: "offline" }] }, "setup");
  assert.deepEqual(results, [{ id: "intro", title: "Introduction", path: "html/intro.html", anchor: "#setup" }]);
});

test("documentation locale and text-size selection is exact, isolated, and falls back safely", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "jelica-doc-catalog-"));
  const catalog = path.join(root, "catalog");
  fixtureVariant(catalog, "en", "standard", "English marker");
  fixtureVariant(catalog, "ru", "standard", "Русский маркер");
  fixtureVariant(catalog, "sr-Latn", "small", "č ć š ž đ");
  const resolver = new DocumentationResourceResolver({ packaged: false, appPath: root, environment: { JELICA_DOCUMENTATION_RELEASE_DIR: catalog } });
  assert.equal(resolver.effectiveBundle({ locale: "ru" })?.manifest.locale, "ru");
  assert.equal(resolver.effectiveBundle({ locale: "sr-Cyrl" })?.manifest.locale, "en");
  assert.equal(resolver.effectiveBundle({ locale: "sr-Latn", textSize: "small" })?.release.textSize, "small");
  assert.equal(resolver.effectiveBundle({ locale: "sr-Latn", textSize: "large" })?.manifest.locale, "en");
  assert.equal(searchDocumentation(resolver.searchIndex({ locale: "ru" })!, "Русский").length, 1);
  assert.equal(searchDocumentation(resolver.searchIndex({ locale: "ru" })!, "English").length, 0);
  fs.rmSync(root, { recursive: true, force: true });
});

test("Unicode search is bounded and preserves heading identity", () => {
  const documents = [
    { id: "ru", title: "Русский", headings: ["Настройка"], headingAnchors: ["#setup"], keywords: [], path: "html/ru.html", anchor: "#ru", content: "кириллица" },
    { id: "sr-latn", title: "Srpski", headings: [], keywords: ["č ć š ž đ"], path: "html/sr-latn.html", anchor: "#sr-latn", content: "latinica" },
    { id: "sr-cyrl", title: "Српски", headings: [], keywords: [], path: "html/sr-cyrl.html", anchor: "#sr-cyrl", content: "ћирилица" },
  ];
  assert.equal(searchDocumentation({ documents }, "настройка")[0]?.anchor, "#setup");
  assert.equal(searchDocumentation({ documents }, "Č Ć Š Ž Đ").length, 1);
  assert.equal(searchDocumentation({ documents }, "ћирилица").length, 1);
  assert.equal(searchDocumentation({ documents: Array.from({ length: 30 }, (_, index) => ({ ...documents[0]!, id: `ru-${index}`, path: `html/ru-${index}.html` })) }, "рус", 7).length, 7);
  assert.deepEqual(searchDocumentation({ documents }, "   "), []);
});

test("semantic location preservation keeps pages and only valid anchors", () => {
  const resolver = new DocumentationResourceResolver({ packaged: false, appPath: "/tmp", cwd: process.cwd() });
  const manifest = resolver.bundle!.manifest;
  const page = manifest.pages[0]!;
  const section = manifest.sections.find((item) => item.id === page.id)!;
  assert.deepEqual(preserveDocumentationLocation(manifest, { pageId: page.id, anchor: section.anchor }), { pageId: page.id, anchor: section.anchor });
  assert.deepEqual(preserveDocumentationLocation(manifest, { pageId: page.id, anchor: "#missing" }), { pageId: page.id, anchor: "" });
  assert.deepEqual(preserveDocumentationLocation(manifest, { pageId: "missing", anchor: "" }), { pageId: null, anchor: "" });
});

test("tampered, ambiguous, and traversal protocol resources are unavailable", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "jelica-doc-invalid-"));
  const catalog = path.join(root, "catalog");
  const first = fixtureVariant(catalog, "en", "standard", "safe");
  fs.cpSync(first, path.join(catalog, "duplicate"), { recursive: true });
  const ambiguous = new DocumentationResourceResolver({ packaged: false, appPath: root, environment: { JELICA_DOCUMENTATION_RELEASE_DIR: catalog } });
  assert.equal(ambiguous.available(), false);
  fs.rmSync(path.join(catalog, "duplicate"), { recursive: true, force: true });
  fs.appendFileSync(path.join(first, "html/index.html"), "tampered");
  const invalid = new DocumentationResourceResolver({ packaged: false, appPath: root, environment: { JELICA_DOCUMENTATION_RELEASE_DIR: catalog } });
  assert.equal(invalid.available(), false);
  assert.equal(invalid.serve("jelica-doc://artifact/key/%2e%2e/secret").status, 404);
  fs.rmSync(root, { recursive: true, force: true });
});

function fixtureVariant(catalog: string, locale: "en" | "ru" | "sr-Latn" | "sr-Cyrl", textSize: "small" | "standard" | "large", marker: string): string {
  const source = path.resolve(process.cwd(), "../../docs/documentation/releases/0.1/format-v1/en/screen-standard");
  const destination = path.join(catalog, locale, `screen-${textSize}`);
  fs.cpSync(source, destination, { recursive: true });
  const manifestPath = path.join(destination, "documentation-manifest.json");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8")); manifest.locale = locale; manifest.textSize = textSize;
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  const searchPath = path.join(destination, "search-index.json");
  const search = JSON.parse(fs.readFileSync(searchPath, "utf8")); search.locale = locale; search.documents[0].content = marker;
  fs.writeFileSync(searchPath, `${JSON.stringify(search, null, 2)}\n`);
  const releasePath = path.join(destination, "release.json");
  const release = JSON.parse(fs.readFileSync(releasePath, "utf8")); release.locale = locale; release.textSize = textSize; release.sourceHash = createHash("sha256").update(`${locale}-${textSize}`).digest("hex");
  fs.writeFileSync(releasePath, `${JSON.stringify(release, null, 2)}\n`);
  const files = walkFiles(destination).filter((item) => item !== "checksums.json").sort().map((relative) => { const body = fs.readFileSync(path.join(destination, relative)); return { path: relative, sha256: createHash("sha256").update(body).digest("hex"), size: body.byteLength }; });
  fs.writeFileSync(path.join(destination, "checksums.json"), `${JSON.stringify({ algorithm: "SHA-256", files }, null, 2)}\n`);
  return destination;
}

function walkFiles(root: string): string[] {
  const files: string[] = [];
  const walk = (directory: string, prefix: string) => { for (const entry of fs.readdirSync(directory, { withFileTypes: true })) { const relative = prefix ? `${prefix}/${entry.name}` : entry.name; if (entry.isDirectory()) walk(path.join(directory, entry.name), relative); else if (entry.isFile()) files.push(relative); } };
  walk(root, "");
  return files;
}
