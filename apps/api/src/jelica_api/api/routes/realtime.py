from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from jelica_api.api.authentication import AUTH_SESSION_COOKIE_NAME
from jelica_api.app_state import get_app_state
from jelica_api.auth import AuthenticationRequiredError
from jelica_api.projects import (
    ProjectCommentNotFoundError,
    ProjectConflictError,
    ProjectDomainError,
    ProjectNotFoundError,
    ProjectPermissionError,
    ProjectValidationError,
)
from jelica_api.realtime import (
    ProjectRealtimeConnection,
    comment_response_from_record,
    reaction_response_from_record,
)
from jelica_api.realtime.protocol import (
    CommandEnvelope,
    ProtocolMessageError,
    TypingEnvelope,
    command_ack,
    command_error,
    parse_client_message,
    protocol_error,
)

router = APIRouter(tags=["project-realtime"])


@router.websocket("/api/projects/{project_id}/realtime")
async def project_realtime(project_id: str, websocket: WebSocket) -> None:
    state = get_app_state(websocket)
    if not _is_same_origin(websocket):
        await websocket.close(code=4400)
        return
    session_token = websocket.cookies.get(AUTH_SESSION_COOKIE_NAME, "")
    try:
        current_user = await run_in_threadpool(
            state.auth_service.current_context,
            session_token=session_token,
        )
    except (AuthenticationRequiredError, ValueError):
        await websocket.close(code=4401)
        return
    try:
        project = await run_in_threadpool(
            state.project_service.get_project,
            actor_user_id=current_user.user.user_id,
            project_id=project_id,
        )
    except (ProjectNotFoundError, ProjectPermissionError, ProjectValidationError):
        await websocket.close(code=4404)
        return

    await websocket.accept()
    connection = ProjectRealtimeConnection(
        websocket=websocket,
        project_id=project.project_id,
        user_id=current_user.user.user_id,
        username=current_user.user.username,
        role=project.current_user_role or "viewer",
        auth_session_id=current_user.session_id,
    )
    snapshot, first_user_connection = await state.realtime_hub.register(
        connection,
        project_status=project.status,
    )
    await connection.send({"type": "presence.snapshot", "users": snapshot})
    if first_user_connection:
        await state.realtime_hub.broadcast(
            project_id=project.project_id,
            message={
                "type": "presence.joined",
                "user": {
                    "user_id": current_user.user.user_id,
                    "username": current_user.user.username,
                },
            },
        )

    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                break
            if frame.get("bytes") is not None:
                await connection.send(
                    protocol_error(
                        code="malformed_command",
                        message="Binary WebSocket frames are not supported.",
                    )
                )
                await connection.close(code=4400)
                break
            raw_text = frame.get("text")
            if raw_text is None:
                continue
            try:
                message = parse_client_message(raw_text)
            except ProtocolMessageError as error:
                if error.command_id is not None and error.command is not None:
                    await connection.send(
                        command_error(
                            command_id=error.command_id,
                            command=error.command,
                            code=error.code,
                            message=str(error),
                        )
                    )
                else:
                    await connection.send(protocol_error(code=error.code, message=str(error)))
                continue
            if isinstance(message, TypingEnvelope):
                if message.type == "typing.start":
                    await state.realtime_hub.typing_start(connection=connection)
                else:
                    await state.realtime_hub.typing_stop(connection=connection)
                continue
            should_continue = await _handle_command(
                connection=connection,
                command=message,
                session_token=session_token,
            )
            if not should_continue:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await state.realtime_hub.unregister(connection)


