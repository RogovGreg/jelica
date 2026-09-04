import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const desktopRoot = path.dirname(fileURLToPath(import.meta.url));
const workspaceRoot = path.resolve(desktopRoot, "../..");

const productionCsp = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "connect-src 'self'",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-src jelica-doc:",
].join("; ");

const developmentCsp = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "connect-src 'self' ws://127.0.0.1:5173",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-src jelica-doc:",
].join("; ");

export default defineConfig(({ command }) => ({
  root: desktopRoot,
  resolve: { alias: { react: path.join(desktopRoot, "node_modules/react"), "react/jsx-runtime": path.join(desktopRoot, "node_modules/react/jsx-runtime.js") } },
  base: "./",
  plugins: [
    react(),
    {
      name: "jelica-desktop-csp",
      transformIndexHtml(html) {
        return html.replace("__JELICA_DESKTOP_CSP__", command === "serve" ? developmentCsp : productionCsp);
      },
    },
    {
      name: "jelica-desktop-notification-sound",
      writeBundle() {
        const destination = path.resolve(desktopRoot, "dist/renderer/notification.wav");
        const source = path.resolve(desktopRoot, "../../assets/notifications/notification.wav");
        if (fs.existsSync(source)) fs.copyFileSync(source, destination);
      },
    },
  ],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    fs: { allow: [workspaceRoot] },
  },
  build: {
    outDir: "dist/renderer",
    emptyOutDir: false,
  },
}));
