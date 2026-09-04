# JELICA documentation pipeline

This directory is the build-time documentation foundation for JELICA. LaTeX under `source/` is the
only content source of truth. Web, Desktop, and CLI integrations must consume generated artifacts;
runtime applications must not compile or interpret LaTeX.

## Structure

```text
docs/documentation/
├── source/<locale>/       # en; future ru, sr-Latn, sr-Cyrl
│   ├── main.tex
│   ├── chapters/
│   └── assets/
├── template/
│   ├── latex/
│   │   ├── jelica-base.sty
│   │   ├── jelica-doc.sty
│   │   ├── jelica-html.cfg
│   │   ├── profiles/
│   │   └── assets/
│   └── html/
├── schema/
│   ├── documentation-manifest.schema.json
│   └── search-index.schema.json
├── tooling/
├── version.json           # artifact format version
├── build.sh
├── build/                 # temporary generated output, gitignored
└── releases/              # stable generated exports, gitignored
```

`jelica-base.sty` owns the memoir layout, typography, engine guard, and language foundation.
`jelica-doc.sty` owns JELICA metadata, branding, the title page, and stable documentation commands.
The source loads only `jelica-doc`; the presentation layer loads the base layer and selected profile.

## Build commands

Run from the repository root:

```bash
./docs/documentation/build.sh pdf
./docs/documentation/build.sh html
./docs/documentation/build.sh all
./docs/documentation/build.sh validate
./docs/documentation/build.sh release
```

The default variant is `en`, `screen`, `standard`. Locale, presentation profile, and body-text size
are independent build dimensions:

```bash
./docs/documentation/build.sh all --profile print --size large
./docs/documentation/build.sh pdf --profile screen --size small
./docs/documentation/build.sh release --locale ru --profile screen --size standard
```

Supported locale identifiers are `en`, `ru`, `sr-Latn`, and `sr-Cyrl`. A locale is buildable only
when `source/<locale>/main.tex` exists; otherwise the command fails before starting the toolchain
with `Documentation source for locale is unavailable: <locale>`. The pipeline does not synthesize
or copy translations.

By default, the command runs the Island of TeX TeX Live medium image pinned to the
multi-architecture digest
`sha256:7ff9aba34e665d3899215008a65e72eb2a8faaf569e846c34303827f805e81a5`.
Docker must be running for the container path. Failures stop immediately and retain compiler logs
under the selected build variant's `work/` directory. A fixed `SOURCE_DATE_EPOCH` is supplied by
default so identical sources and toolchain inputs produce byte-identical PDFs.

Maintainers who deliberately want an installed LuaLaTeX/latexmk/make4ht/Python toolchain can set
`JELICA_DOCS_USE_LOCAL_TOOLCHAIN=1`. That opt-in path is convenient for template development but is
not the pinned release-build path.

`validate` checks an existing variant and never starts Docker or recompiles LaTeX. `all` and `html`
run the same validation automatically after generating their client artifacts.

`release` performs a complete build of the selected variant, validates it, and exports the
versioned release directory and archive described below.

## Generated artifacts

For the default variant, `build/en/screen-standard/` contains:

- `pdf/jelica-documentation-en-screen-standard.pdf`;
- `html/index.html` and chapter-level HTML pages with stable anchors;
- `documentation-manifest.json` with locale, title, version, sections, pages, paths, and anchors;
- `search-index.json` with title, headings, keywords, and plain text content;
- `version.json` with separate artifact-format and documentation-content versions.

The manifest and search data are derived from the same LaTeX source after the HTML build. They are
static client artifacts, not a search service or database.
The optional additive `headingAnchors` search field maps extracted headings to stable generated HTML
anchors. Search is performed locally by the consumers, is Unicode-normalized, prioritizes title,
heading, keyword, then body matches, and returns a bounded result set.

## Artifact compatibility contract

The stable metadata formats are described by:

- `schema/documentation-manifest.schema.json`;
- `schema/search-index.schema.json`.

Both schemas describe the current format without restricting it to the currently supported locales
or to a closed set of future artifact/search extension fields. Optional additive fields remain
compatible with format version 1; a breaking metadata change requires incrementing
`artifactFormatVersion`.

The tracked `version.json` owns only `artifactFormatVersion`. Documentation content version remains
owned by `\JelicaSetVersion` in the locale's `main.tex`. The generator combines both values into
the build variant's `version.json`, and the manifest exposes that file through `paths.version`.

The dependency-free validator reads the schemas and checks:

- required fields, JSON types, patterns, and uniqueness rules;
- locale, title, content-version, and format-version agreement;
- page/search/section consistency and unique public identifiers;
- relative-path containment, referenced files, HTML/CSS assets, and generated anchors;
- the PDF signature and a safe static-HTML boundary (no scripts, event handlers, unsafe schemes, or
  protocol-relative references).

## Release bundle

The release target keeps temporary compiler output under `build/` and exports only validated,
client-facing files under a separate versioned path. For the default variant it creates:

```text
releases/0.1/format-v1/
├── en/screen-standard/
│   ├── pdf/
│   ├── html/
│   ├── documentation-manifest.json
│   ├── search-index.json
│   ├── version.json
│   ├── release.json
│   └── checksums.json
└── jelica-documentation-0.1-format-v1-en-screen-standard.tar.gz
```

Create another supported variant with the same entry point:

```bash
./docs/documentation/build.sh release --locale en --profile print --size large
```

`release.json` identifies the documentation release and artifact-format versions, locale, profile,
and `textSize`. Its `generatedAt` is the UTC rendering of `SOURCE_DATE_EPOCH`, not a wall-clock
publication time. `sourceHash` is a deterministic SHA-256 provenance fingerprint over files in the
selected locale source and presentation template, sorted by their documentation-relative paths.
For each file it hashes the eight-byte big-endian UTF-8 path length, the path bytes, and the raw
SHA-256 content digest.

`checksums.json` lists the relative path, byte size, and SHA-256 digest of every regular bundle file
except itself. The archive includes that checksum manifest and is reopened and verified during
packaging.

Only files reachable through the validated manifest/HTML dependency graph are exported, so LaTeX
work files and compiler copies do not enter the bundle. File order, ownership, permissions, and
timestamps are normalized from `SOURCE_DATE_EPOCH`; repeated builds with identical inputs and the
pinned toolchain produce the same file digests and archive bytes. Web, Desktop, and CLI consumers
should treat the extracted directory as their documentation artifact root and read its manifest
rather than relying on build internals.

## Web consumption

The read-only Web viewer loads an extracted release bundle through its server-side artifact
adapter. It reads `documentation-manifest.json`, `search-index.json`, `release.json`, and
`version.json`, verifies the complete checksum inventory, then serves bundle-local static
HTML/PDF/assets with a restrictive CSP. The viewer never reads `source/` or `build/` and never
invokes this build pipeline at runtime. Deployment mounts the release catalog read-only; an absent,
invalid, tampered, or ambiguous bundle produces a controlled unavailable state.

Web and Desktop use the same selection rule without crossing artifact versions: requested
locale/profile/size, requested locale/profile/`standard`, then English/profile/`standard`.
The effective locale is shown in metadata. A locale switch preserves a stable page identifier and
anchor when the target bundle contains them, otherwise it falls back to the page or documentation
overview. Unknown direct Web routes remain controlled 404 responses.

Desktop stages validated release directories under `resources/documentation` during `prebuild`.
Electron Main owns discovery, checksum validation, semantic `jelica-doc://` resource resolution,
MIME/CSP headers, and native PDF paths. The sandboxed renderer receives only parsed metadata and
semantic resource URLs over the allowlisted preload bridge; it never receives host paths or reads
files directly.

## Presentation profiles

- `screen`: compact one-sided page, sans-serif body, colored links, intended for electronic use.
- `print`: A4 page, serif body, sans-serif branding/headings, color-independent links.

Google Sans Flex is selected when the font is available. Until an approved font file is supplied,
the pinned TeX toolchain uses TeX Gyre Heros as the deterministic brand/screen fallback and TeX Gyre
Pagella for print body text. Russian and Serbian Cyrillic variants use the pinned DejaVu Sans/Serif
fallbacks so every configured script has deterministic glyph coverage.

Body text sizes are `small` (10/13 pt), `standard` (11/14.5 pt), and `large` (12.5/17 pt). The title
page and heading sizes are fixed explicitly and do not change with the body size profile.

## Branding asset

The title page references `template/latex/assets/jelica-publication-logo.png` as an external file.
It is copied unchanged from `8. publication image.png` supplied for this stage and is cropped only
at LaTeX include time. SHA-256:
`6991e5cf13b857fbd4f1be4df5c2d343a24e24a5944f50ea364d9cdbb81d60da`.

## Localization boundary

Only `source/en` currently exists. Future `ru`, `sr-Latn`, and `sr-Cyrl` directories should be
created only when actual documentation translation begins. Documentation prose never belongs in
the UI catalogs under `i18n/`.

## Intentionally outside this stage

- CLI documentation commands;
- translations;
- documentation CMS or search engine;
- substantive user manual content;
- GitHub release automation, upload, or publishing.