async def _handle_command(
    *,
    connection: ProjectRealtimeConnection,
    command: CommandEnvelope,
    session_token: str,
) -> bool:
    state = get_app_state(connection.websocket)
    try:
        authenticated_user = await run_in_threadpool(
            state.auth_service.current_user,
            session_token=session_token,
        )
    except (AuthenticationRequiredError, ValueError):
        await connection.close(code=4401)
        return False
    if authenticated_user.user_id != connection.user_id:
        await connection.close(code=4401)
        return False
    if command.command == "discussion.clear":
        await connection.send(
            command_error(
                command_id=command.id,
                command=command.command,
                code="unsupported_command",
                message="Discussion clear is available only for Task Discussion rooms.",
            )
        )
        return True

    try:
        result, event = await _execute_persistent_command(
            connection=connection,
            command=command,
        )
    except ProjectPermissionError as error:
        if error.code == "project_membership_required":
            await state.realtime_hub.revoke_user(
                project_id=connection.project_id,
                user_id=connection.user_id,
            )
            return False
        await connection.send(
            command_error(
                command_id=command.id,
                command=command.command,
                code=_domain_error_code(error),
                message=str(error),
            )
        )
        return True
    except ProjectNotFoundError:
        await state.realtime_hub.close_project(project_id=connection.project_id)
        return False
    except ProjectDomainError as error:
        await connection.send(
            command_error(
                command_id=command.id,
                command=command.command,
                code=_domain_error_code(error),
                message=str(error),
            )
        )
        return True

    await connection.send(
        command_ack(
            command_id=command.id,
            command=command.command,
            result=result,
        )
    )
    await event()
    return True


async def _execute_persistent_command(
    *,
    connection: ProjectRealtimeConnection,
    command: CommandEnvelope,
) -> tuple[dict[str, Any], Any]:
    state = get_app_state(connection.websocket)
    service = state.project_service
    publisher = state.realtime_publisher
    payload = command.payload

    if command.command == "comment.create":
        record = await run_in_threadpool(
            service.create_comment,
            actor_user_id=connection.user_id,
            project_id=connection.project_id,
            body=payload["body"],
        )
        response = comment_response_from_record(record=record).model_dump(mode="json")

        async def publish() -> None:
            await publisher.comment_created(record=record, command_id=command.id)

        return response, publish
    if command.command == "comment.update":
        record = await run_in_threadpool(
            service.edit_comment,
            actor_user_id=connection.user_id,
            project_id=connection.project_id,
            comment_id=payload["comment_id"],
            body=payload["body"],
        )
        response = comment_response_from_record(record=record).model_dump(mode="json")

        async def publish() -> None:
            await publisher.comment_updated(record=record, command_id=command.id)

        return response, publish
    if command.command == "comment.delete":
        comment_id = payload["comment_id"]
        await run_in_threadpool(
            service.delete_comment,
            actor_user_id=connection.user_id,
            project_id=connection.project_id,
            comment_id=comment_id,
        )

        async def publish() -> None:
            await publisher.comment_deleted(
                project_id=connection.project_id,
                comment_id=comment_id,
                command_id=command.id,
            )

        return {"comment_id": comment_id}, publish
    if command.command == "reaction.set":
        comment_id = payload["comment_id"]
        summary = await run_in_threadpool(
            service.set_comment_reaction,
            actor_user_id=connection.user_id,
            project_id=connection.project_id,
            comment_id=comment_id,
            reaction=payload["reaction"],
        )
        response = reaction_response_from_record(record=summary).model_dump(mode="json")

        async def publish() -> None:
            await publisher.reaction_updated(
                project_id=connection.project_id,
                comment_id=comment_id,
                summary=summary,
                command_id=command.id,
            )

        return response, publish

    comment_id = payload["comment_id"]
    summary = await run_in_threadpool(
        service.delete_comment_reaction,
        actor_user_id=connection.user_id,
        project_id=connection.project_id,
        comment_id=comment_id,
    )
    response = reaction_response_from_record(record=summary).model_dump(mode="json")

    async def publish() -> None:
        await publisher.reaction_deleted(
            project_id=connection.project_id,
            comment_id=comment_id,
            summary=summary,
            command_id=command.id,
        )

    return response, publish


def _domain_error_code(error: ProjectDomainError) -> str:
    if isinstance(error, ProjectCommentNotFoundError):
        return "comment_not_found"
    if isinstance(error, ProjectConflictError) and error.code == "project_frozen":
        return "project_frozen"
    if isinstance(error, ProjectPermissionError):
        if error.code == "project_comment_self_reaction_forbidden":
            return "reaction_not_allowed"
        return "forbidden"
    if isinstance(error, ProjectValidationError):
        return "validation_error"
    return "resource_unavailable"


def _is_same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if origin is None or host is None:
        return False
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return False
    origin_host = parsed.hostname.casefold()
    if parsed.port is not None:
        origin_host = f"{origin_host}:{parsed.port}"
    return origin_host == host.casefold()


__all__ = ["router"]
