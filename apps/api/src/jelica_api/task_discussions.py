from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload, sessionmaker

from jelica_api.comment_mentions import parse_mention_usernames, resolve_project_member_usernames
from jelica_api.models import (
    Project,
    ProjectMember,
    TaskDiscussion,
    TaskDiscussionComment,
    TaskDiscussionCommentMention,
    TaskDiscussionCommentReaction,
    User,
    WebTask,
)
from jelica_api.notification_producers import (
    NotificationProducer,
    edited_mention_source_id,
    reaction_source_id,
)
from jelica_api.notifications import NotificationService
from jelica_api.projects import (
    PROJECT_COMMENTING_ROLES,
    ProjectCommentNotFoundError,
    ProjectCommentReactionSummaryRecord,
    ProjectConflictError,
    ProjectPermissionError,
    ProjectValidationError,
    _normalize_comment_body,
)
from jelica_api.task_access import WebTaskActor, task_visibility_predicate


@dataclass(frozen=True, slots=True)
class TaskDiscussionRecord:
    task_id: str
    available: bool
    project_id: str | None
    mode: str
    is_task_owner: bool


@dataclass(frozen=True, slots=True)
class TaskDiscussionRealtimeContext:
    discussion: TaskDiscussionRecord
    role: str
    status: str


@dataclass(frozen=True, slots=True)
class TaskDiscussionMentionRecord:
    user_id: str
    username: str


@dataclass(frozen=True, slots=True)
class TaskDiscussionCommentRecord:
    comment_id: str
    task_id: str
    author_user_id: str
    author_username: str
    body: str
    created_at: datetime
    edited_at: datetime | None
    mentions: tuple[TaskDiscussionMentionRecord, ...]


@dataclass(frozen=True, slots=True)
class TaskDiscussionCommentListRecord:
    comment: TaskDiscussionCommentRecord
    reaction_summary: ProjectCommentReactionSummaryRecord


class TaskDiscussionNotFoundError(ProjectValidationError):
    pass


