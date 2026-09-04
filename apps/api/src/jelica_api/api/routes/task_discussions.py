from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Request, status

from jelica_api.api.authentication import require_current_user
from jelica_api.app_state import get_app_state
from jelica_api.contracts.task_discussions import (
    TaskDiscussionCommentCreateRequest,
    TaskDiscussionCommentListResponse,
    TaskDiscussionCommentResponse,
    TaskDiscussionCommentUpdateRequest,
    TaskDiscussionMentionResponse,
    TaskDiscussionReactionRequest,
    TaskDiscussionReactionSummaryResponse,
    TaskDiscussionResponse,
)
from jelica_api.projects import ProjectConflictError, ProjectDomainError, ProjectPermissionError
from jelica_api.task_discussions import (
    TaskDiscussionCommentListRecord,
    TaskDiscussionCommentRecord,
    TaskDiscussionService,
)

router = APIRouter(prefix="/api/tasks", tags=["task-discussions"])
_T = TypeVar("_T")


def _service(request: Request) -> TaskDiscussionService:
    return get_app_state(request).task_discussion_service


@router.get("/{task_id}/discussion", response_model=TaskDiscussionResponse)
def get_task_discussion(task_id: str, request: Request) -> TaskDiscussionResponse:
    user = require_current_user(request)
    record = _run(
        lambda: _service(request).get_discussion(actor_user_id=user.user_id, task_id=task_id)
    )
    return TaskDiscussionResponse(
        task_id=record.task_id,
        available=record.available,
        project_id=record.project_id,
        mode=record.mode,
        is_task_owner=record.is_task_owner,
    )


@router.get("/{task_id}/discussion/comments", response_model=TaskDiscussionCommentListResponse)
def list_task_discussion_comments(
    task_id: str, request: Request
) -> TaskDiscussionCommentListResponse:
    user = require_current_user(request)
    records = _run(
        lambda: _service(request).list_comments(actor_user_id=user.user_id, task_id=task_id)
    )
    return TaskDiscussionCommentListResponse(
        items=tuple(_comment_response(item) for item in records)
    )


