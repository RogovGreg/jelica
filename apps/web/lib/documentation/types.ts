export type {
  DocumentationBundle,
  DocumentationHeading,
  DocumentationManifest,
  DocumentationPage,
  DocumentationPageReference,
  DocumentationRelease,
  DocumentationSearchDocument,
  DocumentationSearchIndex,
  DocumentationSection,
  DocumentationVersion,
} from "../../../../packages/app-platform/src/documentation";
import type { DocumentationBundle } from "../../../../packages/app-platform/src/documentation";
export type DocumentationUnavailableReason = "missing" | "ambiguous" | "invalid";
export type DocumentationLoadResult = { status: "available"; bundle: DocumentationBundle } | { status: "unavailable"; reason: DocumentationUnavailableReason };
export type DocumentationArtifact = { body: ArrayBuffer; contentType: string; fileName: string; size: number };
export type DocumentationArtifactResult = { status: "available"; artifact: DocumentationArtifact } | { status: "not-found" };
