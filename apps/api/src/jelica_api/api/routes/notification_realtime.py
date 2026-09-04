from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from jelica_api.api.authentication import AUTH_SESSION_COOKIE_NAME
from jelica_api.app_state import get_app_state
from jelica_api.auth import AuthenticationRequiredError
from jelica_api.realtime import NotificationRealtimeConnection

router = APIRouter(tags=["notification-realtime"])


@router.websocket("/api/notifications/realtime")
async def notification_realtime(websocket: WebSocket) -> None:
    state = get_app_state(websocket)
    if not _is_same_origin(websocket):
        await websocket.close(code=4400)
        return
    try:
        context = await run_in_threadpool(
            state.auth_service.current_context,
            session_token=websocket.cookies.get(AUTH_SESSION_COOKIE_NAME, ""),
        )
    except (AuthenticationRequiredError, ValueError):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    connection = NotificationRealtimeConnection(
        websocket=websocket,
        user_id=context.user.user_id,
        auth_session_id=context.session_id,
    )
    await state.notification_realtime_hub.register(connection)
    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                break
            if frame.get("bytes") is not None:
                await connection.close(code=4400)
                break
    except WebSocketDisconnect:
        pass
    finally:
        await state.notification_realtime_hub.unregister(connection)


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
