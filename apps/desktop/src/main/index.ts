import path from "node:path";
import { pathToFileURL } from "node:url";

import { app, BrowserWindow, protocol, session, shell } from "electron";

import { createBrowserWindowOptions } from "./browser-window-options";
import { DesktopAnalyticsService } from "./analytics";
import { DesktopCliClient } from "./cli/client";
import { parseExternalUrl } from "./external-url";
import { registerDesktopIpc } from "./ipc";
import { SelectionRegistry } from "./selections";
import { isAllowedRendererNavigation } from "./navigation";
import { DocumentationResourceResolver } from "./documentation";
import { resolveCliExecutable } from "./cli/resolver";
import { DesktopNotificationController } from "./notifications";

const DEVELOPMENT_RENDERER_URL = "http://127.0.0.1:5173";
const PRODUCTION_RENDERER_PATH = path.join(app.getAppPath(), "dist", "renderer", "index.html");
protocol.registerSchemesAsPrivileged([{ scheme: "jelica-doc", privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
const cliClient = new DesktopCliClient(resolveCliExecutable(process.env, process.platform, { packaged: app.isPackaged, appPath: app.getAppPath() }));
const analytics = new DesktopAnalyticsService(cliClient, new SelectionRegistry());
const documentation = new DocumentationResourceResolver({ packaged: app.isPackaged, appPath: app.getAppPath() });
let mainWindow: BrowserWindow | null = null;
let unregisterIpc: (() => void) | null = null;
const notifications = new DesktopNotificationController(cliClient, () => mainWindow);

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", focusMainWindow);
  app.whenReady().then(() => {
    protocol.handle("jelica-doc", (request) => documentation.serve(request.url));
    denyAllPermissions();
    unregisterIpc = registerDesktopIpc({ analytics, documentation, notifications, getWindow: () => mainWindow });
    notifications.start();
    createMainWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
      else focusMainWindow();
    });
  });
}

app.on("before-quit", () => {
  cliClient.dispose();
  notifications.stop();
  unregisterIpc?.();
  unregisterIpc = null;
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

function createMainWindow(): void {
  const preloadPath = path.join(app.getAppPath(), "dist", "apps", "desktop", "src", "preload", "index.cjs");
  const window = new BrowserWindow(createBrowserWindowOptions(preloadPath));
  mainWindow = window;

  const showFallback = setTimeout(() => {
    if (!window.isDestroyed() && !window.isVisible()) window.show();
  }, 5000);
  window.once("ready-to-show", () => {
    clearTimeout(showFallback);
    window.show();
  });
  window.on("closed", () => {
    clearTimeout(showFallback);
    if (mainWindow === window) mainWindow = null;
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    void openApprovedExternal(url);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (isAllowedNavigation(url)) return;
    event.preventDefault();
    void openApprovedExternal(url);
  });
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());

  if (isDevelopment()) void window.loadURL(DEVELOPMENT_RENDERER_URL);
  else void window.loadFile(PRODUCTION_RENDERER_PATH);
}

function isDevelopment(): boolean {
  return !app.isPackaged && process.env.JELICA_DESKTOP_DEV === "1";
}

function isAllowedNavigation(url: string): boolean {
  const development = isDevelopment();
  const entryUrl = development
    ? DEVELOPMENT_RENDERER_URL
    : pathToFileURL(PRODUCTION_RENDERER_PATH).href;
  return isAllowedRendererNavigation(url, entryUrl, development);
}

async function openApprovedExternal(rawUrl: string): Promise<void> {
  const url = parseExternalUrl(rawUrl);
  if (!url) return;
  try {
    await shell.openExternal(url.href);
  } catch {
    console.error("JELICA Desktop could not open an approved external URL.");
  }
}

function denyAllPermissions(): void {
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
}

function focusMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}
