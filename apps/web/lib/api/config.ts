const SERVER_DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const CLIENT_DEFAULT_API_BASE_URL = "/api";

export function buildApiUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const baseUrl = resolveApiBaseUrl();
  if (baseUrl.startsWith("http://") || baseUrl.startsWith("https://")) {
    return new URL(normalizedPath, withTrailingSlash(baseUrl)).toString();
  }
  const normalizedBaseUrl = trimTrailingSlash(baseUrl);
  if (
    normalizedBaseUrl !== "" &&
    (normalizedPath === normalizedBaseUrl || normalizedPath.startsWith(`${normalizedBaseUrl}/`))
  ) {
    return normalizedPath;
  }
  return `${normalizedBaseUrl}${normalizedPath}`;
}

function resolveApiBaseUrl(): string {
  if (typeof window === "undefined") {
    const serverUrl = process.env.JELICA_API_BASE_URL?.trim();
    if (serverUrl) {
      return serverUrl;
    }
    const publicUrl = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
    if (publicUrl) {
      return publicUrl;
    }
    return SERVER_DEFAULT_API_BASE_URL;
  }

  const clientUrl = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  return clientUrl || CLIENT_DEFAULT_API_BASE_URL;
}

function withTrailingSlash(value: string): string {
  return value.endsWith("/") ? value : `${value}/`;
}

function trimTrailingSlash(value: string): string {
  return value.endsWith("/") ? value.slice(0, -1) : value;
}
