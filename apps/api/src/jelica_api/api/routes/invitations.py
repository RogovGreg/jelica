from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import status as http_status

from jelica_api.api.authentication import require_current_user
from jelica_api.app_state import get_app_state
from jelica_api.contracts.invitations import (
    ProjectInvitationCandidateListResponse,
    ProjectInvitationCandidateResponse,
    ProjectInvitationCreateRequest,
    ProjectInvitationListResponse,
    ProjectInvitationResponse,
    ProjectInvitationStatus,
)
from jelica_api.contracts.projects import ProjectMemberRole
from jelica_api.projects import (
    ProjectConflictError,
    ProjectDomainError,
    ProjectInvitationCandidateRecord,
    ProjectInvitationNotFoundError,
    ProjectInvitationRecord,
    ProjectInvitationTargetNotFoundError,
    ProjectMemberNotFoundError,
    ProjectNotFoundError,
    ProjectPermissionError,
    ProjectTaskNotFoundError,
    ProjectValidationError,
)

router = APIRouter(tags=["project-invitations"])

_RecordT = TypeVar("_RecordT")


@router.post(
    "/api/projects/{project_id}/invitations",
    response_model=ProjectInvitationResponse,
    status_code=http_status.HTTP_201_CREATED,
)
def create_project_invitation(
    project_id: str,
    payload: ProjectInvitationCreateRequest,
    request: Request,
) -> ProjectInvitationResponse:
    current_user = require_current_user(request)
    invitation = _run_invitation_operation(
        lambda: get_app_state(request).project_service.create_invitation(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            invited_user_id=payload.invited_user_id,
            role=payload.role,
        )
    )
    return _to_invitation_response(record=invitation)


@router.get(
    "/api/projects/{project_id}/invitations",
    response_model=ProjectInvitationListResponse,
)
def list_project_invitations(
    project_id: str,
    request: Request,
    status: Annotated[list[ProjectInvitationStatus] | None, Query()] = None,
    role: Annotated[list[ProjectMemberRole] | None, Query()] = None,
    invited_user_id: Annotated[list[str] | None, Query()] = None,
) -> ProjectInvitationListResponse:
    current_user = require_current_user(request)
    invitations = _run_invitation_operation(
        lambda: get_app_state(request).project_service.list_project_invitations(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            statuses=tuple(status or ()),
            roles=tuple(role or ()),
            invited_user_ids=tuple(invited_user_id or ()),
        )
    )
    return ProjectInvitationListResponse(
        items=tuple(_to_invitation_response(record=invitation) for invitation in invitations)
    )


@router.get(
    "/api/projects/{project_id}/invitation-candidates",
    response_model=ProjectInvitationCandidateListResponse,
)
def list_project_invitation_candidates(
    project_id: str,
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=64)],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> ProjectInvitationCandidateListResponse:
    current_user = require_current_user(request)
    candidates = _run_invitation_operation(
        lambda: get_app_state(request).project_service.list_invitation_candidates(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            query=q,
            limit=limit,
        )
    )
    return ProjectInvitationCandidateListResponse(
        items=tuple(_to_invitation_candidate_response(record=item) for item in candidates)
    )


@router.post(
    "/api/projects/{project_id}/invitations/{invitation_id}/revoke",
    response_model=ProjectInvitationResponse,
)
def revoke_project_invitation(
    project_id: str,
    invitation_id: str,
    request: Request,
) -> ProjectInvitationResponse:
    current_user = require_current_user(request)
    invitation = _run_invitation_operation(
        lambda: get_app_state(request).project_service.revoke_invitation(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            invitation_id=invitation_id,
        )
    )
    return _to_invitation_response(record=invitation)


