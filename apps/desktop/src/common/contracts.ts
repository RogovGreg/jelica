export const IPC_CHANNELS = Object.freeze({
  getPlatformInfo: "jelica-desktop:platform-info",
  openExternal: "jelica-desktop:open-external",
  selectInputFiles: "jelica-desktop:select-input-files",
  selectInputDirectory: "jelica-desktop:select-input-directory",
  selectConfig: "jelica-desktop:select-config",
  releaseSelection: "jelica-desktop:release-selection",
  listTasks: "jelica-desktop:list-tasks",
  createAnalysis: "jelica-desktop:create-analysis",
  getTask: "jelica-desktop:get-task",
  startTask: "jelica-desktop:start-task",
  pauseTask: "jelica-desktop:pause-task",
  resumeTask: "jelica-desktop:resume-task",
  getResult: "jelica-desktop:get-result",
  showResultInFolder: "jelica-desktop:show-result-in-folder",
  getDocumentationBundle: "jelica-desktop:documentation-bundle",
  resolveDocumentationPage: "jelica-desktop:documentation-page",
  openDocumentationPdf: "jelica-desktop:documentation-open-pdf",
  getNotificationSettings: "jelica-desktop:notification-settings",
  updateNotificationSettings: "jelica-desktop:notification-settings-update",
  notificationEvent: "jelica-desktop:notification-event",
} as const);

export type DesktopErrorCode =
  | "invalid_input"
  | "input_unavailable"
  | "task_not_found"
  | "cli_unavailable"
  | "command_failed"
  | "protocol_error"
  | "result_unavailable"
  | "external_open_failed"
  | "internal_error";

export type DesktopError = Readonly<{
  code: DesktopErrorCode;
  message: string;
}>;

export type DesktopResult<T> =
  | Readonly<{ ok: true; value: T }>
  | Readonly<{ ok: false; error: DesktopError }>;

export type DesktopPlatformInfo = Readonly<{
  platform: "darwin" | "linux" | "win32" | "other";
  architecture: string;
  packaged: boolean;
  appVersion: string;
}>;

export type DesktopSelectionKind = "file" | "directory" | "config";

export type DesktopSelection = Readonly<{
  id: string;
  kind: DesktopSelectionKind;
  displayName: string;
}>;

export type DesktopTaskSummary = Readonly<{
  taskId: string;
  name: string | null;
  state: string;
  currentStage: string | null;
  progress: number | null;
  traceId: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}>;

export type DesktopTaskRuntime = DesktopTaskSummary & Readonly<{
  activeJobState: string | null;
  failure: Readonly<{ code: number; message: string }> | null;
}>;

export type DesktopCreateAnalysisRequest = Readonly<{
  inputSelectionIds: readonly string[];
  configSelectionId: string | null;
  ncbiSources: readonly string[];
  name: string | null;
  traceId: string | null;
  overrides: import("../../../../packages/app-platform/src/analysis").AnalysisOverrides | null;
}>;

export type DesktopResultSummary = Readonly<{
  taskId: string;
  state: string;
  available: boolean;
  contentId: string | null;
  fileName: string | null;
  detail: string | null;
}>;
export type DesktopDocumentationPage = Readonly<{ pageId: string; title: string; resourceId: string }>;
export type DesktopDocumentationBundle = import("../../../../packages/app-platform/src/documentation").DocumentationBundle;
export type DesktopDocumentationSelection = Readonly<{
  locale: import("../../../../packages/app-platform/src/documentation").DocumentationLocale;
  profile?: import("../../../../packages/app-platform/src/documentation").DocumentationProfile;
  textSize?: import("../../../../packages/app-platform/src/documentation").DocumentationTextSize;
}>;
export type LocalNotificationEventId = "task.started" | "task.scheduler_paused" | "task.completed" | "task.failed";
export type DesktopNotificationSettings = Readonly<{
  deviceEnabled: boolean;
  inAppEnabled: boolean;
  soundEnabled: boolean;
  deviceEvents: Readonly<Record<LocalNotificationEventId, boolean>>;
  inAppEvents: Readonly<Record<LocalNotificationEventId, boolean>>;
}>;
export type DesktopNotificationEvent = Readonly<{
  occurrenceId: string;
  eventId: LocalNotificationEventId;
  taskId: string;
  taskName: string;
  timestamp: string;
  state: string;
  playSound: boolean;
}>;

export interface JelicaDesktopBridge {
  getPlatformInfo(): Promise<DesktopResult<DesktopPlatformInfo>>;
  openExternal(url: string): Promise<DesktopResult<null>>;
  selectInputFiles(): Promise<DesktopResult<readonly DesktopSelection[]>>;
  selectInputDirectory(): Promise<DesktopResult<DesktopSelection | null>>;
  selectConfig(): Promise<DesktopResult<DesktopSelection | null>>;
  releaseSelection(selectionId: string): Promise<DesktopResult<null>>;
  listTasks(): Promise<DesktopResult<readonly DesktopTaskSummary[]>>;
  createAnalysis(request: DesktopCreateAnalysisRequest): Promise<DesktopResult<DesktopTaskSummary>>;
  getTask(taskId: string): Promise<DesktopResult<DesktopTaskRuntime>>;
  startTask(taskId: string): Promise<DesktopResult<DesktopTaskRuntime>>;
  pauseTask(taskId: string): Promise<DesktopResult<DesktopTaskRuntime>>;
  resumeTask(taskId: string): Promise<DesktopResult<DesktopTaskRuntime>>;
  getResult(taskId: string): Promise<DesktopResult<DesktopResultSummary>>;
  showResultInFolder(taskId: string): Promise<DesktopResult<null>>;
  getDocumentationBundle(selection: DesktopDocumentationSelection): Promise<DesktopResult<DesktopDocumentationBundle>>;
  resolveDocumentationPage(pageId: string, selection: DesktopDocumentationSelection): Promise<DesktopResult<DesktopDocumentationPage>>;
  openDocumentationPdf(selection: DesktopDocumentationSelection): Promise<DesktopResult<null>>;
  getNotificationSettings(): Promise<DesktopResult<DesktopNotificationSettings>>;
  updateNotificationSettings(settings: DesktopNotificationSettings): Promise<DesktopResult<DesktopNotificationSettings>>;
  subscribeNotifications(listener: (event: DesktopNotificationEvent) => void): () => void;
}
