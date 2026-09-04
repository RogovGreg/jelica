import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DesktopAnalyticsService } from "../src/main/analytics";
import { DesktopCliClient, type ChildProcessHandle, type SpawnProcess } from "../src/main/cli/client";
import { SelectionRegistry } from "../src/main/selections";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

test("analytics service uses one list command and never returns native paths", async () => {
  const taskId = "11111111-1111-4111-8111-111111111111";
  const envelope = JSON.stringify({ machine_protocol_version: "1", jelica_version: "0.1.0", trace_id: null, command_id: "c", ok: true, data: { tasks: [{ task_id: taskId, name: "safe", state: "completed", active_or_latest_job: { current_stage: "done", progress: 100 }, package_path: "/private/secret.zip" }] } });
  const calls: string[][] = []; const spawn: SpawnProcess = (_exe, args) => { calls.push([...args]); const child = new FakeChild(); queueMicrotask(() => { child.stdout.end(envelope + "\n"); child.stderr.end(); child.emit("close", 0, null); }); return child; };
  const service = new DesktopAnalyticsService(new DesktopCliClient("jelica", spawn), new SelectionRegistry());
  const listed = await service.listTasks(); assert.equal(listed.ok, true); assert.equal(calls.length, 1); assert.deepEqual(calls[0]?.slice(0, 3), ["tasks", "list", "--limit"]); assert.equal(JSON.stringify(listed).includes("package_path"), false); assert.equal(JSON.stringify(listed).includes("/private"), false);
});

test("forged task ids are rejected before invoking the CLI", async () => {
  let invoked = false; const spawn: SpawnProcess = () => { invoked = true; throw new Error("must not spawn"); };
  const service = new DesktopAnalyticsService(new DesktopCliClient("jelica", spawn), new SelectionRegistry());
  const result = await service.getTask("../../etc/passwd"); assert.equal(result.ok, false); assert.equal(invoked, false);
});

test("create analysis maps the registered task through the task-details contract", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "jelica-desktop-create-"));
  const input = path.join(root, "sample.fasta"); fs.writeFileSync(input, ">sample\nACGT\n", "utf8");
  const taskId = "22222222-2222-4222-8222-222222222222";
  const calls: string[][] = [];
  const cli = { runMachine: async (args: readonly string[]) => {
    calls.push([...args]);
    if (args[0] === "analyze") return { machine_protocol_version: "1", jelica_version: "0.1.0", trace_id: null, command_id: "create", ok: true, data: { task: { task_id: taskId } } };
    return { machine_protocol_version: "1", jelica_version: "0.1.0", trace_id: null, command_id: "show", ok: true, data: { tasks: [{ task_id: taskId, name: "created", state: "waiting", active_or_latest_job: { progress: 0 } }] } };
  } } as unknown as DesktopCliClient;
  try {
    const selections = new SelectionRegistry(); const selected = selections.register(input, "file");
    const result = await new DesktopAnalyticsService(cli, selections).createAnalysis({ inputSelectionIds: [selected.id], configSelectionId: null, ncbiSources: [], name: null, traceId: null, overrides: null });
    assert.equal(result.ok, true); if (result.ok) { assert.equal(result.value.taskId, taskId); assert.equal(result.value.state, "waiting"); }
    assert.equal(calls[0]?.[0], "analyze"); assert.equal(calls[1]?.[0], "tasks");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

class FakeChild extends EventEmitter implements ChildProcessHandle { readonly stdout = new PassThrough(); readonly stderr = new PassThrough(); kill(): boolean { return true; } }
