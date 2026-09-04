#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file="$repo_root/deployment/compose/docker-compose.yml"
env_file=${JELICA_TEST_ENV_FILE:-$repo_root/deployment/env/.env.local}
test_database=${JELICA_AUTH_TEST_DATABASE:-jelica_auth_test}

case "$test_database" in
  *[!A-Za-z0-9_]* | "")
    echo "JELICA_AUTH_TEST_DATABASE must contain only letters, digits, and underscores." >&2
    exit 1
    ;;
esac

if [ ! -f "$env_file" ]; then
  echo "Environment file not found: $env_file" >&2
  exit 1
fi

compose() {
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
}

cleanup() {
  compose exec -T web-db sh -c \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS '"$test_database"' WITH (FORCE);"' \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

compose up -d web-db
compose build web-backend
compose exec -T web-db sh -c \
  'if [ "'"$test_database"'" = "$POSTGRES_DB" ]; then echo "Refusing to use production database name." >&2; exit 1; fi'
compose exec -T web-db sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS '"$test_database"' WITH (FORCE);" -c "CREATE DATABASE '"$test_database"';"'

compose run --rm --no-deps \
  --entrypoint sh \
  --volume "$repo_root/tests:/workspace/tests:ro" \
  web-backend -c '
    set -eu
    test_url="${DATABASE_URL%/*}/'"$test_database"'"
    export DATABASE_URL="$test_url"
    export JELICA_POSTGRES_AUTH_TEST_DATABASE_URL="$test_url"
    cd /workspace/apps/api
    uv run alembic upgrade head
    cd /workspace
    uv run --project apps/api pytest tests/api/test_postgres_auth_integration.py -q
  '