class TaskDiscussionService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        notification_service: NotificationService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.notification_service = notification_service

    def get_discussion(self, *, actor_user_id: str, task_id: str) -> TaskDiscussionRecord:
        with self.session_factory() as session:
            task = self._load_visible_task(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            discussion = session.get(TaskDiscussion, task.id)
            if discussion is None:
                return TaskDiscussionRecord(
                    task_id=task.core_task_id,
                    available=False,
                    project_id=None,
                    mode="unavailable",
                    is_task_owner=task.owner_user_id == actor_user_id,
                )
            return TaskDiscussionRecord(
                task_id=task.core_task_id,
                available=True,
                project_id=task.project_id,
                mode="collaborative" if task.project_id is not None else "read_only",
                is_task_owner=task.owner_user_id == actor_user_id,
            )

    def get_realtime_context(
        self, *, actor_user_id: str, task_id: str
    ) -> TaskDiscussionRealtimeContext:
        with self.session_factory() as session:
            task = self._load_visible_task(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            discussion = session.get(TaskDiscussion, task.id)
            if discussion is None:
                raise TaskDiscussionNotFoundError(
                    code="task_discussion_unavailable", message="task discussion is not available"
                )
            role = "viewer"
            status = "detached"
            if task.project_id is not None:
                member = session.execute(
                    select(ProjectMember).where(
                        ProjectMember.project_id == task.project_id,
                        ProjectMember.user_id == actor_user_id,
                    )
                ).scalar_one_or_none()
                if member is not None:
                    role = member.role
                project = session.get(Project, task.project_id)
                status = (
                    "frozen" if project is not None and project.status == "frozen" else "active"
                )
            return TaskDiscussionRealtimeContext(
                discussion=TaskDiscussionRecord(
                    task_id=task.core_task_id,
                    available=True,
                    project_id=task.project_id,
                    mode="collaborative" if task.project_id is not None else "read_only",
                    is_task_owner=task.owner_user_id == actor_user_id,
                ),
                role=role,
                status=status,
            )

    def list_project_task_ids(
        self, *, project_id: str, owner_user_id: str | None = None
    ) -> tuple[str, ...]:
        with self.session_factory() as session:
            statement = select(WebTask.core_task_id).where(WebTask.project_id == project_id)
            if owner_user_id is not None:
                statement = statement.where(WebTask.owner_user_id == owner_user_id)
            return tuple(session.execute(statement).scalars())

    def list_comments(
        self, *, actor_user_id: str, task_id: str
    ) -> tuple[TaskDiscussionCommentListRecord, ...]:
        with self.session_factory() as session:
            task, discussion = self._require_readable_discussion(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            comments = tuple(
                session.execute(
                    select(TaskDiscussionComment)
                    .options(
                        joinedload(TaskDiscussionComment.author),
                        selectinload(TaskDiscussionComment.mentions).joinedload(
                            TaskDiscussionCommentMention.mentioned_user
                        ),
                    )
                    .where(TaskDiscussionComment.task_id == discussion.task_id)
                    .order_by(TaskDiscussionComment.created_at, TaskDiscussionComment.id)
                ).scalars()
            )
            ids = tuple(comment.id for comment in comments)
            counts: dict[str, dict[str, int]] = {}
            current: dict[str, str] = {}
            if ids:
                for comment_id, reaction, count in session.execute(
                    select(
                        TaskDiscussionCommentReaction.comment_id,
                        TaskDiscussionCommentReaction.reaction,
                        func.count(),
                    )
                    .where(TaskDiscussionCommentReaction.comment_id.in_(ids))
                    .group_by(
                        TaskDiscussionCommentReaction.comment_id,
                        TaskDiscussionCommentReaction.reaction,
                    )
                ):
                    counts.setdefault(comment_id, {})[reaction] = int(count)
                current = dict(
                    session.execute(
                        select(
                            TaskDiscussionCommentReaction.comment_id,
                            TaskDiscussionCommentReaction.reaction,
                        ).where(
                            TaskDiscussionCommentReaction.comment_id.in_(ids),
                            TaskDiscussionCommentReaction.user_id == actor_user_id,
                        )
                    ).all()
                )
            return tuple(
                TaskDiscussionCommentListRecord(
                    comment=self._to_comment(comment=comment, task_id=task.core_task_id),
                    reaction_summary=ProjectCommentReactionSummaryRecord(
                        support=counts.get(comment.id, {}).get("support", 0),
                        oppose=counts.get(comment.id, {}).get("oppose", 0),
                        current_user_reaction=current.get(comment.id),
                    ),
                )
                for comment in comments
            )

    def create_comment(
        self, *, actor_user_id: str, task_id: str, body: str
    ) -> TaskDiscussionCommentRecord:
        normalized_body = _normalize_comment_body(value=body)
        with self.session_factory() as session, session.begin():
            task, discussion, member = self._require_mutable_discussion(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            author = session.get(User, actor_user_id)
            if author is None:
                raise ProjectPermissionError(
                    code="task_discussion_access_forbidden", message="task access is required"
                )
            now = datetime.now(UTC)
            comment = TaskDiscussionComment(
                task_id=discussion.task_id,
                author_user_id=actor_user_id,
                body=normalized_body,
                created_at=now,
            )
            session.add(comment)
            session.flush()
            self._sync_mentions(
                session=session,
                comment=comment,
                project_id=task.project_id,
                body=normalized_body,
                author_user_id=actor_user_id,
            )
            session.flush()
            hydrated = self._load_comment(
                session=session, task_id=discussion.task_id, comment_id=comment.id
            )
            project = None if task.project_id is None else session.get(Project, task.project_id)
            notification_payload = {
                "task_id": task.core_task_id,
                "task_name": task.name,
                "project_id": task.project_id,
                "project_name": None if project is None else project.name,
                "comment_id": comment.id,
                "actor_username": author.username,
            }
            producer = NotificationProducer(self.notification_service)
            producer.enqueue(
                session=session,
                recipient_user_ids=(
                    ()
                    if task.project_id is None
                    else producer.project_member_user_ids(
                        session=session,
                        project_id=task.project_id,
                    )
                ),
                event_id="task_discussion.comment.created",
                source_type="task_discussion_comment",
                source_id=comment.id,
                actor_user_id=actor_user_id,
                payload=notification_payload,
            )
            producer.enqueue(
                session=session,
                recipient_user_ids=(mention.mentioned_user_id for mention in hydrated.mentions),
                event_id="task_discussion.comment.mentioned",
                source_type="task_discussion_comment",
                source_id=comment.id,
                actor_user_id=actor_user_id,
                payload=notification_payload,
            )
            return self._to_comment(comment=hydrated, task_id=task.core_task_id)

    def edit_comment(
        self, *, actor_user_id: str, task_id: str, comment_id: str, body: str
    ) -> TaskDiscussionCommentRecord:
        normalized_body = _normalize_comment_body(value=body)
        with self.session_factory() as session, session.begin():
            task, discussion, member = self._require_mutable_discussion(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            comment = self._load_comment(
                session=session, task_id=discussion.task_id, comment_id=comment_id
            )
            if comment.author_user_id != actor_user_id:
                raise ProjectPermissionError(
                    code="task_discussion_comment_author_required",
                    message="only the comment author may edit its text",
                )
            now = datetime.now(UTC)
            comment.body = normalized_body
            comment.edited_at = now
            new_mentioned_user_ids = self._sync_mentions(
                session=session,
                comment=comment,
                project_id=task.project_id,
                body=normalized_body,
                author_user_id=actor_user_id,
            )
            session.flush()
            hydrated = self._load_comment(
                session=session, task_id=discussion.task_id, comment_id=comment.id
            )
            project = None if task.project_id is None else session.get(Project, task.project_id)
            producer = NotificationProducer(self.notification_service)
            for mentioned_user_id in new_mentioned_user_ids:
                producer.enqueue(
                    session=session,
                    recipient_user_ids=(mentioned_user_id,),
                    event_id="task_discussion.comment.mentioned",
                    source_type="task_discussion_comment",
                    source_id=edited_mention_source_id(
                        comment_id=comment.id,
                        mentioned_user_id=mentioned_user_id,
                        edited_at_iso=now.isoformat(),
                    ),
                    actor_user_id=actor_user_id,
                    payload={
                        "task_id": task.core_task_id,
                        "task_name": task.name,
                        "project_id": task.project_id,
                        "project_name": None if project is None else project.name,
                        "comment_id": comment.id,
                        "actor_username": hydrated.author.username,
                    },
                )
            return self._to_comment(comment=hydrated, task_id=task.core_task_id)

    def delete_comment(self, *, actor_user_id: str, task_id: str, comment_id: str) -> None:
        with self.session_factory() as session, session.begin():
            task = self._load_visible_task(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            discussion = session.get(TaskDiscussion, task.id)
            if discussion is None:
                raise TaskDiscussionNotFoundError(
                    code="task_discussion_unavailable", message="task discussion is not available"
                )
            comment = self._load_comment(
                session=session, task_id=discussion.task_id, comment_id=comment_id
            )
            if task.owner_user_id == actor_user_id:
                self._enqueue_admin_removal_notification(
                    session=session,
                    task=task,
                    comment=comment,
                    actor_user_id=actor_user_id,
                )
                session.delete(comment)
                return
            _, _, member = self._require_mutable_discussion(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            if comment.author_user_id != actor_user_id and member.role != "supervisor":
                raise ProjectPermissionError(
                    code="task_discussion_comment_delete_forbidden",
                    message="comment author or supervisor access is required",
                )
            self._enqueue_admin_removal_notification(
                session=session,
                task=task,
                comment=comment,
                actor_user_id=actor_user_id,
            )
            session.delete(comment)

    def clear_discussion(self, *, actor_user_id: str, task_id: str) -> None:
        with self.session_factory() as session, session.begin():
            task = self._load_visible_task(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            if task.owner_user_id != actor_user_id:
                raise ProjectPermissionError(
                    code="task_discussion_owner_required",
                    message="immutable task owner access is required",
                )
            discussion = session.get(TaskDiscussion, task.id)
            if discussion is None:
                raise TaskDiscussionNotFoundError(
                    code="task_discussion_unavailable", message="task discussion is not available"
                )
            session.query(TaskDiscussionComment).filter(
                TaskDiscussionComment.task_id == discussion.task_id
            ).delete(synchronize_session=False)

    def set_reaction(
        self, *, actor_user_id: str, task_id: str, comment_id: str, reaction: str
    ) -> ProjectCommentReactionSummaryRecord:
        if reaction not in {"support", "oppose"}:
            raise ProjectValidationError(
                code="task_discussion_reaction_invalid",
                message="reaction must be support or oppose",
            )
        with self.session_factory() as session, session.begin():
            _, discussion, _ = self._require_mutable_discussion(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            comment = self._load_comment(
                session=session, task_id=discussion.task_id, comment_id=comment_id
            )
            if comment.author_user_id == actor_user_id:
                raise ProjectPermissionError(
                    code="task_discussion_self_reaction_forbidden",
                    message="users cannot react to their own task comments",
                )
            persisted = session.get(TaskDiscussionCommentReaction, (comment.id, actor_user_id))
            previous_reaction = None if persisted is None else persisted.reaction
            changed = persisted is None or persisted.reaction != reaction
            if persisted is None:
                session.add(
                    TaskDiscussionCommentReaction(
                        comment_id=comment.id, user_id=actor_user_id, reaction=reaction
                    )
                )
            else:
                persisted.reaction = reaction
            session.flush()
            if changed:
                task = session.get(WebTask, discussion.task_id)
                project = (
                    None
                    if task is None or task.project_id is None
                    else session.get(Project, task.project_id)
                )
                actor = session.get(User, actor_user_id)
                occurred_at = datetime.now(UTC)
                NotificationProducer(self.notification_service).enqueue(
                    session=session,
                    recipient_user_ids=(comment.author_user_id,),
                    event_id="task_discussion.comment.reacted",
                    source_type="task_discussion_comment_reaction",
                    source_id=reaction_source_id(
                        comment_id=comment.id,
                        actor_user_id=actor_user_id,
                        previous_reaction=previous_reaction,
                        reaction=reaction,
                        occurred_at_iso=occurred_at.isoformat(),
                    ),
                    actor_user_id=actor_user_id,
                    payload={
                        "task_id": task_id,
                        "task_name": None if task is None else task.name,
                        "project_id": None if task is None else task.project_id,
                        "project_name": None if project is None else project.name,
                        "comment_id": comment.id,
                        "actor_username": None if actor is None else actor.username,
                        "reaction": reaction,
                    },
                )
            return self._reaction_summary(
                session=session, comment_id=comment.id, actor_user_id=actor_user_id
            )

    def delete_reaction(
        self, *, actor_user_id: str, task_id: str, comment_id: str
    ) -> ProjectCommentReactionSummaryRecord:
        with self.session_factory() as session, session.begin():
            _, discussion, _ = self._require_mutable_discussion(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            comment = self._load_comment(
                session=session, task_id=discussion.task_id, comment_id=comment_id
            )
            persisted = session.get(TaskDiscussionCommentReaction, (comment.id, actor_user_id))
            if persisted is not None:
                session.delete(persisted)
                session.flush()
            return self._reaction_summary(
                session=session, comment_id=comment.id, actor_user_id=actor_user_id
            )

    def get_reactions(
        self, *, actor_user_id: str, task_id: str, comment_id: str
    ) -> ProjectCommentReactionSummaryRecord:
        with self.session_factory() as session:
            _, discussion = self._require_readable_discussion(
                session=session, actor_user_id=actor_user_id, task_id=task_id
            )
            comment = self._load_comment(
                session=session, task_id=discussion.task_id, comment_id=comment_id
            )
            return self._reaction_summary(
                session=session, comment_id=comment.id, actor_user_id=actor_user_id
            )

    def _enqueue_admin_removal_notification(
        self,
        *,
        session: Session,
        task: WebTask,
        comment: TaskDiscussionComment,
        actor_user_id: str,
    ) -> None:
        if comment.author_user_id == actor_user_id:
            return
        project = None if task.project_id is None else session.get(Project, task.project_id)
        actor = session.get(User, actor_user_id)
        NotificationProducer(self.notification_service).enqueue(
            session=session,
            recipient_user_ids=(comment.author_user_id,),
            event_id="task_discussion.comment.removed_by_admin",
            source_type="task_discussion_comment",
            source_id=comment.id,
            actor_user_id=actor_user_id,
            payload={
                "task_id": task.core_task_id,
                "task_name": task.name,
                "project_id": task.project_id,
                "project_name": None if project is None else project.name,
                "comment_id": comment.id,
                "actor_username": None if actor is None else actor.username,
            },
        )

    def _load_visible_task(self, *, session: Session, actor_user_id: str, task_id: str) -> WebTask:
        task = session.execute(
            select(WebTask).where(
                WebTask.core_task_id == task_id.strip(),
                task_visibility_predicate(actor=WebTaskActor(user_id=actor_user_id)),
            )
        ).scalar_one_or_none()
        if task is None:
            raise ProjectPermissionError(code="task_not_found", message="task was not found")
        return task

    def _require_readable_discussion(
        self, *, session: Session, actor_user_id: str, task_id: str
    ) -> tuple[WebTask, TaskDiscussion]:
        task = self._load_visible_task(
            session=session, actor_user_id=actor_user_id, task_id=task_id
        )
        discussion = session.get(TaskDiscussion, task.id)
        if discussion is None:
            raise TaskDiscussionNotFoundError(
                code="task_discussion_unavailable", message="task discussion is not available"
            )
        return task, discussion

    def _require_mutable_discussion(
        self, *, session: Session, actor_user_id: str, task_id: str
    ) -> tuple[WebTask, TaskDiscussion, ProjectMember]:
        task, discussion = self._require_readable_discussion(
            session=session, actor_user_id=actor_user_id, task_id=task_id
        )
        if task.project_id is None:
            raise ProjectConflictError(
                code="discussion_read_only", message="detached task discussion is read-only"
            )
        project = session.get(Project, task.project_id)
        if project is None:
            raise ProjectConflictError(
                code="discussion_read_only", message="task project is unavailable"
            )
        if project.status == "frozen":
            raise ProjectConflictError(
                code="project_frozen",
                message="task discussion is read-only while the project is frozen",
            )
        member = session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == task.project_id, ProjectMember.user_id == actor_user_id
            )
        ).scalar_one_or_none()
        if member is None or member.role not in PROJECT_COMMENTING_ROLES:
            raise ProjectPermissionError(
                code="task_discussion_comment_role_required",
                message="commenter, member, or supervisor access is required",
            )
        return task, discussion, member

    @staticmethod
    def _load_comment(*, session: Session, task_id: str, comment_id: str) -> TaskDiscussionComment:
        comment = session.execute(
            select(TaskDiscussionComment)
            .options(
                joinedload(TaskDiscussionComment.author),
                selectinload(TaskDiscussionComment.mentions).joinedload(
                    TaskDiscussionCommentMention.mentioned_user
                ),
            )
            .where(
                TaskDiscussionComment.id == comment_id.strip(),
                TaskDiscussionComment.task_id == task_id,
            )
        ).scalar_one_or_none()
        if comment is None:
            raise ProjectCommentNotFoundError(
                code="task_discussion_comment_not_found",
                message="task discussion comment was not found",
            )
        return comment

    @staticmethod
    def _sync_mentions(
        *,
        session: Session,
        comment: TaskDiscussionComment,
        project_id: str | None,
        body: str,
        author_user_id: str,
    ) -> tuple[str, ...]:
        existing = {
            row.mentioned_user_id: row
            for row in session.execute(
                select(TaskDiscussionCommentMention).where(
                    TaskDiscussionCommentMention.comment_id == comment.id
                )
            ).scalars()
        }
        resolved = (
            resolve_project_member_usernames(
                session=session, project_id=project_id, usernames=parse_mention_usernames(body)
            )
            if project_id
            else {}
        )
        desired = {user.id for user in resolved.values() if user.id != author_user_id}
        for user_id, row in existing.items():
            if user_id not in desired:
                session.delete(row)
        new_user_ids = desired - existing.keys()
        for user_id in new_user_ids:
            session.add(
                TaskDiscussionCommentMention(
                    comment_id=comment.id, mentioned_user_id=user_id, created_at=datetime.now(UTC)
                )
            )
        return tuple(sorted(new_user_ids))

    @staticmethod
    def _to_comment(*, comment: TaskDiscussionComment, task_id: str) -> TaskDiscussionCommentRecord:
        return TaskDiscussionCommentRecord(
            comment_id=comment.id,
            task_id=task_id,
            author_user_id=comment.author_user_id,
            author_username=comment.author.username,
            body=comment.body,
            created_at=comment.created_at,
            edited_at=comment.edited_at,
            mentions=tuple(
                TaskDiscussionMentionRecord(
                    user_id=row.mentioned_user_id, username=row.mentioned_user.username
                )
                for row in sorted(
                    comment.mentions,
                    key=lambda row: (row.mentioned_user.username, row.mentioned_user_id),
                )
            ),
        )

    @staticmethod
    def _reaction_summary(
        *, session: Session, comment_id: str, actor_user_id: str
    ) -> ProjectCommentReactionSummaryRecord:
        counts = dict(
            session.execute(
                select(TaskDiscussionCommentReaction.reaction, func.count())
                .where(TaskDiscussionCommentReaction.comment_id == comment_id)
                .group_by(TaskDiscussionCommentReaction.reaction)
            ).all()
        )
        current = session.get(TaskDiscussionCommentReaction, (comment_id, actor_user_id))
        return ProjectCommentReactionSummaryRecord(
            support=int(counts.get("support", 0)),
            oppose=int(counts.get("oppose", 0)),
            current_user_reaction=None if current is None else current.reaction,
        )


__all__ = [
    "TaskDiscussionCommentListRecord",
    "TaskDiscussionCommentRecord",
    "TaskDiscussionMentionRecord",
    "TaskDiscussionNotFoundError",
    "TaskDiscussionRecord",
    "TaskDiscussionService",
    "TaskDiscussionRealtimeContext",
]
