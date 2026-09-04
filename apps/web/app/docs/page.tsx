import Link from "next/link";

import { DocumentationMetadata } from "@/components/documentation/DocumentationMetadata";
import { DocumentationLocaleSwitchBoundary } from "@/components/documentation/DocumentationLocaleSwitchBoundary";
import { DocumentationOverview } from "@/components/documentation/DocumentationOverview";
import { DocumentationSearch } from "@/components/documentation/DocumentationSearch";
import { DocumentationUnavailable } from "@/components/documentation/DocumentationUnavailable";
import { TranslatedText } from "@/components/TranslatedText";
import { loadDocumentationBundle } from "@/lib/documentation/artifacts";
import { requestedDocumentationLocale, requestedDocumentationTextSize } from "@/lib/documentation/request";

export const dynamic = "force-dynamic";

export default async function DocsPage() {
  const result = await loadDocumentationBundle({ locale: requestedDocumentationLocale(), textSize: requestedDocumentationTextSize() });
  if (result.status === "unavailable") {
    return <DocumentationUnavailable />;
  }

  const { bundle } = result;
  return (
    <div className="docs-overview stack">
      <DocumentationLocaleSwitchBoundary />
      <header className="panel stack">
        <div className="docs-title-block">
          <h1>{bundle.manifest.title}</h1>
          <p className="muted">{bundle.manifest.subtitle}</p>
        </div>
        <DocumentationMetadata bundle={bundle} />
        <div className="actions-row">
          <Link href="/docs/download" className="secondary-button">
            <TranslatedText id="docs.navigation.download" />
          </Link>
        </div>
      </header>

      <section className="panel stack">
        <DocumentationSearch documents={bundle.searchIndex.documents} />
      </section>

      <DocumentationOverview bundle={bundle} />
    </div>
  );
}
