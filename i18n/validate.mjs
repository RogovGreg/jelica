import { readFile } from "node:fs/promises";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const i18nDirectory = dirname(fileURLToPath(import.meta.url));
const localesDirectory = join(i18nDirectory, "locales");
const supportedLocales = ["en", "ru", "sr-Latn", "sr-Cyrl"];
const catalogNames = ["messages", "reports", "notifications"];
const requiredKeys = [
  "common.action.save",
  "common.action.cancel",
  "common.action.close",
  "common.action.retry",
  "common.action.download",
  "common.state.loading",
  "common.state.error",
  "common.state.empty",
  "common.state.success",
  "task.status.created",
  "task.status.queued",
  "task.status.running",
  "task.status.paused",
  "task.status.completed",
  "task.status.failed",
  "task.status.cancelled",
  "task.action.start",
  "task.action.pause",
  "task.action.resume",
  "task.action.cancel",
];
const metadataFields = ["verifiedBy", "verifiedAt", "translatedBy", "translatedAt"];
const errors = [];

const source = await readJson(join(i18nDirectory, "source.json"));
if (source) {
  validateSource(source);
}

const localeKeys = new Map();
for (const locale of supportedLocales) {
  const seenKeys = new Set();
  for (const catalogName of catalogNames) {
    const catalogPath = join(localesDirectory, locale, `${catalogName}.json`);
    const catalog = await readJson(catalogPath);
    if (catalog) {
      validateCatalog(catalog, catalogPath, catalogName, source, seenKeys);
    }
  }
  localeKeys.set(locale, seenKeys);
}

if (source) {
  for (const key of requiredKeys) {
    if (!Object.hasOwn(source, key)) {
      errors.push(`source.json is missing required key "${key}"`);
    }
  }

  const englishKeys = localeKeys.get("en") ?? new Set();
  for (const key of Object.keys(source)) {
    if (!englishKeys.has(key)) {
      errors.push(`English locale is missing source key "${key}"`);
    }
  }
}

if (errors.length > 0) {
  console.error(`i18n validation failed with ${errors.length} error(s):`);
  for (const error of errors) {
    console.error(`- ${error}`);
  }
  process.exitCode = 1;
} else {
  console.log(
    `i18n validation passed: ${Object.keys(source).length} source keys, ` +
      `${supportedLocales.length * catalogNames.length} locale catalogs.`,
  );
}

async function readJson(filePath) {
  const displayPath = relative(i18nDirectory, filePath);
  let rawValue;
  try {
    rawValue = await readFile(filePath, "utf8");
  } catch (error) {
    errors.push(`${displayPath} cannot be read: ${error.message}`);
    return null;
  }

  let parsedValue;
  try {
    parsedValue = JSON.parse(rawValue);
  } catch (error) {
    errors.push(`${displayPath} contains invalid JSON: ${error.message}`);
    return null;
  }

  try {
    assertNoDuplicateObjectKeys(rawValue, displayPath);
  } catch (error) {
    errors.push(error.message);
  }

  if (!isObject(parsedValue)) {
    errors.push(`${displayPath} must contain a JSON object at its root`);
    return null;
  }
  return parsedValue;
}

function validateSource(sourceCatalog) {
  for (const [key, entry] of Object.entries(sourceCatalog)) {
    if (!isObject(entry)) {
      errors.push(`source.json entry "${key}" must be an object`);
      continue;
    }
    if (typeof entry["default-text"] !== "string" || entry["default-text"].trim() === "") {
      errors.push(`source.json entry "${key}" must have non-empty default-text`);
    }
    if (typeof entry.context !== "string" || entry.context.trim() === "") {
      errors.push(`source.json entry "${key}" must have non-empty context`);
    }
  }
}

