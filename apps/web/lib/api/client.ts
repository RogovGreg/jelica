import { buildApiUrl } from "@/lib/api/config";
import { ApiClientError, toErrorMessage } from "@/lib/api/errors";
import type {
  ApiErrorEnvelope,
  AuthLoginRequest,
  AuthRegisterRequest,
  AuthRegisterResponse,
  AuthActionResponse,
  AuthUser,
  AuthUserResponse,
  AuthSessionListResponse,
  AuthVerifyEmailRequest,
  HealthResponse,
  Project,
  ProjectCommentListResponse,
  ProjectHistoryListResponse,
  InvitationCandidateListResponse,
  NotificationListResponse,
  NotificationPreferencesPatch,
  NotificationPreferencesResponse,
  ProjectInvitation,
  ProjectInvitationListResponse,
  ProjectListResponse,
  ProjectMemberListResponse,
  ProjectMember,
  ProjectMemberRole,
  ProjectTaskListResponse,
  ProjectTask,
  SupportRequestCreatePayload,
  SupportRequestResponse,
  TaskListResponse,
  TaskResultLookupResponse,
  TaskStatusSnapshot,
  TaskDiscussionMetadata,
  TaskDiscussionCommentListResponse,
  TaskSubmissionRequest,
  TaskSubmissionResult,
  UploadSession,
  UploadItemsResponse,
  UploadItem,
  WebNotification,
  WebPushConfigResponse,
  WebPushSubscriptionPayload,
  TelegramIntegrationState,
  TelegramLinkResponse,
  SupportedLocale,
} from "@/types/api";

type TaskStatusBatchFailure = {
  taskId: string;
  error: string;
};

type TaskStatusBatchResult = {
  items: TaskStatusSnapshot[];
  failures: TaskStatusBatchFailure[];
};

export async function getHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/health", { method: "GET" });
}

