import Link from "next/link";

import { DocumentationMetadata } from "@/components/documentation/DocumentationMetadata";
import { DocumentationUnavailable } from "@/components/documentation/DocumentationUnavailable";
import { TranslatedText } from "@/components/TranslatedText";
import {
  documentationArtifactUrl,
  documentationArtifactCacheKey,
  loadDocumentationBundle,
} from "@/lib/documentation/artifacts";
import { requestedDocumentationLocale, requestedDocumentationTextSize } from "@/lib/documentation/request";

export const dynamic = "force-dynamic";

export default async function DocumentationDownloadPage() {
  const result = await loadDocumentationBundle({ locale: requestedDocumentationLocale(), textSize: requestedDocumentationTextSize() });
  if (result.status === "unavailable") {
    return <DocumentationUnavailable />;
  }

  const pdfPath = result.bundle.manifest.paths.pdf;
  if (!pdfPath) {
    return <DocumentationUnavailable />;
  }

  return (
    <section className="panel stack">
      <div className="docs-title-block">
        <h1>
          <TranslatedText id="docs.download.title" />
        </h1>
        <p className="muted">{result.bundle.manifest.title}</p>
      </div>

      <DocumentationMetadata bundle={result.bundle} />

      <div className="actions-row">
        <a
          href={documentationArtifactUrl(pdfPath, { cacheKey: documentationArtifactCacheKey(result.bundle.release), download: true })}
          className="primary-button"
          download
        >
          <TranslatedText id="docs.download.pdf" />
        </a>
        <Link href="/docs" className="secondary-button">
          <TranslatedText id="docs.navigation.overview" />
        </Link>
      </div>

      <section className="stack" aria-labelledby="documentation-release-metadata-heading">
        <h2 id="documentation-release-metadata-heading" className="docs-section-heading">
          <TranslatedText id="docs.download.release-metadata" />
        </h2>
        <pre className="docs-release-metadata">
          {JSON.stringify(result.bundle.release, null, 2)}
        </pre>
      </section>
    </section>
  );
}
