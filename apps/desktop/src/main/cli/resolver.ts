import path from "node:path";

export const CLI_EXECUTABLE_ENV = "JELICA_DESKTOP_CLI_EXECUTABLE";

export function resolveCliExecutable(
  environment: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
  options: { packaged?: boolean; appPath?: string } = {},
): string {
  if (options.packaged && options.appPath) return path.join(options.appPath, "resources", "runtime", platform === "win32" ? "jelica.exe" : "jelica");
  const explicit = environment[CLI_EXECUTABLE_ENV]?.trim();
  if (explicit) {
    if (explicit.includes("\0")) throw new Error("The configured CLI executable is invalid.");
    return explicit;
  }
  return platform === "win32" ? "jelica.exe" : "jelica";
}
