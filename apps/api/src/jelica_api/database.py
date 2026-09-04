from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker


class DatabaseUnavailableError(RuntimeError):
    """Raised when the backend cannot reach PostgreSQL."""


def create_database_engine(*, database_url: str) -> Engine:
    return create_engine(
        database_url,
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(*, engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def probe_database(*, engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        raise DatabaseUnavailableError(f"PostgreSQL connectivity check failed: {error}") from error


__all__ = [
    "DatabaseUnavailableError",
    "create_database_engine",
    "create_session_factory",
    "probe_database",
]
