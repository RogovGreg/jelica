from __future__ import annotations

from fastapi import HTTPException, Request, status

from jelica_api.app_state import get_app_state
from jelica_api.auth import AuthenticatedContext, AuthenticationRequiredError, UserRecord

AUTH_SESSION_COOKIE_NAME = "jelica_session"


def require_current_user(request: Request) -> UserRecord:
    return require_authenticated_context(request).user


def require_authenticated_context(request: Request) -> AuthenticatedContext:
    state = get_app_state(request)
    raw_session_token = request.cookies.get(AUTH_SESSION_COOKIE_NAME, "")
    try:
        return state.auth_service.current_context(session_token=raw_session_token)
    except (AuthenticationRequiredError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "authentication_required",
                "message": "Authentication is required.",
            },
        ) from error


def optional_current_user(request: Request) -> UserRecord | None:
    raw_session_token = request.cookies.get(AUTH_SESSION_COOKIE_NAME, "")
    if raw_session_token.strip() == "":
        return None
    state = get_app_state(request)
    try:
        return state.auth_service.current_user(session_token=raw_session_token)
    except (AuthenticationRequiredError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "authentication_required",
                "message": "Authentication is required.",
            },
        ) from error


__all__ = [
    "AUTH_SESSION_COOKIE_NAME",
    "optional_current_user",
    "require_current_user",
    "require_authenticated_context",
]
