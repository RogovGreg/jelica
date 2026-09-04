#!/usr/bin/env sh
set -eu

uv run --package jelica-api alembic upgrade head

exec uv run --package jelica-api uvicorn jelica_api.main:app \
  --host "${API_HOST:-0.0.0.0}" \
  --port "${API_PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
