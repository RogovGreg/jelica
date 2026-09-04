import { DocumentationUnavailable as SharedDocumentationUnavailable } from "../../../../packages/app-platform/src/documentation-ui";
import { TranslatedText } from "@/components/TranslatedText";

export function DocumentationUnavailable() {
  return <SharedDocumentationUnavailable title={<TranslatedText id="docs.unavailable.title" />} description={<TranslatedText id="docs.unavailable.description" />} />;
}
