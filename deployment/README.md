# JELICA deployment skeleton

This directory contains stage-1 deployment scaffolding for the JELICA Web/Desktop stack.

## Development

1. Copy and adjust environment template:

   ```bash
   cp deployment/env/.env.example deployment/env/.env.local
   ```

   Local development uses `EMAIL_DELIVERY_MODE=development`; set
   `AUTH_EXPOSE_DEV_TOKENS=true` only when a local verification token is needed for testing.

2. Start the stack:

   Build the read-only documentation artifact first when the viewer should be available:

   ```bash
   ./docs/documentation/build.sh release
   ```

   Compose requires the configured host mount directory to exist and will not create it as root.
   If no release should be exposed yet, create an empty directory owned by the deployment user and
   point `JELICA_DOCUMENTATION_RELEASES_PATH` at it. The Web app then starts and shows a
   documentation unavailable state instead of compiling documentation at runtime.

   ```bash
   docker compose --env-file deployment/env/.env.local -f deployment/compose/docker-compose.yml up --build
   ```

3. Open:
   - reverse proxy: `http://localhost` (or your configured `DOMAIN`);
   - backend health: `http://localhost/api/health`.

   Use the reverse-proxy URL for authentication. The HttpOnly session cookie is intentionally
   same-origin; standalone Next.js on port 3000 does not provide a development API proxy.

## Production

Minimal VPS requirements:

- Docker Engine with Compose plugin;
- public domain name pointed to the VPS IP;
- open ports `80` and `443`;
- persistent storage for Docker volumes.

Production checklist:

1. Create a production env file from [deployment/env/.env.example](env/.env.example).
2. Set strong values for:
   - `POSTGRES_PASSWORD`;
   - `WEB_SECRET_KEY`;
   - `EMAIL_DELIVERY_MODE=smtp`, `PUBLIC_WEB_BASE_URL`, and SMTP host/from settings;
   - SMTP credentials, if your provider requires authentication.
3. Set `AUTH_COOKIE_SECURE=true` and keep `AUTH_EXPOSE_DEV_TOKENS=false`.
4. Keep `AUTH_RATE_LIMIT_ENABLED=true`. The limiter and realtime invalidation assume one
   `web-backend` process; multiple workers/containers require shared state.
5. Keep `INTERNAL_API_ENABLED=false`, or configure a long random `INTERNAL_API_TOKEN`. Caddy
   always returns 404 for `/api/internal/*`; enabled operator calls must go directly to the
   backend container/private network with `X-JELICA-Internal-Token`.
6. Configure `DOMAIN` and DNS records. Keep Caddy as the only host-published service; backend,
   PostgreSQL, runtime, and frontend use only the private Compose network.
7. Confirm `PUBLIC_WEB_BASE_URL` is the public HTTPS origin, SMTP mode is enabled, PostgreSQL
   storage is persistent, and no development token exposure is enabled.
8. Point `JELICA_DOCUMENTATION_RELEASES_PATH` at a prepared, validated bundle directory. If a
   mounted release tree contains multiple bundles for the same locale/profile/text-size, pin one
   bundle with `JELICA_DOCUMENTATION_RELEASE_DIR` using its path below
   `/opt/jelica/documentation-releases`.
9. To enable Telegram, create a private-chat bot with BotFather and set
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, and a random
   `TELEGRAM_WEBHOOK_SECRET`. With those values loaded in the operator shell, configure the bot:

   ```bash
   curl --fail-with-body --request POST \
     --url "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
     --header 'Content-Type: application/json' \
     --data "{\"url\":\"${PUBLIC_WEB_BASE_URL}/api/integrations/telegram/webhook\",\"secret_token\":\"${TELEGRAM_WEBHOOK_SECRET}\",\"allowed_updates\":[\"message\",\"callback_query\"]}"
   curl --fail-with-body --request POST \
     --url "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setMyCommands" \
     --header 'Content-Type: application/json' \
     --data '{"scope":{"type":"all_private_chats"},"commands":[{"command":"start","description":"Connect or check JELICA"},{"command":"status","description":"Show JELICA status"},{"command":"active_tasks","description":"Show your active tasks"},{"command":"active_project_tasks","description":"Show active project tasks"},{"command":"project_status","description":"Show project status"},{"command":"disconnect","description":"Disconnect JELICA"},{"command":"help","description":"Show supported commands"}]}'
   ```

   The canonical webhook uses the existing Caddy `/api` HTTPS proxy. Do not use `getUpdates`
   while the webhook is configured. Telegram configuration is optional and startup never makes a
   Telegram network call.
