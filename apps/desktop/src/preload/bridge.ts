import {
  IPC_CHANNELS,
  type DesktopPlatformInfo,
  type DesktopResult,
  type JelicaDesktopBridge,
  type DesktopCreateAnalysisRequest,
  type DesktopSelection,
  type DesktopTaskSummary,
  type DesktopTaskRuntime,
  type DesktopResultSummary,
  type DesktopDocumentationBundle,
  type DesktopDocumentationPage,
  type DesktopDocumentationSelection,
  type DesktopNotificationSettings,
  type DesktopNotificationEvent,
} from "../common/contracts";

type Invoke = <T>(channel: string, payload?: string) => Promise<DesktopResult<T>>;

type InvokeWithPayload = <T>(channel: string, payload: unknown) => Promise<DesktopResult<T>>;
type Subscribe = (channel: string, listener: (payload: unknown) => void) => () => void;

export function createDesktopBridge(
  invoke: Invoke,
  invokeWithPayload: InvokeWithPayload = (channel, payload) =>
    invoke(channel, typeof payload === "string" ? payload : undefined),
  subscribe: Subscribe = () => () => undefined,
): JelicaDesktopBridge {
  return Object.freeze({
    getPlatformInfo: () => invoke<DesktopPlatformInfo>(IPC_CHANNELS.getPlatformInfo),
    openExternal: (url: string) => invoke<null>(IPC_CHANNELS.openExternal, url),
    selectInputFiles: () => invoke<readonly DesktopSelection[]>(IPC_CHANNELS.selectInputFiles),
    selectInputDirectory: () => invoke<DesktopSelection | null>(IPC_CHANNELS.selectInputDirectory),
    selectConfig: () => invoke<DesktopSelection | null>(IPC_CHANNELS.selectConfig),
    releaseSelection: (selectionId: string) => invoke<null>(IPC_CHANNELS.releaseSelection, selectionId),
    listTasks: () => invoke<readonly DesktopTaskSummary[]>(IPC_CHANNELS.listTasks),
    createAnalysis: (request: DesktopCreateAnalysisRequest) => invokeWithPayload<DesktopTaskSummary>(IPC_CHANNELS.createAnalysis, request),
    getTask: (taskId: string) => invoke<DesktopTaskRuntime>(IPC_CHANNELS.getTask, taskId),
    startTask: (taskId: string) => invoke<DesktopTaskRuntime>(IPC_CHANNELS.startTask, taskId),
    pauseTask: (taskId: string) => invoke<DesktopTaskRuntime>(IPC_CHANNELS.pauseTask, taskId),
    resumeTask: (taskId: string) => invoke<DesktopTaskRuntime>(IPC_CHANNELS.resumeTask, taskId),
    getResult: (taskId: string) => invoke<DesktopResultSummary>(IPC_CHANNELS.getResult, taskId),
    showResultInFolder: (taskId: string) => invoke<null>(IPC_CHANNELS.showResultInFolder, taskId),
    getDocumentationBundle: (selection: DesktopDocumentationSelection) => invokeWithPayload<DesktopDocumentationBundle>(IPC_CHANNELS.getDocumentationBundle, selection),
    resolveDocumentationPage: (pageId: string, selection: DesktopDocumentationSelection) => invokeWithPayload<DesktopDocumentationPage>(IPC_CHANNELS.resolveDocumentationPage, { pageId, selection }),
    openDocumentationPdf: (selection: DesktopDocumentationSelection) => invokeWithPayload<null>(IPC_CHANNELS.openDocumentationPdf, selection),
    getNotificationSettings: () => invoke<DesktopNotificationSettings>(IPC_CHANNELS.getNotificationSettings),
    updateNotificationSettings: (settings: DesktopNotificationSettings) => invokeWithPayload<DesktopNotificationSettings>(IPC_CHANNELS.updateNotificationSettings, settings),
    subscribeNotifications: (listener: (event: DesktopNotificationEvent) => void) =>
      subscribe(IPC_CHANNELS.notificationEvent, (payload) => listener(payload as DesktopNotificationEvent)),
  });
}
