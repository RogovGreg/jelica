
import type { AnalysisOverrides } from "../../../packages/app-platform/src/analysis";
export type { AnalysisOverrides };

export type ReconciliationDiagnostics = {
  scanned: number;
  updated: number;
  errors: number;
  last_run_at: string | null;
};

export type HealthResponse = {
  service: "web-backend";
  status: "ok" | "degraded";
  database: "ok" | "error";
  detail: string | null;
  reconciliation: ReconciliationDiagnostics | null;
};

export type TaskSubmissionRequest = {
  upload_session_id: string;
  ncbi_sources?: string[];
  name?: string | null;
  trace_id?: string | null;
  analysis_overrides?: AnalysisOverrides | null;
};

export type UploadItemKind = "input_file" | "input_directory" | "config_file";
export type UploadItem = { id: string; kind: UploadItemKind; display_name: string; file_count: number; total_bytes: number; ready: boolean; created_at: string };
export type UploadSession = { id: string; created_at: string; updated_at: string; expires_at: string; file_count: number; total_bytes: number; items: UploadItem[]; submission_status: "open" | "submitting" | "consumed"; task_id: string | null };
export type UploadItemsResponse = { items: UploadItem[] };

export type TaskSubmissionResult = {
  task_id: string;
  final_state: string;
  trace_id: string | null;
  command_id: string;
};

export type TaskStatusSnapshot = {
  task_id: string;
  project_id: string | null;
  trace_id: string | null;
  state: string;
  active_job_state: string | null;
  current_stage: string | null;
  progress: number | null;
  command_id: string | null;
  state_source: "core" | "projection_cache";
  authoritative: boolean;
  projection_updated_at: string | null;
  stale_state: boolean;
  detail: string | null;
  can_control_lifecycle?: boolean;
};

export type TaskListItem = TaskStatusSnapshot & {
  owner_user_id: string | null;
  created_at: string;
  updated_at: string;
};

export type TaskListResponse = {
  items: TaskListItem[];
};

export const ACTIVE_TASK_STATES = ["created", "queued", "running"] as const;
export const TERMINAL_TASK_STATES = ["completed", "failed", "interrupted", "cancelled"] as const;

export function normalizeTaskState(rawState: string): string {
  return rawState.trim().toLowerCase();
}

export function isActiveTaskState(state: string): boolean {
  const normalized = normalizeTaskState(state);
  return ACTIVE_TASK_STATES.some((item) => item === normalized);
}

export function isTerminalTaskState(state: string): boolean {
  const normalized = normalizeTaskState(state);
  return TERMINAL_TASK_STATES.some((item) => item === normalized);
}

export type TaskResultPackageReference = {
  content_id: string;
  package_path: string;
  command_id: string;
};

export type TaskResultLookupResponse = {
  task_id: string;
  trace_id: string | null;
  state: string;
  available: boolean;
  status_command_id: string;
  result_reference: TaskResultPackageReference | null;
  detail: string | null;
};

export type TaskDiscussionMode = "unavailable" | "collaborative" | "read_only";
export type TaskDiscussionMetadata = {
  task_id: string;
  available: boolean;
  project_id: string | null;
  mode: TaskDiscussionMode;
  is_task_owner: boolean;
};
export type TaskDiscussionMention = { user_id: string; username: string };
export type TaskDiscussionReaction = "support" | "oppose";
export type TaskDiscussionReactionSummary = {
  support: number;
  oppose: number;
  current_user_reaction: TaskDiscussionReaction | null;
};
export type TaskDiscussionComment = {
  id: string;
  task_id: string;
  author_user_id: string;
  author_username: string;
  body: string;
  created_at: string;
  edited_at: string | null;
  mentions: TaskDiscussionMention[];
  reaction_summary: TaskDiscussionReactionSummary;
};
export type TaskDiscussionCommentListResponse = { items: TaskDiscussionComment[] };

export type SupportRequestCreatePayload = {
  name: string;
  email: string;
  subject: string;
  message: string;
};

export type SupportRequestResponse = {
  id: string;
  name: string;
  email: string;
  subject: string;
  message: string;
  created_at: string;
  status: "open" | "closed";
};

export type AuthUser = {
  id: string;
  username: string;
  email: string;
  email_verified: boolean;
  language: string;
  theme: "system" | "light" | "dark" | "mono";
  interface_scale: 80 | 100 | 125 | 150;
  created_at: string;
  updated_at: string;
};

export type AuthRegisterRequest = {
  username: string;
  email: string;
  password: string;
};

export type AuthRegisterResponse = {
  user: AuthUser;
  email_verification_required: boolean;
  verification_token?: string | null;
  email_delivery_failed?: boolean;
};

export type AuthActionResponse = { message: string };

export type AuthVerifyEmailRequest = {
  token: string;
};

export type AuthLoginRequest = {
  identifier: string;
  password: string;
};

export type AuthUserResponse = {
  user: AuthUser;
};
export type SupportedLocale = "en" | "ru" | "sr-Latn" | "sr-Cyrl";
export type AuthSessionSummary = { id: string; created_at: string; last_used_at: string; expires_at: string; current: boolean };
export type AuthSessionListResponse = { items: AuthSessionSummary[] };

