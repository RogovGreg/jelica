from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from jelica_api.api.authentication import require_authenticated_context
from jelica_api.app_state import get_app_state
from jelica_api.contracts import (
    WebPushConfigResponse,
    WebPushSubscriptionDeleteRequest,
    WebPushSubscriptionUpsertRequest,
)
from jelica_api.web_push import (
    WebPushSessionUnavailableError,
    WebPushSubscriptionConflictError,
)

router = APIRouter(prefix="/api/notifications/push", tags=["notification-push"])


@router.get("/config", response_model=WebPushConfigResponse)
def get_web_push_config(request: Request) -> WebPushConfigResponse:
    context = require_authenticated_context(request)
    state = get_app_state(request)
    counts = state.web_push_subscription_service.counts(
        user_id=context.user.user_id,
        auth_session_id=context.session_id,
    )
    available = state.settings.web_push_configured
    return WebPushConfigResponse(
        available=available,
        vapid_public_key=(state.settings.web_push_vapid_public_key if available else None),
        active_subscription_count=counts.active,
        current_session_subscription_count=counts.current_session,
    )


@router.post(
    "/subscriptions",
    status_code=status.HTTP_204_NO_CONTENT,
)
def upsert_web_push_subscription(
    payload: WebPushSubscriptionUpsertRequest,
    request: Request,
    response: Response,
) -> None:
    context = require_authenticated_context(request)
    state = get_app_state(request)
    if not state.settings.web_push_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "web_push_unavailable",
                "message": "Web Push is not configured on this server.",
            },
        )
    try:
        state.web_push_subscription_service.upsert(
            user_id=context.user.user_id,
            auth_session_id=context.session_id,
            endpoint=payload.endpoint,
            expiration_time=payload.expiration_time,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
        )
    except WebPushSubscriptionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "web_push_subscription_conflict",
                "message": "Push subscription is unavailable.",
            },
        ) from error
    except WebPushSessionUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "authentication_required",
                "message": "Authentication is required.",
            },
        ) from error
    response.status_code = status.HTTP_204_NO_CONTENT


@router.delete(
    "/subscriptions/current",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_current_web_push_subscription(
    payload: WebPushSubscriptionDeleteRequest,
    request: Request,
    response: Response,
) -> None:
    context = require_authenticated_context(request)
    state = get_app_state(request)
    state.web_push_subscription_service.delete_current(
        user_id=context.user.user_id,
        auth_session_id=context.session_id,
        endpoint=payload.endpoint,
    )
    response.status_code = status.HTTP_204_NO_CONTENT


__all__ = ["router"]
