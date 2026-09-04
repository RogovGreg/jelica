from __future__ import annotations

import asyncio
from dataclasses import dataclass

from jelica_api.contracts.task_discussions import (
    TaskDiscussionCommentResponse,
    TaskDiscussionMentionResponse,
    TaskDiscussionReactionSummaryResponse,
)
from jelica_api.projects import ProjectCommentReactionSummaryRecord
from jelica_api.task_discussions import TaskDiscussionCommentRecord

from .hub import ProjectRealtimeHub


class TaskRealtimeHub(ProjectRealtimeHub):
    async def close_task_context(self, *, task_id: str) -> None:
        async with self._lock:
            room = self._rooms.pop(task_id, {})
            self._project_statuses.pop(task_id, None)
            connections = tuple(
                connection
                for user_connections in room.values()
                for connection in user_connections.values()
            )
        if connections:
            await asyncio.gather(
                *(connection.send({"type": "task.context_changed"}) for connection in connections)
            )
            await asyncio.gather(*(connection.close(code=4410) for connection in connections))


def task_comment_response_from_record(
    *, record: TaskDiscussionCommentRecord
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
            TaskDiscussionMentionResponse(user_id=item.user_id, username=item.username)
            for item in record.mentions
        ),
        reaction_summary=TaskDiscussionReactionSummaryResponse(
            support=0, oppose=0, current_user_reaction=None
        ),
    )


@dataclass(frozen=True, slots=True)
class TaskRealtimePublisher:
    hub: TaskRealtimeHub

    async def comment_created(
        self, *, record: TaskDiscussionCommentRecord, command_id: str | None = None
    ) -> None:
        await self.hub.broadcast(
            project_id=record.task_id,
            message={
                "type": "comment.created",
                "command_id": command_id,
                "comment": task_comment_response_from_record(record=record).model_dump(mode="json"),
            },
        )

    def comment_created_sync(self, *, record: TaskDiscussionCommentRecord) -> None:
        self.hub.run_from_sync(self.comment_created(record=record))

    async def comment_updated(
        self, *, record: TaskDiscussionCommentRecord, command_id: str | None = None
    ) -> None:
        await self.hub.broadcast(
            project_id=record.task_id,
            message={
                "type": "comment.updated",
                "command_id": command_id,
                "comment": task_comment_response_from_record(record=record).model_dump(mode="json"),
            },
        )

    def comment_updated_sync(self, *, record: TaskDiscussionCommentRecord) -> None:
        self.hub.run_from_sync(self.comment_updated(record=record))

    async def comment_deleted(
        self, *, task_id: str, comment_id: str, command_id: str | None = None
    ) -> None:
        await self.hub.broadcast(
            project_id=task_id,
            message={"type": "comment.deleted", "command_id": command_id, "comment_id": comment_id},
        )

    def comment_deleted_sync(self, *, task_id: str, comment_id: str) -> None:
        self.hub.run_from_sync(self.comment_deleted(task_id=task_id, comment_id=comment_id))

    async def reaction_updated(
        self,
        *,
        task_id: str,
        comment_id: str,
        summary: ProjectCommentReactionSummaryRecord,
        command_id: str | None = None,
    ) -> None:
        await self.hub.broadcast(
            project_id=task_id,
            message={
                "type": "reaction.updated",
                "command_id": command_id,
                "comment_id": comment_id,
                "support": summary.support,
                "oppose": summary.oppose,
            },
        )

    def reaction_updated_sync(
        self, *, task_id: str, comment_id: str, summary: ProjectCommentReactionSummaryRecord
    ) -> None:
        self.hub.run_from_sync(
            self.reaction_updated(task_id=task_id, comment_id=comment_id, summary=summary)
        )

    async def reaction_deleted(
        self,
        *,
        task_id: str,
        comment_id: str,
        summary: ProjectCommentReactionSummaryRecord,
        command_id: str | None = None,
    ) -> None:
        await self.hub.broadcast(
            project_id=task_id,
            message={
                "type": "reaction.deleted",
                "command_id": command_id,
                "comment_id": comment_id,
                "support": summary.support,
                "oppose": summary.oppose,
            },
        )

    def reaction_deleted_sync(
        self, *, task_id: str, comment_id: str, summary: ProjectCommentReactionSummaryRecord
    ) -> None:
        self.hub.run_from_sync(
            self.reaction_deleted(task_id=task_id, comment_id=comment_id, summary=summary)
        )

    async def context_changed(self, *, task_id: str) -> None:
        await self.hub.close_task_context(task_id=task_id)

    def context_changed_sync(self, *, task_id: str) -> None:
        self.hub.run_from_sync(self.context_changed(task_id=task_id))

    async def discussion_cleared(self, *, task_id: str, command_id: str | None = None) -> None:
        await self.hub.broadcast(
            project_id=task_id,
            message={"type": "discussion.cleared", "command_id": command_id},
        )

    def discussion_cleared_sync(self, *, task_id: str) -> None:
        self.hub.run_from_sync(self.discussion_cleared(task_id=task_id))


__all__ = ["TaskRealtimeHub", "TaskRealtimePublisher", "task_comment_response_from_record"]
