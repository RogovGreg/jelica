"use client";

import Link from "next/link";
import { DocumentationSearch as SharedDocumentationSearch } from "../../../../packages/app-platform/src/documentation-ui";
import { useI18n } from "@/components/I18nProvider";
import type { DocumentationSearchDocument } from "@/lib/documentation/types";

export function DocumentationSearch({ documents }: { documents: readonly DocumentationSearchDocument[] }) {
  const { t } = useI18n();
  return <SharedDocumentationSearch documents={documents} labels={{ label: t("docs.search.label"), placeholder: t("docs.search.placeholder"), results: t("docs.search.results"), noResults: t("docs.search.no-results") }} link={({ href, children }) => <Link href={href}>{children}</Link>} />;
}
