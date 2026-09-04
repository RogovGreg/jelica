import fs from "node:fs";
import path from "node:path";
import { URL } from "node:url";

import type {
  DesktopCreateAnalysisRequest,
  DesktopError,
  DesktopResult,
  DesktopResultSummary,
  DesktopTaskRuntime,
  DesktopTaskSummary,
} from "../common/contracts";
import { serializeAnalysisOverrides, validateAnalysisOverrides } from "../../../../packages/app-platform/src/analysis";
import { DesktopCliClient } from "./cli/client";
import { DesktopCliError } from "./cli/errors";
import type { JsonObject, JsonValue } from "./cli/protocol";
import { SelectionRegistry, SelectionUnavailableError } from "./selections";

const LIST_TIMEOUT_MS = 15_000;
const READ_TIMEOUT_MS = 20_000;
const CREATE_TIMEOUT_MS = 120_000;
const LIFECYCLE_TIMEOUT_MS = 300_000;
const NCBI_ACCESSION = /^[A-Z][A-Z0-9_]*\d(?:\.\d+)?$/i;

export class DesktopAnalyticsService {
  constructor(
    readonly cli: DesktopCliClient,
    readonly selections: SelectionRegistry,
  ) {}

  async listTasks(): Promise<DesktopResult<readonly DesktopTaskSummary[]>> {
    try {
      const envelope = await this.cli.runMachine(["tasks", "list", "--limit", "200", "--offset", "0"], { timeoutMs: LIST_TIMEOUT_MS });
      const tasks = objectArray(envelope.data?.tasks);
      return success(tasks.map((task) => mapTask(task)));
    } catch (error) {
      return failureFrom(error);
    }
  }

  async createAnalysis(request: unknown): Promise<DesktopResult<DesktopTaskSummary>> {
    try {
      const payload = validateCreateRequest(request);
      const paths = payload.inputSelectionIds.map((id) => {
        const kind = this.selections.kindOf(id);
        if (kind === "config") throw new RequestError("A configuration selection cannot be used as an input.");
        return this.selections.resolve(id, kind);
      });
      if (paths.length === 0) throw new RequestError("At least one local input is required.");
      const configPath = payload.configSelectionId === null ? null : this.selections.resolve(payload.configSelectionId, "config");
      const args: string[] = ["analyze", "--no-watch"];
      if (payload.name !== null) args.push("--name", payload.name);
      if (payload.traceId !== null) args.push("--trace-id", payload.traceId);
      if (configPath !== null) args.push(configPath);
      args.push(...serializeAnalysisOverrides(payload.overrides), ...paths, ...payload.ncbiSources);
      const envelope = await this.cli.runMachine(args, { timeoutMs: CREATE_TIMEOUT_MS });
      const data = requiredObject(envelope.data, "analyze data");
      const task = requiredObject(data.task, "analyze task");
      const taskId = requiredText(task.task_id, "task id");
      for (const id of payload.inputSelectionIds) this.selections.remove(id);
      if (payload.configSelectionId !== null) this.selections.remove(payload.configSelectionId);
      const created = await this.getTask(taskId);
      if (!created.ok) return created;
      return success(created.value);
    } catch (error) {
      return failureFrom(error);
    }
  }

  async getTask(taskId: unknown): Promise<DesktopResult<DesktopTaskRuntime>> {
    try {
      const id = validateTaskId(taskId);
      const envelope = await this.cli.runMachine(["tasks", "show", id], { timeoutMs: READ_TIMEOUT_MS });
      const tasks = objectArray(envelope.data?.tasks);
      if (tasks.length === 0) throw new RequestError("The local task was not found.", "task_not_found");
      return success(mapRuntime(tasks[0]!));
    } catch (error) {
      return failureFrom(error);
    }
  }

  async lifecycle(action: "start" | "pause" | "resume", taskId: unknown): Promise<DesktopResult<DesktopTaskRuntime>> {
    try {
      const id = validateTaskId(taskId);
      const envelope = await this.cli.runMachine(["tasks", action, id], { timeoutMs: action === "pause" ? READ_TIMEOUT_MS : LIFECYCLE_TIMEOUT_MS });
      const tasks = objectArray(envelope.data?.tasks);
      if (tasks.length > 0) return success(mapRuntime(tasks[0]!));
      return this.getTask(id);
    } catch (error) {
      return failureFrom(error);
    }
  }