export async function createTask(payload: TaskSubmissionRequest): Promise<TaskSubmissionResult> {
  return requestJson<TaskSubmissionResult>("/api/tasks", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createUploadSession(): Promise<UploadSession> {
  return requestJson<UploadSession>("/api/analysis-uploads", { method: "POST" });
}
export function getUploadSession(sessionId: string): Promise<UploadSession> {
  return requestJson<UploadSession>(`/api/analysis-uploads/${encodeURIComponent(sessionId)}`, { method: "GET" });
}
export async function deleteUploadSession(sessionId: string): Promise<void> {
  await requestJson<void>(`/api/analysis-uploads/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}
export async function deleteUploadItem(sessionId: string, itemId: string): Promise<void> {
  await requestJson<void>(`/api/analysis-uploads/${encodeURIComponent(sessionId)}/items/${encodeURIComponent(itemId)}`, { method: "DELETE" });
}

export function uploadInputFiles(sessionId: string, files: File[], onProgress: (value: number) => void): Promise<UploadItemsResponse> {
  const form = new FormData(); files.forEach((file) => form.append("files", file, file.name));
  return uploadMultipartWithProgress<UploadItemsResponse>(`/api/analysis-uploads/${encodeURIComponent(sessionId)}/files`, form, onProgress);
}
export function uploadInputDirectory(sessionId: string, files: File[], relativePaths: string[], displayName: string, onProgress: (value: number) => void): Promise<UploadItem> {
  const form = new FormData(); form.append("display_name", displayName); files.forEach((file, i) => { form.append("files", file, file.name); form.append("relative_paths", relativePaths[i]); });
  return uploadMultipartWithProgress<UploadItem>(`/api/analysis-uploads/${encodeURIComponent(sessionId)}/directories`, form, onProgress);
}
export function uploadConfig(sessionId: string, file: File, onProgress: (value: number) => void): Promise<UploadItem> {
  const form = new FormData(); form.append("file", file, file.name);
  return uploadMultipartWithProgress<UploadItem>(`/api/analysis-uploads/${encodeURIComponent(sessionId)}/config`, form, onProgress);
}

function uploadMultipartWithProgress<T>(path: string, body: FormData, onProgress: (value: number) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); xhr.open("POST", buildApiUrl(path)); xhr.withCredentials = true;
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100)); };
    xhr.onerror = () => reject(new ApiClientError({ message: "Upload failed.", status: 0, payload: null }));
    xhr.onload = () => { let payload: unknown = null; try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch { payload = { message: xhr.responseText }; } if (xhr.status < 200 || xhr.status >= 300) { reject(new ApiClientError({ message: extractErrorMessage(payload, xhr.status), status: xhr.status, payload })); return; } resolve(payload as T); };
    xhr.send(body);
  });
}

export async function getTaskStatus(taskId: string): Promise<TaskStatusSnapshot> {
  return requestJson<TaskStatusSnapshot>(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "GET" });
}

export function startTask(taskId: string): Promise<TaskStatusSnapshot> {
  return requestJson<TaskStatusSnapshot>(`/api/tasks/${encodeURIComponent(taskId)}/start`, { method: "POST" });
}
export function pauseTask(taskId: string): Promise<TaskStatusSnapshot> {
  return requestJson<TaskStatusSnapshot>(`/api/tasks/${encodeURIComponent(taskId)}/pause`, { method: "POST" });
}
export function resumeTask(taskId: string): Promise<TaskStatusSnapshot> {
  return requestJson<TaskStatusSnapshot>(`/api/tasks/${encodeURIComponent(taskId)}/resume`, { method: "POST" });
}

export async function getTaskResult(taskId: string): Promise<TaskResultLookupResponse> {
  return requestJson<TaskResultLookupResponse>(
    `/api/tasks/${encodeURIComponent(taskId)}/result`,
    { method: "GET" },
  );
}

export type TaskListFilters = Readonly<{
  owner?: "me";
  project_id?: string[];
  project?: "none";
  state?: string[];
}>;

export async function getTaskList(filters?: TaskListFilters): Promise<TaskListResponse> {
  const query = new URLSearchParams();
  filters?.project_id?.forEach((projectId) => query.append("project_id", projectId));
  filters?.state?.forEach((state) => query.append("state", state));
  if (filters?.owner) query.set("owner", filters.owner);
  if (filters?.project) query.set("project", filters.project);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return requestJson<TaskListResponse>(`/api/tasks${suffix}`, { method: "GET" });
}

export async function getTaskDiscussion(taskId: string): Promise<TaskDiscussionMetadata> {
  return requestJson<TaskDiscussionMetadata>(`/api/tasks/${encodeURIComponent(taskId)}/discussion`, { method: "GET" });
}

export async function getTaskDiscussionComments(taskId: string): Promise<TaskDiscussionCommentListResponse> {
  return requestJson<TaskDiscussionCommentListResponse>(`/api/tasks/${encodeURIComponent(taskId)}/discussion/comments`, { method: "GET" });
}

export async function createSupportRequest(
  payload: SupportRequestCreatePayload,
): Promise<SupportRequestResponse> {
  return requestJson<SupportRequestResponse>("/api/support", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getSupportRequest(requestId: string): Promise<SupportRequestResponse> {
  return requestJson<SupportRequestResponse>(`/api/support/${encodeURIComponent(requestId)}`, {
    method: "GET",
  });
}

export async function registerUser(payload: AuthRegisterRequest): Promise<AuthRegisterResponse> {
  return requestJson<AuthRegisterResponse>("/api/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function verifyEmail(payload: AuthVerifyEmailRequest): Promise<AuthUserResponse> {
  return requestJson<AuthUserResponse>("/api/auth/verify-email", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function resendVerification(email: string): Promise<AuthActionResponse> {
  return requestJson<AuthActionResponse>("/api/auth/resend-verification", { method: "POST", body: JSON.stringify({ email }) });
}

export async function requestPasswordReset(email: string): Promise<AuthActionResponse> {
  return requestJson<AuthActionResponse>("/api/auth/forgot-password", { method: "POST", body: JSON.stringify({ email }) });
}

export async function resetPassword(token: string, newPassword: string): Promise<AuthActionResponse> {
  return requestJson<AuthActionResponse>("/api/auth/reset-password", { method: "POST", body: JSON.stringify({ token, new_password: newPassword }) });
}

export async function login(payload: AuthLoginRequest): Promise<AuthUserResponse> {
  return requestJson<AuthUserResponse>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function logout(): Promise<void> {
  await requestJson<void>("/api/auth/logout", { method: "POST" });
}

export async function getCurrentUser(): Promise<AuthUser> {
  return requestJson<AuthUser>("/api/auth/me", { method: "GET" });
}

export type AuthUserPreferencesPatch = Readonly<{
  language?: SupportedLocale;
  theme?: AuthUser["theme"];
  interface_scale?: AuthUser["interface_scale"];
}>;

export async function updateCurrentUserPreferences(
  payload: AuthUserPreferencesPatch,
): Promise<AuthUser> {
  return requestJson<AuthUser>("/api/auth/me", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function updateCurrentUserLanguage(language: SupportedLocale): Promise<AuthUser> {
  return updateCurrentUserPreferences({ language });
}

export async function getAuthSessions(): Promise<AuthSessionListResponse> {
  return requestJson<AuthSessionListResponse>("/api/auth/sessions", { method: "GET" });
}

export async function revokeAuthSession(sessionId: string): Promise<void> {
  await requestJson<void>(`/api/auth/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

export async function revokeOtherAuthSessions(): Promise<void> {
  await requestJson<void>("/api/auth/sessions/revoke-others", { method: "POST" });
}

export function getNotifications(): Promise<NotificationListResponse> {
  return requestJson<NotificationListResponse>("/api/notifications", { method: "GET" });
}

export function getNotificationPreferences(): Promise<NotificationPreferencesResponse> {
  return requestJson<NotificationPreferencesResponse>("/api/notifications/preferences", {
    method: "GET",
  });
}

export function updateNotificationPreferences(
  payload: NotificationPreferencesPatch,
): Promise<NotificationPreferencesResponse> {
  return requestJson<NotificationPreferencesResponse>("/api/notifications/preferences", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function setNotificationReadState(
  notificationId: string,
  read: boolean,
): Promise<WebNotification> {
  return requestJson<WebNotification>(
    `/api/notifications/${encodeURIComponent(notificationId)}`,
    { method: "PATCH", body: JSON.stringify({ read }) },
  );
}

export async function markAllNotificationsRead(): Promise<void> {
  await requestJson<void>("/api/notifications/mark-all-read", { method: "POST" });
}

export function getWebPushConfig(): Promise<WebPushConfigResponse> {
  return requestJson<WebPushConfigResponse>("/api/notifications/push/config", {
    method: "GET",
  });
}

export function registerWebPushSubscription(
  payload: WebPushSubscriptionPayload,
): Promise<void> {
  return requestJson<void>("/api/notifications/push/subscriptions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function deleteCurrentWebPushSubscription(endpoint: string): Promise<void> {
  await requestJson<void>("/api/notifications/push/subscriptions/current", {
    method: "DELETE",
    body: JSON.stringify({ endpoint }),
  });
}

export function getTelegramIntegrationState(): Promise<TelegramIntegrationState> {
  return requestJson<TelegramIntegrationState>("/api/notifications/telegram", {
    method: "GET",
  });
}

export function createTelegramLink(): Promise<TelegramLinkResponse> {
  return requestJson<TelegramLinkResponse>("/api/notifications/telegram/link", {
    method: "POST",
  });
}

export async function disconnectTelegram(): Promise<void> {
  await requestJson<void>("/api/notifications/telegram/link", { method: "DELETE" });
}

export async function getProjects(): Promise<ProjectListResponse> {
  return requestJson<ProjectListResponse>("/api/projects", { method: "GET" });
}

export async function createProject(payload: { name: string; description?: string | null }): Promise<Project> {
  return requestJson<Project>("/api/projects", { method: "POST", body: JSON.stringify(payload) });
}

export async function getProject(projectId: string): Promise<Project> {
  return requestJson<Project>(`/api/projects/${encodeURIComponent(projectId)}`, { method: "GET" });
}

export async function updateProject(projectId: string, payload: Partial<Pick<Project, "name" | "description" | "status">>): Promise<Project> {
  return requestJson<Project>(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteProject(projectId: string): Promise<void> {
  await requestJson<void>(`/api/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
}

export async function leaveProject(projectId: string): Promise<void> {
  await requestJson<void>(`/api/projects/${encodeURIComponent(projectId)}/leave`, { method: "POST" });
}

export async function getProjectTasks(projectId: string): Promise<ProjectTaskListResponse> {
  return requestJson<ProjectTaskListResponse>(`/api/projects/${encodeURIComponent(projectId)}/tasks`, { method: "GET" });
}

export async function attachTaskToProject(projectId: string, taskId: string): Promise<ProjectTask> {
  return requestJson<ProjectTask>(`/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`, { method: "PUT" });
}

export async function detachTaskFromProject(projectId: string, taskId: string): Promise<ProjectTask> {
  return requestJson<ProjectTask>(`/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

export async function getProjectMembers(projectId: string): Promise<ProjectMemberListResponse> {
  return requestJson<ProjectMemberListResponse>(`/api/projects/${encodeURIComponent(projectId)}/members`, { method: "GET" });
}

export async function getProjectHistory(projectId: string): Promise<ProjectHistoryListResponse> {
  return requestJson<ProjectHistoryListResponse>(`/api/projects/${encodeURIComponent(projectId)}/history`, { method: "GET" });
}

export async function getProjectComments(projectId: string): Promise<ProjectCommentListResponse> {
  return requestJson<ProjectCommentListResponse>(`/api/projects/${encodeURIComponent(projectId)}/comments`, { method: "GET" });
}

export async function getReceivedInvitations(): Promise<ProjectInvitationListResponse> {
  return requestJson<ProjectInvitationListResponse>("/api/invitations", { method: "GET" });
}

export async function acceptInvitation(invitationId: string): Promise<ProjectInvitation> {
  return requestJson<ProjectInvitation>(`/api/invitations/${encodeURIComponent(invitationId)}/accept`, { method: "POST" });
}

export async function declineInvitation(invitationId: string): Promise<ProjectInvitation> {
  return requestJson<ProjectInvitation>(`/api/invitations/${encodeURIComponent(invitationId)}/decline`, { method: "POST" });
}

export async function updateProjectMemberRole(projectId: string, userId: string, role: ProjectMemberRole): Promise<ProjectMember> {
  return requestJson<ProjectMember>(`/api/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`, { method: "PATCH", body: JSON.stringify({ role }) });
}

export async function removeProjectMember(projectId: string, userId: string): Promise<void> {
  await requestJson<void>(`/api/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`, { method: "DELETE" });
}

export async function transferProjectOwnership(projectId: string, newOwnerUserId: string): Promise<Project> {
  return requestJson<Project>(`/api/projects/${encodeURIComponent(projectId)}/transfer-ownership`, { method: "POST", body: JSON.stringify({ new_owner_user_id: newOwnerUserId }) });
}

export async function listProjectInvitations(projectId: string): Promise<ProjectInvitationListResponse> {
  return requestJson<ProjectInvitationListResponse>(`/api/projects/${encodeURIComponent(projectId)}/invitations?status=pending`, { method: "GET" });
}

export async function listInvitationCandidates(projectId: string, query: string, limit = 10): Promise<InvitationCandidateListResponse> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  return requestJson<InvitationCandidateListResponse>(`/api/projects/${encodeURIComponent(projectId)}/invitation-candidates?${params.toString()}`, { method: "GET" });
}

export async function createProjectInvitation(projectId: string, invitedUserId: string, role: ProjectMemberRole): Promise<ProjectInvitation> {
  return requestJson<ProjectInvitation>(`/api/projects/${encodeURIComponent(projectId)}/invitations`, { method: "POST", body: JSON.stringify({ invited_user_id: invitedUserId, role }) });
}

export async function revokeProjectInvitation(projectId: string, invitationId: string): Promise<ProjectInvitation> {
  return requestJson<ProjectInvitation>(`/api/projects/${encodeURIComponent(projectId)}/invitations/${encodeURIComponent(invitationId)}/revoke`, { method: "POST" });
}

export async function getTaskStatuses(taskIds: string[]): Promise<TaskStatusBatchResult> {
  const uniqueTaskIds = Array.from(new Set(taskIds.map((item) => item.trim()).filter(Boolean)));
  const items: TaskStatusSnapshot[] = [];
  const failures: TaskStatusBatchFailure[] = [];
  const settled = await Promise.allSettled(uniqueTaskIds.map((taskId) => getTaskStatus(taskId)));

  for (let index = 0; index < settled.length; index += 1) {
    const result = settled[index];
    const taskId = uniqueTaskIds[index];
    if (result.status === "fulfilled") {
      items.push(result.value);
      continue;
    }
    failures.push({ taskId, error: toErrorMessage(result.reason) });
  }

  return { items, failures };
}

async function requestJson<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(buildApiUrl(path), {
    ...init,
    cache: "no-store",
    credentials: "include",
    headers: {
      "content-type": "application/json",
      ...(init.headers ?? {}),
    },
  });

  const payload = await parseJsonPayload(response);
  if (!response.ok) {
    throw new ApiClientError({
      message: extractErrorMessage(payload, response.status),
      status: response.status,
      payload,
      retryAfterSeconds: parseRetryAfter(response.headers.get("retry-after")),
    });
  }
  return payload as T;
}

function parseRetryAfter(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

async function parseJsonPayload(response: Response): Promise<ApiErrorEnvelope | unknown> {
  const text = await response.text();
  if (text.trim() === "") {
    return null;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { message: text };
  }
}

function extractErrorMessage(payload: unknown, statusCode: number): string {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    if (typeof record.message === "string" && record.message.trim() !== "") {
      return record.message;
    }
    if (typeof record.detail === "string" && record.detail.trim() !== "") {
      return record.detail;
    }
    if (Array.isArray(record.detail)) {
      const messages = record.detail
        .map((item) => {
          if (item && typeof item === "object" && "msg" in item) {
            const message = (item as Record<string, unknown>).msg;
            if (typeof message === "string" && message.trim() !== "") {
              return message;
            }
          }
          return null;
        })
        .filter((item): item is string => item !== null);
      if (messages.length > 0) {
        return messages.join("; ");
      }
    }
  }
  return `API request failed with status ${statusCode}.`;
}
