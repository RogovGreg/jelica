# Third-party notices

JELICA-owned software remains under the PolyForm Noncommercial License 1.0.0;
third-party dependencies and materials remain subject to the respective
upstream licenses and notices. This file does not claim ownership of them.

## Bundled Google Sans font

The shared application assets include the Google Sans font family from the
local source directory `packages/app-platform/assets/fonts/google-sans/`.
Web and Desktop use its two variable TrueType files for normal and italic
application UI text. The source bundle also contains the static font variants,
the upstream `README.txt`, and the applicable `OFL.txt`.

The included font metadata identifies the family as Google Sans. The supplied
license and README do not state a separate copyright holder or provider, so no
additional ownership attribution is inferred here.

The included license identifies the font software as licensed under the SIL
Open Font License, Version 1.1. The OFL text remains next to the canonical
font source and must remain available with any font distribution. A future
Desktop packaged release must include the applicable OFL notice with its
release/legal notices when the renderer font assets are packaged.

## Source repository

Project manifests and lockfiles identify dependency packages and versions to
the extent supported by the package ecosystem. Applicable license terms and
notices remain those of the respective projects; a lockfile is not a complete
license registry.

## Current CLI distribution model

The installer uses `uv tool install --editable` for `apps/cli`. CLI runtime
dependencies are resolved separately by `uv`; the CLI declares Typer directly
and uses the repository’s Core and contracts packages. The current runtime
dependency graph therefore resolves Typer, Biopython, Pydantic, platformdirs,
and tomli-w (plus their transitive dependencies) through the package manager.
They are not bundled into a standalone JELICA binary or installer by the
current repository. No additional manually bundled third-party CLI component
or release notice was identified. Upstream package licenses and notices apply
to packages installed by `uv`; the current upstream metadata identifies Typer,
Pydantic, platformdirs, and tomli-w as MIT, and Biopython as
`LicenseRef-Biopython-License-Agreement`.

## Current Desktop distribution model

The Desktop build uses Vite/Electron and currently produces application build
output, not a packaged installer. The build stages the validated documentation
release tree, JELICA branding assets, and the canonical JELICA notification
sound. It does not copy Electron/Chromium runtime files or `node_modules` into
the build output. The Desktop npm lockfile identifies all resolved packages;
the current lockfile entries have license metadata, including Electron (MIT),
React (MIT), Vite (MIT), and TypeScript (Apache-2.0).

The installed Electron development package currently contains its own
`node_modules/electron/LICENSE` and
`node_modules/electron/dist/LICENSES.chromium.html` files. Because the current
build has no installer or runtime-packaging step, those files are not yet
distributed by JELICA. A future Desktop packager must retain and ship the
applicable Electron and Chromium notices with the packaged application.

The staged documentation uses system-font CSS and the repository’s JELICA
publication logo; no third-party font or runtime asset was identified. No
installer notice is integrated here because no installer packaging
configuration currently exists.

## Release status

No standalone CLI binary, wheel/sdist release workflow, Desktop installer, or
binary release notice bundle is currently produced by this repository. A
future release build must repeat the package-manager audit and include
applicable upstream notices in the distributable artifact.
