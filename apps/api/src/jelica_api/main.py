from __future__ import annotations

import uvicorn

from .app import create_app
from .settings import load_api_settings

app = create_app()


def main() -> None:
    settings = load_api_settings()
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
    )


__all__ = ["app", "main"]
