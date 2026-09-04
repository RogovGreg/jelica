import { app, dialog, ipcMain, shell, type BrowserWindow } from "electron";

import {
  IPC_CHANNELS,
  type DesktopResult,
  type DesktopSelection,
  type DesktopTaskRuntime,
  type DesktopTaskSummary,
  type DesktopDocumentationBundle,
  type DesktopDocumentationPage,
  type DesktopDocumentationSelection,
  type DesktopNotificationSettings,
} from "../common/contracts";
import { isDocumentationLocale, isDocumentationProfile, isDocumentationTextSize } from "../../../../packages/app-platform/src/documentation";
import { DesktopAnalyticsService } from "./analytics";
import { DocumentationResourceResolver } from "./documentation";
import { parseExternalUrl } from "./external-url";
import { DesktopNotificationController } from "./notifications";

type Dependencies = Readonly<{
  analytics: DesktopAnalyticsService;
  getWindow: () => BrowserWindow | null;
  documentation: DocumentationResourceResolver;
  notifications: DesktopNotificationController;
}>;

export function registerDesktopIpc(dependencies: Dependencies): () => void {
  const { analytics, getWindow, documentation, notifications } = dependencies;
  ipcMain.handle(IPC_CHANNELS.getPlatformInfo, () => ({
    ok: true,
    value: {
      platform: normalizePlatform(process.platform),
      architecture: process.arch,
      packaged: app.isPackaged,
      appVersion: app.getVersion(),
    },
  }));
  ipcMain.handle(IPC_CHANNELS.openExternal, async (_event, rawUrl: unknown) => {
    if (typeof rawUrl !== "string") return failure("invalid_input", "The external URL is invalid.");
    const url = parseExternalUrl(rawUrl);
    if (!url) return failure("invalid_input", "The external URL is not allowed.");
    try {
      await shell.openExternal(url.href);
      return { ok: true, value: null };
    } catch {
      console.error("JELICA Desktop could not open an approved external URL.");
      return failure("external_open_failed", "The external URL could not be opened.");
    }
  });
  ipcMain.handle(IPC_CHANNELS.selectInputFiles, async (): Promise<DesktopResult<readonly DesktopSelection[]>> => {
    try {
    const result = await showOpenDialog(getWindow(), {
      properties: ["openFile", "multiSelections"],
      filters: [{ name: "Sequence and data files", extensions: ["fasta", "fa", "fas", "fastq", "gb", "gbk", "txt", "csv"] }],
    });
    if (result.canceled) return { ok: true, value: [] };
    return { ok: true, value: result.filePaths.map((item) => analytics.selections.register(item, "file")) };
    } catch { return failure("internal_error", "Native selection could not be completed."); }
  });
  ipcMain.handle(IPC_CHANNELS.selectInputDirectory, async (): Promise<DesktopResult<DesktopSelection | null>> => {
    try {
    const result = await showOpenDialog(getWindow(), { properties: ["openDirectory"] });
    if (result.canceled || result.filePaths[0] === undefined) return { ok: true, value: null };
    return { ok: true, value: analytics.selections.register(result.filePaths[0], "directory") };
    } catch { return failure("internal_error", "Native selection could not be completed."); }
  });
  ipcMain.handle(IPC_CHANNELS.selectConfig, async (): Promise<DesktopResult<DesktopSelection | null>> => {
    try {
    const result = await showOpenDialog(getWindow(), {
      properties: ["openFile"],
      filters: [{ name: "JELICA configuration", extensions: ["json"] }],
    });
    if (result.canceled || result.filePaths[0] === undefined) return { ok: true, value: null };
    return { ok: true, value: analytics.selections.register(result.filePaths[0], "config") };
    } catch { return failure("internal_error", "Native selection could not be completed."); }
  });
  ipcMain.handle(IPC_CHANNELS.releaseSelection, (_event, selectionId: unknown) => {
    if (typeof selectionId !== "string" || !/^[0-9a-f-]{36}$/i.test(selectionId)) return failure("invalid_input", "The selection reference is invalid.");
    analytics.selections.remove(selectionId);
    return { ok: true, value: null };
  });
  ipcMain.handle(IPC_CHANNELS.listTasks, (): Promise<DesktopResult<readonly DesktopTaskSummary[]>> => analytics.listTasks());
  ipcMain.handle(IPC_CHANNELS.createAnalysis, (_event, request: unknown) => analytics.createAnalysis(request));
  ipcMain.handle(IPC_CHANNELS.getTask, (_event, taskId: unknown): Promise<DesktopResult<DesktopTaskRuntime>> => analytics.getTask(taskId));
  for (const [channel, action] of [[IPC_CHANNELS.startTask, "start"], [IPC_CHANNELS.pauseTask, "pause"], [IPC_CHANNELS.resumeTask, "resume"]] as const) {
    ipcMain.handle(channel, (_event, taskId: unknown) => analytics.lifecycle(action, taskId));
  }
  ipcMain.handle(IPC_CHANNELS.getResult, (_event, taskId: unknown) => analytics.getResult(taskId));
  ipcMain.handle(IPC_CHANNELS.showResultInFolder, (_event, taskId: unknown) => analytics.showResultInFolder(taskId, (filePath) => shell.showItemInFolder(filePath)));
  ipcMain.handle(IPC_CHANNELS.getDocumentationBundle, (_event, rawSelection: unknown): DesktopResult<DesktopDocumentationBundle> => { const selection = documentationSelection(rawSelection); const bundle = selection && documentation.effectiveBundle(selection); return bundle ? { ok: true, value: bundle } : selection ? failure("internal_error", "Offline documentation is unavailable.") : failure("invalid_input", "The documentation selection is invalid."); });
  ipcMain.handle(IPC_CHANNELS.resolveDocumentationPage, (_event, raw: unknown): DesktopResult<DesktopDocumentationPage> => { const value = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : null; const pageId = value?.pageId; const selection = documentationSelection(value?.selection); if (typeof pageId !== "string" || !/^[A-Za-z0-9._-]+$/.test(pageId) || !selection) return failure("invalid_input", "The documentation page reference is invalid."); const page = documentation.page(pageId, selection); const resourceId = page && documentation.resourceUrl(page.path, selection); return page && resourceId ? { ok: true, value: { pageId: page.id, title: page.title, resourceId } } : failure("internal_error", "Offline documentation is unavailable."); });
  ipcMain.handle(IPC_CHANNELS.openDocumentationPdf, async (_event, rawSelection: unknown): Promise<DesktopResult<null>> => { const selection = documentationSelection(rawSelection); if (!selection) return failure("invalid_input", "The documentation selection is invalid."); const file = documentation.nativePdfPath(selection); if (!file) return failure("internal_error", "Offline documentation PDF is unavailable."); try { const error = await shell.openPath(file); return error ? failure("internal_error", "Offline documentation PDF could not be opened.") : { ok: true, value: null }; } catch { return failure("internal_error", "Offline documentation PDF could not be opened."); } });
  ipcMain.handle(IPC_CHANNELS.getNotificationSettings, async (): Promise<DesktopResult<DesktopNotificationSettings>> => {
    try { return { ok: true, value: await notifications.getSettings() }; } catch { return failure("internal_error", "Notification settings are unavailable."); }
  });
  ipcMain.handle(IPC_CHANNELS.updateNotificationSettings, async (_event, raw: unknown): Promise<DesktopResult<DesktopNotificationSettings>> => {
    if (!isNotificationSettings(raw)) return failure("invalid_input", "Notification settings are invalid.");
    try { return { ok: true, value: await notifications.updateSettings(raw) }; } catch { return failure("internal_error", "Notification settings could not be saved."); }
  });

  return () => {
    for (const channel of Object.values(IPC_CHANNELS)) ipcMain.removeHandler(channel);
  };
}

