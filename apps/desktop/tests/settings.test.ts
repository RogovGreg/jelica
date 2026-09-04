import assert from "node:assert/strict";
import test from "node:test";
import { parseLocale, parseScale, parseTheme } from "../src/renderer/settings";
import { documentationTextSizeForScale } from "../../../packages/app-platform/src/theme";

test("Desktop UI settings validate persisted values", () => {
  assert.equal(parseScale("80"), 80); assert.equal(parseScale("100"), 100); assert.equal(parseScale("125"), 125); assert.equal(parseScale("150"), 150); assert.equal(parseScale("90"), 100); assert.equal(parseScale("79"), 100); assert.equal(parseScale("151"), 100); assert.equal(parseScale("bad"), 100);
  assert.equal(documentationTextSizeForScale(80), "small"); assert.equal(documentationTextSizeForScale(100), "standard"); assert.equal(documentationTextSizeForScale(125), "large"); assert.equal(documentationTextSizeForScale(150), "large");
  assert.equal(parseTheme("dark"), "dark"); assert.equal(parseTheme("invalid"), "system");
  assert.equal(parseLocale("sr-Latn", "en"), "sr-Latn"); assert.equal(parseLocale("invalid", "en"), "en");
});
