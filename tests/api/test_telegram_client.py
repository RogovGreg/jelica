from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from jelica_api.settings import ApiSettingsError, load_api_settings
from jelica_api.telegram_client import TelegramBotApiClient, TelegramDeliveryError


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self.body = json.dumps({"ok": True, "result": payload}).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_client_uses_https_and_canonical_webhook_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, object], float]] = []

    def fake_urlopen(request, timeout: float):
        captured.append((request.full_url, json.loads(request.data), timeout))
        return _Response(True)

    monkeypatch.setattr("jelica_api.telegram_client.urlopen", fake_urlopen)
    client = TelegramBotApiClient(bot_token="123:secret-value", timeout_seconds=7)
    client.set_webhook(
        url="https://jelica.example/api/integrations/telegram/webhook",
        secret_token="webhook-secret",
    )
    client.set_my_commands()
    assert captured[0][0].startswith("https://api.telegram.org/bot")
    assert captured[0][1] == {
        "url": "https://jelica.example/api/integrations/telegram/webhook",
        "secret_token": "webhook-secret",
        "allowed_updates": ["message", "callback_query"],
    }
    assert captured[0][2] == 7
    commands = captured[1][1]["commands"]
    assert [item["command"] for item in commands] == [
        "start",
        "status",
        "active_tasks",
        "active_project_tasks",
        "project_status",
        "disconnect",
        "help",
    ]


def test_client_returns_message_id_and_does_not_leak_token_in_errors_or_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "123:do-not-leak"
    client = TelegramBotApiClient(bot_token=token)
    monkeypatch.setattr(
        "jelica_api.telegram_client.urlopen", lambda request, timeout: _Response({"message_id": 42})
    )
    assert client.send_message(chat_id=7, text="hello") == 42
    assert token not in repr(client)

    def fail(request, timeout: float):
        raise URLError(f"network error involving {request.full_url}")

    monkeypatch.setattr("jelica_api.telegram_client.urlopen", fail)
    with pytest.raises(TelegramDeliveryError) as raised:
        client.send_message(chat_id=7, text="hello")
    assert token not in str(raised.value)
    assert raised.value.__cause__ is None


def test_telegram_settings_are_optional_complete_and_repr_hidden() -> None:
    base = {"DATABASE_URL": "postgresql+psycopg://jelica:test@localhost/jelica"}
    assert load_api_settings(base).telegram_configured is False
    with pytest.raises(ApiSettingsError, match="must be provided together"):
        load_api_settings({**base, "TELEGRAM_BOT_TOKEN": "123:secret"})
    settings = load_api_settings(
        {
            **base,
            "TELEGRAM_BOT_TOKEN": "123:secret",
            "TELEGRAM_BOT_USERNAME": "@JelicaBot",
            "TELEGRAM_WEBHOOK_SECRET": "header-secret_123",
            "PUBLIC_WEB_BASE_URL": "http://localhost:3000",
        }
    )
    assert settings.telegram_configured is True
    assert settings.telegram_bot_username == "JelicaBot"
    assert (
        settings.telegram_webhook_url == "http://localhost:3000/api/integrations/telegram/webhook"
    )
    assert "123:secret" not in repr(settings)
    assert "header-secret_123" not in repr(settings)
