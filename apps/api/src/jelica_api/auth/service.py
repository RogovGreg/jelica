from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .email import EmailDeliveryError, EmailSender
from .security import Argon2idPasswordHasher, generate_opaque_token, hash_opaque_token
from .store import AuthStore, SessionRecord, UserRecord

_EMAIL_VERIFICATION_TTL = timedelta(hours=24)
_PASSWORD_RESET_TTL = timedelta(hours=1)
_SESSION_TTL = timedelta(days=30)
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$0jv/COdz99biOW7I5sx9mA$"
    "wuzW2S2iAkUozBg1to3IN4ya2bI02Of9EvU4/9Ggcpw"
)


class InvalidCredentialsError(ValueError):
    """Raised when a login identifier/password pair is invalid."""


class EmailVerificationRequiredError(ValueError):
    """Raised when a valid account has not verified its email address."""


class InvalidAccountTokenError(ValueError):
    """Raised when an account token is invalid, expired, or already used."""


class AuthenticationRequiredError(ValueError):
    """Raised when an opaque session token does not resolve to a current user."""


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    user: UserRecord
    verification_token: str
    email_delivery_failed: bool = False


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    user: UserRecord
    token: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedContext:
    user: UserRecord
    session_id: str


@dataclass(frozen=True, slots=True)
class AuthService:
    store: AuthStore
    email_sender: EmailSender
    password_hasher: Argon2idPasswordHasher = field(default_factory=Argon2idPasswordHasher)
    token_factory: Callable[[], str] = generate_opaque_token
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    password_reset_ttl: timedelta = _PASSWORD_RESET_TTL

    def register(self, *, username: str, email: str, password: str) -> RegistrationResult:
        normalized_username = _normalize_username(value=username)
        normalized_email = _normalize_email(value=email)
        _validate_password(password=password)
        created_at = self.clock()
        raw_verification_token = self.token_factory()
        user = self.store.create_user_with_verification_token(
            username=normalized_username,
            email=normalized_email,
            password_hash=self.password_hasher.hash(password),
            token_hash=hash_opaque_token(raw_verification_token),
            created_at=created_at,
            token_expires_at=created_at + _EMAIL_VERIFICATION_TTL,
        )
        email_delivery_failed = False
        try:
            self.email_sender.send_email_verification(
                email=user.email, token=raw_verification_token, language=user.language
            )
        except EmailDeliveryError:
            email_delivery_failed = True
        return RegistrationResult(
            user=user,
            verification_token=raw_verification_token,
            email_delivery_failed=email_delivery_failed,
        )

    def resend_verification(self, *, email: str) -> None:
        normalized_email = _normalize_email(value=email)
        created_at = self.clock()
        raw_token = self.token_factory()
        user = self.store.create_verification_token(
            email=normalized_email,
            token_hash=hash_opaque_token(raw_token),
            created_at=created_at,
            expires_at=created_at + _EMAIL_VERIFICATION_TTL,
        )
        if user is None or user.email_verified:
            return
        try:
            self.email_sender.send_email_verification(
                email=user.email, token=raw_token, language=user.language
            )
        except EmailDeliveryError:
            return

    def request_password_reset(self, *, email: str) -> None:
        normalized_email = _normalize_email(value=email)
        created_at = self.clock()
        raw_token = self.token_factory()
        user = self.store.create_password_reset_token(
            email=normalized_email,
            token_hash=hash_opaque_token(raw_token),
            created_at=created_at,
            expires_at=created_at + self.password_reset_ttl,
        )
        if user is None:
            return
        try:
            self.email_sender.send_password_reset(
                email=user.email, token=raw_token, language=user.language
            )
        except EmailDeliveryError:
            return

    def reset_password(self, *, token: str, password: str) -> None:
        self.reset_password_with_revoked_sessions(token=token, password=password)

    def reset_password_with_revoked_sessions(
        self, *, token: str, password: str
    ) -> tuple[str, tuple[str, ...]]:
        normalized_token = _require_non_empty(value=token, field_name="token")
        _validate_password(password=password)
        reset_at = self.clock()
        result = self.store.reset_password_and_revoke_sessions(
            token_hash=hash_opaque_token(normalized_token),
            password_hash=self.password_hasher.hash(password),
            reset_at=reset_at,
        )
        if result is None:
            raise InvalidAccountTokenError("password reset token is invalid or expired")
        return result

    def verify_email(self, *, token: str) -> AuthenticatedSession:
        normalized_token = _require_non_empty(value=token, field_name="token")
        verified_at = self.clock()
        raw_session_token = self.token_factory()
        created_session = self.store.verify_email_and_create_session(
            verification_token_hash=hash_opaque_token(normalized_token),
            session_token_hash=hash_opaque_token(raw_session_token),
            verified_at=verified_at,
            session_expires_at=verified_at + _SESSION_TTL,
        )
        if created_session is None:
            raise InvalidAccountTokenError("email verification token is invalid or expired")
        return AuthenticatedSession(
            user=created_session.user,
            token=raw_session_token,
            created_at=created_session.created_at,
            expires_at=created_session.expires_at,
        )

    def login(self, *, identifier: str, password: str) -> AuthenticatedSession:
        normalized_identifier = _normalize_identifier(value=identifier)
        credentials = self.store.get_user_credentials(identifier=normalized_identifier)
        if credentials is None:
            self.password_hasher.verify(
                password_hash=_DUMMY_PASSWORD_HASH,
                password=password,
            )
            raise InvalidCredentialsError("invalid username/email or password")
        if not self.password_hasher.verify(
            password_hash=credentials.password_hash, password=password
        ):
            raise InvalidCredentialsError("invalid username/email or password")
        if not credentials.user.email_verified:
            raise EmailVerificationRequiredError("email address must be verified before login")

        created_at = self.clock()
        raw_session_token = self.token_factory()
        created_session = self.store.create_session(
            user_id=credentials.user.user_id,
            token_hash=hash_opaque_token(raw_session_token),
            created_at=created_at,
            expires_at=created_at + _SESSION_TTL,
        )
        return AuthenticatedSession(
            user=created_session.user,
            token=raw_session_token,
            created_at=created_session.created_at,
            expires_at=created_session.expires_at,
        )

    def current_user(self, *, session_token: str) -> UserRecord:
        return self.current_context(session_token=session_token).user

    def current_context(self, *, session_token: str) -> AuthenticatedContext:
        normalized_token = _require_non_empty(value=session_token, field_name="session token")
        context = self.store.resolve_session_context(
            token_hash=hash_opaque_token(normalized_token),
            used_at=self.clock(),
        )
        if context is None:
            raise AuthenticationRequiredError("authentication is required")
        return AuthenticatedContext(user=context[0], session_id=context[1])

    def logout(self, *, session_token: str) -> None:
        normalized_token = session_token.strip()
        if normalized_token == "":
            return
        self.store.invalidate_session(token_hash=hash_opaque_token(normalized_token))

    def update_language(self, *, user_id: str, language: str) -> UserRecord:
        return self.update_preferences(
            user_id=user_id,
            language=language,
            theme=None,
            interface_scale=None,
        )

    def update_preferences(
        self,
        *,
        user_id: str,
        language: str | None,
        theme: str | None,
        interface_scale: int | None,
    ) -> UserRecord:
        updated = self.store.update_user_preferences(
            user_id=user_id,
            language=language,
            theme=theme,
            interface_scale=interface_scale,
            updated_at=self.clock(),
        )
        if updated is None:
            raise AuthenticationRequiredError("authentication is required")
        return updated

    def list_sessions(self, *, user_id: str) -> tuple[SessionRecord, ...]:
        return self.store.list_active_sessions(user_id=user_id, now=self.clock())


