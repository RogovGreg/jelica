from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from jelica_api.app_state import get_app_state
from jelica_api.contracts import SupportRequestCreateRequest, SupportRequestResponse
from jelica_api.support_requests import SupportRequestRecord

router = APIRouter(prefix="/api/support", tags=["support"])


@router.post("", response_model=SupportRequestResponse, status_code=status.HTTP_201_CREATED)
def create_support_request(
    payload: SupportRequestCreateRequest,
    request: Request,
) -> SupportRequestResponse:
    state = get_app_state(request)
    created = state.support_request_store.create_request(
        name=payload.name,
        email=payload.email,
        subject=payload.subject,
        message=payload.message,
    )
    return _to_support_response(record=created)


@router.get("/{id}", response_model=SupportRequestResponse)
def get_support_request(id: str, request: Request) -> SupportRequestResponse:
    normalized_request_id = _normalize_request_id(raw_request_id=id)
    state = get_app_state(request)
    support_request = state.support_request_store.get_request(request_id=normalized_request_id)
    if support_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "support_request_not_found",
                "message": f"Support request '{normalized_request_id}' was not found.",
            },
        )
    return _to_support_response(record=support_request)


def _normalize_request_id(*, raw_request_id: str) -> str:
    normalized = raw_request_id.strip()
    if normalized == "":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation_error", "message": "request_id must not be empty."},
        )
    return normalized


def _to_support_response(*, record: SupportRequestRecord) -> SupportRequestResponse:
    return SupportRequestResponse(
        id=record.request_id,
        name=record.name,
        email=record.email,
        subject=record.subject,
        message=record.message,
        created_at=record.created_at,
        status=record.status,
    )


__all__ = ["router"]
