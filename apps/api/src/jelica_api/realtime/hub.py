from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import WebSocket

REALTIME_TYPING_TTL_SECONDS = 5.0


@dataclass(eq=False, slots=True)
class ProjectRealtimeConnection:
    websocket: WebSocket
    project_id: str
    user_id: str
    username: str
    role: str
    connection_id: str = field(default_factory=lambda: str(uuid4()))
    auth_session_id: str = ""
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


@dataclass(slots=True)
class _TypingState:
    username: str
    token: str
    task: asyncio.Task[None]


class ProjectRealtimeHub:
    def __init__(self, *, typing_ttl_seconds: float = REALTIME_TYPING_TTL_SECONDS) -> None:
        self._typing_ttl_seconds = typing_ttl_seconds
        self._rooms: dict[
            str,
            dict[str, dict[str, ProjectRealtimeConnection]],
        ] = {}
        self._project_statuses: dict[str, str] = {}
        self._typing: dict[tuple[str, str], _TypingState] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def register(
        self,
        connection: ProjectRealtimeConnection,
        *,
        project_status: str,
    ) -> tuple[tuple[dict[str, str], ...], bool]:
        self._bind_loop()
        async with self._lock:
            room = self._rooms.setdefault(connection.project_id, {})
            user_connections = room.setdefault(connection.user_id, {})
            first_user_connection = not user_connections
            user_connections[connection.connection_id] = connection
            self._project_statuses[connection.project_id] = project_status
            snapshot = tuple(
                {"user_id": user_id, "username": next(iter(connections.values())).username}
                for user_id, connections in sorted(room.items())
            )
        return snapshot, first_user_connection

    async def unregister(self, connection: ProjectRealtimeConnection) -> None:
        typing_user: dict[str, str] | None = None
        left_user: dict[str, str] | None = None
        async with self._lock:
            room = self._rooms.get(connection.project_id)
            if room is None:
                return
            user_connections = room.get(connection.user_id)
            if user_connections is None:
                return
            user_connections.pop(connection.connection_id, None)
            if user_connections:
                return
            room.pop(connection.user_id, None)
            typing_user = self._remove_typing_locked(
                project_id=connection.project_id,
                user_id=connection.user_id,
            )
            left_user = {"user_id": connection.user_id, "username": connection.username}
            if not room:
                self._rooms.pop(connection.project_id, None)
                self._project_statuses.pop(connection.project_id, None)
        if typing_user is not None:
            await self.broadcast(
                project_id=connection.project_id,
                message={"type": "typing.stopped", "user": typing_user},
            )
        if left_user is not None:
            await self.broadcast(
                project_id=connection.project_id,
                message={"type": "presence.left", "user": left_user},
            )

    async def broadcast(self, *, project_id: str, message: dict[str, Any]) -> None:
        async with self._lock:
            connections = tuple(self._connections_locked(project_id=project_id))
        if connections:
            await asyncio.gather(*(connection.send(message) for connection in connections))

    async def send_to_user(
        self,
        *,
        project_id: str,
        user_id: str,
        message: dict[str, Any],
    ) -> None:
        async with self._lock:
            room = self._rooms.get(project_id, {})
            connections = tuple(room.get(user_id, {}).values())
        if connections:
            await asyncio.gather(*(connection.send(message) for connection in connections))

    async def typing_start(self, *, connection: ProjectRealtimeConnection) -> bool:
        self._bind_loop()
        key = (connection.project_id, connection.user_id)
        async with self._lock:
            if self._project_statuses.get(
                connection.project_id
            ) == "frozen" or connection.role not in {"commenter", "member", "supervisor"}:
                return False
            existing = self._typing.get(key)
            if existing is not None:
                existing.task.cancel()
            token = str(uuid4())
            task = asyncio.create_task(
                self._expire_typing(
                    project_id=connection.project_id,
                    user_id=connection.user_id,
                    token=token,
                )
            )
            self._typing[key] = _TypingState(
                username=connection.username,
                token=token,
                task=task,
            )
            is_new = existing is None
        if is_new:
            await self.broadcast(
                project_id=connection.project_id,
                message={
                    "type": "typing.started",
                    "user": {"user_id": connection.user_id, "username": connection.username},
                },
            )
        return True

    async def typing_stop(self, *, connection: ProjectRealtimeConnection) -> None:
        async with self._lock:
            user = self._remove_typing_locked(
                project_id=connection.project_id,
                user_id=connection.user_id,
            )
        if user is not None:
            await self.broadcast(
                project_id=connection.project_id,
                message={"type": "typing.stopped", "user": user},
            )

    async def set_project_status(self, *, project_id: str, status: str) -> None:
        async with self._lock:
            if project_id in self._rooms:
                self._project_statuses[project_id] = status
        await self.broadcast(
            project_id=project_id,
            message={
                "type": "project.frozen" if status == "frozen" else "project.unfrozen",
                "status": status,
            },
        )
        if status == "frozen":
            await self.clear_project_typing(project_id=project_id)

    async def update_user_role(
        self,
        *,
        project_id: str,
        user_id: str,
        role: str,
        message: dict[str, Any],
    ) -> None:
        async with self._lock:
            room = self._rooms.get(project_id, {})
            connections = tuple(room.get(user_id, {}).values())
            for connection in connections:
                connection.role = role
            should_clear_typing = role not in {"commenter", "member", "supervisor"}
        await self.broadcast(project_id=project_id, message=message)
        if should_clear_typing:
            async with self._lock:
                user = self._remove_typing_locked(project_id=project_id, user_id=user_id)
            if user is not None:
                await self.broadcast(
                    project_id=project_id,
                    message={"type": "typing.stopped", "user": user},
                )

    async def clear_project_typing(self, *, project_id: str) -> None:
        async with self._lock:
            users = [
                user
                for (typing_project_id, user_id) in tuple(self._typing)
                if typing_project_id == project_id
                and (
                    user := self._remove_typing_locked(
                        project_id=project_id,
                        user_id=user_id,
                    )
                )
                is not None
            ]
        for user in users:
            await self.broadcast(
                project_id=project_id,
                message={"type": "typing.stopped", "user": user},
            )

    async def revoke_user(
        self,
        *,
        project_id: str,
        user_id: str,
        room_event: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            room = self._rooms.get(project_id, {})
            connections = tuple(room.pop(user_id, {}).values())
            typing_user = self._remove_typing_locked(project_id=project_id, user_id=user_id)
            if not room:
                self._rooms.pop(project_id, None)
                self._project_statuses.pop(project_id, None)
        if room_event is not None:
            await self.broadcast(project_id=project_id, message=room_event)
        if typing_user is not None:
            await self.broadcast(
                project_id=project_id,
                message={"type": "typing.stopped", "user": typing_user},
            )
        if connections:
            revoked = {
                "type": "access.revoked",
                "error": {"code": "access_revoked", "message": "Project access was revoked."},
            }
            await asyncio.gather(*(connection.send(revoked) for connection in connections))
            await asyncio.gather(*(connection.close(code=4403) for connection in connections))
            username = connections[0].username
            await self.broadcast(
                project_id=project_id,
                message={
                    "type": "presence.left",
                    "user": {"user_id": user_id, "username": username},
                },
            )

    async def evict_auth_session(self, *, session_id: str) -> None:
        """Close sockets authenticated by one revoked server-side session."""
        async with self._lock:
            connections: list[ProjectRealtimeConnection] = []
            for project_id, room in tuple(self._rooms.items()):
                for user_id, user_connections in tuple(room.items()):
                    matched = [
                        connection
                        for connection in tuple(user_connections.values())
                        if connection.auth_session_id == session_id
                    ]
                    for connection in matched:
                        user_connections.pop(connection.connection_id, None)
                    connections.extend(matched)
                    if not user_connections:
                        self._remove_typing_locked(project_id=project_id, user_id=user_id)
                        room.pop(user_id, None)
                if not room:
                    self._rooms.pop(project_id, None)
                    self._project_statuses.pop(project_id, None)
        if connections:
            await asyncio.gather(*(connection.close(code=4401) for connection in connections))

    async def close_project(self, *, project_id: str) -> None:
        async with self._lock:
            room = self._rooms.pop(project_id, {})
            self._project_statuses.pop(project_id, None)
            connections = tuple(
                connection
                for user_connections in room.values()
                for connection in user_connections.values()
            )
            for key in [key for key in self._typing if key[0] == project_id]:
                state = self._typing.pop(key)
                state.task.cancel()
        if connections:
            deleted = {"type": "project.deleted"}
            await asyncio.gather(*(connection.send(deleted) for connection in connections))
            await asyncio.gather(*(connection.close(code=4404) for connection in connections))

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
                for room in self._rooms.values()
                for user_connections in room.values()
                for connection in user_connections.values()
            )
            tasks = tuple(state.task for state in self._typing.values())
            self._rooms.clear()
            self._project_statuses.clear()
            self._typing.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if connections:
            await asyncio.gather(*(connection.close(code=1012) for connection in connections))

    async def _expire_typing(self, *, project_id: str, user_id: str, token: str) -> None:
        try:
            await asyncio.sleep(self._typing_ttl_seconds)
        except asyncio.CancelledError:
            return
        async with self._lock:
            state = self._typing.get((project_id, user_id))
            if state is None or state.token != token:
                return
            self._typing.pop((project_id, user_id), None)
            user = {"user_id": user_id, "username": state.username}
        await self.broadcast(
            project_id=project_id,
            message={"type": "typing.stopped", "user": user},
        )

    def _remove_typing_locked(self, *, project_id: str, user_id: str) -> dict[str, str] | None:
        state = self._typing.pop((project_id, user_id), None)
        if state is None:
            return None
        state.task.cancel()
        return {"user_id": user_id, "username": state.username}

    def _connections_locked(self, *, project_id: str) -> list[ProjectRealtimeConnection]:
        return [
            connection
            for user_connections in self._rooms.get(project_id, {}).values()
            for connection in user_connections.values()
        ]

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("ProjectRealtimeHub cannot span multiple event loops.")


__all__ = [
    "ProjectRealtimeConnection",
    "ProjectRealtimeHub",
    "REALTIME_TYPING_TTL_SECONDS",
]
