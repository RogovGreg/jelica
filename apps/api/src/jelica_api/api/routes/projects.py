from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import status as http_status

from jelica_api.api.authentication import require_current_user
from jelica_api.app_state import get_app_state
from jelica_api.contracts.projects import (
    ProjectCreateRequest,
    ProjectHistoryEventResponse,
    ProjectHistoryEventType,
    ProjectHistoryListResponse,
    ProjectListResponse,
    ProjectMemberListResponse,
    ProjectMemberResponse,
    ProjectMemberRole,
    ProjectMemberUpdateRequest,
    ProjectRelation,
    ProjectResponse,
    ProjectStatus,
    ProjectTaskListResponse,
    ProjectTaskResponse,
    ProjectTransferOwnershipRequest,
    ProjectUpdateRequest,
)
from jelica_api.projects import (
    ProjectConflictError,
    ProjectDomainError,
    ProjectHistoryRecord,
    ProjectMemberNotFoundError,
    ProjectMemberRecord,
    ProjectNotFoundError,
    ProjectPermissionError,
    ProjectRecord,
    ProjectTaskNotFoundError,
    ProjectTaskRecord,
    ProjectValidationError,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])

_RecordT = TypeVar("_RecordT")


@router.post("", response_model=ProjectResponse, status_code=http_status.HTTP_201_CREATED)
def create_project(payload: ProjectCreateRequest, request: Request) -> ProjectResponse:
    current_user = require_current_user(request)
    project = _run_project_operation(
        lambda: get_app_state(request).project_service.create_project(
            actor_user_id=current_user.user_id,
            name=payload.name,
            description=payload.description,
            status=payload.status,
        )
    )
    return _to_project_response(record=project)


@router.get("", response_model=ProjectListResponse)
def list_projects(
    request: Request,
    relation: Annotated[list[ProjectRelation] | None, Query()] = None,
    status: Annotated[list[ProjectStatus] | None, Query()] = None,
) -> ProjectListResponse:
    current_user = require_current_user(request)
    projects = _run_project_operation(
        lambda: get_app_state(request).project_service.list_projects(
            actor_user_id=current_user.user_id,
            relations=tuple(relation or ("any",)),
            statuses=tuple(status or ()),
        )
    )
    return ProjectListResponse(
        items=tuple(_to_project_response(record=project) for project in projects)
    )


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, request: Request) -> ProjectResponse:
    current_user = require_current_user(request)
    project = _run_project_read_operation(
        lambda: get_app_state(request).project_service.get_project(
            actor_user_id=current_user.user_id,
            project_id=project_id,
        )
    )
    return _to_project_response(record=project)


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    payload: ProjectUpdateRequest,
    request: Request,
) -> ProjectResponse:
    current_user = require_current_user(request)
    state = get_app_state(request)
    changes = payload.model_dump(exclude_unset=True)
    project = _run_project_operation(
        lambda: state.project_service.update_project(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            changes=changes,
        )
    )
    if "status" in changes:
        state.realtime_publisher.project_status_sync(
            project_id=project.project_id,
            status=project.status,
        )
        for task_id in state.task_discussion_service.list_project_task_ids(
            project_id=project.project_id
        ):
            state.task_realtime_hub.run_from_sync(
                state.task_realtime_hub.set_project_status(
                    project_id=task_id, status=project.status
                )
            )
    return _to_project_response(record=project)


