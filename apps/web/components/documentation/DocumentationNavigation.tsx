"use client";

import Link from "next/link";
import { DocumentationNavigation as SharedDocumentationNavigation } from "../../../../packages/app-platform/src/documentation-ui";
import { TranslatedText } from "@/components/TranslatedText";
import type { DocumentationManifest } from "@/lib/documentation/types";

export function DocumentationNavigation({ manifest, activePageId }: { manifest: DocumentationManifest; activePageId?: string }) {
  return <SharedDocumentationNavigation manifest={manifest} activePageId={activePageId} labels={{ heading: <TranslatedText id="docs.section.list" />, overview: <TranslatedText id="docs.navigation.overview" />, download: <TranslatedText id="docs.navigation.download" /> }} link={({ href, children, current }) => <Link href={href} aria-current={current ? "page" : undefined}>{children}</Link>} />;
}