  async getResult(taskId: unknown): Promise<DesktopResult<DesktopResultSummary>> {
    try {
      const id = validateTaskId(taskId);
      const status = await this.getTask(id);
      if (!status.ok) return status;
      try {
        const envelope = await this.cli.runMachine(["results", "path", id], { timeoutMs: READ_TIMEOUT_MS });
        const data = requiredObject(envelope.data, "result data");
        const resultPath = requiredText(data.path, "result path");
        if (!fs.statSync(resultPath).isFile()) throw new Error("result is not a file");
        return success({ taskId: id, state: status.value.state, available: true, contentId: typeof data.content_id === "string" ? data.content_id : null, fileName: path.basename(resultPath), detail: null });
      } catch (error) {
        if (error instanceof DesktopCliError && error.code === "cli_command_error") {
          return success({ taskId: id, state: status.value.state, available: false, contentId: null, fileName: null, detail: "A result package is not available for this task." });
        }
        throw error;
      }
    } catch (error) {
      return failureFrom(error);
    }
  }

  async showResultInFolder(taskId: unknown, showItemInFolder: (filePath: string) => void): Promise<DesktopResult<null>> {
    try {
      const id = validateTaskId(taskId);
      const envelope = await this.cli.runMachine(["results", "path", id], { timeoutMs: READ_TIMEOUT_MS });
      const data = requiredObject(envelope.data, "result data");
      const resultPath = requiredText(data.path, "result path");
      if (!fs.statSync(resultPath).isFile()) throw new RequestError("The result package is unavailable.", "result_unavailable");
      showItemInFolder(resultPath);
      return success(null);
    } catch (error) {
      return failureFrom(error);
    }
  }

}

function validateCreateRequest(value: unknown): DesktopCreateAnalysisRequest {
  if (!isObject(value)) throw new RequestError("The analysis request is invalid.");
  if (!Array.isArray(value.inputSelectionIds) || value.inputSelectionIds.length > 100 || value.inputSelectionIds.some((id) => typeof id !== "string" || id.length === 0)) throw new RequestError("The local input selection is invalid.");
  const ids = (value.inputSelectionIds as unknown[]).map(validateSelectionId);
  if (new Set(ids).size !== ids.length) throw new RequestError("The same local input cannot be selected twice.");
  const configSelectionId = value.configSelectionId === null || value.configSelectionId === undefined ? null : validateSelectionId(value.configSelectionId);
  const ncbiSources = value.ncbiSources === undefined ? [] : validateNcbiSources(value.ncbiSources);
  const name = value.name === null || value.name === undefined ? null : validateName(value.name);
  const traceId = value.traceId === null || value.traceId === undefined || value.traceId === "" ? null : validateTraceId(value.traceId);
  let overrides = null;
  if (value.overrides !== null && value.overrides !== undefined) overrides = validateAnalysisOverrides(value.overrides);
  return { inputSelectionIds: ids, configSelectionId, ncbiSources, name, traceId, overrides };
}

function validateSelectionId(value: unknown): string {
  if (typeof value !== "string" || !/^[0-9a-f-]{36}$/i.test(value)) throw new RequestError("The selection reference is invalid.");
  return value;
}
function validateTaskId(value: unknown): string {
  if (typeof value !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) throw new RequestError("The task reference is invalid.");
  return value;
}
function validateName(value: unknown): string {
  if (typeof value !== "string" || value.trim() === "" || value.length > 200 || /[\u0000-\u001f\u007f]/.test(value)) throw new RequestError("The task name is invalid.");
  return value.trim();
}
function validateTraceId(value: unknown): string {
  if (typeof value !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) throw new RequestError("The trace identifier is invalid.");
  return value;
}
function validateNcbiSources(value: unknown): readonly string[] {
  if (!Array.isArray(value) || value.length > 100) throw new RequestError("NCBI sources are invalid.");
  return value.map((item) => {
    if (typeof item !== "string" || item.trim() === "") throw new RequestError("NCBI sources are invalid.");
    const source = item.trim();
    if (NCBI_ACCESSION.test(source)) return source.toUpperCase();
    let url: URL;
    try { url = new URL(source); } catch { throw new RequestError("Only supported NCBI sources are allowed."); }
    if (url.protocol !== "https:" || !["ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"].includes(url.hostname.toLowerCase())) throw new RequestError("Only supported NCBI sources are allowed.");
    if (!url.pathname.toLowerCase().startsWith("/nuccore/") && !url.pathname.toLowerCase().startsWith("/nucleotide/") && !["/entrez/viewer.fcgi", "/sviewer/viewer.fcgi"].includes(url.pathname.toLowerCase())) throw new RequestError("Only supported NCBI sources are allowed.");
    return source;
  });
}