@router.delete("/{project_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, request: Request) -> None:
    current_user = require_current_user(request)
    state = get_app_state(request)
    task_ids = state.task_discussion_service.list_project_task_ids(project_id=project_id)
    _run_project_operation(
        lambda: state.project_service.delete_project(
            actor_user_id=current_user.user_id,
            project_id=project_id,
        )
    )
    state.realtime_publisher.project_deleted_sync(project_id=project_id)
    for task_id in task_ids:
        state.task_realtime_publisher.context_changed_sync(task_id=task_id)


@router.post("/{project_id}/transfer-ownership", response_model=ProjectResponse)
def transfer_project_ownership(
    project_id: str,
    payload: ProjectTransferOwnershipRequest,
    request: Request,
) -> ProjectResponse:
    current_user = require_current_user(request)
    state = get_app_state(request)
    transfer_target = next(
        (
            member
            for member in _run_project_operation(
                lambda: state.project_service.list_members(
                    actor_user_id=current_user.user_id,
                    project_id=project_id,
                )
            )
            if member.user_id == payload.new_owner_user_id
        ),
        None,
    )
    project = _run_project_operation(
        lambda: state.project_service.transfer_ownership(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            new_owner_user_id=payload.new_owner_user_id,
        )
    )
    if transfer_target is not None and transfer_target.role != "supervisor":
        state.realtime_publisher.member_role_changed_sync(
            project_id=project_id,
            user_id=transfer_target.user_id,
            username=transfer_target.username,
            role="supervisor",
        )
    state.realtime_publisher.ownership_transferred_sync(
        project_id=project_id,
        previous_owner_user_id=current_user.user_id,
        new_owner_user_id=project.owner_user_id,
    )
    return _to_project_response(record=project)


@router.get("/{project_id}/members", response_model=ProjectMemberListResponse)
def list_project_members(
    project_id: str,
    request: Request,
    role: Annotated[list[ProjectMemberRole] | None, Query()] = None,
) -> ProjectMemberListResponse:
    current_user = require_current_user(request)
    members = _run_project_read_operation(
        lambda: get_app_state(request).project_service.list_members(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            roles=tuple(role or ()),
        )
    )
    return ProjectMemberListResponse(
        items=tuple(_to_member_response(record=member) for member in members)
    )


@router.patch("/{project_id}/members/{user_id}", response_model=ProjectMemberResponse)
def update_project_member(
    project_id: str,
    user_id: str,
    payload: ProjectMemberUpdateRequest,
    request: Request,
) -> ProjectMemberResponse:
    current_user = require_current_user(request)
    state = get_app_state(request)
    member = _run_project_operation(
        lambda: state.project_service.update_member_role(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            user_id=user_id,
            role=payload.role,
        )
    )
    state.realtime_publisher.member_role_changed_sync(
        project_id=project_id,
        user_id=member.user_id,
        username=member.username,
        role=member.role,
    )
    for task_id in state.task_discussion_service.list_project_task_ids(project_id=project_id):
        state.task_realtime_hub.run_from_sync(
            state.task_realtime_hub.update_user_role(
                project_id=task_id,
                user_id=member.user_id,
                role=member.role,
                message={
                    "type": "member.role_changed",
                    "user_id": member.user_id,
                    "username": member.username,
                    "role": member.role,
                },
            )
        )
    return _to_member_response(record=member)


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
def remove_project_member(project_id: str, user_id: str, request: Request) -> None:
    current_user = require_current_user(request)
    state = get_app_state(request)
    project_task_ids = state.task_discussion_service.list_project_task_ids(project_id=project_id)
    task_ids = state.task_discussion_service.list_project_task_ids(
        project_id=project_id, owner_user_id=user_id
    )
    _run_project_operation(
        lambda: state.project_service.remove_member(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            user_id=user_id,
        )
    )
    state.realtime_publisher.member_removed_sync(project_id=project_id, user_id=user_id)
    for task_id in task_ids:
        state.task_realtime_publisher.context_changed_sync(task_id=task_id)
    for task_id in set(project_task_ids) - set(task_ids):
        state.task_realtime_hub.run_from_sync(
            state.task_realtime_hub.revoke_user(project_id=task_id, user_id=user_id)
        )


@router.post("/{project_id}/leave", status_code=http_status.HTTP_204_NO_CONTENT)
def leave_project(project_id: str, request: Request) -> None:
    current_user = require_current_user(request)
    state = get_app_state(request)
    project_task_ids = state.task_discussion_service.list_project_task_ids(project_id=project_id)
    task_ids = state.task_discussion_service.list_project_task_ids(
        project_id=project_id, owner_user_id=current_user.user_id
    )
    _run_project_operation(
        lambda: state.project_service.leave_project(
            actor_user_id=current_user.user_id,
            project_id=project_id,
        )
    )
    state.realtime_publisher.member_removed_sync(
        project_id=project_id,
        user_id=current_user.user_id,
    )
    for task_id in task_ids:
        state.task_realtime_publisher.context_changed_sync(task_id=task_id)
    for task_id in set(project_task_ids) - set(task_ids):
        state.task_realtime_hub.run_from_sync(
            state.task_realtime_hub.revoke_user(project_id=task_id, user_id=current_user.user_id)
        )


@router.get("/{project_id}/tasks", response_model=ProjectTaskListResponse)
def list_project_tasks(
    project_id: str,
    request: Request,
    owner_user_id: Annotated[list[str] | None, Query()] = None,
    state: Annotated[list[str] | None, Query()] = None,
) -> ProjectTaskListResponse:
    current_user = require_current_user(request)
    tasks = _run_project_read_operation(
        lambda: get_app_state(request).project_service.list_tasks(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            owner_user_ids=tuple(owner_user_id or ()),
            states=tuple(state or ()),
        )
    )
    return ProjectTaskListResponse(items=tuple(_to_task_response(record=task) for task in tasks))


@router.put("/{project_id}/tasks/{task_id}", response_model=ProjectTaskResponse)
def attach_project_task(project_id: str, task_id: str, request: Request) -> ProjectTaskResponse:
    current_user = require_current_user(request)
    task = _run_project_operation(
        lambda: get_app_state(request).project_service.attach_task(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            task_id=task_id,
        )
    )
    get_app_state(request).task_realtime_publisher.context_changed_sync(task_id=task.task_id)
    return _to_task_response(record=task)


@router.delete("/{project_id}/tasks/{task_id}", response_model=ProjectTaskResponse)
def detach_project_task(project_id: str, task_id: str, request: Request) -> ProjectTaskResponse:
    current_user = require_current_user(request)
    task = _run_project_operation(
        lambda: get_app_state(request).project_service.detach_task(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            task_id=task_id,
        )
    )
    get_app_state(request).task_realtime_publisher.context_changed_sync(task_id=task.task_id)
    return _to_task_response(record=task)


@router.get("/{project_id}/history", response_model=ProjectHistoryListResponse)
def list_project_history(
    project_id: str,
    request: Request,
    event_type: Annotated[list[ProjectHistoryEventType] | None, Query()] = None,
) -> ProjectHistoryListResponse:
    current_user = require_current_user(request)
    events = _run_project_read_operation(
        lambda: get_app_state(request).project_service.list_history(
            actor_user_id=current_user.user_id,
            project_id=project_id,
            event_types=tuple(event_type or ()),
        )
    )
    return ProjectHistoryListResponse(
        items=tuple(_to_history_response(record=event) for event in events)
    )


def _run_project_operation(operation: Callable[[], _RecordT]) -> _RecordT:
    try:
        return operation()
    except ProjectDomainError as error:
        raise _http_from_project_error(error=error) from error


def _run_project_read_operation(operation: Callable[[], _RecordT]) -> _RecordT:
    try:
        return operation()
    except ProjectPermissionError as error:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"error": "project_not_found", "message": "project was not found"},
        ) from error
    except ProjectDomainError as error:
        raise _http_from_project_error(error=error) from error


