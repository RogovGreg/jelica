FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /workspace

RUN pip install --no-cache-dir uv

COPY pyproject.toml /workspace/pyproject.toml
COPY uv.lock /workspace/uv.lock
COPY apps /workspace/apps
COPY packages /workspace/packages
COPY i18n /workspace/i18n

RUN uv sync --all-packages

COPY deployment/docker/web-backend/entrypoint.sh /usr/local/bin/jelica-api-entrypoint
RUN chmod +x /usr/local/bin/jelica-api-entrypoint

WORKDIR /workspace/apps/api

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/jelica-api-entrypoint"]