function mapTask(value: JsonObject): DesktopTaskSummary {
  return {
    taskId: requiredText(value.task_id, "task id"),
    name: typeof value.name === "string" ? value.name : null,
    state: requiredText(value.state, "task state"),
    currentStage: jobField(value, "current_stage"),
    progress: jobProgress(value),
    traceId: typeof value.trace_id === "string" ? value.trace_id : null,
    createdAt: typeof value.created_at === "string" ? value.created_at : null,
    updatedAt: typeof value.updated_at === "string" ? value.updated_at : null,
  };
}
function mapRuntime(value: JsonObject): DesktopTaskRuntime {
  const summary = mapTask(value);
  const job = isObject(value.active_or_latest_job) ? value.active_or_latest_job : null;
  return { ...summary, activeJobState: job && typeof job.state === "string" ? job.state : null, failure: job && typeof job.error_event_code === "number" ? { code: job.error_event_code, message: "The task reported a failure." } : null };
}
function jobField(value: JsonObject, field: string): string | null {
  const job = isObject(value.active_or_latest_job) ? value.active_or_latest_job : null;
  return job && typeof job[field] === "string" ? job[field] : null;
}
function jobProgress(value: JsonObject): number | null {
  const job = isObject(value.active_or_latest_job) ? value.active_or_latest_job : null;
  return job && typeof job.progress === "number" && Number.isInteger(job.progress) && job.progress >= 0 && job.progress <= 100 ? job.progress : null;
}
function objectArray(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.filter(isJsonObject) : [];
}
function requiredObject(value: JsonValue | null | undefined, label: string): JsonObject {
  if (!isJsonObject(value)) throw new RequestError(`The CLI returned an invalid ${label}.`, "protocol_error");
  return value;
}
function requiredText(value: JsonValue | undefined, label: string): string {
  if (typeof value !== "string" || value.trim() === "") throw new RequestError(`The CLI returned an invalid ${label}.`, "protocol_error");
  return value;
}
function isObject(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function isJsonObject(value: unknown): value is JsonObject { return isObject(value) && Object.values(value).every(isJsonValue); }
function isJsonValue(value: unknown): value is import("./cli/protocol").JsonValue { return value === null || typeof value === "string" || typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value)) || (Array.isArray(value) && value.every(isJsonValue)) || isJsonObject(value); }

class RequestError extends Error {
  constructor(message: string, readonly code: DesktopError["code"] = "invalid_input") { super(message); this.name = "RequestError"; }
}
function success<T>(value: T): DesktopResult<T> { return { ok: true, value }; }
function failureFrom(error: unknown): DesktopResult<never> {
  if (error instanceof RequestError) return { ok: false, error: { code: error.code, message: error.message } };
  if (error instanceof SelectionUnavailableError) return { ok: false, error: { code: "input_unavailable", message: error.message } };
  if (error instanceof DesktopCliError) {
    const code: DesktopError["code"] = error.code === "cli_not_found" ? "cli_unavailable" : error.code === "cli_protocol_error" ? "protocol_error" : error.code === "cli_command_error" ? "command_failed" : "cli_unavailable";
    return { ok: false, error: { code, message: error.message } };
  }
  return { ok: false, error: { code: "internal_error", message: "The local JELICA operation could not be completed." } };
}
