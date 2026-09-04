from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from jelica_api.models import AccountToken, AuthSession, User

EMAIL_VERIFICATION_TOKEN_TYPE = "email_verification"
PASSWORD_RESET_TOKEN_TYPE = "password_reset"


class AuthIdentityConflictError(ValueError):
    """Raised when a username or email already belongs to an account."""

    def __init__(self, *, field: str | None = None) -> None:
        self.field = field
        message = "username or email is already registered"
        if field is not None:
            message = f"{field} is already registered"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class UserRecord:
    user_id: str
    username: str
    email: str
    email_verified: bool
    language: str
    theme: str
    interface_scale: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserCredentialsRecord:
    user: UserRecord
    password_hash: str


@dataclass(frozen=True, slots=True)
class CreatedSessionRecord:
    user: UserRecord
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    user_id: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthStore:
    session_factory: sessionmaker[Session]

    def create_user_with_verification_token(
        self,
        *,
        username: str,
        email: str,
        password_hash: str,
        token_hash: str,
        created_at: datetime,
        token_expires_at: datetime,
    ) -> UserRecord:
        with self.session_factory() as session:
            conflict_field = _find_conflict_field(
                session=session,
                username=username,
                email=email,
            )
            if conflict_field is not None:
                raise AuthIdentityConflictError(field=conflict_field)

            user = User(
                username=username,
                email=email,
                password_hash=password_hash,
                email_verified=False,
                language="en",
                theme="light",
                interface_scale=100,
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(user)
            try:
                session.flush()
                session.add(
                    AccountToken(
                        user_id=user.id,
                        type=EMAIL_VERIFICATION_TOKEN_TYPE,
                        token_hash=token_hash,
                        created_at=created_at,
                        expires_at=token_expires_at,
                    )
                )
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise AuthIdentityConflictError() from error
            session.refresh(user)
            return _to_user_record(user=user)

    def get_user_credentials(self, *, identifier: str) -> UserCredentialsRecord | None:
        with self.session_factory() as session:
            if "@" in identifier:
                statement = select(User).where(User.email == identifier)
            else:
                statement = select(User).where(User.username == identifier)
            user = session.execute(statement).scalar_one_or_none()
            if user is None:
                return None
            return UserCredentialsRecord(
                user=_to_user_record(user=user),
                password_hash=user.password_hash,
            )

    def create_session(
        self,
        *,
        user_id: str,
        token_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> CreatedSessionRecord:
        with self.session_factory() as session:
            user = session.get(User, user_id)
            if user is None:
                raise RuntimeError("cannot create an auth session for an unknown user")
            session.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=token_hash,
                    created_at=created_at,
                    expires_at=expires_at,
                    last_used_at=created_at,
                )
            )
            session.commit()
            return CreatedSessionRecord(
                user=_to_user_record(user=user),
                created_at=created_at,
                expires_at=expires_at,
            )

    def verify_email_and_create_session(
        self,
        *,
        verification_token_hash: str,
        session_token_hash: str,
        verified_at: datetime,
        session_expires_at: datetime,
    ) -> CreatedSessionRecord | None:
        with self.session_factory() as session:
            statement = (
                select(AccountToken)
                .where(
                    AccountToken.token_hash == verification_token_hash,
                    AccountToken.type == EMAIL_VERIFICATION_TOKEN_TYPE,
                    AccountToken.used_at.is_(None),
                    AccountToken.expires_at > verified_at,
                )
                .with_for_update()
            )
            account_token = session.execute(statement).scalar_one_or_none()
            if account_token is None:
                return None
            user = session.get(User, account_token.user_id)
            if user is None:
                return None

            account_token.used_at = verified_at
            user.email_verified = True
            user.updated_at = verified_at
            session.execute(
                update(AccountToken)
                .where(
                    AccountToken.user_id == user.id,
                    AccountToken.type == EMAIL_VERIFICATION_TOKEN_TYPE,
                    AccountToken.used_at.is_(None),
                    AccountToken.id != account_token.id,
                )
                .values(used_at=verified_at)
            )
            session.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=session_token_hash,
                    created_at=verified_at,
                    expires_at=session_expires_at,
                    last_used_at=verified_at,
                )
            )
            session.commit()
            return CreatedSessionRecord(
                user=_to_user_record(user=user),
                created_at=verified_at,
                expires_at=session_expires_at,
            )

    def create_verification_token(
        self,
        *,
        email: str,
        token_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> UserRecord | None:
        with self.session_factory() as session:
            user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if user is None:
                return None
            if user.email_verified:
                return _to_user_record(user=user)
            session.execute(
                update(AccountToken)
                .where(
                    AccountToken.user_id == user.id,
                    AccountToken.type == EMAIL_VERIFICATION_TOKEN_TYPE,
                    AccountToken.used_at.is_(None),
                )
                .values(used_at=created_at)
            )
            session.add(
                AccountToken(
                    user_id=user.id,
                    type=EMAIL_VERIFICATION_TOKEN_TYPE,
                    token_hash=token_hash,
                    created_at=created_at,
                    expires_at=expires_at,
                )
            )
            session.commit()
            return _to_user_record(user=user)

    def create_password_reset_token(
        self,
        *,
        email: str,
        token_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> UserRecord | None:
        with self.session_factory() as session:
            user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if user is None:
                return None
            session.execute(
                update(AccountToken)
                .where(
                    AccountToken.user_id == user.id,
                    AccountToken.type == PASSWORD_RESET_TOKEN_TYPE,
                    AccountToken.used_at.is_(None),
                )
                .values(used_at=created_at)
            )
            session.add(
                AccountToken(
                    user_id=user.id,
                    type=PASSWORD_RESET_TOKEN_TYPE,
                    token_hash=token_hash,
                    created_at=created_at,
                    expires_at=expires_at,
                )
            )
            session.commit()
            return _to_user_record(user=user)

    def reset_password(
        self,
        *,
        token_hash: str,
        password_hash: str,
        reset_at: datetime,
    ) -> bool:
        result = self.reset_password_and_revoke_sessions(
            token_hash=token_hash, password_hash=password_hash, reset_at=reset_at
        )
        return result is not None

    def reset_password_and_revoke_sessions(
        self,
        *,
        token_hash: str,
        password_hash: str,
        reset_at: datetime,
    ) -> tuple[str, tuple[str, ...]] | None:
        with self.session_factory() as session:
            statement = (
                select(AccountToken)
                .where(
                    AccountToken.token_hash == token_hash,
                    AccountToken.type == PASSWORD_RESET_TOKEN_TYPE,
                    AccountToken.used_at.is_(None),
                    AccountToken.expires_at > reset_at,
                )
                .with_for_update()
            )
            account_token = session.execute(statement).scalar_one_or_none()
            if account_token is None:
                return None
            user = session.get(User, account_token.user_id, with_for_update=True)
            if user is None:
                return None
            user.password_hash = password_hash
            user.updated_at = reset_at
            account_token.used_at = reset_at
            session.execute(
                update(AccountToken)
                .where(
                    AccountToken.user_id == user.id,
                    AccountToken.type == PASSWORD_RESET_TOKEN_TYPE,
                    AccountToken.used_at.is_(None),
                )
                .values(used_at=reset_at)
            )
            session_ids = tuple(
                row[0]
                for row in session.execute(
                    select(AuthSession.id).where(AuthSession.user_id == user.id)
                ).all()
            )
            session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
            session.commit()
            return user.id, session_ids

    def update_user_language(
        self, *, user_id: str, language: str, updated_at: datetime
    ) -> UserRecord | None:
        return self.update_user_preferences(
            user_id=user_id,
            language=language,
            theme=None,
            interface_scale=None,
            updated_at=updated_at,
        )

    def update_user_preferences(
        self,
        *,
        user_id: str,
        language: str | None,
        theme: str | None,
        interface_scale: int | None,
        updated_at: datetime,
    ) -> UserRecord | None:
        with self.session_factory() as session:
            user = session.get(User, user_id)
            if user is None:
                return None
            if language is not None:
                user.language = language
            if theme is not None:
                user.theme = theme
            if interface_scale is not None:
                user.interface_scale = interface_scale
            user.updated_at = updated_at
            session.commit()
            session.refresh(user)
            return _to_user_record(user=user)

    def list_active_sessions(self, *, user_id: str, now: datetime) -> tuple[SessionRecord, ...]:
        with self.session_factory() as session:
            rows = (
                session.execute(
                    select(AuthSession)
                    .where(AuthSession.user_id == user_id, AuthSession.expires_at > now)
                    .order_by(AuthSession.last_used_at.desc(), AuthSession.id.asc())
                )
                .scalars()
                .all()
            )
            return tuple(
                SessionRecord(
                    session_id=row.id,
                    user_id=row.user_id,
                    created_at=_as_utc(value=row.created_at),
                    last_used_at=_as_utc(value=row.last_used_at),
                    expires_at=_as_utc(value=row.expires_at),
                )
                for row in rows
            )

    def revoke_session(self, *, user_id: str, session_id: str) -> bool:
        with self.session_factory() as session:
            result = session.execute(
                delete(AuthSession).where(
                    AuthSession.user_id == user_id, AuthSession.id == session_id
                )
            )
            session.commit()
            return bool(result.rowcount)

    def revoke_other_sessions(self, *, user_id: str, current_session_id: str) -> tuple[str, ...]:
        with self.session_factory() as session:
            ids = tuple(
                row[0]
                for row in session.execute(
                    select(AuthSession.id).where(
                        AuthSession.user_id == user_id, AuthSession.id != current_session_id
                    )
                ).all()
            )
            if ids:
                session.execute(
                    delete(AuthSession).where(
                        AuthSession.user_id == user_id, AuthSession.id != current_session_id
                    )
                )
            session.commit()
            return ids

    def session_id_for_token(self, *, token_hash: str) -> str | None:
        with self.session_factory() as session:
            row = session.execute(
                select(AuthSession.id).where(AuthSession.token_hash == token_hash)
            ).scalar_one_or_none()
            return row

    def resolve_session(self, *, token_hash: str, used_at: datetime) -> UserRecord | None:
        context = self.resolve_session_context(token_hash=token_hash, used_at=used_at)
        return context[0] if context is not None else None

    def resolve_session_context(
        self, *, token_hash: str, used_at: datetime
    ) -> tuple[UserRecord, str] | None:
        with self.session_factory() as session:
            statement = select(AuthSession).where(
                AuthSession.token_hash == token_hash,
                AuthSession.expires_at > used_at,
            )
            auth_session = session.execute(statement).scalar_one_or_none()
            if auth_session is None:
                return None
            user = session.get(User, auth_session.user_id)
            if user is None:
                return None
            auth_session.last_used_at = used_at
            session.commit()
            return _to_user_record(user=user), auth_session.id

    def invalidate_session(self, *, token_hash: str) -> bool:
        with self.session_factory() as session:
            result = session.execute(
                delete(AuthSession).where(AuthSession.token_hash == token_hash)
            )
            session.commit()
            return bool(result.rowcount)


def _find_conflict_field(*, session: Session, username: str, email: str) -> str | None:
    username_exists = session.execute(
        select(User.id).where(User.username == username)
    ).scalar_one_or_none()
    if username_exists is not None:
        return "username"
    email_exists = session.execute(select(User.id).where(User.email == email)).scalar_one_or_none()
    if email_exists is not None:
        return "email"
    return None


def _to_user_record(*, user: User) -> UserRecord:
    return UserRecord(
        user_id=user.id,
        username=user.username,
        email=user.email,
        email_verified=user.email_verified,
        language=user.language,
        theme=user.theme,
        interface_scale=user.interface_scale,
        created_at=_as_utc(value=user.created_at),
        updated_at=_as_utc(value=user.updated_at),
    )


def _as_utc(*, value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "AuthIdentityConflictError",
    "AuthStore",
    "CreatedSessionRecord",
    "EMAIL_VERIFICATION_TOKEN_TYPE",
    "PASSWORD_RESET_TOKEN_TYPE",
    "UserCredentialsRecord",
    "UserRecord",
    "SessionRecord",
]
