from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from jelica_api.auth import AuthService, AuthStore
from jelica_api.database import create_database_engine, create_session_factory
from jelica_api.models import AuthSession, User

_DATABASE_URL = os.environ.get("JELICA_POSTGRES_AUTH_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _DATABASE_URL.startswith("postgresql"),
    reason="dedicated PostgreSQL auth test database is not configured",
)


class _CaptureEmailSender:
    def __init__(self) -> None:
        self.verification_tokens: list[str] = []
        self.reset_tokens: list[str] = []

    def send_email_verification(self, *, email: str, token: str, language: str = "en") -> None:
        self.verification_tokens.append(token)

    def send_password_reset(self, *, email: str, token: str, language: str = "en") -> None:
        self.reset_tokens.append(token)


def test_postgres_auth_token_session_and_language_transactions() -> None:
    engine = create_database_engine(database_url=_DATABASE_URL)
    session_factory = create_session_factory(engine=engine)
    store = AuthStore(session_factory=session_factory)
    sender = _CaptureEmailSender()
    service = AuthService(store=store, email_sender=sender)
    suffix = uuid4().hex
    username = f"pg-{suffix}"
    email = f"pg-{suffix}@example.test"

    try:
        registered = service.register(username=username, email=email, password="password-123")
        assert registered.user.user_id
        assert sender.verification_tokens

        first_verification = sender.verification_tokens[-1]
        service.resend_verification(email=email)
        second_verification = sender.verification_tokens[-1]
        assert first_verification != second_verification

        verified = service.verify_email(token=second_verification)
        second_session = service.login(identifier=email, password="password-123")
        current = service.current_context(session_token=verified.token)
        assert current.user.user_id == registered.user.user_id

        sessions = store.list_active_sessions(
            user_id=registered.user.user_id, now=datetime.now(UTC)
        )
        assert {item.session_id for item in sessions} == {
            current.session_id,
            service.current_context(session_token=second_session.token).session_id,
        }

        updated = service.update_language(user_id=registered.user.user_id, language="sr-Latn")
        assert updated.language == "sr-Latn"

        second_id = service.current_context(session_token=second_session.token).session_id
        assert store.revoke_session(user_id=registered.user.user_id, session_id=second_id)
        remaining = store.revoke_other_sessions(
            user_id=registered.user.user_id, current_session_id=current.session_id
        )
        assert remaining == ()

        service.request_password_reset(email=email)
        service.reset_password(token=sender.reset_tokens[-1], password="replacement-123")
        assert (
            store.resolve_session(token_hash="not-a-token-hash", used_at=datetime.now(UTC)) is None
        )
        with session_factory() as session:
            assert (
                session.execute(
                    select(AuthSession).where(AuthSession.user_id == registered.user.user_id)
                )
                .scalars()
                .all()
                == []
            )
            user = session.get(User, registered.user.user_id)
            assert user is not None
            assert user.password_hash != "replacement-123"
            assert user.language == "sr-Latn"
    finally:
        with session_factory() as session:
            session.execute(delete(User).where(User.username == username))
            session.commit()
        engine.dispose()


def test_postgres_identity_unique_constraints() -> None:
    engine = create_database_engine(database_url=_DATABASE_URL)
    session_factory = create_session_factory(engine=engine)
    suffix = uuid4().hex
    username = f"unique-{suffix}"
    email = f"unique-{suffix}@example.test"
    try:
        with session_factory() as session:
            session.add(
                User(
                    username=username,
                    email=email,
                    password_hash="hash",
                    email_verified=False,
                    language="en",
                )
            )
            session.commit()
        with session_factory() as session:
            session.add(
                User(
                    username=username,
                    email=f"other-{email}",
                    password_hash="hash",
                    email_verified=False,
                    language="en",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
    finally:
        with session_factory() as session:
            session.execute(delete(User).where(User.username == username))
            session.commit()
        engine.dispose()
