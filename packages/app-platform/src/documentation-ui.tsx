"use client";

import { useMemo, useState, type ReactNode, type RefObject } from "react";
import { searchDocumentation, type DocumentationBundle, type DocumentationManifest, type DocumentationSearchDocument } from "./documentation";

export type DocumentationLinkProps = { href: string; children: ReactNode; current?: boolean };
export type DocumentationLink = (props: DocumentationLinkProps) => ReactNode;

export function DocumentationHtmlViewer({ source, title, frameRef, onLoad }: { source: string; title: string; frameRef?: RefObject<HTMLIFrameElement>; onLoad?: () => void }) {
  return <iframe ref={frameRef} className="docs-viewer-frame docs-frame" src={source} title={title} sandbox="allow-same-origin" onLoad={onLoad} />;
}

export function documentationHref(pageId: string, anchor = ""): string {
  if (!/^[A-Za-z0-9._-]+$/.test(pageId) || (anchor && !/^#[A-Za-z0-9._:-]+$/.test(anchor))) throw new Error("Invalid documentation reference");
  return `/docs/${encodeURIComponent(pageId)}${anchor}`;
}

export function DocumentationMetadata({ bundle, labels }: { bundle: DocumentationBundle; labels: { version: ReactNode; artifactFormat: ReactNode; locale: ReactNode; profile: ReactNode; textSize: ReactNode } }) {
  return <dl className="docs-metadata"><div><dt>{labels.version}</dt><dd>{bundle.version.documentationVersion}</dd></div><div><dt>{labels.artifactFormat}</dt><dd>{bundle.version.artifactFormatVersion}</dd></div><div><dt>{labels.locale}</dt><dd>{bundle.release.locale}</dd></div><div><dt>{labels.profile}</dt><dd>{bundle.release.profile}</dd></div><div><dt>{labels.textSize}</dt><dd>{bundle.release.textSize}</dd></div></dl>;
}

export function DocumentationNavigation({ manifest, activePageId, labels, link }: { manifest: DocumentationManifest; activePageId?: string; labels: { heading: ReactNode; overview: ReactNode; download: ReactNode }; link: DocumentationLink }) {
  const pageIdByPath = new Map(manifest.pages.map((page) => [page.path, page.id]));
  return <aside className="docs-sidebar panel stack"><nav className="stack" aria-labelledby="docs-navigation-heading"><h2 id="docs-navigation-heading" className="docs-subheading">{labels.heading}</h2><div className="docs-navigation-actions">{link({ href: "/docs", children: labels.overview })}{link({ href: "/docs/download", children: labels.download })}</div><ul className="docs-navigation-list">{manifest.sections.map((section) => { const sectionPageId = pageIdByPath.get(section.pages[0]?.path ?? ""); if (!sectionPageId) return null; return <li key={section.id}>{link({ href: documentationHref(sectionPageId, section.anchor), current: activePageId === sectionPageId, children: section.title })}{section.headings.length > 0 && <ul>{section.headings.map((heading) => { const headingPageId = pageIdByPath.get(heading.path); return headingPageId ? <li key={heading.id}>{link({ href: documentationHref(headingPageId, heading.anchor), children: heading.title })}</li> : null; })}</ul>}</li>; })}</ul></nav></aside>;
}

export function DocumentationOverview({ bundle, labels, link }: { bundle: DocumentationBundle; labels: { sections: ReactNode }; link: DocumentationLink }) {
  const pageIdByPath = new Map(bundle.manifest.pages.map((page) => [page.path, page.id]));
  return <section className="panel stack" aria-labelledby="documentation-sections-heading"><h2 id="documentation-sections-heading" className="docs-section-heading">{labels.sections}</h2><div className="docs-section-grid">{bundle.manifest.sections.map((section) => <article key={section.id} className="state-box stack docs-section-card"><h3>{link({ href: documentationHref(pageIdByPath.get(section.pages[0]?.path ?? "") ?? "", section.anchor), children: section.title })}</h3><ul>{section.pages.map((reference) => { const page = bundle.manifest.pages.find((candidate) => candidate.path === reference.path); return page ? <li key={`${section.id}-${page.id}`}>{link({ href: documentationHref(page.id, reference.anchor), children: page.title })}</li> : null; })}</ul>{section.headings.length > 0 && <ul className="docs-heading-list">{section.headings.map((heading) => { const pageId = pageIdByPath.get(heading.path); return pageId ? <li key={heading.id}>{link({ href: documentationHref(pageId, heading.anchor), children: heading.title })}</li> : null; })}</ul>}</article>)}</div></section>;
}

export function DocumentationSearch({ documents, labels, link }: { documents: readonly DocumentationSearchDocument[]; labels: { label: ReactNode; placeholder: string; results: ReactNode; noResults: ReactNode }; link: DocumentationLink }) {
  const [query, setQuery] = useState(""); const normalized = query.trim().toLocaleLowerCase();
  const results = useMemo(() => searchDocumentation({ documents }, normalized), [documents, normalized]);
  return <section className="docs-search stack"><label className="input-field"><span>{labels.label}</span><input type="search" value={query} placeholder={labels.placeholder} onChange={(event) => setQuery(event.target.value)} /></label>{normalized && <div className="stack" aria-live="polite"><h2 className="docs-subheading">{labels.results}</h2>{results.length > 0 ? <ul className="docs-search-results">{results.map((result) => <li key={result.id}>{link({ href: documentationHref(result.id, result.anchor), children: result.title })}</li>)}</ul> : <div className="state-box">{labels.noResults}</div>}</div>}</section>;
}

export function DocumentationUnavailable({ title, description }: { title: ReactNode; description: ReactNode }) { return <section className="panel stack" role="alert"><h1 style={{ margin: 0 }}>{title}</h1><div className="state-box state-error">{description}</div></section>; }
