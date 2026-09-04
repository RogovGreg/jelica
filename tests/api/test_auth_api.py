from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy import select
from starlette.requests import Request

from jelica_api.api.routes.auth import (
    AUTH_SESSION_COOKIE_NAME,
    login,
    logout,
    me,
    register,
    resend_verification,
    update_me,
    verify_email,
)
from jelica_api.app import create_app
from jelica_api.auth import (
    Argon2idPasswordHasher,
    AuthService,
    InvalidAccountTokenError,
    InvalidCredentialsError,
    hash_opaque_token,
)
from jelica_api.contracts import (
    AuthEmailRequest,
    AuthLoginRequest,
    AuthMeUpdateRequest,
    AuthRegisterRequest,
    AuthRegisterResponse,
    AuthSessionResponse,
    AuthVerifyEmailRequest,
)
from jelica_api.models import AccountToken, AuthSession, Base, User
from jelica_api.settings import ApiSettings

_USERNAME = "ada"
_EMAIL = "ada.researcher@example.org"
_PASSWORD = "correct horse battery staple"


class _CaptureEmailSender:
    def __init__(self) -> None:
        self.verification_tokens: list[str] = []
        self.reset_tokens: list[str] = []

    def send_email_verification(self, *, email: str, token: str, language: str = "en") -> None:
        self.verification_tokens.append(token)

    def send_password_reset(self, *, email: str, token: str, language: str = "en") -> None:
        self.reset_tokens.append(token)


def _settings() -> ApiSettings:
    return ApiSettings(
        app_name="JELICA Web Backend",
        api_host="127.0.0.1",
        api_port=8000,
        database_url="sqlite+pysqlite:///:memory:",
        cli_command_prefix=("jelica",),
        cli_timeout_seconds=30.0,
        auth_cookie_secure=True,
        auth_expose_dev_tokens=True,
    )


@pytest.fixture
def auth_app() -> Iterator[FastAPI]:
    app = create_app(settings=_settings())
    state = app.state.jelica_api_state
    Base.metadata.create_all(state.engine)
    try:
        yield app
    finally:
        state.task_orchestrator.shutdown()
        state.engine.dispose()


def _request_for_app(
    app: FastAPI,
    *,
    path: str,
    method: str,
    session_token: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if session_token is not None:
        headers.append(
            (
                b"cookie",
                f"{AUTH_SESSION_COOKIE_NAME}={session_token}".encode("ascii"),
            )
        )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers,
        "app": app,
    }
    return Request(scope)


def _register_user(app: FastAPI) -> tuple[AuthRegisterResponse, str]:
    payload = register(
        AuthRegisterRequest(
            username="Ada",
            email="Ada.Researcher@Example.ORG",
            password=_PASSWORD,
        ),
        _request_for_app(app, path="/api/auth/register", method="POST"),
    )
    verification_token = payload.verification_token
    assert verification_token is not None
    return payload, verification_token


def _verify_user(app: FastAPI, *, token: str) -> tuple[AuthSessionResponse, Response, str]:
    response = Response()
    payload = verify_email(
        AuthVerifyEmailRequest(token=token),
        _request_for_app(app, path="/api/auth/verify-email", method="POST"),
        response,
    )
    return payload, response, _cookie_value(response=response)


def _cookie_value(*, response: Response) -> str:
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    return cookies[AUTH_SESSION_COOKIE_NAME].value


def _cookie_attributes(*, response: Response) -> set[str]:
    return {part.strip().lower() for part in response.headers["set-cookie"].split(";")[1:]}


