from __future__ import annotations

import json
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

_LOGGER = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    """Raised when an email cannot be delivered without exposing transport details."""

    def __init__(
        self,
        message: str = "email delivery failed",
        *,
        code: str = "email_delivery_failed",
        transient: bool = True,
    ) -> None:
        self.code = code
        self.transient = transient
        super().__init__(message)


class EmailSender(Protocol):
    def send_email_verification(self, *, email: str, token: str, language: str = "en") -> None:
        """Deliver an email verification token to a registered address."""

    def send_password_reset(self, *, email: str, token: str, language: str = "en") -> None:
        """Deliver a password reset token to a registered address."""


class NotificationEmailSender(Protocol):
    def send_notification(self, *, email: str, subject: str, body: str) -> None:
        """Deliver a plaintext notification email."""


@dataclass(frozen=True, slots=True)
class DevelopmentEmailSender:
    expose_verification_tokens: bool = False
    public_web_base_url: str = "http://localhost:3000"

    def send_email_verification(self, *, email: str, token: str, language: str = "en") -> None:
        if self.expose_verification_tokens:
            _LOGGER.warning("Development email verification token for %s: %s", email, token)
            return
        _LOGGER.info("Email verification requested for %s; SMTP delivery is not configured.", email)

    def send_password_reset(self, *, email: str, token: str, language: str = "en") -> None:
        if self.expose_verification_tokens:
            _LOGGER.warning("Development password reset token for %s: %s", email, token)
            return
        _LOGGER.info("Password reset requested for %s; SMTP delivery is not configured.", email)

    def send_notification(self, *, email: str, subject: str, body: str) -> None:
        _LOGGER.info("Notification email requested for %s; SMTP delivery is not configured.", email)


@dataclass(frozen=True, slots=True)
class SmtpEmailSender:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    from_email: str
    from_name: str
    public_web_base_url: str
    tls_mode: str = "starttls"
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.tls_mode not in {"starttls", "ssl"}:
            raise ValueError("tls_mode must be 'starttls' or 'ssl'")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def send_email_verification(self, *, email: str, token: str, language: str = "en") -> None:
        link = f"{self.public_web_base_url}/auth/verify?token={quote(token, safe='')}"
        subject = _notification("notification.auth.verification.subject", language)
        body = _notification("notification.auth.verification.body", language).format(link=link)
        self._send(to_email=email, subject=subject, body=body)

    def send_password_reset(self, *, email: str, token: str, language: str = "en") -> None:
        link = f"{self.public_web_base_url}/auth/reset-password?token={quote(token, safe='')}"
        subject = _notification("notification.auth.reset.subject", language)
        body = _notification("notification.auth.reset.body", language).format(link=link)
        self._send(to_email=email, subject=subject, body=body)

    def send_notification(self, *, email: str, subject: str, body: str) -> None:
        self._send(to_email=email, subject=subject, body=body)

    def _send(self, *, to_email: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{self.from_name} <{self.from_email}>"
        message["To"] = to_email
        message.set_content(body)
        try:
            if self.tls_mode == "ssl":
                with smtplib.SMTP_SSL(
                    self.host,
                    self.port,
                    timeout=self.timeout_seconds,
                    context=ssl.create_default_context(),
                ) as client:
                    self._authenticate(client)
                    client.send_message(message)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as client:
                    client.ehlo()
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                    self._authenticate(client)
                    client.send_message(message)
        except (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
        ) as error:
            raise EmailDeliveryError(code="smtp_rejected", transient=False) from error
        except smtplib.SMTPResponseException as error:
            raise EmailDeliveryError(
                code="smtp_rejected" if error.smtp_code >= 500 else "smtp_transport_error",
                transient=error.smtp_code < 500,
            ) from error
        except (OSError, TimeoutError, smtplib.SMTPException) as error:
            raise EmailDeliveryError(code="smtp_transport_error", transient=True) from error
        except Exception as error:
            raise EmailDeliveryError(code="email_delivery_failed", transient=False) from error

    def _authenticate(self, client: smtplib.SMTP) -> None:
        if self.username:
            client.login(self.username, self.password)


def _notification(key: str, language: str) -> str:
    source_root = Path(__file__).resolve().parents[5] / "i18n"
    requested = language.strip() if language else "en"
    locales = [requested, "en"] if requested != "en" else ["en"]
    for locale in locales:
        catalog_path = source_root / "locales" / locale / "notifications.json"
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            entry = catalog.get(key)
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                return entry["text"]
        except (OSError, json.JSONDecodeError):
            continue
    try:
        source = json.loads((source_root / "source.json").read_text(encoding="utf-8"))
        entry = source.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("default-text"), str):
            return entry["default-text"]
    except (OSError, json.JSONDecodeError):
        pass
    return key


def notification_text(key: str, language: str = "en") -> str:
    """Resolve a canonical English/localized notification catalog string."""

    return _notification(key, language)


__all__ = [
    "DevelopmentEmailSender",
    "EmailDeliveryError",
    "EmailSender",
    "NotificationEmailSender",
    "notification_text",
    "SmtpEmailSender",
]
