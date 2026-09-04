from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_DEFAULT_APP_NAME = "JELICA Web Backend"
_DEFAULT_API_HOST = "0.0.0.0"
_DEFAULT_API_PORT = 8000
_DEFAULT_CLI_TIMEOUT_SECONDS = 120.0
_DEFAULT_CLI_COMMAND_PREFIX = "uv run --package jelica-cli jelica"
_DEFAULT_AUTH_COOKIE_SECURE = False
_DEFAULT_AUTH_EXPOSE_DEV_TOKENS = False
_DEFAULT_UPLOAD_ROOT = Path("/var/lib/jelica/web-storage/analysis-uploads")
# Provisional transport defaults sized for genomic inputs; deployments can override every limit.
_DEFAULT_UPLOAD_MAX_FILE_BYTES = 10 * 1024**3
_DEFAULT_UPLOAD_MAX_SESSION_BYTES = 100 * 1024**3
_DEFAULT_UPLOAD_MAX_SESSION_FILES = 10_000
_DEFAULT_UPLOAD_MAX_RELATIVE_PATH_LENGTH = 1_024
_DEFAULT_UPLOAD_SESSION_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_EMAIL_DELIVERY_MODE = "development"
_DEFAULT_SMTP_PORT = 587
_DEFAULT_SMTP_TLS_MODE = "starttls"
_DEFAULT_SMTP_TIMEOUT_SECONDS = 15.0
_DEFAULT_PUBLIC_WEB_BASE_URL = "http://localhost:3000"
_DEFAULT_TELEGRAM_TIMEOUT_SECONDS = 10.0
_DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 15 * 60


class ApiSettingsError(ValueError):
    """Raised when backend settings are invalid."""


