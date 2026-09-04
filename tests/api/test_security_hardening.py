from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status
from starlette.requests import Request

from jelica_api.api.routes.internal_reconciliation import get_reconciliation_report
from jelica_api.request_security import (
    FixedWindowRateLimiter,
    client_rate_limit_identity,
    request_origin_is_allowed,
)
from jelica_api.settings import ApiSettingsError, load_api_settings


def _request(
    *,
    method: str = "POST",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("192.0.2.5", 1234),
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "headers": headers or [],
            "client": client,
            "app": object(),
        }
    )


def test_fixed_window_limiter_is_deterministic_and_independent() -> None:
    now = [100.0]
    limiter = FixedWindowRateLimiter(window_seconds=10, clock=lambda: now[0])

    assert limiter.consume(keys=(("a", 2),)) is None
    assert limiter.consume(keys=(("a", 2),)) is None
    assert limiter.consume(keys=(("b", 1),)) is None
    assert limiter.consume(keys=(("a", 2),)) == 10
    assert limiter.consume(keys=(("b", 1),)) == 10

    now[0] = 110.0
    assert limiter.consume(keys=(("a", 2),)) is None


def test_fixed_window_limiter_clear_resets_failure_bucket() -> None:
    limiter = FixedWindowRateLimiter(window_seconds=60, clock=lambda: 10.0)
    limiter.record(key="identity")
    assert limiter.retry_after(key="identity", limit=1) == 60
    limiter.clear(key="identity")
    assert limiter.retry_after(key="identity", limit=1) is None


def test_fixed_window_limiter_concurrent_consumption_respects_limit() -> None:
    limiter = FixedWindowRateLimiter(window_seconds=60, clock=lambda: 10.0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: limiter.consume(keys=(("shared", 5),)),
                range(20),
            )
        )
    assert sum(result is None for result in results) == 5


def test_origin_guard_uses_configured_origin_and_fetch_metadata() -> None:
    public_url = "https://jelica.example/app"
    assert request_origin_is_allowed(
        request=_request(headers=[(b"origin", b"https://jelica.example")]),
        public_web_base_url=public_url,
    )
    assert not request_origin_is_allowed(
        request=_request(
            headers=[
                (b"origin", b"https://hostile.example"),
                (b"host", b"hostile.example"),
            ]
        ),
        public_web_base_url=public_url,
    )
    assert not request_origin_is_allowed(
        request=_request(headers=[(b"sec-fetch-site", b"cross-site")]),
        public_web_base_url=public_url,
    )
    assert request_origin_is_allowed(request=_request(), public_web_base_url=public_url)
    assert request_origin_is_allowed(
        request=_request(
            method="GET",
            headers=[(b"origin", b"https://hostile.example")],
        ),
        public_web_base_url=public_url,
    )


def test_client_identity_does_not_parse_forwarded_headers() -> None:
    first = client_rate_limit_identity(_request(headers=[(b"x-forwarded-for", b"198.51.100.1")]))
    second = client_rate_limit_identity(_request(headers=[(b"x-forwarded-for", b"203.0.113.9")]))
    assert first == second


@pytest.mark.parametrize(
    "overrides, message",
    [
        (
            {"PUBLIC_WEB_BASE_URL": "https://jelica.example", "AUTH_COOKIE_SECURE": "false"},
            "AUTH_COOKIE_SECURE",
        ),
        (
            {
                "PUBLIC_WEB_BASE_URL": "https://jelica.example",
                "AUTH_COOKIE_SECURE": "true",
                "AUTH_EXPOSE_DEV_TOKENS": "true",
            },
            "AUTH_EXPOSE_DEV_TOKENS",
        ),
        ({"INTERNAL_API_ENABLED": "true"}, "INTERNAL_API_TOKEN"),
    ],
)
def test_security_configuration_rejects_unsafe_combinations(
    overrides: dict[str, str], message: str
) -> None:
    environment = {
        "DATABASE_URL": "postgresql+psycopg://jelica:test@db/jelica",
        **overrides,
    }
    with pytest.raises(ApiSettingsError, match=message):
        load_api_settings(environment)


def test_http_local_configuration_allows_insecure_cookie() -> None:
    settings = load_api_settings(
        {
            "DATABASE_URL": "postgresql+psycopg://jelica:test@db/jelica",
            "PUBLIC_WEB_BASE_URL": "http://localhost:3000",
            "AUTH_COOKIE_SECURE": "false",
        }
    )
    assert settings.auth_cookie_secure is False


def test_web_push_vapid_configuration_is_atomic_and_private_key_is_repr_hidden() -> None:
    base_environment = {
        "DATABASE_URL": "postgresql+psycopg://jelica:test@db/jelica",
    }
    with pytest.raises(ApiSettingsError, match="must be provided together"):
        load_api_settings(
            {
                **base_environment,
                "WEB_PUSH_VAPID_PUBLIC_KEY": "public-key",
            }
        )

    private_key = "private-key-must-not-leak"
    settings = load_api_settings(
        {
            **base_environment,
            "WEB_PUSH_VAPID_PUBLIC_KEY": "public-key",
            "WEB_PUSH_VAPID_PRIVATE_KEY": private_key,
            "WEB_PUSH_VAPID_SUBJECT": "mailto:notifications@example.test",
        }
    )
    assert settings.web_push_configured is True
    assert private_key not in repr(settings)


def test_web_push_is_honestly_unavailable_without_vapid_configuration() -> None:
    settings = load_api_settings(
        {
            "DATABASE_URL": "postgresql+psycopg://jelica:test@db/jelica",
        }
    )
    assert settings.web_push_configured is False


@pytest.mark.parametrize(
    "enabled, presented",
    [(False, "test-secret"), (True, ""), (True, "wrong")],
)
def test_internal_reconciliation_hides_unauthorized_requests(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, presented: str
) -> None:
    state = SimpleNamespace(
        settings=SimpleNamespace(
            internal_api_enabled=enabled,
            internal_api_token="test-secret",
        ),
        web_task_reconciler=SimpleNamespace(get_diagnostics=lambda: None),
    )
    monkeypatch.setattr(
        "jelica_api.api.routes.internal_reconciliation.get_app_state", lambda _: state
    )
    headers = [(b"x-jelica-internal-token", presented.encode())] if presented else []
    with pytest.raises(HTTPException) as raised:
        get_reconciliation_report(_request(method="GET", headers=headers))
    assert raised.value.status_code == status.HTTP_404_NOT_FOUND
