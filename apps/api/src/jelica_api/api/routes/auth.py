from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

from jelica_api.api.authentication import (
    AUTH_SESSION_COOKIE_NAME,
    require_authenticated_context,
    require_current_user,
)
from jelica_api.app_state import get_app_state
from jelica_api.auth import (
    AuthenticatedSession,
    AuthIdentityConflictError,
    EmailVerificationRequiredError,
    InvalidAccountTokenError,
    InvalidCredentialsError,
    UserRecord,
)
from jelica_api.contracts import (
    AuthActionResponse,
    AuthEmailRequest,
    AuthLoginRequest,
    AuthMeUpdateRequest,
    AuthRegisterRequest,
    AuthRegisterResponse,
    AuthResetPasswordRequest,
    AuthSessionListResponse,
    AuthSessionResponse,
    AuthSessionSummaryResponse,
    AuthUserResponse,
    AuthVerifyEmailRequest,
)
from jelica_api.request_security import (
    account_rate_limit_identity,
    client_rate_limit_identity,
    enforce_rate_limit,
    raise_rate_limited,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.post(
    "/register",
    response_model=AuthRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(payload: AuthRegisterRequest, request: Request) -> AuthRegisterResponse:
    state = get_app_state(request)
    _consume_client_limit(
        request=request,
        bucket="register",
        limit=state.settings.auth_rate_limit_register_client,
    )
    try:
        result = state.auth_service.register(
            username=payload.username,
            email=payload.email,
            password=payload.password,
        )
    except AuthIdentityConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "auth_identity_conflict",
                "message": str(error),
                "field": error.field,
            },
        ) from error
    return AuthRegisterResponse(
        user=_to_user_response(user=result.user),
        email_verification_required=True,
        verification_token=(
            result.verification_token
            if state.settings.auth_expose_dev_tokens
            and state.settings.email_delivery_mode == "development"
            else None
        ),
        email_delivery_failed=result.email_delivery_failed,
    )


@router.post("/resend-verification", response_model=AuthActionResponse)
def resend_verification(payload: AuthEmailRequest, request: Request) -> AuthActionResponse:
    state = get_app_state(request)
    _consume_client_and_identity_limit(
        request=request,
        bucket="resend",
        identity=payload.email,
        client_limit=state.settings.auth_rate_limit_resend_client,
        identity_limit=state.settings.auth_rate_limit_resend_identity,
    )
    state.auth_service.resend_verification(email=payload.email)
    return AuthActionResponse(
        message="If an account requires verification, a message will be sent."
    )


@router.post("/forgot-password", response_model=AuthActionResponse)
def forgot_password(payload: AuthEmailRequest, request: Request) -> AuthActionResponse:
    state = get_app_state(request)
    _consume_client_and_identity_limit(
        request=request,
        bucket="forgot",
        identity=payload.email,
        client_limit=state.settings.auth_rate_limit_forgot_client,
        identity_limit=state.settings.auth_rate_limit_forgot_identity,
    )
    state.auth_service.request_password_reset(email=payload.email)
    return AuthActionResponse(
        message="If an account matches, password reset instructions will be sent."
    )


@router.post("/reset-password", response_model=AuthActionResponse)
def reset_password(payload: AuthResetPasswordRequest, request: Request) -> AuthActionResponse:
    state = get_app_state(request)
    _consume_client_limit(
        request=request,
        bucket="reset",
        limit=state.settings.auth_rate_limit_reset_client,
    )
    try:
        _, revoked_ids = state.auth_service.reset_password_with_revoked_sessions(
            token=payload.token, password=payload.new_password
        )
    except InvalidAccountTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_password_reset_token", "message": str(error)},
        ) from error
    _evict_sessions(state=state, session_ids=revoked_ids)
    return AuthActionResponse(message="Password reset successfully. Please sign in again.")


@router.post("/verify-email", response_model=AuthSessionResponse)
def verify_email(
    payload: AuthVerifyEmailRequest,
    request: Request,
    response: Response,
) -> AuthSessionResponse:
    state = get_app_state(request)
    _consume_client_limit(
        request=request,
        bucket="verify",
        limit=state.settings.auth_rate_limit_verify_client,
    )
    try:
        authenticated_session = state.auth_service.verify_email(token=payload.token)
    except InvalidAccountTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_email_verification_token",
                "message": str(error),
            },
        ) from error
    _set_session_cookie(
        response=response,
        authenticated_session=authenticated_session,
        secure=state.settings.auth_cookie_secure,
    )
    return AuthSessionResponse(user=_to_user_response(user=authenticated_session.user))