@router.get("/api/invitations", response_model=ProjectInvitationListResponse)
def list_received_invitations(
    request: Request,
    status: Annotated[list[ProjectInvitationStatus] | None, Query()] = None,
    role: Annotated[list[ProjectMemberRole] | None, Query()] = None,
    project_id: Annotated[list[str] | None, Query()] = None,
) -> ProjectInvitationListResponse:
    current_user = require_current_user(request)
    invitations = _run_invitation_operation(
        lambda: get_app_state(request).project_service.list_received_invitations(
            actor_user_id=current_user.user_id,
            statuses=tuple(status or ()),
            roles=tuple(role or ()),
            project_ids=tuple(project_id or ()),
        )
    )
    return ProjectInvitationListResponse(
        items=tuple(_to_invitation_response(record=invitation) for invitation in invitations)
    )


@router.post(
    "/api/invitations/{invitation_id}/accept",
    response_model=ProjectInvitationResponse,
)
def accept_project_invitation(
    invitation_id: str,
    request: Request,
) -> ProjectInvitationResponse:
    current_user = require_current_user(request)
    state = get_app_state(request)
    invitation = _run_invitation_operation(
        lambda: state.project_service.accept_invitation(
            actor_user_id=current_user.user_id,
            invitation_id=invitation_id,
        )
    )
    state.realtime_publisher.member_joined_sync(
        project_id=invitation.project_id,
        user_id=invitation.invited_user_id,
        username=invitation.invited_username,
        role=invitation.role,
    )
    for task_id in state.task_discussion_service.list_project_task_ids(
        project_id=invitation.project_id
    ):
        state.task_realtime_hub.run_from_sync(
            state.task_realtime_hub.broadcast(
                project_id=task_id,
                message={
                    "type": "member.joined",
                    "user_id": invitation.invited_user_id,
                    "username": invitation.invited_username,
                    "role": invitation.role,
                },
            )
        )
    return _to_invitation_response(record=invitation)


@router.post(
    "/api/invitations/{invitation_id}/decline",
    response_model=ProjectInvitationResponse,
)
def decline_project_invitation(
    invitation_id: str,
    request: Request,
) -> ProjectInvitationResponse:
    current_user = require_current_user(request)
    invitation = _run_invitation_operation(
        lambda: get_app_state(request).project_service.decline_invitation(
            actor_user_id=current_user.user_id,
            invitation_id=invitation_id,
        )
    )
    return _to_invitation_response(record=invitation)


def _run_invitation_operation(operation: Callable[[], _RecordT]) -> _RecordT:
    try:
        return operation()
    except ProjectDomainError as error:
        raise _http_from_project_error(error=error) from error


def _http_from_project_error(*, error: ProjectDomainError) -> HTTPException:
    if isinstance(
        error,
        (
            ProjectNotFoundError,
            ProjectMemberNotFoundError,
            ProjectTaskNotFoundError,
            ProjectInvitationNotFoundError,
            ProjectInvitationTargetNotFoundError,
        ),
    ):
        status_code = http_status.HTTP_404_NOT_FOUND
    elif isinstance(error, ProjectPermissionError):
        status_code = http_status.HTTP_403_FORBIDDEN
    elif isinstance(error, ProjectConflictError):
        status_code = http_status.HTTP_409_CONFLICT
    elif isinstance(error, ProjectValidationError):
        status_code = http_status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        status_code = http_status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={"error": error.code, "message": str(error)},
    )


def _to_invitation_response(*, record: ProjectInvitationRecord) -> ProjectInvitationResponse:
    return ProjectInvitationResponse(
        invitation_id=record.invitation_id,
        project_id=record.project_id,
        project_name=record.project_name,
        invited_user_id=record.invited_user_id,
        invited_username=record.invited_username,
        invited_by_user_id=record.invited_by_user_id,
        inviter_username=record.inviter_username,
        role=record.role,
        status=record.status,
        invited_at=record.invited_at,
        expires_at=record.expires_at,
        resolved_at=record.resolved_at,
    )


def _to_invitation_candidate_response(
    *, record: ProjectInvitationCandidateRecord
) -> ProjectInvitationCandidateResponse:
    return ProjectInvitationCandidateResponse(
        user_id=record.user_id,
        username=record.username,
    )


__all__ = ["router"]