function validateCatalog(catalog, filePath, catalogName, sourceCatalog, seenKeys) {
  const displayPath = relative(i18nDirectory, filePath);
  for (const [key, entry] of Object.entries(catalog)) {
    if (seenKeys.has(key)) {
      errors.push(`${displayPath} repeats locale key "${key}" from another catalog`);
    }
    seenKeys.add(key);

    if (!sourceCatalog || !Object.hasOwn(sourceCatalog, key)) {
      errors.push(`${displayPath} contains unknown source key "${key}"`);
    }
    if (catalogName !== catalogForKey(key)) {
      errors.push(`${displayPath} contains key "${key}" in the wrong catalog`);
    }
    if (!isObject(entry)) {
      errors.push(`${displayPath} entry "${key}" must be an object`);
      continue;
    }
    if (typeof entry.text !== "string" || entry.text.trim() === "") {
      errors.push(`${displayPath} entry "${key}" must have non-empty text`);
    }
    const sourceEntry = sourceCatalog?.[key];
    if (sourceEntry && typeof entry.text === "string") {
      const expected = placeholders(sourceEntry["default-text"]);
      const actual = placeholders(entry.text);
      if (expected.join("\u0000") !== actual.join("\u0000")) {
        errors.push(
          `${displayPath} entry "${key}" has placeholders ${formatPlaceholders(actual)}; ` +
            `expected ${formatPlaceholders(expected)}`,
        );
      }
    }
    if (typeof entry.verified !== "boolean") {
      errors.push(`${displayPath} entry "${key}" must have boolean verified`);
    }

    for (const field of metadataFields) {
      if (!(entry[field] === null || typeof entry[field] === "string")) {
        errors.push(`${displayPath} entry "${key}" has invalid ${field}`);
      }
    }
  }
}

function placeholders(text) {
  return [...text.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)]
    .map((match) => match[1])
    .sort();
}

function formatPlaceholders(values) {
  return values.length === 0 ? "none" : `{${values.join(", ")}}`;
}

function catalogForKey(key) {
  if (key.startsWith("report.")) {
    return "reports";
  }
  if (key.startsWith("notification.")) {
    return "notifications";
  }
  return "messages";
}

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertNoDuplicateObjectKeys(rawValue, displayPath) {
  let cursor = 0;

  scanValue("$");

  function scanValue(path) {
    skipWhitespace();
    if (rawValue[cursor] === "{") {
      scanObject(path);
      return;
    }
    if (rawValue[cursor] === "[") {
      scanArray(path);
      return;
    }
    if (rawValue[cursor] === '"') {
      readString();
      return;
    }
    while (cursor < rawValue.length && !/[\s,\]}]/.test(rawValue[cursor])) {
      cursor += 1;
    }
  }

  function scanObject(path) {
    const keys = new Set();
    cursor += 1;
    skipWhitespace();
    if (rawValue[cursor] === "}") {
      cursor += 1;
      return;
    }

    while (cursor < rawValue.length) {
      const key = readString();
      if (keys.has(key)) {
        throw new Error(`${displayPath} contains duplicate key "${key}" at ${path}`);
      }
      keys.add(key);
      skipWhitespace();
      cursor += 1;
      scanValue(`${path}.${key}`);
      skipWhitespace();
      if (rawValue[cursor] === "}") {
        cursor += 1;
        return;
      }
      cursor += 1;
      skipWhitespace();
    }
  }

  function scanArray(path) {
    let itemIndex = 0;
    cursor += 1;
    skipWhitespace();
    if (rawValue[cursor] === "]") {
      cursor += 1;
      return;
    }

    while (cursor < rawValue.length) {
      scanValue(`${path}[${itemIndex}]`);
      itemIndex += 1;
      skipWhitespace();
      if (rawValue[cursor] === "]") {
        cursor += 1;
        return;
      }
      cursor += 1;
      skipWhitespace();
    }
  }

  function readString() {
    const start = cursor;
    cursor += 1;
    while (cursor < rawValue.length) {
      if (rawValue[cursor] === "\\") {
        cursor += 2;
        continue;
      }
      if (rawValue[cursor] === '"') {
        cursor += 1;
        return JSON.parse(rawValue.slice(start, cursor));
      }
      cursor += 1;
    }
    return "";
  }

  function skipWhitespace() {
    while (cursor < rawValue.length && /\s/.test(rawValue[cursor])) {
      cursor += 1;
    }
  }
}
