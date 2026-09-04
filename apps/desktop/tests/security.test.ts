import assert from "node:assert/strict";
import test from "node:test";

import { createBrowserWindowOptions } from "../src/main/browser-window-options";
import { parseExternalUrl } from "../src/main/external-url";
import { isAllowedRendererNavigation } from "../src/main/navigation";

test("BrowserWindow options enforce the required renderer isolation", () => {
  const options = createBrowserWindowOptions("/safe/preload.js");
  assert.equal(options.minWidth, 320);
  assert.equal(options.minHeight, 480);
  assert.equal(options.show, false);
  assert.deepEqual(options.webPreferences, {
    preload: "/safe/preload.js",
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    webSecurity: true,
    webviewTag: false,
  });
});

test("external URL policy permits only HTTP and HTTPS", () => {
  assert.equal(parseExternalUrl("https://example.com/path")?.protocol, "https:");
  assert.equal(parseExternalUrl("http://127.0.0.1:8080")?.protocol, "http:");
  for (const rejected of [
    "javascript:alert(1)",
    "file:///tmp/private",
    "data:text/plain,hello",
    "vbscript:msgbox(1)",
    "mailto:test@example.com",
    "not a url",
    " https://example.com",
  ]) {
    assert.equal(parseExternalUrl(rejected), null, rejected);
  }
});

test("renderer navigation stays on the fixed app entry", () => {
  const productionEntry = "file:///Applications/JELICA/resources/app/dist/renderer/index.html";
  assert.equal(isAllowedRendererNavigation(productionEntry, productionEntry, false), true);
  assert.equal(isAllowedRendererNavigation(`${productionEntry}#section`, productionEntry, false), true);
  assert.equal(isAllowedRendererNavigation("file:///tmp/untrusted.html", productionEntry, false), false);
  assert.equal(
    isAllowedRendererNavigation("http://127.0.0.1:5173/src/renderer/index.tsx", "http://127.0.0.1:5173", true),
    true,
  );
  assert.equal(
    isAllowedRendererNavigation("https://example.com", "http://127.0.0.1:5173", true),
    false,
  );
});