export type ProjectStatus = "active" | "frozen";
export type ProjectMemberRole = "viewer" | "commenter" | "member" | "supervisor";
export type Project = {
  id: string;
  name: string;
  description: string | null;
  status: ProjectStatus;
  created_by_user_id: string;
  owner_user_id: string;
  current_user_role?: ProjectMemberRole | null;
  created_at: string;
  updated_at: string;
};
export type ProjectListResponse = { items: Project[] };
export type ProjectMember = {
  project_id: string;
  user_id: string;
  username: string;
  email: string;
  role: ProjectMemberRole;
  joined_at: string;
};
export type ProjectMemberListResponse = { items: ProjectMember[] };
export type ProjectTask = {
  task_id: string;
  name: string | null;
  state: string;
  owner_user_id: string | null;
  project_id: string | null;
  created_at: string;
  updated_at: string;
};
export type ProjectTaskListResponse = { items: ProjectTask[] };
export type ProjectHistoryEvent = {
  id: string;
  project_id: string;
  actor_user_id: string | null;
  subject_user_id: string | null;
  event_type: string;
  data: Record<string, unknown> | null;
  occurred_at: string;
};
export type ProjectHistoryListResponse = { items: ProjectHistoryEvent[] };
export type ProjectInvitationStatus = "pending" | "accepted" | "declined" | "revoked" | "expired";
export type ProjectInvitation = {
  invitation_id: string;
  project_id: string;
  project_name: string;
  invited_user_id: string;
  invited_username: string;
  invited_by_user_id: string;
  inviter_username: string;
  role: ProjectMemberRole;
  status: ProjectInvitationStatus;
  invited_at: string;
  expires_at: string;
  resolved_at: string | null;
};
export type ProjectInvitationListResponse = { items: ProjectInvitation[] };
export type InvitationCandidate = { user_id: string; username: string };
export type InvitationCandidateListResponse = { items: InvitationCandidate[] };

export type ProjectCommentMention = { user_id: string; username: string };
export type ProjectCommentReaction = "support" | "oppose";
export type ProjectCommentReactionSummary = {
  support: number;
  oppose: number;
  current_user_reaction: ProjectCommentReaction | null;
};
export type ProjectComment = {
  id: string;
  project_id: string;
  author_user_id: string;
  author_username: string;
  body: string;
  created_at: string;
  edited_at: string | null;
  mentions: ProjectCommentMention[];
};
export type ProjectCommentListItem = ProjectComment & {
  reaction_summary: ProjectCommentReactionSummary;
};
export type ProjectCommentListResponse = { items: ProjectCommentListItem[] };

export type WebNotificationChannel = "in_app" | "device" | "email" | "telegram";

export type NotificationChannelPreference = {
  channel: WebNotificationChannel;
  enabled: boolean;
  available: boolean;
};

export type NotificationEventPreference = {
  event_id: string;
  category: string;
  scope: "web" | "both";
  default_enabled: boolean;
  channels: WebNotificationChannel[];
  enabled: Partial<Record<WebNotificationChannel, boolean>>;
  effective: Partial<Record<WebNotificationChannel, boolean>>;
};

export type NotificationPreferencesResponse = {
  enabled: boolean;
  sound_enabled: boolean;
  channels: NotificationChannelPreference[];
  events: NotificationEventPreference[];
};

export type NotificationPreferencesPatch = {
  enabled?: boolean;
  sound_enabled?: boolean;
  channels?: Partial<Record<WebNotificationChannel, boolean>>;
  events?: Array<{
    event_id: string;
    channel: WebNotificationChannel;
    enabled: boolean;
  }>;
};

export type TelegramIntegrationState = {
  integration_available: boolean;
  linked: boolean;
  username: string | null;
  display_name: string | null;
  linked_at: string | null;
};

export type TelegramLinkResponse = {
  url: string;
  expires_at: string;
};

export type NotificationResource = {
  kind:
    | "task"
    | "project"
    | "project_tasks"
    | "project_discussion"
    | "task_discussion"
    | "invitation";
  project_id?: string | null;
  task_id?: string | null;
  display_name?: string | null;
};

export type WebNotification = {
  id: string;
  event_id: string;
  category: string;
  actor_username: string | null;
  resource: NotificationResource | null;
  created_at: string;
  read_at: string | null;
  target_path: string | null;
};

export type NotificationListResponse = { items: WebNotification[] };

export type NotificationRealtimeMessage =
  | { type: "notification.created"; notification: WebNotification }
  | {
      type: "notification.read_changed";
      notification_id: string;
      read_at: string | null;
    }
  | { type: "notifications.all_read"; read_at: string };

export type WebPushConfigResponse = {
  available: boolean;
  vapid_public_key: string | null;
  active_subscription_count: number;
  current_session_subscription_count: number;
};

export type WebPushSubscriptionPayload = {
  endpoint: string;
  expiration_time: number | null;
  keys: { p256dh: string; auth: string };
};

export type ApiErrorEnvelope = {
  error?: string;
  message?: string;
  details?: unknown;
  command_id?: string;
};