def _http_from_project_error(*, error: ProjectDomainError) -> HTTPException:
    if isinstance(
        error,
        (ProjectNotFoundError, ProjectMemberNotFoundError, ProjectTaskNotFoundError),
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


def _to_project_response(*, record: ProjectRecord) -> ProjectResponse:
    return ProjectResponse(
        id=record.project_id,
        name=record.name,
        description=record.description,
        status=record.status,
        created_by_user_id=record.created_by_user_id,
        owner_user_id=record.owner_user_id,
        current_user_role=record.current_user_role,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_member_response(*, record: ProjectMemberRecord) -> ProjectMemberResponse:
    return ProjectMemberResponse(
        project_id=record.project_id,
        user_id=record.user_id,
        username=record.username,
        email=record.email,
        role=record.role,
        joined_at=record.joined_at,
    )


def _to_task_response(*, record: ProjectTaskRecord) -> ProjectTaskResponse:
    return ProjectTaskResponse(
        task_id=record.task_id,
        name=record.name,
        state=record.state,
        owner_user_id=record.owner_user_id,
        project_id=record.project_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_history_response(*, record: ProjectHistoryRecord) -> ProjectHistoryEventResponse:
    return ProjectHistoryEventResponse(
        id=record.event_id,
        project_id=record.project_id,
        actor_user_id=record.actor_user_id,
        subject_user_id=record.subject_user_id,
        event_type=record.event_type,
        data=record.data,
        occurred_at=record.occurred_at,
    )


__all__ = ["router"]
