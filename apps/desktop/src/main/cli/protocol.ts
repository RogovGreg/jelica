import { DesktopCliError } from "./errors";

export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export type JsonObject = { readonly [key: string]: JsonValue };

export type MachineErrorPayload = Readonly<{
  code: number;
  name: string;
  message: string;
  details: JsonObject;
}>;

export type MachineResponseEnvelope = Readonly<{
  machineProtocolVersion: "1";
  jelicaVersion: string;
  traceId: string | null;
  commandId: string;
  ok: boolean;
  data: JsonObject | null;
  error: MachineErrorPayload | null;
}>;

export function parseMachineResponse(lines: readonly string[]): MachineResponseEnvelope {
  const records = lines.filter((line) => line.trim() !== "");
  if (records.length !== 1) throw protocolError("The CLI returned an unexpected record count.");
  const line = records[0];
  if (line === undefined) throw protocolError("The CLI returned no machine response.");

  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    throw protocolError("The CLI returned malformed machine JSON.");
  }
  if (!isObject(value)) throw protocolError("The CLI machine response is not an object.");
  if (value.machine_protocol_version !== "1") {
    throw protocolError("The CLI machine protocol version is unsupported.");
  }
  if (typeof value.jelica_version !== "string" || value.jelica_version.length === 0) {
    throw protocolError("The CLI machine response has no JELICA version.");
  }
  if (typeof value.command_id !== "string" || value.command_id.length === 0) {
    throw protocolError("The CLI machine response has no command identifier.");
  }
  if (value.trace_id !== null && typeof value.trace_id !== "string") {
    throw protocolError("The CLI machine response has an invalid trace identifier.");
  }
  if (typeof value.ok !== "boolean") throw protocolError("The CLI machine response has no status.");

  const data = isObject(value.data) && isJsonObject(value.data) ? value.data : null;
  const error = parseMachineError(value.error);
  if (value.ok && (data === null || value.error !== undefined)) {
    throw protocolError("The CLI success envelope is invalid.");
  }
  if (!value.ok && (error === null || value.data !== undefined)) {
    throw protocolError("The CLI error envelope is invalid.");
  }

  return {
    machineProtocolVersion: "1",
    jelicaVersion: value.jelica_version,
    traceId: value.trace_id,
    commandId: value.command_id,
    ok: value.ok,
    data,
    error,
  };
}

function parseMachineError(value: unknown): MachineErrorPayload | null {
  if (value === undefined) return null;
  if (!isObject(value)) throw protocolError("The CLI error payload is invalid.");
  if (
    typeof value.code !== "number" ||
    !Number.isInteger(value.code) ||
    typeof value.name !== "string" ||
    value.name.length === 0 ||
    typeof value.message !== "string" ||
    value.message.length === 0 ||
    !isObject(value.details) ||
    !isJsonObject(value.details)
  ) {
    throw protocolError("The CLI error payload is invalid.");
  }
  return { code: value.code, name: value.name, message: value.message, details: value.details };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isJsonObject(value: Record<string, unknown>): value is JsonObject {
  return Object.values(value).every(isJsonValue);
}

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  return isObject(value) && isJsonObject(value);
}

function protocolError(message: string): DesktopCliError {
  return new DesktopCliError("cli_protocol_error", message);
}