10. Start in detached mode:

   ```bash
   docker compose --env-file deployment/env/.env.prod -f deployment/compose/docker-compose.yml up -d --build
   ```

11. Verify service status:

   ```bash
   docker compose -f deployment/compose/docker-compose.yml ps
   ```

Storage and data ownership:

- `web-db-data`: PostgreSQL data for Web domain only;
- `web-storage`: Web-owned temporary Analysis Upload storage. It is mounted read/write only in
  `web-backend` and read-only at the same absolute path in `jelica-runtime`, because the existing
  CLI reads config bytes in the backend process while the existing Service/Core runtime later
  reads materialized input file and directory paths. It is not mounted in the frontend or Caddy;
- `jelica-runtime-data`: JELICA runtime home and task workspace data.

Documentation artifacts are mounted read-only from an existing
`JELICA_DOCUMENTATION_RELEASES_PATH`. They are generated by the documentation release pipeline and
are not stored in an application volume. A production deployment should expose one curated bundle;
the optional `JELICA_DOCUMENTATION_RELEASE_DIR` is an explicit in-container selector when the
mounted directory is a larger versioned release tree.

Browser upload retention and transport limits are provisional deployment settings rather than
product limits. Configure `JELICA_WEB_UPLOAD_ROOT`, `JELICA_UPLOAD_MAX_FILE_BYTES`,
`JELICA_UPLOAD_MAX_SESSION_BYTES`, `JELICA_UPLOAD_MAX_SESSION_FILES`,
`JELICA_UPLOAD_MAX_RELATIVE_PATH_LENGTH`, and `JELICA_UPLOAD_SESSION_TTL_SECONDS` in the deployment
environment. The browser API exposes only opaque session/item IDs; this storage must not be served
as static files.

## Maintenance

Update images and restart:

```bash
docker compose --env-file deployment/env/.env.prod -f deployment/compose/docker-compose.yml pull
docker compose --env-file deployment/env/.env.prod -f deployment/compose/docker-compose.yml up -d --build
```

Apply future backend migrations:

```bash
docker compose --env-file deployment/env/.env.prod -f deployment/compose/docker-compose.yml exec web-backend alembic upgrade head
```

Run the disposable PostgreSQL auth integration path against the Compose database:

```bash
JELICA_TEST_ENV_FILE=deployment/env/.env.local ./scripts/test-postgres-auth.sh
```

The script recreates only `jelica_auth_test`, migrates it to the current Alembic head, runs the
targeted PostgreSQL auth/session/token tests inside a temporary backend container, and drops the
test database on exit. It never targets `POSTGRES_DB`.

When internal reconciliation is intentionally enabled, invoke it from the private backend
network/container, never through Caddy. For example, an operator can execute a request inside the
backend container with the configured `X-JELICA-Internal-Token` header.

Restart a single service:

```bash
docker compose -f deployment/compose/docker-compose.yml restart web-backend
```

Backup volumes (example):

```bash
docker run --rm -v jelica_web-db-data:/source -v "$PWD":/backup alpine tar czf /backup/web-db-data.tgz -C /source .
docker run --rm -v jelica_web-storage:/source -v "$PWD":/backup alpine tar czf /backup/web-storage.tgz -C /source .
docker run --rm -v jelica_jelica-runtime-data:/source -v "$PWD":/backup alpine tar czf /backup/jelica-runtime-data.tgz -C /source .
```

## Current stage limitation

`web-frontend` includes guest task/result flows, account verification, password recovery, and a
read-only documentation viewer. Task ownership transfer, project persistence, and advanced visual
analytics remain out of scope.
