import type { BrowserWindow } from "electron";

import type {
  DesktopNotificationEvent,
  DesktopNotificationSettings,
  LocalNotificationEventId,
} from "../common/contracts";
import { IPC_CHANNELS } from "../common/contracts";
import { DesktopCliClient, type CliWatchHandle } from "./cli/client";

const LOCAL_EVENTS: readonly LocalNotificationEventId[] = ["task.started", "task.scheduler_paused", "task.completed", "task.failed"];
const DEFAULTS: Record<LocalNotificationEventId, boolean> = { "task.started": false, "task.scheduler_paused": true, "task.completed": true, "task.failed": true };

export class DesktopNotificationController {
  #watch: CliWatchHandle | null = null;
  #reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  #reconnectDelay = 1000;
  #stopped = false;
  readonly #seen = new Set<string>();
  #settings: DesktopNotificationSettings | null = null;
  constructor(private readonly cli: DesktopCliClient, private readonly getWindow: () => BrowserWindow | null) {}

  start(): void { this.#stopped = false; this.#reconnectDelay = 1000; this.#connect(); }
  stop(): void { this.#stopped = true; this.#watch?.stop(); this.#watch = null; if (this.#reconnectTimer) clearTimeout(this.#reconnectTimer); this.#reconnectTimer = null; }

  async getSettings(): Promise<DesktopNotificationSettings> {
    const response = await this.cli.runMachine(["config", "show"], { timeoutMs: 10_000 });
    this.#settings = parseSettings(response.data?.config);
    return this.#settings;
  }

  async updateSettings(settings: DesktopNotificationSettings): Promise<DesktopNotificationSettings> {
    const values: Array<readonly [string, boolean]> = [
      ["notifications.device.enabled", settings.deviceEnabled],
      ["desktop.notifications.in_app.enabled", settings.inAppEnabled],
      ["notifications.sound.enabled", settings.soundEnabled],
      ...LOCAL_EVENTS.map((id) => [`notifications.device.events.${id}`, settings.deviceEvents[id]] as const),
      ...LOCAL_EVENTS.map((id) => [`desktop.notifications.in_app.events.${id}`, settings.inAppEvents[id]] as const),
    ];
    for (const [parameter, value] of values) await this.cli.runMachine(["config", "set", parameter, String(value)], { timeoutMs: 10_000 });
    return this.getSettings();
  }

  #connect(): void {
    if (this.#stopped) return;
    this.#watch = this.cli.watchMachine(["events", "watch"], (record) => { void this.#handle(record); }, () => {
      this.#watch = null;
      if (!this.#stopped) {
        const delay = this.#reconnectDelay;
        this.#reconnectDelay = Math.min(this.#reconnectDelay * 2, 30_000);
        this.#reconnectTimer = setTimeout(() => this.#connect(), delay);
      }
    });
  }

  async #handle(record: Readonly<Record<string, unknown>>): Promise<void> {
    const settings = await this.getSettings().catch(() => this.#settings);
    if (!settings) return;
    const eventId = mapEvent(record.name);
    const occurrenceId = typeof record.event_id === "string" ? record.event_id : "";
    const taskId = typeof record.task_id === "string" ? record.task_id : "";
    if (!eventId || !occurrenceId || !taskId || this.#seen.has(occurrenceId) || !settings.inAppEnabled || !settings.inAppEvents[eventId]) return;
    this.#seen.add(occurrenceId);
    const context = record.context && typeof record.context === "object" && !Array.isArray(record.context) ? record.context as Record<string, unknown> : {};
    const payload: DesktopNotificationEvent = {
      occurrenceId,
      eventId,
      taskId,
      taskName: typeof context.name === "string" && context.name.trim() ? context.name : taskId,
      timestamp: typeof record.timestamp === "string" ? record.timestamp : new Date().toISOString(),
      state: eventId === "task.completed" ? "completed" : eventId === "task.failed" ? "failed" : eventId === "task.scheduler_paused" ? "paused" : "started",
      playSound: settings.soundEnabled && !(settings.deviceEnabled && settings.deviceEvents[eventId]),
    };
    const window = this.getWindow();
    if (window && !window.isDestroyed()) window.webContents.send(IPC_CHANNELS.notificationEvent, payload);
  }
}

function parseSettings(raw: unknown): DesktopNotificationSettings {
  const root = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  const notifications = object(root.notifications); const desktop = object(root.desktop);
  const device = object(notifications.device); const sound = object(notifications.sound);
  const inApp = object(object(desktop.notifications).in_app);
  return {
    deviceEnabled: bool(device.enabled, true), inAppEnabled: bool(inApp.enabled, true), soundEnabled: bool(sound.enabled, true),
    deviceEvents: eventValues(object(device.events)), inAppEvents: eventValues(object(inApp.events)),
  };
}
function eventValues(value: Record<string, unknown>): Record<LocalNotificationEventId, boolean> { return Object.fromEntries(LOCAL_EVENTS.map((id) => [id, bool(value[id], DEFAULTS[id])])) as Record<LocalNotificationEventId, boolean>; }
function object(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function bool(value: unknown, fallback: boolean): boolean { return typeof value === "boolean" ? value : fallback; }
function mapEvent(name: unknown): LocalNotificationEventId | null {
  if (name === "CORE_ANALYTICAL_TASK_START_APPLIED") return "task.started";
  if (name === "CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION") return "task.scheduler_paused";
  if (name === "CORE_RUNTIME_JOB_COMPLETED") return "task.completed";
  if (name === "CORE_RUNTIME_JOB_FAILED") return "task.failed";
  return null;
}
