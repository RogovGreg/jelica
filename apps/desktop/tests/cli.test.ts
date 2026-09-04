import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";

import {
  DEFAULT_OUTPUT_LIMITS,
  DesktopCliClient,
  type ChildProcessHandle,
  type SpawnOptions,
  type SpawnProcess,
} from "../src/main/cli/client";
import { DesktopCliError } from "../src/main/cli/errors";
import { resolveCliExecutable } from "../src/main/cli/resolver";

const successEnvelope = JSON.stringify({
  machine_protocol_version: "1",
  jelica_version: "0.1.0",
  trace_id: null,
  command_id: "command-1",
  ok: true,
  data: { count: 0, tasks: [] },
});

test("CLI runner preserves executable and argv elements and disables the shell", async () => {
  const invocation = fakeSpawn(`${successEnvelope}\n`, 0);
  const client = new DesktopCliClient("/path with spaces/jelica", invocation.spawn);
  const envelope = await client.runMachine(
    ["tasks", "show", "name with spaces;$(ignored)"],
    { timeoutMs: 1000 },
  );

  assert.equal(envelope.ok, true);
  assert.equal(invocation.calls.length, 1);
  assert.equal(invocation.calls[0]?.executable, "/path with spaces/jelica");
  assert.deepEqual(invocation.calls[0]?.args, [
    "tasks",
    "show",
    "name with spaces;$(ignored)",
    "--machine",
  ]);
  assert.equal(invocation.calls[0]?.options.shell, false);
});

test("CLI runner maps nonzero, command, malformed, and bounded-output failures", async (context) => {
  await context.test("nonzero success envelope", async () => {
    const client = new DesktopCliClient("jelica", fakeSpawn(successEnvelope, 3).spawn);
    await assertCliError(client.runMachine(["tasks", "list"], { timeoutMs: 1000 }), "cli_process_error");
  });
  await context.test("machine command error", async () => {
    const failure = JSON.stringify({
      machine_protocol_version: "1",
      jelica_version: "0.1.0",
      trace_id: null,
      command_id: "command-2",
      ok: false,
      error: { code: 2210, name: "CORE_NOT_FOUND", message: "Internal detail", details: {} },
    });
    const client = new DesktopCliClient("jelica", fakeSpawn(failure, 1).spawn);
    await assertCliError(client.runMachine(["tasks", "show", "missing"], { timeoutMs: 1000 }), "cli_command_error");
  });
  await context.test("malformed payload", async () => {
    const client = new DesktopCliClient("jelica", fakeSpawn("not-json", 0).spawn);
    await assertCliError(client.runMachine(["tasks", "list"], { timeoutMs: 1000 }), "cli_protocol_error");
  });
  await context.test("stdout bound", async () => {
    const oversized = "x".repeat(DEFAULT_OUTPUT_LIMITS.stdoutBytes + 1);
    const client = new DesktopCliClient("jelica", fakeSpawn(oversized, 0).spawn);
    await assertCliError(client.runMachine(["tasks", "list"], { timeoutMs: 1000 }), "cli_output_limit");
  });
});

test("CLI executable resolution is cross-platform and explicit setting contains no argv", () => {
  assert.equal(resolveCliExecutable({}, "linux"), "jelica");
  assert.equal(resolveCliExecutable({}, "darwin"), "jelica");
  assert.equal(resolveCliExecutable({}, "win32"), "jelica.exe");
  assert.equal(
    resolveCliExecutable({ JELICA_DESKTOP_CLI_EXECUTABLE: "C:\\Program Files\\JELICA\\jelica.exe" }, "win32"),
    "C:\\Program Files\\JELICA\\jelica.exe",
  );
  assert.equal(
    resolveCliExecutable({ JELICA_DESKTOP_CLI_EXECUTABLE: "/unsafe/host/jelica" }, "linux", { packaged: true, appPath: "/Applications/JELICA" }),
    "/Applications/JELICA/resources/runtime/jelica",
  );
});

type RecordedCall = Readonly<{
  executable: string;
  args: readonly string[];
  options: SpawnOptions;
}>;

function fakeSpawn(stdoutText: string, exitCode: number): {
  spawn: SpawnProcess;
  calls: RecordedCall[];
} {
  const calls: RecordedCall[] = [];
  const spawn: SpawnProcess = (executable, args, options) => {
    calls.push({ executable, args: [...args], options });
    const child = new FakeChild();
    queueMicrotask(() => {
      child.stdout.end(stdoutText);
      child.stderr.end("diagnostic detail that must not reach the renderer");
      child.emit("close", exitCode, null);
    });
    return child;
  };
  return { spawn, calls };
}

class FakeChild extends EventEmitter implements ChildProcessHandle {
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  killed = false;

  kill(): boolean {
    this.killed = true;
    return true;
  }
}

async function assertCliError(
  promise: Promise<unknown>,
  code: DesktopCliError["code"],
): Promise<void> {
  await assert.rejects(promise, (error: unknown) => error instanceof DesktopCliError && error.code === code);
}