def test_password_reset_replaces_token_and_revokes_sessions(auth_app: FastAPI) -> None:
    state = auth_app.state.jelica_api_state
    sender = _CaptureEmailSender()
    service = AuthService(store=state.auth_store, email_sender=sender)
    result = service.register(username="Ada", email=_EMAIL, password=_PASSWORD)
    service.verify_email(token=result.verification_token)
    service.request_password_reset(email=_EMAIL)
    first_token = sender.reset_tokens[-1]
    service.request_password_reset(email=_EMAIL)
    second_token = sender.reset_tokens[-1]
    assert first_token != second_token
    with pytest.raises(InvalidAccountTokenError):
        service.reset_password(token=first_token, password="new password")
    service.reset_password(token=second_token, password="new password")
    with state.session_factory() as session:
        assert session.execute(select(AuthSession)).scalars().all() == []
        account_tokens = session.execute(select(AccountToken)).scalars().all()
        assert all(
            token.type != "password_reset" or token.used_at is not None for token in account_tokens
        )
    with pytest.raises(InvalidCredentialsError):
        service.login(identifier=_EMAIL, password=_PASSWORD)
    assert service.login(identifier=_EMAIL, password="new password").user.email == _EMAIL


def test_create_app_registers_auth_routes(auth_app: FastAPI) -> None:
    paths = auth_app.openapi()["paths"]

    assert "post" in paths["/api/auth/register"]
    assert "post" in paths["/api/auth/verify-email"]
    assert "post" in paths["/api/auth/login"]
    assert "post" in paths["/api/auth/logout"]
    assert "get" in paths["/api/auth/me"]


def test_login_failed_identity_limit_and_successful_reset() -> None:
    app = create_app(
        settings=replace(
            _settings(),
            auth_rate_limit_login_client=100,
            auth_rate_limit_login_identity_failures=2,
        )
    )
    state = app.state.jelica_api_state
    Base.metadata.create_all(state.engine)
    try:
        _, token = _register_user(app)
        _verify_user(app, token=token)
        request = _request_for_app(app, path="/api/auth/login", method="POST")
        wrong = AuthLoginRequest(identifier=_EMAIL, password="wrong password")
        with pytest.raises(HTTPException) as first:
            login(wrong, request, Response())
        assert first.value.status_code == status.HTTP_401_UNAUTHORIZED

        login(AuthLoginRequest(identifier=_EMAIL, password=_PASSWORD), request, Response())

        with pytest.raises(HTTPException) as second:
            login(wrong, request, Response())
        assert second.value.status_code == status.HTTP_401_UNAUTHORIZED
        with pytest.raises(HTTPException) as third:
            login(wrong, request, Response())
        assert third.value.status_code == status.HTTP_401_UNAUTHORIZED
        with pytest.raises(HTTPException) as limited:
            login(wrong, request, Response())
        assert limited.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert limited.value.headers is not None
        assert int(limited.value.headers["Retry-After"]) > 0
    finally:
        state.task_orchestrator.shutdown()
        state.engine.dispose()


def test_resend_limit_keeps_existing_and_unknown_accounts_indistinguishable() -> None:
    app = create_app(
        settings=replace(
            _settings(),
            auth_rate_limit_resend_client=100,
            auth_rate_limit_resend_identity=1,
        )
    )
    state = app.state.jelica_api_state
    Base.metadata.create_all(state.engine)
    try:
        _register_user(app)
        request = _request_for_app(app, path="/api/auth/resend-verification", method="POST")
        existing = resend_verification(AuthEmailRequest(email=_EMAIL), request)
        unknown = resend_verification(AuthEmailRequest(email="unknown@example.org"), request)
        assert existing.message == unknown.message

        for email in (_EMAIL, "unknown@example.org"):
            with pytest.raises(HTTPException) as limited:
                resend_verification(AuthEmailRequest(email=email), request)
            assert limited.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
            assert limited.value.detail["error"] == "rate_limit_exceeded"
    finally:
        state.task_orchestrator.shutdown()
        state.engine.dispose()


