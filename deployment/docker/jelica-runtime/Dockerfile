FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /workspace

RUN pip install --no-cache-dir uv

COPY pyproject.toml /workspace/pyproject.toml
COPY uv.lock /workspace/uv.lock
COPY apps /workspace/apps
COPY packages /workspace/packages

RUN uv sync --all-packages

COPY deployment/docker/jelica-runtime/entrypoint.sh /usr/local/bin/jelica-runtime-entrypoint
RUN chmod +x /usr/local/bin/jelica-runtime-entrypoint

ENV JELICA_HOME=/var/lib/jelica/service

VOLUME ["/var/lib/jelica"]

ENTRYPOINT ["/usr/local/bin/jelica-runtime-entrypoint"]
