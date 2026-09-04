from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from jelica_api.app_state import get_app_state

router = APIRouter(prefix="/api/integrations/telegram", tags=["telegram-webhook"])
_MAX_WEBHOOK_BODY_BYTES = 64 * 1024


def webhook_secret_matches(*, provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    )


@router.post("/webhook")
async def receive_telegram_webhook(request: Request) -> dict[str, bool]:
    state = get_app_state(request)
    provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not webhook_secret_matches(
        provided=provided_secret,
        expected=state.settings.telegram_webhook_secret,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "telegram_webhook_forbidden", "message": "Webhook rejected."},
        )
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if content_type != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"error": "telegram_webhook_json_required", "message": "JSON is required."},
        )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"error": "telegram_webhook_too_large", "message": "Webhook rejected."},
            )
    try:
        update = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "telegram_webhook_invalid_json", "message": "Invalid JSON."},
        ) from error
    if not isinstance(update, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "telegram_webhook_invalid_update", "message": "Invalid update."},
        )
    await run_in_threadpool(state.telegram_integration.handle_update, update)
    return {"ok": True}


__all__ = ["receive_telegram_webhook", "router", "webhook_secret_matches"]