@dataclass(frozen=True, slots=True)
class ApiSettings:
    app_name: str
    api_host: str
    api_port: int
    database_url: str
    cli_command_prefix: tuple[str, ...]
    cli_timeout_seconds: float
    auth_cookie_secure: bool = _DEFAULT_AUTH_COOKIE_SECURE
    auth_expose_dev_tokens: bool = _DEFAULT_AUTH_EXPOSE_DEV_TOKENS
    upload_root: Path = _DEFAULT_UPLOAD_ROOT
    upload_max_file_bytes: int = _DEFAULT_UPLOAD_MAX_FILE_BYTES
    upload_max_session_bytes: int = _DEFAULT_UPLOAD_MAX_SESSION_BYTES
    upload_max_session_files: int = _DEFAULT_UPLOAD_MAX_SESSION_FILES
    upload_max_relative_path_length: int = _DEFAULT_UPLOAD_MAX_RELATIVE_PATH_LENGTH
    upload_session_ttl_seconds: int = _DEFAULT_UPLOAD_SESSION_TTL_SECONDS
    email_delivery_mode: str = _DEFAULT_EMAIL_DELIVERY_MODE
    public_web_base_url: str = _DEFAULT_PUBLIC_WEB_BASE_URL
    smtp_host: str = ""
    smtp_port: int = _DEFAULT_SMTP_PORT
    smtp_username: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from_email: str = ""
    smtp_from_name: str = _DEFAULT_APP_NAME
    smtp_tls_mode: str = _DEFAULT_SMTP_TLS_MODE
    smtp_timeout_seconds: float = _DEFAULT_SMTP_TIMEOUT_SECONDS
    password_reset_ttl_seconds: int = 60 * 60
    internal_api_enabled: bool = False
    internal_api_token: str = field(default="", repr=False)
    auth_rate_limit_enabled: bool = True
    auth_rate_limit_window_seconds: int = _DEFAULT_RATE_LIMIT_WINDOW_SECONDS
    auth_rate_limit_login_client: int = 60
    auth_rate_limit_login_identity_failures: int = 10
    auth_rate_limit_register_client: int = 10
    auth_rate_limit_resend_client: int = 30
    auth_rate_limit_resend_identity: int = 5
    auth_rate_limit_forgot_client: int = 30
    auth_rate_limit_forgot_identity: int = 5
    auth_rate_limit_verify_client: int = 30
    auth_rate_limit_reset_client: int = 30
    web_push_vapid_public_key: str = ""
    web_push_vapid_private_key: str = field(default="", repr=False)
    web_push_vapid_subject: str = ""
    telegram_bot_token: str = field(default="", repr=False)
    telegram_bot_username: str = ""
    telegram_webhook_secret: str = field(default="", repr=False)
    telegram_timeout_seconds: float = _DEFAULT_TELEGRAM_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        parsed_public_url = urlsplit(self.public_web_base_url)
        if parsed_public_url.scheme == "https":
            if not self.auth_cookie_secure:
                raise ApiSettingsError(
                    "AUTH_COOKIE_SECURE must be true when PUBLIC_WEB_BASE_URL uses HTTPS."
                )
            if self.auth_expose_dev_tokens:
                raise ApiSettingsError(
                    "AUTH_EXPOSE_DEV_TOKENS must be false when PUBLIC_WEB_BASE_URL uses HTTPS."
                )
        if self.internal_api_enabled and not self.internal_api_token.strip():
            raise ApiSettingsError(
                "INTERNAL_API_TOKEN must be provided when INTERNAL_API_ENABLED=true."
            )
        web_push_values = (
            self.web_push_vapid_public_key.strip(),
            self.web_push_vapid_private_key.strip(),
            self.web_push_vapid_subject.strip(),
        )
        if any(web_push_values) and not all(web_push_values):
            raise ApiSettingsError(
                "WEB_PUSH_VAPID_PUBLIC_KEY, WEB_PUSH_VAPID_PRIVATE_KEY, and "
                "WEB_PUSH_VAPID_SUBJECT must be provided together."
            )
        if self.web_push_vapid_subject and not _is_vapid_subject(self.web_push_vapid_subject):
            raise ApiSettingsError("WEB_PUSH_VAPID_SUBJECT must be an https URL or mailto URI.")
        telegram_values = (
            self.telegram_bot_token.strip(),
            self.telegram_bot_username.strip(),
            self.telegram_webhook_secret.strip(),
        )
        if any(telegram_values) and not all(telegram_values):
            raise ApiSettingsError(
                "TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME, and TELEGRAM_WEBHOOK_SECRET "
                "must be provided together."
            )
        if self.telegram_bot_username and not re.fullmatch(
            r"[A-Za-z0-9_]{5,32}", self.telegram_bot_username
        ):
            raise ApiSettingsError("TELEGRAM_BOT_USERNAME is invalid.")
        if self.telegram_webhook_secret and not re.fullmatch(
            r"[A-Za-z0-9_-]{1,256}", self.telegram_webhook_secret
        ):
            raise ApiSettingsError("TELEGRAM_WEBHOOK_SECRET is invalid.")
        if self.telegram_timeout_seconds <= 0:
            raise ApiSettingsError("TELEGRAM_TIMEOUT_SECONDS must be > 0.")
        for name in (
            "auth_rate_limit_window_seconds",
            "auth_rate_limit_login_client",
            "auth_rate_limit_login_identity_failures",
            "auth_rate_limit_register_client",
            "auth_rate_limit_resend_client",
            "auth_rate_limit_resend_identity",
            "auth_rate_limit_forgot_client",
            "auth_rate_limit_forgot_identity",
            "auth_rate_limit_verify_client",
            "auth_rate_limit_reset_client",
        ):
            if getattr(self, name) <= 0:
                raise ApiSettingsError(f"{name.upper()} must be > 0.")

    @property
    def web_push_configured(self) -> bool:
        return bool(
            self.web_push_vapid_public_key.strip()
            and self.web_push_vapid_private_key.strip()
            and self.web_push_vapid_subject.strip()
        )

    @property
    def telegram_configured(self) -> bool:
        return bool(
            self.telegram_bot_token.strip()
            and self.telegram_bot_username.strip()
            and self.telegram_webhook_secret.strip()
        )

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_web_base_url.rstrip('/')}/api/integrations/telegram/webhook"


