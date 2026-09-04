from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from jelica_api.api.authentication import AUTH_SESSION_COOKIE_NAME
from jelica_api.app_state import get_app_state
from jelica_api.auth import AuthenticationRequiredError
from jelica_api.projects import ProjectDomainError, ProjectPermissionError
from jelica_api.realtime import ProjectRealtimeConnection
from jelica_api.realtime.protocol import (
    CommandEnvelope,
    ProtocolMessageError,
    TypingEnvelope,
    command_ack,
    command_error,
    parse_client_message,
    protocol_error,
)

router = APIRouter(tags=["task-realtime"])


@router.websocket("/api/tasks/{task_id}/realtime")
async def task_realtime(task_id: str, websocket: WebSocket) -> None:
    if not _same_origin(websocket):
        await websocket.close(code=4400)
        return
    state = get_app_state(websocket)
    token = websocket.cookies.get(AUTH_SESSION_COOKIE_NAME, "")
    try:
        auth_context = await run_in_threadpool(
            state.auth_service.current_context, session_token=token
        )
        user = auth_context.user
        context = await run_in_threadpool(
            state.task_discussion_service.get_realtime_context,
            actor_user_id=user.user_id,
            task_id=task_id,
        )
    except (AuthenticationRequiredError, ValueError):
        await websocket.close(code=4401)
        return
    except (ProjectDomainError,):
        await websocket.close(code=4404)
        return

    await websocket.accept()
    connection = ProjectRealtimeConnection(
        websocket=websocket,
        project_id=context.discussion.task_id,
        user_id=user.user_id,
        username=user.username,
        role=context.role,
        auth_session_id=auth_context.session_id,
    )
    snapshot, first = await state.task_realtime_hub.register(
        connection,
        project_status=context.status,
    )
    await connection.send({"type": "presence.snapshot", "users": snapshot})
    if first:
        await state.task_realtime_hub.broadcast(
            project_id=task_id,
            message={
                "type": "presence.joined",
                "user": {"user_id": user.user_id, "username": user.username},
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
            raw = frame.get("text")
            if raw is None:
                continue
            try:
                message = parse_client_message(raw)
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
                    await state.task_realtime_hub.typing_start(connection=connection)
                else:
                    await state.task_realtime_hub.typing_stop(connection=connection)
                continue
            if not await _handle_command(connection=connection, command=message):
                break
    except WebSocketDisconnect:
        pass
    finally:
        await state.task_realtime_hub.unregister(connection)


async def _handle_command(
    *, connection: ProjectRealtimeConnection, command: CommandEnvelope
) -> bool:
    state = get_app_state(connection.websocket)
    try:
        user = await run_in_threadpool(
            state.auth_service.current_user,
            session_token=connection.websocket.cookies.get(AUTH_SESSION_COOKIE_NAME, ""),
        )
        if user.user_id != connection.user_id:
            await connection.close(code=4401)
            return False
        service = state.task_discussion_service
        payload = command.payload
        task_id = connection.project_id
        if command.command == "discussion.clear":
            await run_in_threadpool(
                service.clear_discussion, actor_user_id=user.user_id, task_id=task_id
            )
            result = {}
            publish = state.task_realtime_publisher.discussion_cleared(
                task_id=task_id, command_id=command.id
            )
        elif command.command == "comment.create":
            record = await run_in_threadpool(
                service.create_comment,
                actor_user_id=user.user_id,
                task_id=task_id,
                body=payload["body"],
            )
            result = _comment_payload(record)
            publish = state.task_realtime_publisher.comment_created(
                record=record, command_id=command.id
            )
        elif command.command == "comment.update":
            record = await run_in_threadpool(
                service.edit_comment,
                actor_user_id=user.user_id,
                task_id=task_id,
                comment_id=payload["comment_id"],
                body=payload["body"],
            )
            result = _comment_payload(record)
            publish = state.task_realtime_publisher.comment_updated(
                record=record, command_id=command.id
            )
        elif command.command == "comment.delete":
            await run_in_threadpool(
                service.delete_comment,
                actor_user_id=user.user_id,
                task_id=task_id,
                comment_id=payload["comment_id"],
            )
            result = {"comment_id": payload["comment_id"]}
            publish = state.task_realtime_publisher.comment_deleted(
                task_id=task_id, comment_id=payload["comment_id"], command_id=command.id
            )
        elif command.command == "reaction.set":
            summary = await run_in_threadpool(
                service.set_reaction,
                actor_user_id=user.user_id,
                task_id=task_id,
                comment_id=payload["comment_id"],
                reaction=payload["reaction"],
            )
            result = {
                "support": summary.support,
                "oppose": summary.oppose,
                "current_user_reaction": summary.current_user_reaction,
            }
            publish = state.task_realtime_publisher.reaction_updated(
                task_id=task_id,
                comment_id=payload["comment_id"],
                summary=summary,
                command_id=command.id,
            )
        else:
            summary = await run_in_threadpool(
                service.delete_reaction,
                actor_user_id=user.user_id,
                task_id=task_id,
                comment_id=payload["comment_id"],
            )
            result = {
                "support": summary.support,
                "oppose": summary.oppose,
                "current_user_reaction": summary.current_user_reaction,
            }
            publish = state.task_realtime_publisher.reaction_deleted(
                task_id=task_id,
                comment_id=payload["comment_id"],
                summary=summary,
                command_id=command.id,
            )
    except ProjectPermissionError as error:
        if error.code == "task_not_found":
            await connection.close(code=4404)
            return False
        await connection.send(
            command_error(
                command_id=command.id, command=command.command, code=error.code, message=str(error)
            )
        )
        return True
    except ProjectDomainError as error:
        await connection.send(
            command_error(
                command_id=command.id, command=command.command, code=error.code, message=str(error)
            )
        )
        return True
    await connection.send(
        command_ack(command_id=command.id, command=command.command, result=result)
    )
    await publish
    return True


def _comment_payload(record: Any) -> dict[str, Any]:
    from jelica_api.realtime.task import task_comment_response_from_record

    return task_comment_response_from_record(record=record).model_dump(mode="json")


def _same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        return False
    return urlsplit(origin).netloc == host


__all__ = ["router"]
