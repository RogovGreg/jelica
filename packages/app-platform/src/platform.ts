export type PlatformKind = "web" | "desktop";

export interface PlatformAdapter {
  readonly kind: PlatformKind;
  openExternal(url: string): Promise<void>;
}

export class PlatformAdapterError extends Error {
  readonly code: "invalid_external_url" | "external_open_failed";

  constructor(
    code: "invalid_external_url" | "external_open_failed",
    message: string,
  ) {
    super(message);
    this.name = "PlatformAdapterError";
    this.code = code;
  }
}

export function isSafeExternalUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

type BrowserOpen = (url: string, target: string, features: string) => Window | null;

export function createWebPlatformAdapter(openWindow?: BrowserOpen): PlatformAdapter {
  return Object.freeze({
    kind: "web" as const,
    async openExternal(url: string): Promise<void> {
      if (!isSafeExternalUrl(url)) {
        throw new PlatformAdapterError("invalid_external_url", "The external URL is not allowed.");
      }
      const open = openWindow ?? window.open.bind(window);
      if (open(url, "_blank", "noopener,noreferrer") === null) {
        throw new PlatformAdapterError(
          "external_open_failed",
          "The external URL could not be opened.",
        );
      }
    },
  });
}
