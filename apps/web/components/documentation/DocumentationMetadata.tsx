import { DocumentationMetadata as SharedDocumentationMetadata } from "../../../../packages/app-platform/src/documentation-ui";
import { TranslatedText } from "@/components/TranslatedText";
import type { DocumentationBundle } from "@/lib/documentation/types";

export function DocumentationMetadata({ bundle }: { bundle: DocumentationBundle }) {
  return <SharedDocumentationMetadata bundle={bundle} labels={{ version: <TranslatedText id="docs.metadata.version" />, artifactFormat: <TranslatedText id="docs.metadata.artifact-format" />, locale: <TranslatedText id="docs.metadata.locale" />, profile: <TranslatedText id="docs.metadata.profile" />, textSize: <TranslatedText id="docs.metadata.text-size" /> }} />;
}
