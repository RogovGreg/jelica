from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from jelica_api.auth.email import EmailDeliveryError, SmtpEmailSender


def _sender(
    *, tls_mode: str = "starttls", username: str = "", password: str = ""
) -> SmtpEmailSender:
    return SmtpEmailSender(
        host="smtp.example.org",
        port=587,
        username=username,
        password=password,
        from_email="no-reply@example.org",
        from_name="JELICA",
        public_web_base_url="https://jelica.example",
        tls_mode=tls_mode,
    )


def test_smtp_starttls_sends_message_without_auth_when_unconfigured() -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    with patch("jelica_api.auth.email.smtplib.SMTP", return_value=client) as smtp:
        _sender().send_email_verification(email="user@example.org", token="raw token")
    smtp.assert_called_once_with("smtp.example.org", 587, timeout=15.0)
    client.starttls.assert_called_once()
    client.login.assert_not_called()
    message = client.send_message.call_args.args[0]
    assert message["To"] == "user@example.org"
    assert "https://jelica.example/auth/verify?token=raw%20token" in message.get_content()


def test_smtp_ssl_authenticates_only_when_credentials_are_configured() -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    with patch("jelica_api.auth.email.smtplib.SMTP_SSL", return_value=client) as smtp:
        _sender(tls_mode="ssl", username="smtp-user", password="smtp-secret").send_password_reset(
            email="user@example.org", token="reset-token"
        )
    smtp.assert_called_once()
    client.starttls.assert_not_called()
    client.login.assert_called_once_with("smtp-user", "smtp-secret")
    assert "smtp-secret" not in repr(
        _sender(tls_mode="ssl", username="smtp-user", password="smtp-secret")
    )


def test_smtp_transport_errors_are_typed() -> None:
    with patch("jelica_api.auth.email.smtplib.SMTP", side_effect=TimeoutError("secret")):
        with pytest.raises(EmailDeliveryError) as raised:
            _sender().send_email_verification(email="user@example.org", token="raw-token")
    assert "secret" not in str(raised.value)
    assert "raw-token" not in str(raised.value)
