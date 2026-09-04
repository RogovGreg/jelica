from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jelica_api.contracts.comments import (
    ProjectCommentMentionResponse,
    ProjectCommentReactionSummaryResponse,
    ProjectCommentResponse,
)
from jelica_api.projects import (
    ProjectCommentReactionSummaryRecord,
    ProjectCommentRecord,
)

from .hub import ProjectRealtimeHub


def comment_response_from_record(*, record: ProjectCommentRecord) -> ProjectCommentResponse:
    return ProjectCommentResponse(
        id=record.comment_id,
        project_id=record.project_id,
        author_user_id=record.author_user_id,
        author_username=record.author_username,
        body=record.body,
        created_at=record.created_at,
        edited_at=record.edited_at,
        mentions=tuple(
            ProjectCommentMentionResponse(
                user_id=mention.user_id,
                username=mention.username,
            )
            for mention in record.mentions
        ),
    )


def reaction_response_from_record(
    *,
    record: ProjectCommentReactionSummaryRecord,
) -> ProjectCommentReactionSummaryResponse:
    return ProjectCommentReactionSummaryResponse(
        support=record.support,
        oppose=record.oppose,
        current_user_reaction=record.current_user_reaction,
    )


def _model_payload(
    model: ProjectCommentResponse | ProjectCommentReactionSummaryResponse,
) -> dict[str, Any]:
    return model.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class ProjectRealtimePublisher:
    hub: ProjectRealtimeHub

    async def comment_created(
        self,
        *,
        record: ProjectCommentRecord,
        command_id: str | None = None,
    ) -> None:
        await self.hub.broadcast(
            project_id=record.project_id,
            message={
                "type": "comment.created",
                "command_id": command_id,
                "comment": _model_payload(comment_response_from_record(record=record)),
            },
        )

    def comment_created_sync(self, *, record: ProjectCommentRecord) -> None:
        self.hub.run_from_sync(self.comment_created(record=record))

    async def comment_updated(
        self,
        *,
        record: ProjectCommentRecord,
        command_id: str | None = None,
    ) -> None:
        await self.hub.broadcast(
            project_id=record.project_id,
            message={
                "type": "comment.updated",
                "command_id": command_id,
                "comment": _model_payload(comment_response_from_record(record=record)),
            },
        )

    def comment_updated_sync(self, *, record: ProjectCommentRecord) -> None:
        self.hub.run_from_sync(self.comment_updated(record=record))

    async def comment_deleted(
        self,
        *,
        project_id: str,
        comment_id: str,
        command_id: str | None = None,
    ) -> None:
        await self.hub.broadcast(
            project_id=project_id,
            message={
                "type": "comment.deleted",
                "command_id": command_id,
                "comment_id": comment_id,
            },
        )

    def comment_deleted_sync(self, *, project_id: str, comment_id: str) -> None:
        self.hub.run_from_sync(self.comment_deleted(project_id=project_id, comment_id=comment_id))

    async def reaction_updated(
        self,
        *,
        project_id: str,
        comment_id: str,
        summary: ProjectCommentReactionSummaryRecord,
        command_id: str | None = None,
    ) -> None:
        await self._reaction_event(
            event_type="reaction.updated",
            project_id=project_id,
            comment_id=comment_id,
            summary=summary,
            command_id=command_id,
        )

    def reaction_updated_sync(
        self,
        *,
        project_id: str,
        comment_id: str,
        summary: ProjectCommentReactionSummaryRecord,
    ) -> None:
        self.hub.run_from_sync(
            self.reaction_updated(
                project_id=project_id,
                comment_id=comment_id,
                summary=summary,
            )
        )

    async def reaction_deleted(
        self,
        *,
        project_id: str,
        comment_id: str,
        summary: ProjectCommentReactionSummaryRecord,
        command_id: str | None = None,
    ) -> None:
        await self._reaction_event(
            event_type="reaction.deleted",
            project_id=project_id,
            comment_id=comment_id,
            summary=summary,
            command_id=command_id,
        )

    def reaction_deleted_sync(
        self,
        *,
        project_id: str,
        comment_id: str,
        summary: ProjectCommentReactionSummaryRecord,
    ) -> None:
        self.hub.run_from_sync(
            self.reaction_deleted(
                project_id=project_id,
                comment_id=comment_id,
                summary=summary,
            )
        )

    def member_joined_sync(
        self,
        *,
        project_id: str,
        user_id: str,
        username: str,
        role: str,
    ) -> None:
        self._broadcast_sync(
            project_id=project_id,
            message={
                "type": "member.joined",
                "user_id": user_id,
                "username": username,
                "role": role,
            },
        )

    def member_role_changed_sync(
        self,
        *,
        project_id: str,
        user_id: str,
        username: str,
        role: str,
    ) -> None:
        self.hub.run_from_sync(
            self.hub.update_user_role(
                project_id=project_id,
                user_id=user_id,
                role=role,
                message={
                    "type": "member.role_changed",
                    "user_id": user_id,
                    "username": username,
                    "role": role,
                },
            )
        )

    def member_removed_sync(self, *, project_id: str, user_id: str) -> None:
        self.hub.run_from_sync(
            self.hub.revoke_user(
                project_id=project_id,
                user_id=user_id,
                room_event={"type": "member.removed", "user_id": user_id},
            )
        )

    def ownership_transferred_sync(
        self,
        *,
        project_id: str,
        previous_owner_user_id: str,
        new_owner_user_id: str,
    ) -> None:
        self._broadcast_sync(
            project_id=project_id,
            message={
                "type": "project.ownership_transferred",
                "previous_owner_user_id": previous_owner_user_id,
                "new_owner_user_id": new_owner_user_id,
            },
        )

    def project_status_sync(self, *, project_id: str, status: str) -> None:
        self.hub.run_from_sync(self.hub.set_project_status(project_id=project_id, status=status))

    def project_deleted_sync(self, *, project_id: str) -> None:
        self.hub.run_from_sync(self.hub.close_project(project_id=project_id))

    async def _reaction_event(
        self,
        *,
        event_type: str,
        project_id: str,
        comment_id: str,
        summary: ProjectCommentReactionSummaryRecord,
        command_id: str | None,
    ) -> None:
        await self.hub.broadcast(
            project_id=project_id,
            message={
                "type": event_type,
                "command_id": command_id,
                "comment_id": comment_id,
                "support": summary.support,
                "oppose": summary.oppose,
            },
        )

    def _broadcast_sync(self, *, project_id: str, message: dict[str, Any]) -> None:
        self.hub.run_from_sync(self.hub.broadcast(project_id=project_id, message=message))


__all__ = [
    "ProjectRealtimePublisher",
    "comment_response_from_record",
    "reaction_response_from_record",
]