@router.post(
    "/{task_id}/discussion/comments",
    response_model=TaskDiscussionCommentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_task_discussion_comment(
    task_id: str, payload: TaskDiscussionCommentCreateRequest, request: Request
) -> TaskDiscussionCommentResponse:
    user = require_current_user(request)
    record = _run(
        lambda: _service(request).create_comment(
            actor_user_id=user.user_id, task_id=task_id, body=payload.body
        )
    )
    get_app_state(request).task_realtime_publisher.comment_created_sync(record=record)
    return _comment_with_summary(
        request=request, user_id=user.user_id, task_id=task_id, record=record
    )


@router.patch(
    "/{task_id}/discussion/comments/{comment_id}", response_model=TaskDiscussionCommentResponse
)
def edit_task_discussion_comment(
    task_id: str, comment_id: str, payload: TaskDiscussionCommentUpdateRequest, request: Request
) -> TaskDiscussionCommentResponse:
    user = require_current_user(request)
    record = _run(
        lambda: _service(request).edit_comment(
            actor_user_id=user.user_id, task_id=task_id, comment_id=comment_id, body=payload.body
        )
    )
    get_app_state(request).task_realtime_publisher.comment_updated_sync(record=record)
    return _comment_with_summary(
        request=request, user_id=user.user_id, task_id=task_id, record=record
    )


@router.delete("/{task_id}/discussion/comments", status_code=status.HTTP_204_NO_CONTENT)
def clear_task_discussion(task_id: str, request: Request) -> None:
    user = require_current_user(request)
    _run(lambda: _service(request).clear_discussion(actor_user_id=user.user_id, task_id=task_id))
    get_app_state(request).task_realtime_publisher.discussion_cleared_sync(task_id=task_id)


@router.delete(
    "/{task_id}/discussion/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_task_discussion_comment(task_id: str, comment_id: str, request: Request) -> None:
    user = require_current_user(request)
    _run(
        lambda: _service(request).delete_comment(
            actor_user_id=user.user_id, task_id=task_id, comment_id=comment_id
        )
    )
    get_app_state(request).task_realtime_publisher.comment_deleted_sync(
        task_id=task_id, comment_id=comment_id
    )


@router.get(
    "/{task_id}/discussion/comments/{comment_id}/reaction",
    response_model=TaskDiscussionReactionSummaryResponse,
)
def get_task_discussion_reaction(
    task_id: str, comment_id: str, request: Request
) -> TaskDiscussionReactionSummaryResponse:
    user = require_current_user(request)
    summary = _run(
        lambda: _service(request).get_reactions(
            actor_user_id=user.user_id, task_id=task_id, comment_id=comment_id
        )
    )
    return TaskDiscussionReactionSummaryResponse(
        support=summary.support,
        oppose=summary.oppose,
        current_user_reaction=summary.current_user_reaction,
    )


@router.put(
    "/{task_id}/discussion/comments/{comment_id}/reaction",
    response_model=TaskDiscussionReactionSummaryResponse,
)
def set_task_discussion_reaction(
    task_id: str, comment_id: str, payload: TaskDiscussionReactionRequest, request: Request
) -> TaskDiscussionReactionSummaryResponse:
    user = require_current_user(request)
    summary = _run(
        lambda: _service(request).set_reaction(
            actor_user_id=user.user_id,
            task_id=task_id,
            comment_id=comment_id,
            reaction=payload.reaction,
        )
    )
    get_app_state(request).task_realtime_publisher.reaction_updated_sync(
        task_id=task_id, comment_id=comment_id, summary=summary
    )
    return TaskDiscussionReactionSummaryResponse(
        support=summary.support,
        oppose=summary.oppose,
        current_user_reaction=summary.current_user_reaction,
    )


@router.delete(
    "/{task_id}/discussion/comments/{comment_id}/reaction",
    response_model=TaskDiscussionReactionSummaryResponse,
)
def delete_task_discussion_reaction(
    task_id: str, comment_id: str, request: Request
) -> TaskDiscussionReactionSummaryResponse:
    user = require_current_user(request)
    summary = _run(
        lambda: _service(request).delete_reaction(
            actor_user_id=user.user_id, task_id=task_id, comment_id=comment_id
        )
    )
    get_app_state(request).task_realtime_publisher.reaction_deleted_sync(
        task_id=task_id, comment_id=comment_id, summary=summary
    )
    return TaskDiscussionReactionSummaryResponse(
        support=summary.support,
        oppose=summary.oppose,
        current_user_reaction=summary.current_user_reaction,
    )


def _comment_with_summary(
    *, request: Request, user_id: str, task_id: str, record: TaskDiscussionCommentRecord
) -> TaskDiscussionCommentResponse:
    summary = _run(
        lambda: _service(request).get_reactions(
            actor_user_id=user_id, task_id=task_id, comment_id=record.comment_id
        )
    )
    return _comment_record_response(record=record, summary=summary)


def _comment_response(item: TaskDiscussionCommentListRecord) -> TaskDiscussionCommentResponse:
    return _comment_record_response(record=item.comment, summary=item.reaction_summary)


def _comment_record_response(
    *, record: TaskDiscussionCommentRecord, summary
) -> TaskDiscussionCommentResponse:
    return TaskDiscussionCommentResponse(
        id=record.comment_id,
        task_id=record.task_id,
        author_user_id=record.author_user_id,
        author_username=record.author_username,
        body=record.body,
        created_at=record.created_at,
        edited_at=record.edited_at,
        mentions=tuple(
            TaskDiscussionMentionResponse(user_id=m.user_id, username=m.username)
            for m in record.mentions
        ),
        reaction_summary=TaskDiscussionReactionSummaryResponse(
            support=summary.support,
            oppose=summary.oppose,
            current_user_reaction=summary.current_user_reaction,
        ),
    )


def _run(operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except ProjectDomainError as error:
        if isinstance(error, ProjectPermissionError) and error.code == "task_not_found":
            raise HTTPException(
                status_code=404,
                detail={"error": "task_not_found", "message": "Task was not found."},
            ) from error
        if isinstance(error, (ProjectPermissionError,)):
            raise HTTPException(
                status_code=403, detail={"error": error.code, "message": str(error)}
            ) from error
        if isinstance(error, ProjectConflictError):
            raise HTTPException(
                status_code=409, detail={"error": error.code, "message": str(error)}
            ) from error
        if error.code in {"task_discussion_unavailable", "task_discussion_comment_not_found"}:
            raise HTTPException(
                status_code=404, detail={"error": error.code, "message": str(error)}
            ) from error
        raise HTTPException(
            status_code=422, detail={"error": error.code, "message": str(error)}
        ) from error


__all__ = ["router"]
