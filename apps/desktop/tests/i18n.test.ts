import assert from "node:assert/strict";
import test from "node:test";

import {
  createTranslator,
  resolveLocale,
} from "../../../packages/app-platform/src/i18n";

test("shared i18n resolves supported locale families and sparse fallback", () => {
  assert.equal(resolveLocale("sr-Latn-RS"), "sr-Latn");
  assert.equal(resolveLocale("sr-Cyrl-RS"), "sr-Cyrl");
  assert.equal(resolveLocale("ru-RU"), "ru");
  assert.equal(resolveLocale("unknown"), "en");

  const translateRussian = createTranslator("ru");
  assert.equal(translateRussian("task.polling.active", { seconds: 5 }), "Automatic polling is active (every 5 seconds).");
});

test("shared i18n interpolates values without leaking known placeholders", () => {
  const translate = createTranslator("en");
  assert.equal(translate("task.label.task-prefix", { task: "abc-123" }), "Task abc-123");
  assert.equal(translate("task.polling.active", { seconds: 5 }), "Automatic polling is active (every 5 seconds).");
});