@router.post("/login", response_model=AuthSessionResponse)
def login(
    payload: AuthLoginRequest,
    request: Request,
    response: Response,
) -> AuthSessionResponse:
    state = get_app_state(request)
    client_id = client_rate_limit_identity(request)
    identity_id = account_rate_limit_identity(payload.identifier)
    if state.settings.auth_rate_limit_enabled:
        enforce_rate_limit(
            limiter=state.auth_rate_limiter,
            keys=((f"login:client:{client_id}", state.settings.auth_rate_limit_login_client),),
        )
        retry_after = state.auth_rate_limiter.retry_after(
            key=f"login:identity:{identity_id}",
            limit=state.settings.auth_rate_limit_login_identity_failures,
        )
        if retry_after is not None:
            raise_rate_limited(retry_after=retry_after)
    try:
        authenticated_session = state.auth_service.login(
            identifier=payload.identifier,
            password=payload.password,
        )
    except InvalidCredentialsError as error:
        if state.settings.auth_rate_limit_enabled:
            state.auth_rate_limiter.record(key=f"login:identity:{identity_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_credentials", "message": str(error)},
        ) from error
    except EmailVerificationRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "email_verification_required", "message": str(error)},
        ) from error
    if state.settings.auth_rate_limit_enabled:
        state.auth_rate_limiter.clear(key=f"login:identity:{identity_id}")
    _set_session_cookie(
        response=response,
        authenticated_session=authenticated_session,
        secure=state.settings.auth_cookie_secure,
    )
    return AuthSessionResponse(user=_to_user_response(user=authenticated_session.user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response) -> None:
    state = get_app_state(request)
    raw_session_token = request.cookies.get(AUTH_SESSION_COOKIE_NAME, "")
    session_id = _session_id_for_token(state=state, token=raw_session_token)
    state.auth_service.logout(session_token=raw_session_token)
    if session_id is not None:
        _evict_sessions(state=state, session_ids=(session_id,))
    response.status_code = status.HTTP_204_NO_CONTENT
    response.delete_cookie(
        key=AUTH_SESSION_COOKIE_NAME,
        path="/",
        secure=state.settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.get("/me", response_model=AuthUserResponse)
def me(request: Request) -> AuthUserResponse:
    return _to_user_response(user=require_current_user(request))


@router.patch("/me", response_model=AuthUserResponse)
def update_me(payload: AuthMeUpdateRequest, request: Request) -> AuthUserResponse:
    context = require_authenticated_context(request)
    state = get_app_state(request)
    return _to_user_response(
        user=state.auth_service.update_preferences(
            user_id=context.user.user_id,
            language=payload.language,
            theme=payload.theme,
            interface_scale=payload.interface_scale,
        )
    )


@router.get("/sessions", response_model=AuthSessionListResponse)
def list_sessions(request: Request) -> AuthSessionListResponse:
    context = require_authenticated_context(request)
    state = get_app_state(request)
    sessions = state.auth_service.list_sessions(user_id=context.user.user_id)
    sessions = tuple(
        sorted(
            sessions,
            key=lambda item: (
                item.session_id != context.session_id,
                -item.last_used_at.timestamp(),
                item.session_id,
            ),
        )
    )
    return AuthSessionListResponse(
        items=tuple(
            AuthSessionSummaryResponse(
                id=item.session_id,
                created_at=item.created_at,
                last_used_at=item.last_used_at,
                expires_at=item.expires_at,
                current=item.session_id == context.session_id,
            )
            for item in sessions
        )
    )


@router.delete("/sessions/{session_id}", response_model=AuthActionResponse)
def revoke_session(session_id: str, request: Request) -> AuthActionResponse:
    context = require_authenticated_context(request)
    if session_id == context.session_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "session_not_found", "message": "Session not found."},
        )
    state = get_app_state(request)
    if not state.auth_store.revoke_session(user_id=context.user.user_id, session_id=session_id):
        raise HTTPException(
            status_code=404,
            detail={"error": "session_not_found", "message": "Session not found."},
        )
    _evict_sessions(state=state, session_ids=(session_id,))
    return AuthActionResponse(message="Session revoked.")


@router.post("/sessions/revoke-others", response_model=AuthActionResponse)
def revoke_other_sessions(request: Request) -> AuthActionResponse:
    context = require_authenticated_context(request)
    state = get_app_state(request)
    revoked_ids = state.auth_store.revoke_other_sessions(
        user_id=context.user.user_id, current_session_id=context.session_id
    )
    _evict_sessions(state=state, session_ids=revoked_ids)
    return AuthActionResponse(message="Other sessions revoked.")


def _session_id_for_token(*, state, token: str) -> str | None:
    if not token.strip():
        return None
    from jelica_api.auth import hash_opaque_token

    return state.auth_store.session_id_for_token(token_hash=hash_opaque_token(token.strip()))


def _consume_client_limit(*, request: Request, bucket: str, limit: int) -> None:
    state = get_app_state(request)
    if not state.settings.auth_rate_limit_enabled:
        return
    client_id = client_rate_limit_identity(request)
    enforce_rate_limit(
        limiter=state.auth_rate_limiter,
        keys=((f"{bucket}:client:{client_id}", limit),),
    )


def _consume_client_and_identity_limit(
    *,
    request: Request,
    bucket: str,
    identity: str,
    client_limit: int,
    identity_limit: int,
) -> None:
    state = get_app_state(request)
    if not state.settings.auth_rate_limit_enabled:
        return
    client_id = client_rate_limit_identity(request)
    identity_id = account_rate_limit_identity(identity)
    enforce_rate_limit(
        limiter=state.auth_rate_limiter,
        keys=(
            (f"{bucket}:client:{client_id}", client_limit),
            (f"{bucket}:identity:{identity_id}", identity_limit),
        ),
    )


def _evict_sessions(*, state, session_ids: tuple[str, ...]) -> None:
    for session_id in session_ids:
        hubs = (
            state.realtime_hub,
            state.task_realtime_hub,
            state.notification_realtime_hub,
        )
        for hub in hubs:
            try:
                hub.run_from_sync(hub.evict_auth_session(session_id=session_id))
            except Exception:
                logger.exception("realtime auth-session eviction failed")


def _set_session_cookie(
    *,
    response: Response,
    authenticated_session: AuthenticatedSession,
    secure: bool,
) -> None:
    response.set_cookie(
        key=AUTH_SESSION_COOKIE_NAME,
        value=authenticated_session.token,
        expires=authenticated_session.expires_at,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _to_user_response(*, user: UserRecord) -> AuthUserResponse:
    return AuthUserResponse(
        id=user.user_id,
        username=user.username,
        email=user.email,
        email_verified=user.email_verified,
        language=user.language,
        theme=user.theme,
        interface_scale=user.interface_scale,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


__all__ = ["AUTH_SESSION_COOKIE_NAME", "router"]
