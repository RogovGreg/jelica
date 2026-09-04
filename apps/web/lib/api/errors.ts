import { translate, type Translator } from "@/lib/i18n";

export class ApiClientError extends Error {
  status: number;
  payload: unknown;
  retryAfterSeconds: number | null;

  constructor({
    message,
    status,
    payload,
    retryAfterSeconds = null,
  }: {
    message: string;
    status: number;
    payload: unknown;
    retryAfterSeconds?: number | null;
  }) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.payload = payload;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export function toApiClientError(error: unknown): ApiClientError {
  if (error instanceof ApiClientError) {
    return error;
  }
  if (error instanceof Error) {
    return new ApiClientError({
      message: error.message,
      status: 0,
      payload: null,
      retryAfterSeconds: null,
    });
  }
  return new ApiClientError({
    message: String(error),
    status: 0,
    payload: null,
    retryAfterSeconds: null,
  });
}

export function toErrorMessage(error: unknown): string {
  return translate("en", "common.error.generic");
}

export function toLocalizedErrorMessage(error: unknown, t: Translator): string {
  const status = toApiClientError(error).status;
  if (status === 401 || status === 403) return t("common.error.access-denied");
  if (status === 404) return t("common.error.not-found");
  if (status === 409) return t("common.error.conflict");
  if (status === 422) return t("common.error.invalid-request");
  if (status === 429) return t("common.error.rate-limited");
  if (status >= 500) return t("common.error.service-unavailable");
  return t("common.error.generic");
}

export function isResourceUnavailableError(error: unknown): boolean {
  return toApiClientError(error).status === 404;
}

export function isRateLimitedError(error: unknown): boolean {
  return toApiClientError(error).status === 429;
}
