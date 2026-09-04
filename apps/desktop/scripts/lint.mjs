import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const rendererRoot = path.join(desktopRoot, "src", "renderer");
const forbiddenImports = new Set([
  "electron",
  "fs",
  "path",
  "child_process",
  "os",
  "net",
  "http",
  "https",
]);

const failures = [];
for (const file of sourceFiles(rendererRoot)) {
  const source = fs.readFileSync(file, "utf8");
  for (const match of source.matchAll(/(?:from\s+|import\s*\()\s*["']([^"']+)["']/g)) {
    const specifier = match[1];
    const root = specifier.startsWith("node:") ? specifier.slice(5).split("/")[0] : specifier.split("/")[0];
    if (forbiddenImports.has(root)) failures.push(`${relative(file)} imports forbidden module ${specifier}`);
  }
  if (path.basename(file) !== "platform.ts" && source.includes("window.jelicaDesktop")) {
    failures.push(`${relative(file)} bypasses the DesktopPlatformAdapter`);
  }
}

const privilegedSurface = [
  path.join(desktopRoot, "src", "common", "contracts.ts"),
  path.join(desktopRoot, "src", "preload", "bridge.ts"),
].map((file) => fs.readFileSync(file, "utf8")).join("\n");
for (const forbidden of ["executeCli", "runCommand", "argv", "ipcRenderer:", "readFile", "writeFile", "process.env"]) {
  if (privilegedSurface.includes(forbidden)) failures.push(`preload surface contains forbidden token ${forbidden}`);
}

if (failures.length > 0) {
  console.error(failures.join("\n"));
  process.exitCode = 1;
} else {
  console.log("Desktop renderer import and preload surface restrictions passed.");
}

function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(target);
    return /\.tsx?$/.test(entry.name) ? [target] : [];
  });
}

function relative(file) {
  return path.relative(desktopRoot, file);
}