def _normalize_username(*, value: str) -> str:
    normalized = _require_non_empty(value=value, field_name="username").lower()
    if len(normalized) > 64:
        raise ValueError("username must be at most 64 characters")
    if "@" in normalized or any(character.isspace() for character in normalized):
        raise ValueError("username must not contain whitespace or '@'")
    return normalized


def _normalize_email(*, value: str) -> str:
    normalized = _require_non_empty(value=value, field_name="email").casefold()
    if len(normalized) > 320:
        raise ValueError("email must be at most 320 characters")
    if any(character.isspace() or not character.isprintable() for character in normalized):
        raise ValueError("email must not contain whitespace or control characters")
    local_part, separator, domain = normalized.partition("@")
    if separator == "" or local_part == "" or domain == "" or "@" in domain:
        raise ValueError("email must be a valid address")
    return normalized


def _normalize_identifier(*, value: str) -> str:
    normalized = _require_non_empty(value=value, field_name="identifier")
    if "@" in normalized:
        return normalized.casefold()
    return normalized.lower()


def _validate_password(*, password: str) -> None:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(password) > 1024:
        raise ValueError("password must be at most 1024 characters")


def _require_non_empty(*, value: str, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{field_name} must not be empty")
    return normalized


__all__ = [
    "AuthenticatedSession",
    "AuthenticationRequiredError",
    "AuthService",
    "EmailVerificationRequiredError",
    "InvalidAccountTokenError",
    "InvalidCredentialsError",
    "RegistrationResult",
]
