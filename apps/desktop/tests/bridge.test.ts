import assert from "node:assert/strict";
import test from "node:test";

import { IPC_CHANNELS, type DesktopResult } from "../src/common/contracts";
import { createDesktopBridge } from "../src/preload/bridge";

test("preload bridge exposes only the approved frozen methods", () => {
  const calls: Array<readonly [string, string | undefined]> = [];
  const bridge = createDesktopBridge(<T>(channel: string, payload?: string) => {
    calls.push([channel, payload]);
    return Promise.resolve({ ok: true, value: null } as DesktopResult<T>);
  });

  assert.deepEqual(Object.keys(bridge).sort(), ["createAnalysis", "getDocumentationBundle", "getNotificationSettings", "getPlatformInfo", "getResult", "getTask", "listTasks", "openDocumentationPdf", "openExternal", "pauseTask", "releaseSelection", "resolveDocumentationPage", "resumeTask", "selectConfig", "selectInputDirectory", "selectInputFiles", "showResultInFolder", "startTask", "subscribeNotifications", "updateNotificationSettings"]);
  assert.equal(Object.isFrozen(bridge), true);
  assert.equal("ipcRenderer" in bridge, false);
  assert.equal("invoke" in bridge, false);
  assert.equal("send" in bridge, false);

  void bridge.getPlatformInfo();
  void bridge.openExternal("https://example.com");
  assert.deepEqual(calls, [
    [IPC_CHANNELS.getPlatformInfo, undefined],
    [IPC_CHANNELS.openExternal, "https://example.com"],
  ]);
});
