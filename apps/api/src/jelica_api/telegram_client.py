from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_MAX_RESPONSE_BYTES = 64 * 1024
_BOT_API_BASE = "https://api.telegram.org"


class TelegramDeliveryError(RuntimeError):
    """Normalized Bot API failure that never includes credentials or raw responses."""

    def __init__(
        self,
        *,
        code: str = "telegram_delivery_failed",
        transient: bool,
        destination_unusable: bool = False,
    ) -> None:
        self.code = code
        self.transient = transient
        self.destination_unusable = destination_unusable
        super().__init__("Telegram delivery failed")


@dataclass(frozen=True, slots=True)
class TelegramBotApiClient:
    bot_token: str = field(repr=False)
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.bot_token.strip():
            raise ValueError("bot_token must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> int:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text[:4096]}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not _is_int64(message_id):
            raise TelegramDeliveryError(code="telegram_invalid_response", transient=False)
        return message_id

    def answer_callback_query(self, *, callback_query_id: str, text: str) -> None:
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id[:128], "text": text[:200]},
        )

    def edit_message_reply_markup(
        self, *, chat_id: int, message_id: int, reply_markup: dict[str, object]
    ) -> None:
        self._call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
            },
        )

    def set_webhook(self, *, url: str, secret_token: str) -> None:
        if not url.startswith("https://"):
            raise ValueError("Telegram webhook URL must use HTTPS")
        self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
            },
        )

    def set_my_commands(self) -> None:
        self._call(
            "setMyCommands",
            {
                "scope": {"type": "all_private_chats"},
                "commands": [
                    {"command": "start", "description": "Connect or check JELICA"},
                    {"command": "status", "description": "Show JELICA status"},
                    {"command": "active_tasks", "description": "Show your active tasks"},
                    {
                        "command": "active_project_tasks",
                        "description": "Show active project tasks",
                    },
                    {"command": "project_status", "description": "Show project status"},
                    {"command": "disconnect", "description": "Disconnect JELICA"},
                    {"command": "help", "description": "Show supported commands"},
                ],
            },
        )

    def get_me(self) -> dict[str, Any]:
        result = self._call("getMe", {})
        if not isinstance(result, dict):
            raise TelegramDeliveryError(code="telegram_invalid_response", transient=False)
        return {key: result[key] for key in ("id", "is_bot", "username") if key in result}

    def _call(self, method: str, payload: dict[str, object]) -> object:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{_BOT_API_BASE}/bot{self.bot_token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status_code: int | None = None
        response_body = b""
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                status_code = response.status
                response_body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status_code = error.code
            response_body = error.read(_MAX_RESPONSE_BYTES + 1)
        except (URLError, OSError, TimeoutError):
            raise TelegramDeliveryError(code="telegram_transport_error", transient=True) from None
        if len(response_body) > _MAX_RESPONSE_BYTES:
            raise TelegramDeliveryError(code="telegram_response_too_large", transient=False)
        try:
            decoded = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TelegramDeliveryError(
                code="telegram_invalid_response",
                transient=status_code is not None and status_code >= 500,
            ) from error
        if not isinstance(decoded, dict) or decoded.get("ok") is not True:
            error_code = decoded.get("error_code") if isinstance(decoded, dict) else status_code
            description = decoded.get("description") if isinstance(decoded, dict) else None
            raise _normalized_error(error_code=error_code, description=description)
        return decoded.get("result")


def _normalized_error(*, error_code: object, description: object) -> TelegramDeliveryError:
    code = error_code if isinstance(error_code, int) else 0
    normalized_description = description.casefold() if isinstance(description, str) else ""
    destination_unusable = code == 403 or (
        code == 400
        and any(
            marker in normalized_description
            for marker in ("chat not found", "user is deactivated", "bot was blocked")
        )
    )
    if destination_unusable:
        return TelegramDeliveryError(
            code="telegram_destination_unavailable",
            transient=False,
            destination_unusable=True,
        )
    if code == 429:
        return TelegramDeliveryError(code="telegram_rate_limited", transient=True)
    if code >= 500 or code == 0:
        return TelegramDeliveryError(code="telegram_transport_error", transient=True)
    return TelegramDeliveryError(code="telegram_request_rejected", transient=False)


def _is_int64(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and -(2**63) <= value < 2**63


__all__ = ["TelegramBotApiClient", "TelegramDeliveryError"]
