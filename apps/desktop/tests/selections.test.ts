import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { SelectionRegistry, SelectionUnavailableError } from "../src/main/selections";

test("selection registry returns opaque ids, safe labels, and revalidates paths", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "jelica-selection-"));
  const file = path.join(root, "sample.fasta"); fs.writeFileSync(file, ">a\nAC");
  const registry = new SelectionRegistry(); const selected = registry.register(file, "file");
  assert.equal(selected.displayName, "sample.fasta"); assert.equal("nativePath" in selected, false); assert.match(selected.id, /^[0-9a-f-]{36}$/);
  assert.equal(registry.resolve(selected.id, "file"), path.resolve(file));
  assert.throws(() => registry.resolve(selected.id, "directory"), SelectionUnavailableError);
  fs.unlinkSync(file); assert.throws(() => registry.resolve(selected.id, "file"), SelectionUnavailableError);
  fs.rmSync(root, { recursive: true, force: true });
});
