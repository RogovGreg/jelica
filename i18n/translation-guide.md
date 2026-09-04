# JELICA translation guide

## Purpose

`source.json` is the canonical registry of localization keys. Each entry contains the English
`default-text` and enough `context` for a translator to understand where and how the text is used.
The source entry is also the final runtime fallback.

Runtime translations live under `locales/<locale>/` and stay separated by consumer category:

- `messages.json` — Web and Desktop UI text;
- `reports.json` — report text shared by Core, CLI, Web, and Desktop;
- `notifications.json` — notification text shared by Core, CLI, Web, and Desktop.

Supported locale identifiers are `en`, `ru`, `sr-Latn`, and `sr-Cyrl`. English is the base locale.
The non-English files are intentionally empty until reviewed translations are supplied.

## Developer rule

- Add every new piece of user-visible UI text to `source.json` and consume it through the existing
  i18n runtime.
- Treat `source.json` as the source of truth for keys, English fallback text, and context.
- Existing hardcoded UI copy may remain until a dedicated migration stage changes it.
- New components must not introduce hardcoded user-visible copy. Technical identifiers such as API
  paths and machine values are not translation keys.

## Stable keys

- Treat every key as a permanent identifier, not as English copy.
- Do not rename a key when its displayed text changes.
- Add a new key only for a new semantic message or when the same English text needs different
  translation context.
- Put `report.*` keys in `reports.json`, `notification.*` keys in `notifications.json`, and other UI
  keys in `messages.json`.
- Keep the same key in `source.json` and the matching locale file.

## Translation entries

Each locale entry has this shape:

```json
{
  "text": "Translated text",
  "verified": false,
  "verifiedBy": null,
  "verifiedAt": null,
  "translatedBy": "translator or model identifier",
  "translatedAt": "2026-08-24T10:00:00Z"
}
```

`translatedBy` and `translatedAt` record who produced the text and when. `verified` stays `false`
until a reviewer approves the translation; after review, set `verifiedBy` and `verifiedAt`. Use UTC
ISO 8601 timestamps. Metadata records workflow state and does not alter runtime fallback behavior.

## Translation rules

- Read the source `context` before translating.
- Preserve product names, file extensions, variables, and placeholders exactly unless the context
  explicitly says otherwise.
- Do not add HTML to translation text.
- Keep terminology consistent across all three catalogs.
- Leave an entry absent instead of inserting an uncertain or empty translation. Runtime fallback is
  requested locale, then `en`, then source `default-text`.

## Example workflow

1. Choose a key from `source.json`, for example `task.action.start`.
2. Open the matching locale file, here `locales/ru/messages.json`.
3. Add the translated `text`, translator identity, timestamp, and `verified: false`.
4. Ask a reviewer to compare it with the source context and update the verification fields.
5. Run the frontend lint and build checks before shipping catalog changes used by Web.
