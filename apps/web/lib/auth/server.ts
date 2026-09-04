import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { buildApiUrl } from "@/lib/api/config";
import { DEFAULT_LOCALE, translate } from "@/lib/i18n";
import type { AuthUser } from "@/types/api";

export async function requireCurrentUser(nextPath: string): Promise<AuthUser> {
  const user = await getCurrentUserIfPresent();
  if (!user) {
    redirect(`/auth/login?next=${encodeURIComponent(nextPath)}`);
  }
  return user;
}

export async function getCurrentUserIfPresent(): Promise<AuthUser | null> {
  const cookieHeader = cookies().toString();
  let response: Response;
  try {
    response = await fetch(buildApiUrl("/api/auth/me"), {
      method: "GET",
      cache: "no-store",
      headers: cookieHeader ? { cookie: cookieHeader } : undefined,
    });
  } catch {
    throw new Error(translate(DEFAULT_LOCALE, "auth.error.service-unavailable"));
  }

  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new Error(translate(DEFAULT_LOCALE, "auth.error.service-unavailable"));
  }

  try {
    return (await response.json()) as AuthUser;
  } catch {
    throw new Error(translate(DEFAULT_LOCALE, "auth.error.service-unavailable"));
  }
}
