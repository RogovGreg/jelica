const ALLOWED_EXTERNAL_PROTOCOLS = new Set(["https:", "http:"]);

export function parseExternalUrl(rawUrl: string): URL | null {
  if (rawUrl.trim() !== rawUrl || rawUrl.length === 0 || rawUrl.length > 2048) return null;
  try {
    const parsed = new URL(rawUrl);
    return ALLOWED_EXTERNAL_PROTOCOLS.has(parsed.protocol) ? parsed : null;
  } catch {
    return null;
  }
}
