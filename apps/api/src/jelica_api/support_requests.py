from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jelica_api.models import SupportRequest

_OPEN_SUPPORT_REQUEST_STATUS = "open"
_CLOSED_SUPPORT_REQUEST_STATUS = "closed"
_ALLOWED_SUPPORT_REQUEST_STATUSES = frozenset(
    {_OPEN_SUPPORT_REQUEST_STATUS, _CLOSED_SUPPORT_REQUEST_STATUS}
)


@dataclass(frozen=True, slots=True)
class SupportRequestRecord:
    request_id: str
    name: str
    email: str
    subject: str
    message: str
    created_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class SupportRequestStore:
    session_factory: sessionmaker[Session]

    def create_request(
        self,
        *,
        name: str,
        email: str,
        subject: str,
        message: str,
    ) -> SupportRequestRecord:
        normalized_name = _require_non_empty_text(value=name, field_name="name")
        normalized_email = _normalize_email(value=email)
        normalized_subject = _require_non_empty_text(value=subject, field_name="subject")
        normalized_message = _require_non_empty_text(value=message, field_name="message")

        with self.session_factory() as session:
            request = SupportRequest(
                name=normalized_name,
                email=normalized_email,
                subject=normalized_subject,
                message=normalized_message,
                status=_OPEN_SUPPORT_REQUEST_STATUS,
            )
            session.add(request)
            session.commit()
            session.refresh(request)
            return _to_support_request_record(request=request)

    def get_request(self, *, request_id: str) -> SupportRequestRecord | None:
        normalized_request_id = _require_non_empty_text(
            value=request_id,
            field_name="request_id",
        )
        with self.session_factory() as session:
            request = _load_request(session=session, request_id=normalized_request_id)
            if request is None:
                return None
            return _to_support_request_record(request=request)


def _load_request(*, session: Session, request_id: str) -> SupportRequest | None:
    statement = select(SupportRequest).where(SupportRequest.id == request_id)
    return session.execute(statement).scalar_one_or_none()


def _require_non_empty_text(*, value: str, field_name: str) -> str:
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _normalize_email(*, value: str) -> str:
    normalized = _require_non_empty_text(value=value, field_name="email")
    if "@" not in normalized:
        raise ValueError("email must contain '@'")
    return normalized


def _to_support_request_record(*, request: SupportRequest) -> SupportRequestRecord:
    status = request.status.strip().lower()
    if status not in _ALLOWED_SUPPORT_REQUEST_STATUSES:
        raise ValueError(f"support request has unsupported status '{request.status}'")
    return SupportRequestRecord(
        request_id=request.id,
        name=request.name,
        email=request.email,
        subject=request.subject,
        message=request.message,
        created_at=request.created_at,
        status=status,
    )


__all__ = ["SupportRequestRecord", "SupportRequestStore"]
