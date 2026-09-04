from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import WebSocket


@dataclass(eq=False, slots=True)
class NotificationRealtimeConnection:
    websocket: WebSocket
    user_id: str
    auth_session_id: str
    connection_id: str = field(default_factory=lambda: str(uuid4()))
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _closed: bool = field(default=False, repr=False)

    async def send(self, message: dict[str, Any]) -> bool:
        async with self._send_lock:
            if self._closed:
                return False
            try:
                await self.websocket.send_json(message)
            except RuntimeError:
                self._closed = True
                return False
            return True

    async def close(self, *, code: int) -> None:
        async with self._send_lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self.websocket.close(code=code)
            except RuntimeError:
                return


class NotificationRealtimeHub:
    def __init__(self) -> None:
        self._connections: dict[str, dict[str, NotificationRealtimeConnection]] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def register(self, connection: NotificationRealtimeConnection) -> None:
        self._bind_loop()
        async with self._lock:
            self._connections.setdefault(connection.user_id, {})[connection.connection_id] = (
                connection
            )

    async def unregister(self, connection: NotificationRealtimeConnection) -> None:
        async with self._lock:
            user_connections = self._connections.get(connection.user_id)
            if user_connections is None:
                return
            user_connections.pop(connection.connection_id, None)
            if not user_connections:
                self._connections.pop(connection.user_id, None)

    async def send_to_user(self, *, user_id: str, message: dict[str, Any]) -> None:
        async with self._lock:
            connections = tuple(self._connections.get(user_id, {}).values())
        if connections:
            await asyncio.gather(*(connection.send(message) for connection in connections))

    async def evict_auth_session(self, *, session_id: str) -> None:
        async with self._lock:
            matched: list[NotificationRealtimeConnection] = []
            for user_id, connections in tuple(self._connections.items()):
                for connection_id, connection in tuple(connections.items()):
                    if connection.auth_session_id == session_id:
                        connections.pop(connection_id, None)
                        matched.append(connection)
                if not connections:
                    self._connections.pop(user_id, None)
        if matched:
            await asyncio.gather(*(connection.close(code=4401) for connection in matched))

    def run_from_sync(self, operation: Coroutine[Any, Any, None]) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            operation.close()
            return
        future = asyncio.run_coroutine_threadsafe(operation, loop)
        future.result(timeout=5)

    async def shutdown(self) -> None:
        async with self._lock:
            connections = tuple(
                connection
                for user_connections in self._connections.values()
                for connection in user_connections.values()
            )
            self._connections.clear()
        if connections:
            await asyncio.gather(*(connection.close(code=1012) for connection in connections))

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("NotificationRealtimeHub cannot span multiple event loops.")


__all__ = ["NotificationRealtimeConnection", "NotificationRealtimeHub"]
