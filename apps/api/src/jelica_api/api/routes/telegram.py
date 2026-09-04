from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from jelica_api.api.authentication import require_current_user
from jelica_api.app_state import get_app_state
from jelica_api.contracts.telegram import (
    TelegramIntegrationStateResponse,
    TelegramLinkResponse,
)
from jelica_api.telegram import TelegramLinkError

router = APIRouter(prefix="/api/notifications/telegram", tags=["telegram"])


@router.get("", response_model=TelegramIntegrationStateResponse)
def get_telegram_state(request: Request) -> TelegramIntegrationStateResponse:
    user = require_current_user(request)
    state = get_app_state(request).telegram_integration.link_state(user_id=user.user_id)
    return TelegramIntegrationStateResponse(
        integration_available=state.integration_available,
        linked=state.linked,
        username=state.username,
        display_name=state.display_name,
        linked_at=state.linked_at,
    )


@router.post("/link", response_model=TelegramLinkResponse)
def create_telegram_link(request: Request) -> TelegramLinkResponse:
    user = require_current_user(request)
    try:
        link = get_app_state(request).telegram_integration.create_link_request(user_id=user.user_id)
    except TelegramLinkError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": error.code, "message": str(error)},
        ) from error
    return TelegramLinkResponse(url=link.url, expires_at=link.expires_at)


@router.delete("/link", status_code=status.HTTP_204_NO_CONTENT)
def delete_telegram_link(request: Request) -> None:
    user = require_current_user(request)
    get_app_state(request).telegram_integration.disconnect_user(user_id=user.user_id)


__all__ = ["router"]