def test_registration_creates_user_with_argon2id_hash_and_hashed_token(
    auth_app: FastAPI,
) -> None:
    response, raw_verification_token = _register_user(auth_app)
    session_factory = auth_app.state.jelica_api_state.session_factory

    with session_factory() as session:
        user = session.execute(select(User)).scalar_one()
        account_token = session.execute(select(AccountToken)).scalar_one()

        assert user.username == _USERNAME
        assert user.email == _EMAIL
        assert user.email_verified is False
        assert user.language == "en"
        assert user.theme == "light"
        assert user.interface_scale == 100
        assert user.password_hash != _PASSWORD
        assert _PASSWORD not in user.password_hash
        assert user.password_hash.startswith("$argon2id$")
        assert Argon2idPasswordHasher().verify(
            password_hash=user.password_hash,
            password=_PASSWORD,
        )
        assert "password" not in User.__table__.columns.keys()

        assert account_token.user_id == user.id
        assert account_token.type == "email_verification"
        assert account_token.token_hash != raw_verification_token
        assert account_token.token_hash == hash_opaque_token(raw_verification_token)

    assert response.email_verification_required is True
    assert response.user.email_verified is False
    assert response.user.theme == "light"
    assert response.user.interface_scale == 100
    assert _PASSWORD not in response.model_dump_json()


@pytest.mark.parametrize(
    "email",
    [
        "ada researcher@example.org",
        "ada\nresearcher@example.org",
        "ada\x00researcher@example.org",
    ],
)
def test_registration_rejects_email_whitespace_and_control_characters(email: str) -> None:
    with pytest.raises(ValidationError):
        AuthRegisterRequest(username="ada", email=email, password=_PASSWORD)


def test_verify_email_consumes_token_and_creates_hashed_session_cookie(
    auth_app: FastAPI,
) -> None:
    _, verification_token = _register_user(auth_app)
    payload, response, raw_session_token = _verify_user(
        auth_app,
        token=verification_token,
    )
    session_factory = auth_app.state.jelica_api_state.session_factory

    with session_factory() as session:
        user = session.execute(select(User)).scalar_one()
        account_token = session.execute(select(AccountToken)).scalar_one()
        auth_session = session.execute(select(AuthSession)).scalar_one()

        assert user.email_verified is True
        assert account_token.used_at is not None
        assert auth_session.user_id == user.id
        assert auth_session.token_hash != raw_session_token
        assert auth_session.token_hash == hash_opaque_token(raw_session_token)

    assert payload.user.email_verified is True
    assert raw_session_token != ""
    assert {"httponly", "path=/", "samesite=lax", "secure"}.issubset(
        _cookie_attributes(response=response)
    )

    with pytest.raises(HTTPException) as raised:
        verify_email(
            AuthVerifyEmailRequest(token=verification_token),
            _request_for_app(auth_app, path="/api/auth/verify-email", method="POST"),
            Response(),
        )
    assert raised.value.status_code == status.HTTP_400_BAD_REQUEST
    assert raised.value.detail["error"] == "invalid_email_verification_token"


@pytest.mark.parametrize("identifier", ["ADA", "ADA.RESEARCHER@EXAMPLE.ORG"])
def test_login_by_username_or_email_creates_session(
    auth_app: FastAPI,
    identifier: str,
) -> None:
    _, verification_token = _register_user(auth_app)
    _verify_user(auth_app, token=verification_token)
    session_factory = auth_app.state.jelica_api_state.session_factory
    with session_factory() as session:
        session_count_before = len(session.execute(select(AuthSession)).scalars().all())

    response = Response()
    payload = login(
        AuthLoginRequest(identifier=identifier, password=_PASSWORD),
        _request_for_app(auth_app, path="/api/auth/login", method="POST"),
        response,
    )
    raw_session_token = _cookie_value(response=response)

    with session_factory() as session:
        stored_sessions = session.execute(select(AuthSession)).scalars().all()
        stored_hashes = {stored_session.token_hash for stored_session in stored_sessions}

    assert payload.user.username == _USERNAME
    assert len(stored_sessions) == session_count_before + 1
    assert raw_session_token not in stored_hashes
    assert hash_opaque_token(raw_session_token) in stored_hashes


def test_login_rejects_bad_credentials_with_generic_error(auth_app: FastAPI) -> None:
    _, verification_token = _register_user(auth_app)
    _verify_user(auth_app, token=verification_token)
    failures = [
        AuthLoginRequest(identifier=_USERNAME, password="wrong password"),
        AuthLoginRequest(identifier="unknown-user", password="wrong password"),
    ]
    details: list[object] = []

    for payload in failures:
        with pytest.raises(HTTPException) as raised:
            login(
                payload,
                _request_for_app(auth_app, path="/api/auth/login", method="POST"),
                Response(),
            )
        assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED
        details.append(raised.value.detail)

    assert details[0] == details[1]
    assert details[0] == {
        "error": "invalid_credentials",
        "message": "invalid username/email or password",
    }


