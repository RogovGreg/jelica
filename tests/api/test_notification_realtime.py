from __future__ import annotations

import asyncio

from jelica_api.realtime import NotificationRealtimeConnection, NotificationRealtimeHub


class _WebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.close_codes: list[int] = []

    async def send_json(self, message: dict[str, object]) -> None:
        self.messages.append(message)

    async def close(self, *, code: int) -> None:
        self.close_codes.append(code)


def test_notification_hub_targets_user_and_evicts_only_revoked_session() -> None:
    async def scenario() -> None:
        hub = NotificationRealtimeHub()
        first_socket = _WebSocket()
        second_socket = _WebSocket()
        first = NotificationRealtimeConnection(
            websocket=first_socket,  # type: ignore[arg-type]
            user_id="user-1",
            auth_session_id="session-1",
        )
        second = NotificationRealtimeConnection(
            websocket=second_socket,  # type: ignore[arg-type]
            user_id="user-1",
            auth_session_id="session-2",
        )
        await hub.register(first)
        await hub.register(second)
        await hub.send_to_user(user_id="user-1", message={"type": "notification.created"})
        assert len(first_socket.messages) == 1
        assert len(second_socket.messages) == 1

        await hub.evict_auth_session(session_id="session-1")
        assert first_socket.close_codes == [4401]
        assert second_socket.close_codes == []
        await hub.send_to_user(user_id="user-1", message={"type": "notifications.all_read"})
        assert len(first_socket.messages) == 1
        assert len(second_socket.messages) == 2
        await hub.shutdown()
        assert second_socket.close_codes == [1012]

    asyncio.run(scenario())
