import { access, copyFile, mkdir, unlink } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const canonicalSource = resolve(
  scriptDirectory,
  "../../../assets/notifications/notification.wav",
);
const stagedTarget = resolve(
  scriptDirectory,
  "../public/assets/notification.wav",
);

try {
  await access(canonicalSource);
  await mkdir(dirname(stagedTarget), { recursive: true });
  await copyFile(canonicalSource, stagedTarget);
  console.log(`Staged notification sound from ${canonicalSource}`);
} catch (error) {
  if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
    try {
      await unlink(stagedTarget);
    } catch (unlinkError) {
      if (!(unlinkError && typeof unlinkError === "object" && "code" in unlinkError && unlinkError.code === "ENOENT")) {
        throw unlinkError;
      }
    }
    console.log("Canonical notification sound is absent; Web playback remains safely unavailable.");
  } else {
    throw error;
  }
}
