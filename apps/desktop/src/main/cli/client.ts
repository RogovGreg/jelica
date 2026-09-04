import { spawn } from "node:child_process";
import type { Readable } from "node:stream";

import { DesktopCliError } from "./errors";
import { parseMachineResponse, type MachineResponseEnvelope } from "./protocol";
import { resolveCliExecutable } from "./resolver";

export const DEFAULT_OUTPUT_LIMITS = Object.freeze({
  stdoutBytes: 1024 * 1024,
  stderrBytes: 64 * 1024,
  lineBytes: 256 * 1024,
});

export type SpawnOptions = Readonly<{
  shell: false;
  stdio: readonly ["ignore", "pipe", "pipe"];
  env: NodeJS.ProcessEnv;
}>;

export interface ChildProcessHandle {
  readonly stdout: Readable;
  readonly stderr: Readable;
  once(event: "error", listener: (error: Error) => void): this;
  once(event: "close", listener: (code: number | null, signal: NodeJS.Signals | null) => void): this;
  kill(signal?: NodeJS.Signals): boolean;
}

export type SpawnProcess = (
  executable: string,
  args: readonly string[],
  options: SpawnOptions,
) => ChildProcessHandle;

export type CliRunOptions = Readonly<{
  timeoutMs: number;
  signal?: AbortSignal;
}>;

export type CliWatchHandle = Readonly<{ stop(): void }>;

export class DesktopCliClient {
  readonly executable: string;
  readonly #spawnProcess: SpawnProcess;
  readonly #children = new Set<ChildProcessHandle>();

  constructor(
    executable = resolveCliExecutable(),
    spawnProcess: SpawnProcess = defaultSpawn,
  ) {
    this.executable = executable;
    this.#spawnProcess = spawnProcess;
  }

  runMachine(args: readonly string[], options: CliRunOptions): Promise<MachineResponseEnvelope> {
    if (args.length === 0 || options.timeoutMs <= 0) {
      return Promise.reject(new DesktopCliError("cli_process_error", "The CLI invocation is invalid."));
    }
    const machineArgs = args.includes("--machine") ? [...args] : [...args, "--machine"];

    return new Promise((resolve, reject) => {
      let child: ChildProcessHandle;
      try {
        child = this.#spawnProcess(this.executable, machineArgs, {
          shell: false,
          stdio: ["ignore", "pipe", "pipe"],
          env: process.env,
        });
      } catch (error) {
        reject(startError(error));
        return;
      }

      this.#children.add(child);
      const stdout = new BoundedLineCollector(
        DEFAULT_OUTPUT_LIMITS.stdoutBytes,
        DEFAULT_OUTPUT_LIMITS.lineBytes,
      );
      const stderr = new BoundedLineCollector(
        DEFAULT_OUTPUT_LIMITS.stderrBytes,
        DEFAULT_OUTPUT_LIMITS.lineBytes,
      );
      let terminalError: DesktopCliError | null = null;
      let settled = false;

      const stopWith = (error: DesktopCliError) => {
        terminalError ??= error;
        child.kill("SIGTERM");
      };
      child.stdout.on("data", (chunk: Buffer | string) => {
        try { stdout.accept(chunk); } catch (error) { stopWith(asOutputError(error)); }
      });
      child.stderr.on("data", (chunk: Buffer | string) => {
        try { stderr.accept(chunk); } catch (error) { stopWith(asOutputError(error)); }
      });

      const timeout = setTimeout(
        () => stopWith(new DesktopCliError("cli_timeout", "The CLI operation timed out.")),
        options.timeoutMs,
      );
      const abort = () => stopWith(new DesktopCliError("cli_cancelled", "The CLI operation was cancelled."));
      options.signal?.addEventListener("abort", abort, { once: true });
      if (options.signal?.aborted) abort();

      const finish = (result: () => void) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        options.signal?.removeEventListener("abort", abort);
        this.#children.delete(child);
        result();
      };

