from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from jelica_api.actor_identity import (
    GUEST_SESSION_COOKIE_NAME,
    WebActorIdentity,
    actor_identity_for_request,
    guest_identity_hash_for_creation,
)
from jelica_api.analysis_uploads import AnalysisUploadError, UploadConflictError
from jelica_api.api.authentication import optional_current_user, require_current_user
from jelica_api.app_state import ApiAppState, get_app_state
from jelica_api.auth import UserRecord
from jelica_api.browser_task_submission import BrowserTaskSubmissionService
from jelica_api.cli import (
    JelicaCliCommandError,
    JelicaCliInvocationError,
    JelicaCliProtocolError,
)
from jelica_api.contracts import (
    BrowserTaskSubmissionRequest,
    TaskListItem,
    TaskListResponse,
    TaskResultLookupResponse,
    TaskStatusSnapshot,
    TaskSubmissionRequest,
    TaskSubmissionResult,
)
from jelica_api.task_access import WebTaskActor
from jelica_api.task_lifecycle import (
    TaskLifecycleForbiddenError,
    TaskLifecycleNotFoundError,
)
from jelica_api.web_tasks import WebTaskProjectionRecord

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_TASK_NOT_FOUND_MACHINE_ERROR = "CORE_ANALYTICAL_TASK_NOT_FOUND"
_CLIENT_SIDE_MACHINE_ERRORS = {
    "CLI_ANALYZE_ARGUMENT_INVALID",
    "CORE_ANALYTICAL_TASK_REQUEST_INVALID",
    "CORE_ANALYZE_SOURCE_NOT_FOUND",
    "CORE_ANALYZE_SOURCE_UNAVAILABLE",
    "CORE_ANALYZE_TASK_CONFIG_INVALID",
    "CORE_SYSTEM_CONFIG_INVALID",
}
_INTERRUPTED_MACHINE_ERRORS = {"CLI_COMMAND_INTERRUPTED"}
_LIFECYCLE_CONFLICT_ERRORS = {
    "CORE_ANALYTICAL_TASK_LIFECYCLE_TRANSITION_CONFLICT",
    "CORE_ANALYTICAL_TASK_START_REJECTED",
    "CORE_ANALYTICAL_TASK_PAUSE_REJECTED",
    "CORE_ANALYTICAL_TASK_RESUME_REJECTED",
}
_RESULT_NOT_READY_CODES = {
    "package_not_found",
    "task_has_no_result_package",
}
_RESULT_NOT_FOUND_CODES = {"task_not_found"}
_PROJECTION_LIST_DETAIL = (
    "Task state is served from web projection storage without per-task CLI/Core status calls."
)


@router.get("", response_model=TaskListResponse)
def list_tasks(
    request: Request,
    project_id: Annotated[list[str] | None, Query()] = None,
    project: Literal["none"] | None = None,
    owner: Literal["me"] | None = None,
    state: Annotated[list[str] | None, Query()] = None,
) -> TaskListResponse:
    project_ids = _normalize_project_filters(values=project_id or ())
    states = _normalize_state_filters(values=state or ())
    if project_ids and project == "none":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "task_project_filter_conflict",
                "message": "project_id and project=none cannot be combined.",
            },
        )

    app_state = get_app_state(request)
    current_user = (
        require_current_user(request) if owner == "me" else optional_current_user(request)
    )
    owner_user_id = current_user.user_id if owner == "me" and current_user is not None else None
    actor = _task_actor_for_request(request=request, current_user=current_user)
    projections = app_state.web_task_projection_store.list_recent_tasks(
        actor=actor,
        project_ids=project_ids,
        project_none=project == "none",
        owner_user_id=owner_user_id,
        states=states,
    )
    return TaskListResponse(
        items=tuple(_projection_to_list_item(projection=projection) for projection in projections)
    )


