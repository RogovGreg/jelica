import assert from "node:assert/strict";
import test from "node:test";

import {
  createWebPlatformAdapter,
  PlatformAdapterError,
  type PlatformAdapter,
} from "../../../packages/app-platform/src/platform";
import type { JelicaDesktopBridge } from "../src/common/contracts";
import {
  createDesktopPlatformAdapter,
  DesktopBridgeUnavailableError,
} from "../src/renderer/platform";

test("Web and Desktop implementations satisfy the same platform contract", async () => {
  const opened: string[] = [];
  const web: PlatformAdapter = createWebPlatformAdapter((url) => {
    opened.push(url);
    return {} as Window;
  });
  const desktopBridge = {
    getPlatformInfo: async () => ({
      ok: true,
      value: { platform: "linux", architecture: "x64", packaged: false, appVersion: "0.1.0" },
    }),
    openExternal: async (url) => {
      opened.push(url);
      return { ok: true, value: null };
    },
  } as JelicaDesktopBridge;
  const desktop: PlatformAdapter = createDesktopPlatformAdapter(desktopBridge);

  assert.equal(web.kind, "web");
  assert.equal(desktop.kind, "desktop");
  await web.openExternal("https://example.com/web");
  await desktop.openExternal("https://example.com/desktop");
  assert.deepEqual(opened, ["https://example.com/web", "https://example.com/desktop"]);
});

test("Web adapter rejects unsafe schemes before browser access", async () => {
  const adapter = createWebPlatformAdapter(() => {
    throw new Error("must not open");
  });
  await assert.rejects(adapter.openExternal("file:///tmp/result"), PlatformAdapterError);
});

test("Desktop adapter reports a missing preload bridge as a controlled error", () => {
  const previousWindow = globalThis.window;
  Object.defineProperty(globalThis, "window", { value: {}, configurable: true });
  try {
    assert.throws(() => createDesktopPlatformAdapter(), DesktopBridgeUnavailableError);
  } finally {
    if (previousWindow === undefined) delete (globalThis as { window?: Window }).window;
    else Object.defineProperty(globalThis, "window", { value: previousWindow, configurable: true });
  }
});
