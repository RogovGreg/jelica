import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { DocumentationNavigation } from "@/components/documentation/DocumentationNavigation";
import { DocumentationLocaleSwitchBoundary } from "@/components/documentation/DocumentationLocaleSwitchBoundary";
import { DocumentationUnavailable } from "@/components/documentation/DocumentationUnavailable";
import {
  DocumentationViewer,
  type DocumentationViewerRoute,
} from "@/components/documentation/DocumentationViewer";
import { TranslatedText } from "@/components/TranslatedText";
import {
  documentationArtifactUrl,
  documentationArtifactCacheKey,
  findDocumentationPage,
  loadDocumentationBundle,
} from "@/lib/documentation/artifacts";
import {
  documentationLocaleSwitchRequested,
  requestedDocumentationLocale,
  requestedDocumentationTextSize,
} from "@/lib/documentation/request";

export const dynamic = "force-dynamic";

type DocsSectionPageProps = {
  params: {
    slug: string[];
  };
};

export default async function DocsSectionPage({ params }: DocsSectionPageProps) {
  const result = await loadDocumentationBundle({ locale: requestedDocumentationLocale(), textSize: requestedDocumentationTextSize() });
  if (result.status === "unavailable") {
    return <DocumentationUnavailable />;
  }

  const page = findDocumentationPage(result.bundle, params.slug);
  if (!page) {
    if (documentationLocaleSwitchRequested()) {
      redirect("/docs");
    }
    notFound();
  }

  const searchDocument = result.bundle.searchIndex.documents.find(
    (document) => document.id === page.id,
  );
  const anchors = [searchDocument?.anchor, ...(searchDocument?.headingAnchors ?? [])].filter(
    (anchor): anchor is string => Boolean(anchor),
  );

  const cacheKey = documentationArtifactCacheKey(result.bundle.release);
  const source = documentationArtifactUrl(page.path, { cacheKey });
  const routes: DocumentationViewerRoute[] = result.bundle.manifest.pages.map((candidate) => ({
    artifactSource: documentationArtifactUrl(candidate.path, { cacheKey }),
    viewerHref: `/docs/${encodeURIComponent(candidate.id)}`,
  }));
  const htmlIndexPath = result.bundle.manifest.paths.html;
  if (htmlIndexPath) {
    routes.push({
      artifactSource: documentationArtifactUrl(htmlIndexPath, { cacheKey }),
      viewerHref: "/docs",
    });
  }

  return (
    <div className="docs-layout">
      <DocumentationLocaleSwitchBoundary anchors={anchors} />
      <DocumentationNavigation manifest={result.bundle.manifest} activePageId={page.id} />
      <section className="panel stack docs-viewer">
        <header className="docs-viewer-header">
          <div>
            <p className="muted docs-viewer-kicker">
              <TranslatedText id="docs.viewer.current-page" />
            </p>
            <h1>{page.title}</h1>
          </div>
          <Link href={source} className="secondary-button" target="_blank" rel="noreferrer">
            <TranslatedText id="docs.viewer.open-standalone" />
          </Link>
        </header>
        <DocumentationViewer source={source} title={page.title} routes={routes} />
      </section>
    </div>
  );
}
