import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const PRELOAD_PATH = path.resolve(process.cwd(), "dist/apps/desktop/src/preload/index.cjs");

test("built preload is a self-contained sandbox-compatible CommonJS artifact", () => {
  const stat = fs.statSync(PRELOAD_PATH);
  assert.equal(stat.isFile(), true);
  const source = fs.readFileSync(PRELOAD_PATH, "utf8");
  assert.doesNotMatch(source, /require\(["'](?:\.\.?\/|\/)/);
  assert.doesNotMatch(source, /import\(["'](?:\.\.?\/|\/)/);
  assert.match(source, /require\(["']electron["']\)/);
});
