export function isAllowedRendererNavigation(
  targetUrl: string,
  entryUrl: string,
  development: boolean,
): boolean {
  if (!development) return targetUrl === entryUrl || targetUrl.startsWith(`${entryUrl}#`);
  try {
    const target = new URL(targetUrl);
    const entry = new URL(entryUrl);
    return target.origin === entry.origin;
  } catch {
    return false;
  }
}
