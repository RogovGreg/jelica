from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AuthUserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=320)
    email_verified: bool
    language: str = Field(min_length=2, max_length=16)
    theme: Literal["system", "light", "dark", "mono"]
    interface_scale: Literal[80, 100, 125, 150]
    created_at: datetime
    updated_at: datetime


class AuthRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("username must not be empty")
        if "@" in normalized or any(character.isspace() for character in normalized):
            raise ValueError("username must not contain whitespace or '@'")
        return normalized

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        normalized = value.strip()
        if any(character.isspace() or not character.isprintable() for character in normalized):
            raise ValueError("email must not contain whitespace or control characters")
        local_part, separator, domain = normalized.partition("@")
        if separator == "" or local_part == "" or domain == "" or "@" in domain:
            raise ValueError("email must be a valid address")
        return normalized


class AuthRegisterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user: AuthUserResponse
    email_verification_required: bool
    verification_token: str | None = None
    email_delivery_failed: bool = False


class AuthEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        normalized = value.strip()
        if any(character.isspace() or not character.isprintable() for character in normalized):
            raise ValueError("email must not contain whitespace or control characters")
        return normalized


class AuthResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)

    @field_validator("token")
    @classmethod
    def _normalize_token(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("token must not be empty")
        return normalized


class AuthVerifyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(min_length=1, max_length=1024)

    @field_validator("token")
    @classmethod
    def _normalize_token(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("token must not be empty")
        return normalized


class AuthLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identifier: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("identifier")
    @classmethod
    def _normalize_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            raise ValueError("identifier must not be empty")
        return normalized


class AuthSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user: AuthUserResponse


class AuthActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str


SupportedLocale = Literal["en", "ru", "sr-Latn", "sr-Cyrl"]
SupportedTheme = Literal["system", "light", "dark", "mono"]
SupportedInterfaceScale = Literal[80, 100, 125, 150]


class AuthMeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    language: SupportedLocale | None = None
    theme: SupportedTheme | None = None
    interface_scale: SupportedInterfaceScale | None = None

    @model_validator(mode="after")
    def _require_preference(self) -> "AuthMeUpdateRequest":
        if self.language is None and self.theme is None and self.interface_scale is None:
            raise ValueError("at least one preference is required")
        return self


class AuthSessionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    current: bool


class AuthSessionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[AuthSessionSummaryResponse, ...]


__all__ = [
    "AuthLoginRequest",
    "AuthEmailRequest",
    "AuthActionResponse",
    "AuthRegisterRequest",
    "AuthRegisterResponse",
    "AuthResetPasswordRequest",
    "AuthSessionResponse",
    "AuthUserResponse",
    "AuthMeUpdateRequest",
    "AuthSessionSummaryResponse",
    "AuthSessionListResponse",
    "SupportedLocale",
    "AuthVerifyEmailRequest",
    "SupportedInterfaceScale",
    "SupportedTheme",
]
