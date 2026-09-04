import { readDocumentationArtifact } from "@/lib/documentation/artifacts";
import { requestedDocumentationLocale } from "@/lib/documentation/request";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type DocumentationArtifactRouteProps = {
  params: {
    path: string[];
  };
};

export async function GET(request: Request, { params }: DocumentationArtifactRouteProps) {
  const result = await readDocumentationArtifact(params.path.join("/"), { locale: requestedDocumentationLocale() });
  if (result.status === "not-found") {
    return new Response(null, {
      status: 404,
      headers: commonHeaders(),
    });
  }

  const { artifact } = result;
  const headers = new Headers(commonHeaders());
  headers.set("Content-Type", artifact.contentType);
  headers.set("Content-Length", String(artifact.size));
  if (artifact.contentType.startsWith("text/html")) {
    headers.set(
      "Content-Security-Policy",
      "default-src 'none'; script-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; frame-ancestors 'self'",
    );
  }
  if (
    artifact.contentType === "application/pdf" &&
    new URL(request.url).searchParams.get("download") === "1"
  ) {
    headers.set("Content-Disposition", `attachment; filename="${artifact.fileName}"`);
  }

  return new Response(artifact.body, { status: 200, headers });
}

function commonHeaders(): HeadersInit {
  return {
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
  };
}