@router.post("", response_model=TaskSubmissionResult, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: BrowserTaskSubmissionRequest,
    request: Request,
    response: Response,
) -> TaskSubmissionResult:
    state = get_app_state(request)
    current_user = optional_current_user(request)
    try:
        if isinstance(payload, TaskSubmissionRequest):
            # Compatibility for internal callers/tests; HTTP validation uses the browser-safe model.
            current_user = optional_current_user(request)
            if current_user is None:
                guest_session_hash = _guest_session_hash_for_creation(
                    request=request, response=response, secure=state.settings.auth_cookie_secure
                )
                return state.task_orchestrator.submit_task(
                    request=payload, guest_session_hash=guest_session_hash
                )
            return state.task_orchestrator.submit_task(
                request=payload, owner_user_id=current_user.user_id
            )
        actor = _task_upload_actor_for_request(
            request=request, response=response, state=state, current_user=current_user
        )
        return BrowserTaskSubmissionService(
            uploads=state.analysis_upload_service, orchestrator=state.task_orchestrator
        ).submit(payload=payload, actor=actor)
    except AnalysisUploadError as error:
        if isinstance(error, UploadConflictError):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "upload_conflict", "message": str(error)},
            ) from error
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "upload_not_found", "message": "Upload was not found."},
        ) from error
    except JelicaCliCommandError as error:
        raise _http_from_command_error(error) from error
    except (JelicaCliInvocationError, JelicaCliProtocolError) as error:
        raise _gateway_error(detail=str(error)) from error


@router.post("/{task_id}/start", response_model=TaskStatusSnapshot)
def start_task(task_id: str, request: Request) -> TaskStatusSnapshot:
    return _execute_lifecycle_action(task_id=task_id, action="start", request=request)


@router.post("/{task_id}/pause", response_model=TaskStatusSnapshot)
def pause_task(task_id: str, request: Request) -> TaskStatusSnapshot:
    return _execute_lifecycle_action(task_id=task_id, action="pause", request=request)


@router.post("/{task_id}/resume", response_model=TaskStatusSnapshot)
def resume_task(task_id: str, request: Request) -> TaskStatusSnapshot:
    return _execute_lifecycle_action(task_id=task_id, action="resume", request=request)


def _execute_lifecycle_action(
    *, task_id: str, action: Literal["start", "pause", "resume"], request: Request
) -> TaskStatusSnapshot:
    state = get_app_state(request)
    actor_identity = actor_identity_for_request(
        request=request, current_user=optional_current_user(request)
    )
    try:
        return state.task_lifecycle_service.execute(
            action=action, task_id=task_id, actor=actor_identity
        )
    except TaskLifecycleNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "task_not_found", "message": "Task was not found."},
        ) from error
    except TaskLifecycleForbiddenError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "task_lifecycle_forbidden",
                "message": "Task lifecycle action is restricted to its owner.",
            },
        ) from error
    except JelicaCliCommandError as error:
        raise _http_from_command_error(error) from error
    except (JelicaCliInvocationError, JelicaCliProtocolError) as error:
        raise _gateway_error(detail=str(error)) from error


@router.get("/{task_id}", response_model=TaskStatusSnapshot)
def get_task_status(task_id: str, request: Request) -> TaskStatusSnapshot:
    state = get_app_state(request)
    task_reference = _normalize_task_reference(task_id=task_id)
    projection = _require_visible_task(
        state=state,
        request=request,
        task_reference=task_reference,
    )
    try:
        snapshot = state.cli_client.get_task_status(task_reference=task_reference)
    except JelicaCliCommandError as error:
        raise _http_from_command_error(error) from error
    except (JelicaCliInvocationError, JelicaCliProtocolError) as error:
        cached_snapshot = _projection_fallback_snapshot(
            projection=projection,
            cause=error,
        )
        actor = _task_actor_for_request(
            request=request, current_user=optional_current_user(request)
        )
        return cached_snapshot.model_copy(
            update={
                "can_control_lifecycle": (
                    (projection.owner_user_id == actor.user_id if actor.user_id else False)
                    or (
                        projection.guest_session_hash == actor.guest_session_hash
                        if actor.guest_session_hash
                        else False
                    )
                )
            }
        )
    state.web_task_projection_store.upsert_task(
        core_task_id=snapshot.task_id,
        name=None,
        status=snapshot.state,
    )
    actor = _task_actor_for_request(request=request, current_user=optional_current_user(request))
    can_control = (
        projection.owner_user_id == actor.user_id if actor.user_id is not None else False
    ) or (
        projection.guest_session_hash == actor.guest_session_hash
        if actor.guest_session_hash is not None
        else False
    )
    return snapshot.model_copy(
        update={"project_id": projection.project_id, "can_control_lifecycle": can_control}
    )


