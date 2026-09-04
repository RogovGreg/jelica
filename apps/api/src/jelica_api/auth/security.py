from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


@dataclass(frozen=True, slots=True)
class Argon2idPasswordHasher:
    _hasher: PasswordHasher = field(
        default_factory=lambda: PasswordHasher(type=Type.ID),
        repr=False,
    )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, *, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False


def generate_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def hash_opaque_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = ["Argon2idPasswordHasher", "generate_opaque_token", "hash_opaque_token"]