def test_login_checks_dummy_argon2_hash_for_unknown_identifier(
    auth_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_hashes: list[str] = []

    def record_verification(
        _: Argon2idPasswordHasher,
        *,
        password_hash: str,
        password: str,
    ) -> bool:
        checked_hashes.append(password_hash)
        assert password == "wrong password"
        return False

    monkeypatch.setattr(Argon2idPasswordHasher, "verify", record_verification)

    with pytest.raises(HTTPException) as raised:
        login(
            AuthLoginRequest(identifier="unknown-user", password="wrong password"),
            _request_for_app(auth_app, path="/api/auth/login", method="POST"),
            Response(),
        )

    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert len(checked_hashes) == 1
    assert checked_hashes[0].startswith("$argon2id$")


def test_me_returns_current_user_for_session_cookie(auth_app: FastAPI) -> None:
    _, verification_token = _register_user(auth_app)
    _, _, raw_session_token = _verify_user(auth_app, token=verification_token)

    payload = me(
        _request_for_app(
            auth_app,
            path="/api/auth/me",
            method="GET",
            session_token=raw_session_token,
        )
    )

    assert payload.username == _USERNAME
    assert payload.email == _EMAIL
    assert payload.email_verified is True
    assert payload.language == "en"
    assert payload.theme == "light"
    assert payload.interface_scale == 100
    assert "password_hash" not in payload.model_dump()

    with pytest.raises(HTTPException) as raised:
        me(_request_for_app(auth_app, path="/api/auth/me", method="GET"))
    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert raised.value.detail["error"] == "authentication_required"


def test_me_preferences_are_authoritative_and_persisted(auth_app: FastAPI) -> None:
    _, verification_token = _register_user(auth_app)
    _, _, raw_session_token = _verify_user(auth_app, token=verification_token)
    request = _request_for_app(
        auth_app,
        path="/api/auth/me",
        method="PATCH",
        session_token=raw_session_token,
    )

    updated = update_me(
        AuthMeUpdateRequest(language="sr-Latn", theme="dark", interface_scale=150),
        request,
    )
    assert updated.language == "sr-Latn"
    assert updated.theme == "dark"
    assert updated.interface_scale == 150

    persisted = me(
        _request_for_app(
            auth_app,
            path="/api/auth/me",
            method="GET",
            session_token=raw_session_token,
        )
    )
    assert persisted.language == "sr-Latn"
    assert persisted.theme == "dark"
    assert persisted.interface_scale == 150

    with pytest.raises(ValidationError):
        AuthMeUpdateRequest(theme="invalid")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AuthMeUpdateRequest(interface_scale=90)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as raised:
        update_me(
            AuthMeUpdateRequest(theme="mono"),
            _request_for_app(auth_app, path="/api/auth/me", method="PATCH"),
        )
    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_logout_invalidates_session_and_clears_cookie(auth_app: FastAPI) -> None:
    _, verification_token = _register_user(auth_app)
    _, _, raw_session_token = _verify_user(auth_app, token=verification_token)
    session_factory = auth_app.state.jelica_api_state.session_factory

    response = Response()
    logout(
        _request_for_app(
            auth_app,
            path="/api/auth/logout",
            method="POST",
            session_token=raw_session_token,
        ),
        response,
    )

    with session_factory() as session:
        assert session.execute(select(AuthSession)).scalars().all() == []

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert _cookie_value(response=response) == ""
    assert {"httponly", "max-age=0", "path=/", "samesite=lax", "secure"}.issubset(
        _cookie_attributes(response=response)
    )

    with pytest.raises(HTTPException) as raised:
        me(
            _request_for_app(
                auth_app,
                path="/api/auth/me",
                method="GET",
                session_token=raw_session_token,
            )
        )
    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert raised.value.detail["error"] == "authentication_required"
