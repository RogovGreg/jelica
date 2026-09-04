import type { JelicaDesktopBridge } from "../common/contracts";

declare global {
  interface Window {
    readonly jelicaDesktop?: JelicaDesktopBridge;
  }
}

export {};
