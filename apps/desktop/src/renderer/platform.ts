import {
  PlatformAdapterError,
  type PlatformAdapter,
} from "../../../../packages/app-platform/src/platform";
import type {
  DesktopCreateAnalysisRequest,
  DesktopResultSummary,
  DesktopPlatformInfo,
  DesktopSelection,
  DesktopTaskRuntime,
  DesktopTaskSummary,
  DesktopDocumentationBundle,
  DesktopDocumentationPage,
  DesktopDocumentationSelection,
  DesktopNotificationSettings,
  DesktopNotificationEvent,
  JelicaDesktopBridge,
} from "../common/contracts";

export interface DesktopPlatformAdapter extends PlatformAdapter {
  readonly kind: "desktop";
  getPlatformInfo(): Promise<DesktopPlatformInfo>;
  selectInputFiles(): Promise<readonly DesktopSelection[]>;
  selectInputDirectory(): Promise<DesktopSelection | null>;
  selectConfig(): Promise<DesktopSelection | null>;
  releaseSelection(id: string): Promise<null>;
  listTasks(): Promise<readonly DesktopTaskSummary[]>;
  createAnalysis(request: DesktopCreateAnalysisRequest): Promise<DesktopTaskSummary>;
  getTask(id: string): Promise<DesktopTaskRuntime>;
  startTask(id: string): Promise<DesktopTaskRuntime>;
  pauseTask(id: string): Promise<DesktopTaskRuntime>;
  resumeTask(id: string): Promise<DesktopTaskRuntime>;
  getResult(id: string): Promise<DesktopResultSummary>;
  showResultInFolder(id: string): Promise<null>;
  getDocumentationBundle(selection: DesktopDocumentationSelection): Promise<DesktopDocumentationBundle>;
  resolveDocumentationPage(id: string, selection: DesktopDocumentationSelection): Promise<DesktopDocumentationPage>;
  openDocumentationPdf(selection: DesktopDocumentationSelection): Promise<null>;
  getNotificationSettings(): Promise<DesktopNotificationSettings>;
  updateNotificationSettings(settings: DesktopNotificationSettings): Promise<DesktopNotificationSettings>;
  subscribeNotifications(listener: (event: DesktopNotificationEvent) => void): () => void;
}

export function createDesktopPlatformAdapter(
  suppliedBridge?: JelicaDesktopBridge,
): DesktopPlatformAdapter {
  const bridge = suppliedBridge ?? window.jelicaDesktop;
  if (!bridge) throw new DesktopBridgeUnavailableError();

  const call = async <T>(operation: () => Promise<import("../common/contracts").DesktopResult<T>>): Promise<T> => {
    const result = await operation();
    if (!result.ok) throw new DesktopOperationError(result.error.message, result.error.code);
    return result.value;
  };
  return Object.freeze({
    kind: "desktop" as const,
    async getPlatformInfo() {
      return call(() => bridge.getPlatformInfo());
    },
    async openExternal(url: string) {
      try { await call(() => bridge.openExternal(url)); } catch (error) {
        if (error instanceof DesktopOperationError) throw new PlatformAdapterError(error.code === "invalid_input" ? "invalid_external_url" : "external_open_failed", error.message);
        throw error;
      }
    },
    selectInputFiles: () => call(() => bridge.selectInputFiles()),
    selectInputDirectory: () => call(() => bridge.selectInputDirectory()),
    selectConfig: () => call(() => bridge.selectConfig()),
    releaseSelection: (id: string) => call(() => bridge.releaseSelection(id)),
    listTasks: () => call(() => bridge.listTasks()),
    createAnalysis: (request: DesktopCreateAnalysisRequest) => call(() => bridge.createAnalysis(request)),
    getTask: (id: string) => call(() => bridge.getTask(id)),
    startTask: (id: string) => call(() => bridge.startTask(id)),
    pauseTask: (id: string) => call(() => bridge.pauseTask(id)),
    resumeTask: (id: string) => call(() => bridge.resumeTask(id)),
    getResult: (id: string) => call(() => bridge.getResult(id)),
    showResultInFolder: (id: string) => call(() => bridge.showResultInFolder(id)),
    getDocumentationBundle: (selection: DesktopDocumentationSelection) => call(() => bridge.getDocumentationBundle(selection)),
    resolveDocumentationPage: (id: string, selection: DesktopDocumentationSelection) => call(() => bridge.resolveDocumentationPage(id, selection)),
    openDocumentationPdf: (selection: DesktopDocumentationSelection) => call(() => bridge.openDocumentationPdf(selection)),
    getNotificationSettings: () => call(() => bridge.getNotificationSettings()),
    updateNotificationSettings: (settings: DesktopNotificationSettings) => call(() => bridge.updateNotificationSettings(settings)),
    subscribeNotifications: (listener: (event: DesktopNotificationEvent) => void) => bridge.subscribeNotifications(listener),
  });
}

export class DesktopBridgeUnavailableError extends Error {
  constructor(message = "The JELICA Desktop preload bridge is unavailable.") {
    super(message);
    this.name = "DesktopBridgeUnavailableError";
  }
}

export class DesktopOperationError extends Error {
  constructor(message: string, readonly code: string) { super(message); this.name = "DesktopOperationError"; }
}
