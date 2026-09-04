import { contextBridge, ipcRenderer } from "electron";

import type { DesktopResult } from "../common/contracts";
import { createDesktopBridge } from "./bridge";

const invoke = <T>(channel: string, payload?: string) =>
  ipcRenderer.invoke(channel, payload) as Promise<DesktopResult<T>>;
const bridge = createDesktopBridge(
  invoke,
  <T>(channel: string, payload: unknown) => ipcRenderer.invoke(channel, payload) as Promise<DesktopResult<T>>,
  (channel, listener) => {
    const wrapped = (_event: Electron.IpcRendererEvent, payload: unknown) => listener(payload);
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.removeListener(channel, wrapped);
  },
);

contextBridge.exposeInMainWorld("jelicaDesktop", bridge);
