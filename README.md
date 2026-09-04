# JELICA

JELICA — Juxtaposing Evolutionary Lineages in Comparative Analysis — is a
comparative genomics platform for reproducible analysis of genomic sequences.
The current implementation covers sequence acquisition and preparation,
validation, alignment, comparative analysis, genetic distances, phylogenetic
trees, clade detection, and reproducible result packages.

Project site: <https://jelica.bio>

JELICA is actively developed and source-available; it is not open source.

## Components

- **JELICA Core** — authoritative analytical pipeline, task lifecycle, results,
  and local configuration.
- **JELICA Service** — persistent coordinator for Core execution and events.
- **JELICA CLI** — command-line interface and machine integration surface.
- **JELICA Web** — browser interface for public content, tasks, results, and
  account features.
- **JELICA Desktop** — Electron interface for local tasks, results, and offline
  documentation.
- **Shared packages** — portable contracts and Web/Desktop presentation
  foundations.

The high-level authority flow is: Core → Service → CLI machine interface → Web
Server/Electron Main → UI. Web and Desktop do not replace the Core.

## NCBI / external services

JELICA has been registered with NCBI for use of the Entrez Programming Utilities (E-utilities) since August 3, 2026.

This registration does not imply NCBI certification, approval, partnership, or
endorsement.

## Installation

The recommended local CLI installation uses the repository installers.

macOS/Linux:

```bash
./scripts/install.sh
```

Windows PowerShell:

```powershell
.\scripts\install.ps1
```

The installers ensure `uv` is available, install the editable `jelica-cli`
package as a global `uv` tool, initialize the Core system configuration when
it is missing, and verify the installation with `jelica --version`. If the
shell PATH cannot be updated automatically, restart the shell or run
`uv tool update-shell` as instructed by the installer.

## Running from source

Python source setup requires Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --all-packages
uv run --package jelica-cli jelica --help
uv run --package jelica-cli jelica --version
```

To inspect a potential analysis plan without creating a task:

```bash
uv run --package jelica-cli jelica analyze path/to/config.json path/to/input.fasta --plan
```

Useful local registry commands include:

```bash
uv run --package jelica-cli jelica tasks list
uv run --package jelica-cli jelica results list
```

Web and Desktop development use Node.js 22 in the repository Docker/build
configuration. The current Electron dependency requires Node.js >=22.12.0.

```bash
npm --prefix apps/web ci
npm --prefix apps/web run dev

npm --prefix apps/desktop ci
npm --prefix apps/desktop run dev
```

## Running locally with Docker

This uses the existing Compose setup under `deployment/`.

Prerequisites: Docker Engine with the Docker Compose plugin.

Prepare the safe local environment template:

```bash
cp deployment/env/.env.example deployment/env/.env.local
```

The template contains placeholders and development defaults. Replace local
values as needed; do not put production credentials in the repository.

To make the documentation viewer available, build the validated release
artifact first:

```bash
./docs/documentation/build.sh release
```

Validate and start the stack from the repository root:

```bash
docker compose --env-file deployment/env/.env.local -f deployment/compose/docker-compose.yml config
docker compose --env-file deployment/env/.env.local -f deployment/compose/docker-compose.yml up --build
```

The local reverse-proxy URL is <http://localhost>; the backend health endpoint
is <http://localhost/api/health>.

Inspect or stop the stack:

```bash
docker compose --env-file deployment/env/.env.local -f deployment/compose/docker-compose.yml ps
docker compose --env-file deployment/env/.env.local -f deployment/compose/docker-compose.yml logs -f
docker compose --env-file deployment/env/.env.local -f deployment/compose/docker-compose.yml down
```

The full deployment notes are in
[`deployment/README.md`](deployment/README.md).

## License

JELICA-owned software is source-available under the [PolyForm Noncommercial
License 1.0.0](LICENSE), SPDX `PolyForm-Noncommercial-1.0.0`. Copyright © 2026
Grigorii Rogov.

See [commercial licensing](COMMERCIAL_LICENSING.md),
[trademark and branding rules](TRADEMARKS.md),
[contribution policy](CONTRIBUTING.md),
[Contributor Assignment Agreement](CONTRIBUTOR_AGREEMENT.md), and
[third-party notices](THIRD_PARTY_NOTICES.md).

## System config (Core-owned)

System configuration belongs to JELICA Core, not to the CLI. The canonical
location is `<JELICA_HOME>/config.toml`; Core resolves `JELICA_HOME` from the
environment or the platform user-data directory.

Minimal configuration example:

```toml
schema_version = 1
default_alignment_mode = "compute"

[data]
directory = "data"

[execution]
max_workers = 1
```

Use the CLI to inspect and update it:

```bash
uv run --package jelica-cli jelica config path
uv run --package jelica-cli jelica config show
uv run --package jelica-cli jelica config validate
```

## Documentation

The documentation source and reproducible artifact pipeline are described in
[`docs/documentation/README.md`](docs/documentation/README.md). The Web and
Desktop documentation viewers consume validated generated artifacts.

## Development checks

The main repository checks are:

```bash
uv lock --check
uv run pytest
uv run ruff check .
uv run ruff format --check .
npm --prefix apps/web run validate:i18n
npm --prefix apps/web run lint
npm --prefix apps/web run build
npm --prefix apps/desktop run typecheck
npm --prefix apps/desktop run lint
npm --prefix apps/desktop run test
npm --prefix apps/desktop run build
```