@router.get("/{task_id}/result", response_model=TaskResultLookupResponse)
def get_task_result(task_id: str, request: Request) -> TaskResultLookupResponse:
    state = get_app_state(request)
    task_reference = _normalize_task_reference(task_id=task_id)
    _require_visible_task(
        state=state,
        request=request,
        task_reference=task_reference,
    )
    try:
        status_snapshot = state.cli_client.get_task_status(task_reference=task_reference)
    except JelicaCliCommandError as error:
        raise _http_from_command_error(error) from error
    except (JelicaCliInvocationError, JelicaCliProtocolError) as error:
        raise _gateway_error(detail=str(error)) from error
    state.web_task_projection_store.upsert_task(
        core_task_id=status_snapshot.task_id,
        name=None,
        status=status_snapshot.state,
    )

    if status_snapshot.state != "completed":
        return TaskResultLookupResponse(
            task_id=status_snapshot.task_id,
            trace_id=status_snapshot.trace_id,
            state=status_snapshot.state,
            available=False,
            status_command_id=status_snapshot.command_id,
            detail=(
                f"Result package is not available while task state is '{status_snapshot.state}'."
            ),
        )

    try:
        result_reference = state.cli_client.resolve_result_package_reference(
            task_reference=task_reference
        )
    except JelicaCliCommandError as error:
        result_code = _result_package_error_code(error)
        if result_code in _RESULT_NOT_FOUND_CODES:
            machine_error = error.envelope.error
            if machine_error is None:
                raise _gateway_error(
                    detail="CLI command failed without machine error payload."
                ) from error
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": machine_error.name,
                    "message": machine_error.message,
                    "details": machine_error.details,
                    "command_id": error.envelope.command_id,
                },
            ) from error
        if result_code in _RESULT_NOT_READY_CODES:
            return TaskResultLookupResponse(
                task_id=status_snapshot.task_id,
                trace_id=status_snapshot.trace_id,
                state=status_snapshot.state,
                available=False,
                status_command_id=status_snapshot.command_id,
                detail=f"Result package is not ready yet ({result_code}).",
            )
        raise _http_from_command_error(error) from error
    except (JelicaCliInvocationError, JelicaCliProtocolError) as error:
        raise _gateway_error(detail=str(error)) from error

    return TaskResultLookupResponse(
        task_id=status_snapshot.task_id,
        trace_id=status_snapshot.trace_id,
        state=status_snapshot.state,
        available=True,
        status_command_id=status_snapshot.command_id,
        result_reference=result_reference,
    )


def _normalize_task_reference(*, task_id: str) -> str:
    task_reference = task_id.strip()
    if task_reference == "":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation_error", "message": "task_id must not be empty."},
        )
    return task_reference


def _task_upload_actor_for_request(
    *, request: Request, response: Response, state: ApiAppState, current_user: UserRecord | None
) -> WebActorIdentity:
    if current_user is not None:
        return WebActorIdentity(user_id=current_user.user_id)
    return WebActorIdentity(
        guest_session_hash=_guest_session_hash_for_creation(
            request=request, response=response, secure=state.settings.auth_cookie_secure
        )
    )


def _normalize_project_filters(*, values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        project_id = value.strip()
        if project_id == "":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "task_project_id_invalid",
                    "message": "project_id must not be empty.",
                },
            )
        normalized.append(project_id)
    return tuple(dict.fromkeys(normalized))


