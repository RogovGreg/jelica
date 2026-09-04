from __future__ import annotations

import base64
import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_EXPIRATION_TIME = 9_223_372_036_854_775_807


class WebPushSubscriptionKeys(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=128)

    @field_validator("p256dh")
    @classmethod
    def validate_p256dh(cls, value: str) -> str:
        normalized = value.strip()
        decoded = _decode_base64url(value=normalized, field_name="p256dh")
        if len(decoded) != 65 or decoded[0] != 4:
            raise ValueError("p256dh must be an uncompressed P-256 public key")
        return normalized

    @field_validator("auth")
    @classmethod
    def validate_auth(cls, value: str) -> str:
        normalized = value.strip()
        if len(_decode_base64url(value=normalized, field_name="auth")) != 16:
            raise ValueError("auth must decode to 16 bytes")
        return normalized


class WebPushSubscriptionUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str = Field(min_length=1, max_length=4096)
    expiration_time: Annotated[StrictInt, Field(ge=0, le=_MAX_EXPIRATION_TIME)] | None = None
    keys: WebPushSubscriptionKeys

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _validate_endpoint(value=value)


class WebPushSubscriptionDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str = Field(min_length=1, max_length=4096)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _validate_endpoint(value=value)


class WebPushConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    vapid_public_key: str | None
    active_subscription_count: int = Field(ge=0)
    current_session_subscription_count: int = Field(ge=0)


def _validate_endpoint(*, value: str) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
    ):
        raise ValueError("endpoint must be a secure Web Push URL")
    return normalized


def _decode_base64url(*, value: str, field_name: str) -> bytes:
    if not _BASE64URL_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be unpadded base64url")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{field_name} must be valid base64url") from error


__all__ = [
    "WebPushConfigResponse",
    "WebPushSubscriptionDeleteRequest",
    "WebPushSubscriptionKeys",
    "WebPushSubscriptionUpsertRequest",
]
