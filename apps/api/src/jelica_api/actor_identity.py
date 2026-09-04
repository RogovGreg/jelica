from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request, Response

from jelica_api.auth import UserRecord, generate_opaque_token, hash_opaque_token

GUEST_SESSION_COOKIE_NAME = "jelica_guest_session"


@dataclass(frozen=True, slots=True)
class WebActorIdentity:
    user_id: str | None = None
    guest_session_hash: str | None = None

    def __post_init__(self) -> None:
        if self.user_id is not None and self.guest_session_hash is not None:
            raise ValueError("A Web actor cannot be authenticated and guest at once.")


def actor_identity_for_request(
    *, request: Request, current_user: UserRecord | None
) -> WebActorIdentity:
    if current_user is not None:
        return WebActorIdentity(user_id=current_user.user_id)
    raw_guest_session = request.cookies.get(GUEST_SESSION_COOKIE_NAME, "")
    if raw_guest_session.strip() == "":
        return WebActorIdentity()
    return WebActorIdentity(guest_session_hash=hash_opaque_token(raw_guest_session))


def guest_identity_hash_for_creation(*, request: Request, response: Response, secure: bool) -> str:
    raw_guest_session = request.cookies.get(GUEST_SESSION_COOKIE_NAME, "")
    if raw_guest_session.strip() == "":
        raw_guest_session = generate_opaque_token()
        response.set_cookie(
            key=GUEST_SESSION_COOKIE_NAME,
            value=raw_guest_session,
            path="/",
            secure=secure,
            httponly=True,
            samesite="lax",
        )
    return hash_opaque_token(raw_guest_session)


__all__ = [
    "GUEST_SESSION_COOKIE_NAME",
    "WebActorIdentity",
    "actor_identity_for_request",
    "guest_identity_hash_for_creation",
]