function failure(code: "invalid_input" | "external_open_failed" | "internal_error", message: string): DesktopResult<never> {
  return { ok: false, error: { code, message } };
}

function documentationSelection(value: unknown): DesktopDocumentationSelection | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (typeof item.locale !== "string" || !isDocumentationLocale(item.locale)) return null;
  if (item.profile !== undefined && (typeof item.profile !== "string" || !isDocumentationProfile(item.profile))) return null;
  if (item.textSize !== undefined && (typeof item.textSize !== "string" || !isDocumentationTextSize(item.textSize))) return null;
  const result: { locale: DesktopDocumentationSelection["locale"]; profile?: NonNullable<DesktopDocumentationSelection["profile"]>; textSize?: NonNullable<DesktopDocumentationSelection["textSize"]> } = { locale: item.locale };
  if (typeof item.profile === "string") result.profile = item.profile as NonNullable<DesktopDocumentationSelection["profile"]>;
  if (typeof item.textSize === "string") result.textSize = item.textSize as NonNullable<DesktopDocumentationSelection["textSize"]>;
  return result;
}

function isNotificationSettings(value: unknown): value is DesktopNotificationSettings {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  if (!["deviceEnabled", "inAppEnabled", "soundEnabled", "deviceEvents", "inAppEvents"].every((key) => key in item)) return false;
  return typeof item.deviceEnabled === "boolean" && typeof item.inAppEnabled === "boolean" && typeof item.soundEnabled === "boolean" && validEvents(item.deviceEvents) && validEvents(item.inAppEvents);
}
function validEvents(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return ["task.started", "task.scheduler_paused", "task.completed", "task.failed"].every((id) => typeof (value as Record<string, unknown>)[id] === "boolean");
}

function normalizePlatform(platform: NodeJS.Platform): "darwin" | "linux" | "win32" | "other" {
  return platform === "darwin" || platform === "linux" || platform === "win32" ? platform : "other";
}

type OpenDialogOptions = Parameters<typeof dialog.showOpenDialog>[0];

function showOpenDialog(window: BrowserWindow | null, options: OpenDialogOptions) {
  return window ? dialog.showOpenDialog(window, options) : dialog.showOpenDialog(options);
}
