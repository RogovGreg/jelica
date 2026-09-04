#!/usr/bin/env sh
set -eu

mkdir -p /var/lib/jelica/service
mkdir -p /var/lib/jelica/workspaces
mkdir -p /var/lib/jelica/internal-data

uv run --package jelica-cli jelica config init --non-interactive

exec uv run --package jelica-cli python -m jelica_cli.service_runner