def load_api_settings(environment: Mapping[str, str] | None = None) -> ApiSettings:
    env = os.environ if environment is None else environment
    app_name = env.get("API_APP_NAME", _DEFAULT_APP_NAME).strip() or _DEFAULT_APP_NAME
    api_host = env.get("API_HOST", _DEFAULT_API_HOST).strip() or _DEFAULT_API_HOST
    api_port = _parse_api_port(env.get("API_PORT", str(_DEFAULT_API_PORT)))
    database_url = _read_database_url(env=env)
    cli_command_prefix = _parse_cli_command_prefix(
        env.get("JELICA_CLI_COMMAND_PREFIX", _DEFAULT_CLI_COMMAND_PREFIX)
    )
    cli_timeout_seconds = _parse_positive_float(
        name="JELICA_CLI_TIMEOUT_SECONDS",
        raw_value=env.get("JELICA_CLI_TIMEOUT_SECONDS", str(_DEFAULT_CLI_TIMEOUT_SECONDS)),
    )
    auth_cookie_secure = _parse_boolean(
        name="AUTH_COOKIE_SECURE",
        raw_value=env.get("AUTH_COOKIE_SECURE", str(_DEFAULT_AUTH_COOKIE_SECURE)),
    )
    auth_expose_dev_tokens = _parse_boolean(
        name="AUTH_EXPOSE_DEV_TOKENS",
        raw_value=env.get(
            "AUTH_EXPOSE_DEV_TOKENS",
            str(_DEFAULT_AUTH_EXPOSE_DEV_TOKENS),
        ),
    )
    upload_root = _parse_absolute_path(
        name="JELICA_WEB_UPLOAD_ROOT",
        raw_value=env.get("JELICA_WEB_UPLOAD_ROOT", str(_DEFAULT_UPLOAD_ROOT)),
    )
    upload_max_file_bytes = _parse_positive_int(
        name="JELICA_UPLOAD_MAX_FILE_BYTES",
        raw_value=env.get("JELICA_UPLOAD_MAX_FILE_BYTES", str(_DEFAULT_UPLOAD_MAX_FILE_BYTES)),
    )
    upload_max_session_bytes = _parse_positive_int(
        name="JELICA_UPLOAD_MAX_SESSION_BYTES",
        raw_value=env.get(
            "JELICA_UPLOAD_MAX_SESSION_BYTES", str(_DEFAULT_UPLOAD_MAX_SESSION_BYTES)
        ),
    )
    upload_max_session_files = _parse_positive_int(
        name="JELICA_UPLOAD_MAX_SESSION_FILES",
        raw_value=env.get(
            "JELICA_UPLOAD_MAX_SESSION_FILES", str(_DEFAULT_UPLOAD_MAX_SESSION_FILES)
        ),
    )
    upload_max_relative_path_length = _parse_positive_int(
        name="JELICA_UPLOAD_MAX_RELATIVE_PATH_LENGTH",
        raw_value=env.get(
            "JELICA_UPLOAD_MAX_RELATIVE_PATH_LENGTH",
            str(_DEFAULT_UPLOAD_MAX_RELATIVE_PATH_LENGTH),
        ),
    )
    upload_session_ttl_seconds = _parse_positive_int(
        name="JELICA_UPLOAD_SESSION_TTL_SECONDS",
        raw_value=env.get(
            "JELICA_UPLOAD_SESSION_TTL_SECONDS",
            str(_DEFAULT_UPLOAD_SESSION_TTL_SECONDS),
        ),
    )
    email_delivery_mode = _parse_choice(
        name="EMAIL_DELIVERY_MODE",
        raw_value=env.get("EMAIL_DELIVERY_MODE", _DEFAULT_EMAIL_DELIVERY_MODE),
        choices={"development", "smtp"},
    )
    public_web_base_url = _parse_public_web_base_url(
        env.get("PUBLIC_WEB_BASE_URL", _DEFAULT_PUBLIC_WEB_BASE_URL)
    )
    smtp_tls_mode = _parse_choice(
        name="SMTP_TLS_MODE",
        raw_value=env.get("SMTP_TLS_MODE", _DEFAULT_SMTP_TLS_MODE),
        choices={"starttls", "ssl"},
    )
    smtp_host = env.get("SMTP_HOST", "").strip()
    smtp_port_raw = env.get("SMTP_PORT", str(_DEFAULT_SMTP_PORT)).strip()
    smtp_port = _parse_port(smtp_port_raw or str(_DEFAULT_SMTP_PORT))
    smtp_username = env.get("SMTP_USERNAME", env.get("SMTP_USER", "")).strip()
    smtp_password = env.get("SMTP_PASSWORD", "")
    smtp_from_email = env.get("SMTP_FROM_EMAIL", "").strip()
    smtp_from_name = env.get("SMTP_FROM_NAME", _DEFAULT_APP_NAME).strip() or _DEFAULT_APP_NAME
    smtp_timeout_seconds = _parse_positive_float(
        name="SMTP_TIMEOUT_SECONDS",
        raw_value=env.get("SMTP_TIMEOUT_SECONDS", str(_DEFAULT_SMTP_TIMEOUT_SECONDS)),
    )
    password_reset_ttl_seconds = _parse_positive_int(
        name="PASSWORD_RESET_TTL_SECONDS",
        raw_value=env.get("PASSWORD_RESET_TTL_SECONDS", str(60 * 60)),
    )
    internal_api_enabled = _parse_boolean(
        name="INTERNAL_API_ENABLED",
        raw_value=env.get("INTERNAL_API_ENABLED", "false"),
    )
    internal_api_token = env.get("INTERNAL_API_TOKEN", "")
    web_push_vapid_public_key = env.get("WEB_PUSH_VAPID_PUBLIC_KEY", "").strip()
    web_push_vapid_private_key = env.get("WEB_PUSH_VAPID_PRIVATE_KEY", "").strip()
    web_push_vapid_subject = env.get("WEB_PUSH_VAPID_SUBJECT", "").strip()
    telegram_bot_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_bot_username = env.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    telegram_webhook_secret = env.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    telegram_timeout_seconds = _parse_positive_float(
        name="TELEGRAM_TIMEOUT_SECONDS",
        raw_value=env.get("TELEGRAM_TIMEOUT_SECONDS", str(_DEFAULT_TELEGRAM_TIMEOUT_SECONDS)),
    )
    auth_rate_limit_enabled = _parse_boolean(
        name="AUTH_RATE_LIMIT_ENABLED",
        raw_value=env.get("AUTH_RATE_LIMIT_ENABLED", "true"),
    )
    rate_limit_values = {
        "auth_rate_limit_window_seconds": ("AUTH_RATE_LIMIT_WINDOW_SECONDS", 900),
        "auth_rate_limit_login_client": ("AUTH_RATE_LIMIT_LOGIN_CLIENT", 60),
        "auth_rate_limit_login_identity_failures": (
            "AUTH_RATE_LIMIT_LOGIN_IDENTITY_FAILURES",
            10,
        ),
        "auth_rate_limit_register_client": ("AUTH_RATE_LIMIT_REGISTER_CLIENT", 10),
        "auth_rate_limit_resend_client": ("AUTH_RATE_LIMIT_RESEND_CLIENT", 30),
        "auth_rate_limit_resend_identity": ("AUTH_RATE_LIMIT_RESEND_IDENTITY", 5),
        "auth_rate_limit_forgot_client": ("AUTH_RATE_LIMIT_FORGOT_CLIENT", 30),
        "auth_rate_limit_forgot_identity": ("AUTH_RATE_LIMIT_FORGOT_IDENTITY", 5),
        "auth_rate_limit_verify_client": ("AUTH_RATE_LIMIT_VERIFY_CLIENT", 30),
        "auth_rate_limit_reset_client": ("AUTH_RATE_LIMIT_RESET_CLIENT", 30),
    }
    parsed_rate_limits = {
        field_name: _parse_positive_int(name=env_name, raw_value=env.get(env_name, str(default)))
        for field_name, (env_name, default) in rate_limit_values.items()
    }
    if email_delivery_mode == "smtp":
        if not smtp_host:
            raise ApiSettingsError("SMTP_HOST must be provided when EMAIL_DELIVERY_MODE=smtp.")
        if not smtp_from_email:
            raise ApiSettingsError(
                "SMTP_FROM_EMAIL must be provided when EMAIL_DELIVERY_MODE=smtp."
            )
        if bool(smtp_username) != bool(smtp_password):
            raise ApiSettingsError("SMTP_USERNAME and SMTP_PASSWORD must be provided together.")
    return ApiSettings(
        app_name=app_name,
        api_host=api_host,
        api_port=api_port,
        database_url=database_url,
        cli_command_prefix=cli_command_prefix,
        cli_timeout_seconds=cli_timeout_seconds,
        auth_cookie_secure=auth_cookie_secure,
        auth_expose_dev_tokens=auth_expose_dev_tokens,
        upload_root=upload_root,
        upload_max_file_bytes=upload_max_file_bytes,
        upload_max_session_bytes=upload_max_session_bytes,
        upload_max_session_files=upload_max_session_files,
        upload_max_relative_path_length=upload_max_relative_path_length,
        upload_session_ttl_seconds=upload_session_ttl_seconds,
        email_delivery_mode=email_delivery_mode,
        public_web_base_url=public_web_base_url,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_from_email=smtp_from_email,
        smtp_from_name=smtp_from_name,
        smtp_tls_mode=smtp_tls_mode,
        smtp_timeout_seconds=smtp_timeout_seconds,
        password_reset_ttl_seconds=password_reset_ttl_seconds,
        internal_api_enabled=internal_api_enabled,
        internal_api_token=internal_api_token,
        web_push_vapid_public_key=web_push_vapid_public_key,
        web_push_vapid_private_key=web_push_vapid_private_key,
        web_push_vapid_subject=web_push_vapid_subject,
        telegram_bot_token=telegram_bot_token,
        telegram_bot_username=telegram_bot_username,
        telegram_webhook_secret=telegram_webhook_secret,
        telegram_timeout_seconds=telegram_timeout_seconds,
        auth_rate_limit_enabled=auth_rate_limit_enabled,
        **parsed_rate_limits,
    )


