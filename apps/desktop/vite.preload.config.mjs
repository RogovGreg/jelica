import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vite";

const desktopRoot = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: desktopRoot,
  build: {
    outDir: "dist/apps/desktop/src/preload",
    emptyOutDir: false,
    lib: {
      entry: path.join(desktopRoot, "src/preload/index.ts"),
      formats: ["cjs"],
      fileName: () => "index.cjs",
    },
    rollupOptions: {
      external: ["electron"],
      output: {
        entryFileNames: "index.cjs",
        format: "cjs",
        exports: "auto",
      },
    },
  },
});
