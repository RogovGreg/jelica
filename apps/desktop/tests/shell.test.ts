import assert from "node:assert/strict";
import test from "node:test";

import {
  formatShellDocumentTitle,
  SHELL_PRIMARY_NAVIGATION,
  SHELL_WEB_APP_NAVIGATION,
} from "../../../packages/app-platform/src/shell";
import { nextSelectIndex } from "../../../packages/app-platform/src/select";

test("shared shell navigation preserves the required order", () => {
  assert.deepEqual(SHELL_PRIMARY_NAVIGATION, [
    "download",
    "web-app",
    "docs",
    "support",
    "news",
    "about",
  ]);
  assert.deepEqual(SHELL_WEB_APP_NAVIGATION, ["tasks", "projects", "results"]);
});

test("shared document titles use the JELICA prefix and home special case", () => {
  assert.equal(formatShellDocumentTitle(), "JELICA");
  assert.equal(formatShellDocumentTitle(""), "JELICA");
  assert.equal(formatShellDocumentTitle("Tasks"), "JELICA | Tasks");
  assert.equal(formatShellDocumentTitle("  named analysis  "), "JELICA | named analysis");
});

test("shared select keyboard navigation wraps and supports boundaries", () => {
  assert.equal(nextSelectIndex(0, 4, "ArrowDown"), 1);
  assert.equal(nextSelectIndex(3, 4, "ArrowDown"), 0);
  assert.equal(nextSelectIndex(0, 4, "ArrowUp"), 3);
  assert.equal(nextSelectIndex(2, 4, "Home"), 0);
  assert.equal(nextSelectIndex(1, 4, "End"), 3);
  assert.equal(nextSelectIndex(0, 0, "ArrowDown"), -1);
});