def _read_database_url(*, env: Mapping[str, str]) -> str:
    raw_database_url = env.get("DATABASE_URL")
    if raw_database_url is None or raw_database_url.strip() == "":
        raise ApiSettingsError("DATABASE_URL must be provided for PostgreSQL connection.")
    database_url = raw_database_url.strip()
    if not database_url.startswith("postgresql"):
        raise ApiSettingsError(
            "DATABASE_URL must start with a PostgreSQL SQLAlchemy scheme (postgresql...)."
        )
    return database_url


def _parse_api_port(raw_value: str) -> int:
    try:
        parsed = int(raw_value)
    except ValueError as error:
        raise ApiSettingsError(f"API_PORT must be an integer, got '{raw_value}'.") from error
    if parsed < 1 or parsed > 65535:
        raise ApiSettingsError(f"API_PORT must be in range 1..65535, got '{parsed}'.")
    return parsed


def _parse_port(raw_value: str) -> int:
    return _parse_api_port(raw_value)


def _parse_choice(*, name: str, raw_value: str, choices: set[str]) -> str:
    normalized = raw_value.strip().lower()
    if normalized not in choices:
        options = ", ".join(sorted(choices))
        raise ApiSettingsError(f"{name} must be one of {options}, got '{raw_value}'.")
    return normalized


