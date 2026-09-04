from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Request
from fastapi import status as http_status

from jelica_api.api.authentication import require_current_user
from jelica_api.app_state import get_app_state
from jelica_api.contracts.comments import (
    ProjectCommentCreateRequest,
    ProjectCommentListItemResponse,
    ProjectCommentListResponse,
    ProjectCommentReactionSummaryResponse,
    ProjectCommentReactionUpdateRequest,
    ProjectCommentResponse,
    ProjectCommentUpdateRequest,
)
from jelica_api.projects import (
    ProjectCommentNotFoundError,
    ProjectConflictError,
    ProjectDomainError,
    ProjectNotFoundError,
    ProjectPermissionError,
    ProjectValidationError,
)
from jelica_api.realtime import (
    comment_response_from_record,
    reaction_response_from_record,
)

router = APIRouter(prefix="/api/projects", tags=["project-comments"])

_RecordT = TypeVar("_RecordT")


@router.post(
    "/{project_id}/comments",
    response_model=ProjectCommentResponse,
    status_code=http_status.HTTP_201_CREATED,
)
def create_project_comment(
    project_id: str,
    payload: ProjectCommentCreateRequest,
    request: Request,
) -> ProjectCommentResponse:
    current_user = require_current_user(request)
    state = get_app_state(request)
    comment = _run_comment_operation(
        lambda: state.project_service.create_comment(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            body=payload.body,
        )
    )
    state.realtime_publisher.comment_created_sync(record=comment)
    return comment_response_from_record(record=comment)


@router.get(
    "/{project_id}/comments",
    response_model=ProjectCommentListResponse,
)
def list_project_comments(
    project_id: str,
    request: Request,
) -> ProjectCommentListResponse:
    current_user = require_current_user(request)
    comments = _run_comment_operation(
        lambda: get_app_state(request).project_service.list_comments(
            actor_user_id=current_user.user_id,
            project_id=project_id,
        )
    )
    return ProjectCommentListResponse(
        items=tuple(
            ProjectCommentListItemResponse(
                **comment_response_from_record(record=item.comment).model_dump(),
                reaction_summary=reaction_response_from_record(record=item.reaction_summary),
            )
            for item in comments
        )
    )


@router.patch(
    "/{project_id}/comments/{comment_id}",
    response_model=ProjectCommentResponse,
)
def edit_project_comment(
    project_id: str,
    comment_id: str,
    payload: ProjectCommentUpdateRequest,
    request: Request,
) -> ProjectCommentResponse:
    current_user = require_current_user(request)
    state = get_app_state(request)
    comment = _run_comment_operation(
        lambda: state.project_service.edit_comment(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            comment_id=comment_id,
            body=payload.body,
        )
    )
    state.realtime_publisher.comment_updated_sync(record=comment)
    return comment_response_from_record(record=comment)


@router.delete(
    "/{project_id}/comments/{comment_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
def delete_project_comment(
    project_id: str,
    comment_id: str,
    request: Request,
) -> None:
    current_user = require_current_user(request)
    state = get_app_state(request)
    _run_comment_operation(
        lambda: state.project_service.delete_comment(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            comment_id=comment_id,
        )
    )
    state.realtime_publisher.comment_deleted_sync(
        project_id=project_id,
        comment_id=comment_id,
    )


@router.put(
    "/{project_id}/comments/{comment_id}/reaction",
    response_model=ProjectCommentReactionSummaryResponse,
)
def set_project_comment_reaction(
    project_id: str,
    comment_id: str,
    payload: ProjectCommentReactionUpdateRequest,
    request: Request,
) -> ProjectCommentReactionSummaryResponse:
    current_user = require_current_user(request)
    state = get_app_state(request)
    summary = _run_comment_operation(
        lambda: state.project_service.set_comment_reaction(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            comment_id=comment_id,
            reaction=payload.reaction,
        )
    )
    state.realtime_publisher.reaction_updated_sync(
        project_id=project_id,
        comment_id=comment_id,
        summary=summary,
    )
    return reaction_response_from_record(record=summary)


@router.delete(
    "/{project_id}/comments/{comment_id}/reaction",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
def delete_project_comment_reaction(
    project_id: str,
    comment_id: str,
    request: Request,
) -> None:
    current_user = require_current_user(request)
    state = get_app_state(request)
    summary = _run_comment_operation(
        lambda: state.project_service.delete_comment_reaction(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            comment_id=comment_id,
        )
    )
    state.realtime_publisher.reaction_deleted_sync(
        project_id=project_id,
        comment_id=comment_id,
        summary=summary,
    )


@router.get(
    "/{project_id}/comments/{comment_id}/reactions",
    response_model=ProjectCommentReactionSummaryResponse,
)
def get_project_comment_reactions(
    project_id: str,
    comment_id: str,
    request: Request,
) -> ProjectCommentReactionSummaryResponse:
    current_user = require_current_user(request)
    summary = _run_comment_operation(
        lambda: get_app_state(request).project_service.get_comment_reactions(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            comment_id=comment_id,
        )
    )
    return reaction_response_from_record(record=summary)


def _run_comment_operation(operation: Callable[[], _RecordT]) -> _RecordT:
    try:
        return operation()
    except ProjectDomainError as error:
        raise _http_from_project_error(error=error) from error


def _http_from_project_error(*, error: ProjectDomainError) -> HTTPException:
    if isinstance(error, (ProjectNotFoundError, ProjectCommentNotFoundError)):
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


__all__ = ["router"]