def _normalize_state_filters(*, values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        task_state = value.strip()
        if task_state == "":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "task_state_invalid",
                    "message": "state must not be empty.",
                },
            )
        normalized.append(task_state)
    return tuple(dict.fromkeys(normalized))


def _task_actor_for_request(
    *,
    request: Request,
    current_user: UserRecord | None,
) -> WebTaskActor:
    identity = actor_identity_for_request(request=request, current_user=current_user)
    return WebTaskActor(
        user_id=identity.user_id,
        guest_session_hash=identity.guest_session_hash,
    )


def _guest_session_hash_for_creation(
    *,
    request: Request,
    response: Response,
    secure: bool,
) -> str:
    return guest_identity_hash_for_creation(
        request=request,
        response=response,
        secure=secure,
    )


def _require_visible_task(
    *,
    state: ApiAppState,
    request: Request,
    task_reference: str,
) -> WebTaskProjectionRecord:
    current_user = optional_current_user(request)
    actor = _task_actor_for_request(request=request, current_user=current_user)
    projection = state.web_task_projection_store.get_visible_task(
        core_task_id=task_reference,
        actor=actor,
    )
    if projection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "task_not_found",
                "message": "Task was not found.",
            },
        )
    return projection


def _gateway_error(*, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"error": "cli_gateway_error", "message": detail},
    )


def _http_from_command_error(error: JelicaCliCommandError) -> HTTPException:
    machine_error = error.envelope.error
    if machine_error is None:
        return _gateway_error(detail="CLI command failed without machine error payload.")

    error_name = machine_error.name
    status_code = status.HTTP_502_BAD_GATEWAY
    if error_name == _TASK_NOT_FOUND_MACHINE_ERROR:
        status_code = status.HTTP_404_NOT_FOUND
    elif error_name in _LIFECYCLE_CONFLICT_ERRORS:
        status_code = status.HTTP_409_CONFLICT
    elif error_name in _INTERRUPTED_MACHINE_ERRORS:
        status_code = status.HTTP_409_CONFLICT
    elif error_name in _CLIENT_SIDE_MACHINE_ERRORS:
        status_code = status.HTTP_400_BAD_REQUEST

    return HTTPException(
        status_code=status_code,
        detail={
            "error": error_name,
            "message": machine_error.message,
            "details": machine_error.details,
            "command_id": error.envelope.command_id,
        },
    )


def _result_package_error_code(error: JelicaCliCommandError) -> str | None:
    machine_error = error.envelope.error
    if machine_error is None:
        return None
    details = machine_error.details
    raw_value = details.get("result_package_error_code")
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip()
    return normalized or None


def _projection_fallback_snapshot(
    *,
    projection: WebTaskProjectionRecord,
    cause: Exception,
) -> TaskStatusSnapshot:
    return TaskStatusSnapshot(
        task_id=projection.core_task_id,
        project_id=projection.project_id,
        trace_id=None,
        state=projection.status,
        active_job_state=None,
        current_stage=None,
        progress=None,
        command_id=None,
        state_source="projection_cache",
        authoritative=False,
        projection_updated_at=projection.updated_at,
        stale_state=True,
        detail=(
            "Returned cached projection because authoritative CLI/Core status "
            f"is temporarily unavailable: {cause}"
        ),
    )


def _projection_to_list_item(*, projection: WebTaskProjectionRecord) -> TaskListItem:
    return TaskListItem(
        task_id=projection.core_task_id,
        owner_user_id=getattr(projection, "owner_user_id", None),
        project_id=getattr(projection, "project_id", None),
        trace_id=None,
        state=projection.status,
        active_job_state=None,
        current_stage=None,
        progress=None,
        command_id=None,
        created_at=projection.created_at,
        updated_at=projection.updated_at,
        state_source="projection_cache",
        authoritative=False,
        projection_updated_at=projection.updated_at,
        stale_state=True,
        detail=_PROJECTION_LIST_DETAIL,
    )


__all__ = ["GUEST_SESSION_COOKIE_NAME", "router"]
