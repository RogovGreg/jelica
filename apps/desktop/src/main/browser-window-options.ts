import type { BrowserWindowConstructorOptions } from "electron";

export function createBrowserWindowOptions(preloadPath: string): BrowserWindowConstructorOptions {
  return {
    width: 1080,
    height: 720,
    minWidth: 320,
    minHeight: 480,
    show: false,
    backgroundColor: "#F6FAF8",
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
    },
  };
}
