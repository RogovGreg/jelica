"use client";

import Link from "next/link";

import { TranslatedText } from "@/components/TranslatedText";
import type { DocumentationBundle } from "@/lib/documentation/types";
import { DocumentationOverview as SharedDocumentationOverview } from "../../../../packages/app-platform/src/documentation-ui";

export function DocumentationOverview({ bundle }: { bundle: DocumentationBundle }) {
  return (
    <SharedDocumentationOverview
      bundle={bundle}
      labels={{ sections: <TranslatedText id="docs.section.list" /> }}
      link={({ href, children }) => <Link href={href}>{children}</Link>}
    />
  );
}
