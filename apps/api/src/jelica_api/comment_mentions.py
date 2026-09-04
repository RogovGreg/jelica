from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from jelica_api.auth.service import _normalize_username
from jelica_api.models import ProjectMember, User

_MENTION_TOKEN_CHARACTERS = frozenset({"_", "-"})


def parse_mention_usernames(body: str) -> tuple[str, ...]:
    """Return unique canonical usernames from mention-like body tokens."""
    candidates: list[str] = []
    seen: set[str] = set()
    for index, character in enumerate(body):
        if character != "@":
            continue
        if index > 0 and (body[index - 1] == "@" or _is_mention_token_character(body[index - 1])):
            continue
        end = index + 1
        while end < len(body) and _is_mention_token_character(body[end]):
            end += 1
        raw_username = body[index + 1 : end]
        if raw_username == "":
            continue
        try:
            username = _normalize_username(value=raw_username)
        except ValueError:
            continue
        if username not in seen:
            seen.add(username)
            candidates.append(username)
    return tuple(candidates)


def resolve_project_member_usernames(
    *,
    session: Session,
    project_id: str,
    usernames: tuple[str, ...],
) -> dict[str, User]:
    if not usernames:
        return {}
    users = session.execute(
        select(User)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .where(
            ProjectMember.project_id == project_id,
            User.username.in_(usernames),
        )
    ).scalars()
    return {user.username: user for user in users}


def _is_mention_token_character(character: str) -> bool:
    return character.isalnum() or character in _MENTION_TOKEN_CHARACTERS


__all__ = ["parse_mention_usernames", "resolve_project_member_usernames"]