      child.once("error", (error) => finish(() => reject(startError(error))));
      child.once("close", (code) => finish(() => {
        if (terminalError) {
          reject(terminalError);
          return;
        }
        try {
          const envelope = parseMachineResponse(stdout.finish());
          if (!envelope.ok) {
            reject(new DesktopCliError("cli_command_error", "The JELICA CLI command was rejected."));
          } else if (code !== 0) {
            reject(new DesktopCliError("cli_process_error", "The JELICA CLI process failed."));
          } else {
            resolve(envelope);
          }
        } catch (error) {
          reject(error instanceof DesktopCliError ? error : asOutputError(error));
        }
      }));
    });
  }

  watchMachine(
    args: readonly string[],
    onRecord: (record: Readonly<Record<string, unknown>>) => void,
    onClose?: (error?: Error) => void,
  ): CliWatchHandle {
    const machineArgs = args.includes("--machine") ? [...args] : [...args, "--machine"];
    let child: ChildProcessHandle;
    try {
      child = this.#spawnProcess(this.executable, machineArgs, {
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
        env: process.env,
      });
    } catch (error) {
      onClose?.(startError(error));
      return { stop: () => undefined };
    }
    this.#children.add(child);
    const stdout = new IncrementalJsonlParser(DEFAULT_OUTPUT_LIMITS.lineBytes);
    const stderr = new BoundedLineCollector(DEFAULT_OUTPUT_LIMITS.stderrBytes, DEFAULT_OUTPUT_LIMITS.lineBytes);
    let stopped = false;
    const stop = () => { if (stopped) return; stopped = true; child.kill("SIGTERM"); this.#children.delete(child); };
    child.stdout.on("data", (chunk: Buffer | string) => {
      try {
        stdout.accept(chunk);
        for (const line of stdout.accept(chunk)) {
          try {
            const parsed: unknown = JSON.parse(line);
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) onRecord(parsed as Record<string, unknown>);
          } catch { /* malformed machine records are ignored safely */ }
        }
      } catch { stop(); onClose?.(new DesktopCliError("cli_output_limit", "The JELICA CLI output exceeded its safety limit.")); }
    });
    child.stderr.on("data", (chunk: Buffer | string) => { try { stderr.accept(chunk); } catch { stop(); } });
    child.once("error", (error) => { this.#children.delete(child); if (!stopped) onClose?.(startError(error)); });
    child.once("close", (code) => { this.#children.delete(child); if (!stopped) onClose?.(code === 0 ? undefined : new DesktopCliError("cli_process_error", "The JELICA CLI watcher stopped.")); });
    return { stop };
  }

  dispose(): void {
    for (const child of this.#children) child.kill("SIGTERM");
    this.#children.clear();
  }
}

class BoundedLineCollector {
  readonly #maxBytes: number;
  readonly #maxLineBytes: number;
  #totalBytes = 0;
  #pending = Buffer.alloc(0);
  readonly #lines: string[] = [];

  constructor(maxBytes: number, maxLineBytes: number) {
    this.#maxBytes = maxBytes;
    this.#maxLineBytes = maxLineBytes;
  }

  accept(chunk: Buffer | string): void {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, "utf8");
    this.#totalBytes += bytes.byteLength;
    if (this.#totalBytes > this.#maxBytes) throw new Error("output limit");
    this.#pending = Buffer.concat([this.#pending, bytes]);
    let newline = this.#pending.indexOf(10);
    while (newline >= 0) {
      const line = this.#pending.subarray(0, newline);
      if (line.byteLength > this.#maxLineBytes) throw new Error("line limit");
      this.#lines.push(line.toString("utf8").replace(/\r$/, ""));
      this.#pending = this.#pending.subarray(newline + 1);
      newline = this.#pending.indexOf(10);
    }
    if (this.#pending.byteLength > this.#maxLineBytes) throw new Error("line limit");
  }

  finish(): readonly string[] {
    if (this.#pending.byteLength > 0) this.#lines.push(this.#pending.toString("utf8"));
    this.#pending = Buffer.alloc(0);
    return this.#lines;
  }

  takeLines(): readonly string[] {
    const lines = this.#lines.splice(0, this.#lines.length);
    return lines;
  }
}

class IncrementalJsonlParser {
  readonly #maxLineBytes: number;
  #pending = Buffer.alloc(0);
  constructor(maxLineBytes: number) { this.#maxLineBytes = maxLineBytes; }
  accept(chunk: Buffer | string): readonly string[] {
    this.#pending = Buffer.concat([this.#pending, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, "utf8")]);
    const lines: string[] = [];
    let newline = this.#pending.indexOf(10);
    while (newline >= 0) {
      const line = this.#pending.subarray(0, newline);
      if (line.byteLength > this.#maxLineBytes) throw new Error("line limit");
      lines.push(line.toString("utf8").replace(/\r$/, ""));
      this.#pending = this.#pending.subarray(newline + 1);
      newline = this.#pending.indexOf(10);
    }
    if (this.#pending.byteLength > this.#maxLineBytes) throw new Error("line limit");
    return lines;
  }
}

const defaultSpawn: SpawnProcess = (executable, args, options) =>
  spawn(executable, [...args], {
    shell: options.shell,
    stdio: [...options.stdio],
    env: options.env,
  }) as ChildProcessHandle;

function startError(error: unknown): DesktopCliError {
  const code = isNodeError(error) && error.code === "ENOENT" ? "cli_not_found" : "cli_process_error";
  return new DesktopCliError(code, code === "cli_not_found" ? "The JELICA CLI was not found." : "The JELICA CLI could not be started.");
}

function asOutputError(_error: unknown): DesktopCliError {
  return new DesktopCliError("cli_output_limit", "The JELICA CLI output exceeded its safety limit.");
}

function isNodeError(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error;
}
