export type UploadSessionState = "open" | "submitting" | "consumed";

type UploadSessionLike = Readonly<{
  submission_status: UploadSessionState;
}>;

type ResolveUploadSessionOptions<T extends UploadSessionLike> = Readonly<{
  explicitSessionId: string | null;
  storedSessionId: string | null;
  getSession: (sessionId: string) => Promise<T>;
  createSession: () => Promise<T>;
  isStoredSessionUnavailable: (error: unknown) => boolean;
}>;

export async function resolveUploadSession<T extends UploadSessionLike>({
  explicitSessionId,
  storedSessionId,
  getSession,
  createSession,
  isStoredSessionUnavailable,
}: ResolveUploadSessionOptions<T>): Promise<T> {
  const explicitId = normalizeSessionId(explicitSessionId);
  if (explicitId) {
    return getSession(explicitId);
  }

  const storedId = normalizeSessionId(storedSessionId);
  if (storedId) {
    try {
      const restored = await getSession(storedId);
      if (restored.submission_status === "open") {
        return restored;
      }
    } catch (error) {
      if (!isStoredSessionUnavailable(error)) {
        throw error;
      }
    }
  }

  return createSession();
}

function normalizeSessionId(value: string | null): string | null {
  const normalized = value?.trim() ?? "";
  return normalized === "" ? null : normalized;
}
