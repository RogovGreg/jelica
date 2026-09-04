import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = path.resolve(desktopRoot, "../../docs/documentation/releases");
const destination = path.join(desktopRoot, "resources/documentation");
await fs.rm(destination, { recursive: true, force: true });
await fs.mkdir(destination, { recursive: true });

let stagedReleaseCount = 0;
async function stageReleaseDirectories(directory) {
  const entries = await fs.readdir(directory, { withFileTypes: true });
  if (entries.some((entry) => entry.isFile() && entry.name === "release.json")) {
    await validateReleaseDirectory(directory);
    const relative = path.relative(source, directory);
    await fs.cp(directory, path.join(destination, relative), {
      recursive: true,
      dereference: false,
    });
    stagedReleaseCount += 1;
    return;
  }
  for (const entry of entries) {
    if (entry.isDirectory()) {
      await stageReleaseDirectories(path.join(directory, entry.name));
    }
  }
}

async function validateReleaseDirectory(directory) {
  const checksumManifest = JSON.parse(await fs.readFile(path.join(directory, "checksums.json"), "utf8"));
  if (checksumManifest.algorithm !== "SHA-256" || !Array.isArray(checksumManifest.files)) {
    throw new Error(`Invalid documentation checksum manifest: ${directory}`);
  }
  const listed = new Map();
  for (const entry of checksumManifest.files) {
    const relative = entry?.path;
    const segments = typeof relative === "string" ? relative.split("/") : [];
    if (
      !segments.length ||
      segments.some((segment) => !segment || segment === "." || segment === ".." || !/^[A-Za-z0-9._-]+$/.test(segment)) ||
      !Number.isInteger(entry?.size) ||
      entry.size < 0 ||
      typeof entry?.sha256 !== "string" ||
      !/^[0-9a-f]{64}$/.test(entry.sha256) ||
      listed.has(relative)
    ) {
      throw new Error(`Invalid documentation checksum entry: ${directory}`);
    }
    listed.set(relative, entry);
  }
  const discovered = [];
  async function walk(current, prefix) {
    for (const entry of await fs.readdir(current, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) throw new Error(`Documentation release contains a symbolic link: ${directory}`);
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) await walk(path.join(current, entry.name), relative);
      else if (entry.isFile() && relative !== "checksums.json") discovered.push(relative);
      else if (!entry.isFile()) throw new Error(`Documentation release contains an unsupported entry: ${directory}`);
    }
  }
  await walk(directory, "");
  if (listed.size !== discovered.length || discovered.some((relative) => !listed.has(relative))) {
    throw new Error(`Incomplete documentation checksum inventory: ${directory}`);
  }
  for (const relative of discovered) {
    const entry = listed.get(relative);
    const body = await fs.readFile(path.join(directory, ...relative.split("/")));
    const digest = createHash("sha256").update(body).digest("hex");
    if (!entry || entry.size !== body.byteLength || entry.sha256 !== digest) {
      throw new Error(`Documentation checksum verification failed: ${relative}`);
    }
  }
}

await stageReleaseDirectories(source);
if (stagedReleaseCount === 0) {
  throw new Error(`No validated documentation releases found under ${source}`);
}
console.log(`Staged validated documentation release catalog at ${destination}`);
const soundSource = path.resolve(desktopRoot, "../../assets/notifications/notification.wav");
const soundDestination = path.join(desktopRoot, "resources/notification.wav");
await fs.copyFile(soundSource, soundDestination);
console.log(`Staged canonical notification sound at ${soundDestination}`);