def _parse_public_web_base_url(raw_value: str) -> str:
    value = raw_value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ApiSettingsError(
            "PUBLIC_WEB_BASE_URL must be an absolute http(s) URL without query or fragment."
        )
    return value


def _parse_positive_float(*, name: str, raw_value: str) -> float:
    try:
        parsed = float(raw_value)
    except ValueError as error:
        raise ApiSettingsError(f"{name} must be a number, got '{raw_value}'.") from error
    if parsed <= 0:
        raise ApiSettingsError(f"{name} must be > 0, got '{raw_value}'.")
    return parsed


def _parse_positive_int(*, name: str, raw_value: str) -> int:
    try:
        parsed = int(raw_value)
    except ValueError as error:
        raise ApiSettingsError(f"{name} must be an integer, got '{raw_value}'.") from error
    if parsed <= 0:
        raise ApiSettingsError(f"{name} must be > 0, got '{raw_value}'.")
    return parsed


def _parse_absolute_path(*, name: str, raw_value: str) -> Path:
    normalized = raw_value.strip()
    if normalized == "":
        raise ApiSettingsError(f"{name} must not be empty.")
    path = Path(normalized)
    if not path.is_absolute():
        raise ApiSettingsError(f"{name} must be an absolute path, got '{raw_value}'.")
    return path


def _parse_boolean(*, name: str, raw_value: str) -> bool:
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ApiSettingsError(
        f"{name} must be a boolean (true/false, yes/no, on/off, or 1/0), got '{raw_value}'."
    )


def _parse_cli_command_prefix(raw_value: str) -> tuple[str, ...]:
    try:
        parts = tuple(shlex.split(raw_value))
    except ValueError as error:
        raise ApiSettingsError(
            "JELICA_CLI_COMMAND_PREFIX contains invalid shell-like quoting."
        ) from error
    if len(parts) == 0:
        raise ApiSettingsError("JELICA_CLI_COMMAND_PREFIX must not be empty.")
    return parts


def _is_vapid_subject(value: str) -> bool:
    parsed = urlsplit(value.strip())
    if parsed.scheme == "mailto":
        return bool(parsed.path and "@" in parsed.path and not parsed.query and not parsed.fragment)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


__all__ = ["ApiSettings", "ApiSettingsError", "load_api_settings"]
