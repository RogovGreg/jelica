from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, delete, exists, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload, sessionmaker

from jelica_api.comment_mentions import parse_mention_usernames, resolve_project_member_usernames
from jelica_api.models import (
    Project,
    ProjectComment,
    ProjectCommentMention,
    ProjectCommentReaction,
    ProjectHistoryEvent,
    ProjectInvitation,
    ProjectMember,
    TaskDiscussion,
    User,
    WebTask,
)
from jelica_api.notification_producers import (
    NotificationProducer,
    edited_mention_source_id,
    reaction_source_id,
)
from jelica_api.notifications import NotificationService

PROJECT_STATUSES = frozenset({"active", "frozen"})
PROJECT_MEMBER_ROLES = frozenset({"viewer", "commenter", "member", "supervisor"})
PROJECT_RELATIONS = frozenset({"any", "owned", "participating"})
PROJECT_INVITATION_STORED_STATUSES = frozenset({"pending", "accepted", "declined", "revoked"})
PROJECT_INVITATION_EFFECTIVE_STATUSES = frozenset({*PROJECT_INVITATION_STORED_STATUSES, "expired"})
PROJECT_INVITATION_EXPIRATION = timedelta(days=30)
PROJECT_COMMENT_MAX_LENGTH = 10_000
PROJECT_COMMENTING_ROLES = frozenset({"commenter", "member", "supervisor"})
PROJECT_COMMENT_REACTIONS = frozenset({"support", "oppose"})
PROJECT_HISTORY_EVENT_TYPES = frozenset(
    {
        "project_created",
        "project_updated",
        "project_frozen",
        "project_unfrozen",
        "member_joined",
        "member_removed",
        "member_role_changed",
        "ownership_transferred",
        "task_attached",
        "task_detached",
    }
)


class ProjectDomainError(ValueError):
    """Base error for a rejected Projects-domain operation."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ProjectNotFoundError(ProjectDomainError):
    pass


class ProjectMemberNotFoundError(ProjectDomainError):
    pass


class ProjectTaskNotFoundError(ProjectDomainError):
    pass


class ProjectInvitationNotFoundError(ProjectDomainError):
    pass


class ProjectInvitationTargetNotFoundError(ProjectDomainError):
    pass


class ProjectCommentNotFoundError(ProjectDomainError):
    pass


class ProjectPermissionError(ProjectDomainError):
    pass


class ProjectValidationError(ProjectDomainError):
    pass


class ProjectConflictError(ProjectDomainError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    project_id: str
    name: str
    description: str | None
    status: str
    created_by_user_id: str
    owner_user_id: str
    current_user_role: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectMemberRecord:
    project_id: str
    user_id: str
    username: str
    email: str
    role: str
    joined_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectTaskRecord:
    task_id: str
    name: str | None
    state: str
    owner_user_id: str | None
    project_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectHistoryRecord:
    event_id: str
    project_id: str
    actor_user_id: str | None
    subject_user_id: str | None
    event_type: str
    data: dict[str, Any] | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectInvitationRecord:
    invitation_id: str
    project_id: str
    project_name: str
    invited_user_id: str
    invited_username: str
    invited_by_user_id: str
    inviter_username: str
    role: str
    status: str
    invited_at: datetime
    expires_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProjectInvitationCandidateRecord:
    user_id: str
    username: str


@dataclass(frozen=True, slots=True)
class ProjectCommentRecord:
    comment_id: str
    project_id: str
    author_user_id: str
    author_username: str
    body: str
    created_at: datetime
    edited_at: datetime | None
    mentions: tuple[ProjectCommentMentionRecord, ...]


@dataclass(frozen=True, slots=True)
class ProjectCommentMentionRecord:
    user_id: str
    username: str


@dataclass(frozen=True, slots=True)
class ProjectCommentReactionSummaryRecord:
    support: int
    oppose: int
    current_user_reaction: str | None


@dataclass(frozen=True, slots=True)
class ProjectCommentListRecord:
    comment: ProjectCommentRecord
    reaction_summary: ProjectCommentReactionSummaryRecord


@dataclass(frozen=True, slots=True)
class ProjectService:
    session_factory: sessionmaker[Session]
    notification_service: NotificationService | None = None

    def create_project(
        self,
        *,
        actor_user_id: str,
        name: str,
        description: str | None,
        status: str = "active",
    ) -> ProjectRecord:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_name = _normalize_project_name(value=name)
        normalized_description = _normalize_description(value=description)
        normalized_status = _require_project_status(value=status)
        occurred_at = datetime.now(UTC)

        with self.session_factory() as session, session.begin():
            project = Project(
                name=normalized_name,
                description=normalized_description,
                status=normalized_status,
                created_by_user_id=normalized_actor_id,
                owner_user_id=normalized_actor_id,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            session.add(project)
            session.flush()
            session.add(
                ProjectMember(
                    project_id=project.id,
                    user_id=normalized_actor_id,
                    role="supervisor",
                    joined_at=occurred_at,
                )
            )
            _add_history_event(
                session=session,
                project_id=project.id,
                actor_user_id=normalized_actor_id,
                event_type="project_created",
                occurred_at=occurred_at,
                data={"status": normalized_status},
            )
            _add_history_event(
                session=session,
                project_id=project.id,
                actor_user_id=normalized_actor_id,
                subject_user_id=normalized_actor_id,
                event_type="member_joined",
                data={"role": "supervisor"},
            )
            session.flush()
            return _to_project_record(project=project, current_user_role="supervisor")

    def list_projects(
        self,
        *,
        actor_user_id: str,
        relations: tuple[str, ...] = ("any",),
        statuses: tuple[str, ...] = (),
    ) -> tuple[ProjectRecord, ...]:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_relations = _normalize_filter_values(
            values=relations or ("any",),
            field_name="relation",
            allowed=PROJECT_RELATIONS,
        )
        normalized_statuses = _normalize_filter_values(
            values=statuses,
            field_name="status",
            allowed=PROJECT_STATUSES,
        )

        with self.session_factory() as session:
            relation_predicates = []
            if "any" in normalized_relations or "owned" in normalized_relations:
                relation_predicates.append(Project.owner_user_id == normalized_actor_id)
            if "any" in normalized_relations or "participating" in normalized_relations:
                relation_predicates.append(
                    (Project.owner_user_id != normalized_actor_id)
                    & (ProjectMember.user_id == normalized_actor_id)
                )

            relation_filter = relation_predicates[0]
            if len(relation_predicates) > 1:
                relation_filter = relation_predicates[0] | relation_predicates[1]
            statement = (
                select(Project, ProjectMember.role)
                .outerjoin(
                    ProjectMember,
                    and_(
                        ProjectMember.project_id == Project.id,
                        ProjectMember.user_id == normalized_actor_id,
                    ),
                )
                .where(relation_filter)
            )
            if normalized_statuses:
                statement = statement.where(Project.status.in_(normalized_statuses))
            statement = statement.order_by(
                Project.updated_at.desc(),
                Project.created_at.desc(),
                Project.id.desc(),
            )
            projects = session.execute(statement).all()
            return tuple(
                _to_project_record(project=project, current_user_role=role)
                for project, role in projects
            )

    def get_project(self, *, actor_user_id: str, project_id: str) -> ProjectRecord:
        with self.session_factory() as session:
            project = _load_project(session=session, project_id=project_id)
            member = _require_membership(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            return _to_project_record(project=project, current_user_role=member.role)

    def update_project(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        changes: dict[str, object],
    ) -> ProjectRecord:
        allowed_fields = {"name", "description", "status"}
        unknown_fields = changes.keys() - allowed_fields
        if unknown_fields:
            raise ProjectValidationError(
                code="project_update_invalid",
                message=f"unsupported project fields: {', '.join(sorted(unknown_fields))}",
            )
        if not changes:
            raise ProjectValidationError(
                code="project_update_empty",
                message="at least one project field must be provided",
            )

        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            _require_supervisor(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            changed_fields: list[str] = []
            old_status = project.status
            if "name" in changes:
                name = _normalize_project_name(value=changes["name"])
                if name != project.name:
                    project.name = name
                    changed_fields.append("name")
            if "description" in changes:
                description = _normalize_description(value=changes["description"])
                if description != project.description:
                    project.description = description
                    changed_fields.append("description")
            if "status" in changes:
                status = _require_project_status(value=changes["status"])
                if status != project.status:
                    project.status = status
                    changed_fields.append("status")

            if not changed_fields:
                return _to_project_record(project=project)

            occurred_at = datetime.now(UTC)
            project.updated_at = occurred_at
            content_fields = [field for field in changed_fields if field != "status"]
            if content_fields:
                _add_history_event(
                    session=session,
                    project_id=project.id,
                    actor_user_id=actor_user_id,
                    event_type="project_updated",
                    occurred_at=occurred_at,
                    data={"fields": content_fields},
                )
            if "status" in changed_fields:
                status_event = _add_history_event(
                    session=session,
                    project_id=project.id,
                    actor_user_id=actor_user_id,
                    event_type=(
                        "project_frozen" if project.status == "frozen" else "project_unfrozen"
                    ),
                    occurred_at=occurred_at,
                    data={"previous_status": old_status},
                )
                NotificationProducer(self.notification_service).enqueue(
                    session=session,
                    recipient_user_ids=NotificationProducer.project_member_user_ids(
                        session=session,
                        project_id=project.id,
                    ),
                    event_id=(
                        "project.frozen" if project.status == "frozen" else "project.unfrozen"
                    ),
                    source_type="project_history",
                    source_id=_history_event_source_id(status_event),
                    actor_user_id=actor_user_id,
                    payload={
                        "project_id": project.id,
                        "project_name": project.name,
                        "previous_status": old_status,
                        "status": project.status,
                    },
                )
            session.flush()
            return _to_project_record(project=project)

    def delete_project(self, *, actor_user_id: str, project_id: str) -> None:
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            _require_owner(project=project, actor_user_id=actor_user_id)
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=NotificationProducer.project_member_user_ids(
                    session=session,
                    project_id=project.id,
                ),
                event_id="project.deleted",
                source_type="project",
                source_id=project.id,
                actor_user_id=actor_user_id,
                payload={"project_id": project.id, "project_name": project.name},
            )
            tasks = session.execute(
                select(WebTask).where(WebTask.project_id == project.id).with_for_update()
            ).scalars()
            for task in tasks:
                task.project_id = None
            session.flush()
            session.execute(
                delete(ProjectHistoryEvent).where(ProjectHistoryEvent.project_id == project.id)
            )
            session.execute(delete(ProjectMember).where(ProjectMember.project_id == project.id))
            session.delete(project)

    def transfer_ownership(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        new_owner_user_id: str,
    ) -> ProjectRecord:
        normalized_new_owner_id = _require_id(
            value=new_owner_user_id,
            field_name="new_owner_user_id",
        )
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            _require_owner(project=project, actor_user_id=actor_user_id)
            if normalized_new_owner_id == project.owner_user_id:
                raise ProjectValidationError(
                    code="project_owner_unchanged",
                    message="new owner must differ from the current owner",
                )
            new_owner_member = _load_membership(
                session=session,
                project_id=project.id,
                user_id=normalized_new_owner_id,
                lock=True,
            )
            if new_owner_member is None:
                raise ProjectValidationError(
                    code="project_transfer_target_not_member",
                    message="new owner must already be a project member",
                )

            previous_owner_id = project.owner_user_id
            if new_owner_member.role != "supervisor":
                previous_role = new_owner_member.role
                new_owner_member.role = "supervisor"
                role_event = _add_history_event(
                    session=session,
                    project_id=project.id,
                    actor_user_id=actor_user_id,
                    subject_user_id=normalized_new_owner_id,
                    event_type="member_role_changed",
                    data={"previous_role": previous_role, "role": "supervisor"},
                )
                NotificationProducer(self.notification_service).enqueue(
                    session=session,
                    recipient_user_ids=(normalized_new_owner_id,),
                    event_id="project.member.role_changed",
                    source_type="project_history",
                    source_id=_history_event_source_id(role_event),
                    actor_user_id=actor_user_id,
                    payload={
                        "project_id": project.id,
                        "project_name": project.name,
                        "previous_role": previous_role,
                        "role": "supervisor",
                    },
                )

            project.owner_user_id = normalized_new_owner_id
            project.updated_at = datetime.now(UTC)
            ownership_event = _add_history_event(
                session=session,
                project_id=project.id,
                actor_user_id=actor_user_id,
                subject_user_id=normalized_new_owner_id,
                event_type="ownership_transferred",
                data={
                    "previous_owner_user_id": previous_owner_id,
                    "new_owner_user_id": normalized_new_owner_id,
                },
            )
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=(normalized_new_owner_id, previous_owner_id),
                event_id="project.ownership.transferred",
                source_type="project_history",
                source_id=_history_event_source_id(ownership_event),
                actor_user_id=actor_user_id,
                payload={
                    "project_id": project.id,
                    "project_name": project.name,
                    "previous_owner_user_id": previous_owner_id,
                    "new_owner_user_id": normalized_new_owner_id,
                },
            )
            session.flush()
            return _to_project_record(project=project)

    def list_members(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        roles: tuple[str, ...] = (),
    ) -> tuple[ProjectMemberRecord, ...]:
        normalized_roles = _normalize_filter_values(
            values=roles,
            field_name="role",
            allowed=PROJECT_MEMBER_ROLES,
        )
        with self.session_factory() as session:
            project = _load_project(session=session, project_id=project_id)
            _require_membership(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            statement = (
                select(ProjectMember)
                .options(joinedload(ProjectMember.user))
                .where(ProjectMember.project_id == project.id)
            )
            if normalized_roles:
                statement = statement.where(ProjectMember.role.in_(normalized_roles))
            members = session.execute(
                statement.order_by(ProjectMember.joined_at, ProjectMember.user_id)
            ).scalars()
            return tuple(_to_member_record(member=member) for member in members)

    def update_member_role(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        user_id: str,
        role: str,
    ) -> ProjectMemberRecord:
        normalized_user_id = _require_id(value=user_id, field_name="user_id")
        normalized_role = _require_member_role(value=role)
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            _require_supervisor(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            member = _load_membership(
                session=session,
                project_id=project.id,
                user_id=normalized_user_id,
                lock=True,
            )
            if member is None:
                raise ProjectMemberNotFoundError(
                    code="project_member_not_found",
                    message="project member was not found",
                )
            if normalized_user_id == project.owner_user_id and normalized_role != "supervisor":
                raise ProjectConflictError(
                    code="project_owner_role_protected",
                    message="transfer ownership before lowering the owner's supervisor role",
                )
            if member.role == normalized_role:
                return _to_member_record(member=member)

            previous_role = member.role
            member.role = normalized_role
            role_event = _add_history_event(
                session=session,
                project_id=project.id,
                actor_user_id=actor_user_id,
                subject_user_id=normalized_user_id,
                event_type="member_role_changed",
                data={"previous_role": previous_role, "role": normalized_role},
            )
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=(normalized_user_id,),
                event_id="project.member.role_changed",
                source_type="project_history",
                source_id=_history_event_source_id(role_event),
                actor_user_id=actor_user_id,
                payload={
                    "project_id": project.id,
                    "project_name": project.name,
                    "previous_role": previous_role,
                    "role": normalized_role,
                },
            )
            session.flush()
            return _to_member_record(member=member)

    def remove_member(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        user_id: str,
    ) -> None:
        normalized_user_id = _require_id(value=user_id, field_name="user_id")
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            _require_supervisor(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            self._remove_member_in_session(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
                removed_user_id=normalized_user_id,
            )

    def leave_project(self, *, actor_user_id: str, project_id: str) -> None:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            _require_membership(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
            )
            self._remove_member_in_session(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
                removed_user_id=normalized_actor_id,
            )

    def create_invitation(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        invited_user_id: str,
        role: str,
    ) -> ProjectInvitationRecord:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_invited_user_id = _require_id(
            value=invited_user_id,
            field_name="invited_user_id",
        )
        normalized_role = _require_member_role(value=role)
        now = datetime.now(UTC)
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            _require_supervisor(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
            )
            if project.status == "frozen":
                raise ProjectConflictError(
                    code="project_frozen",
                    message="new invitations cannot be created for a frozen project",
                )
            invited_user = session.get(User, normalized_invited_user_id)
            if invited_user is None:
                raise ProjectInvitationTargetNotFoundError(
                    code="project_invitation_target_not_found",
                    message="invited user was not found",
                )
            if (
                _load_membership(
                    session=session,
                    project_id=project.id,
                    user_id=normalized_invited_user_id,
                )
                is not None
            ):
                raise ProjectConflictError(
                    code="project_invitation_target_already_member",
                    message="invited user is already a project member",
                )
            active_invitation = session.execute(
                select(ProjectInvitation.id)
                .where(
                    ProjectInvitation.project_id == project.id,
                    ProjectInvitation.invited_user_id == normalized_invited_user_id,
                    ProjectInvitation.status == "pending",
                    ProjectInvitation.expires_at > now,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if active_invitation is not None:
                raise ProjectConflictError(
                    code="project_invitation_active_duplicate",
                    message="an active invitation already exists for this user and project",
                )
            inviter = session.get(User, normalized_actor_id)
            if inviter is None:
                raise ProjectPermissionError(
                    code="project_supervisor_required",
                    message="project supervisor access is required",
                )
            invitation = ProjectInvitation(
                project_id=project.id,
                invited_user_id=invited_user.id,
                invited_by_user_id=inviter.id,
                role=normalized_role,
                status="pending",
                invited_at=now,
                expires_at=now + PROJECT_INVITATION_EXPIRATION,
                resolved_at=None,
            )
            session.add(invitation)
            session.flush()
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=(invited_user.id,),
                event_id="project.invitation.received",
                source_type="project_invitation",
                source_id=invitation.id,
                actor_user_id=normalized_actor_id,
                payload={
                    "invitation_id": invitation.id,
                    "project_id": project.id,
                    "project_name": project.name,
                    "inviter_username": inviter.username,
                    "role": invitation.role,
                },
            )
            return _to_invitation_record(
                invitation=invitation,
                project_name=project.name,
                inviter_username=inviter.username,
                now=now,
            )

    def list_project_invitations(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        statuses: tuple[str, ...] = (),
        roles: tuple[str, ...] = (),
        invited_user_ids: tuple[str, ...] = (),
    ) -> tuple[ProjectInvitationRecord, ...]:
        normalized_statuses = _normalize_filter_values(
            values=statuses,
            field_name="invitation_status",
            allowed=PROJECT_INVITATION_EFFECTIVE_STATUSES,
        )
        normalized_roles = _normalize_filter_values(
            values=roles,
            field_name="role",
            allowed=PROJECT_MEMBER_ROLES,
        )
        normalized_invited_user_ids = _normalize_ids(
            values=invited_user_ids,
            field_name="invited_user_id",
        )
        now = datetime.now(UTC)
        with self.session_factory() as session:
            project = _load_project(session=session, project_id=project_id)
            _require_supervisor(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            statement = (
                select(ProjectInvitation)
                .options(
                    joinedload(ProjectInvitation.invited_by),
                    joinedload(ProjectInvitation.invited_user),
                )
                .where(ProjectInvitation.project_id == project.id)
            )
            if normalized_statuses:
                statement = statement.where(
                    or_(*_invitation_status_predicates(statuses=normalized_statuses, now=now))
                )
            if normalized_roles:
                statement = statement.where(ProjectInvitation.role.in_(normalized_roles))
            if normalized_invited_user_ids:
                statement = statement.where(
                    ProjectInvitation.invited_user_id.in_(normalized_invited_user_ids)
                )
            invitations = session.execute(
                statement.order_by(
                    ProjectInvitation.invited_at.desc(),
                    ProjectInvitation.id.desc(),
                )
            ).scalars()
            return tuple(
                _to_invitation_record(
                    invitation=invitation,
                    project_name=project.name,
                    inviter_username=invitation.invited_by.username,
                    now=now,
                )
                for invitation in invitations
            )

    def list_received_invitations(
        self,
        *,
        actor_user_id: str,
        statuses: tuple[str, ...] = (),
        roles: tuple[str, ...] = (),
        project_ids: tuple[str, ...] = (),
    ) -> tuple[ProjectInvitationRecord, ...]:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_statuses = _normalize_filter_values(
            values=statuses,
            field_name="invitation_status",
            allowed=PROJECT_INVITATION_EFFECTIVE_STATUSES,
        )
        normalized_roles = _normalize_filter_values(
            values=roles,
            field_name="role",
            allowed=PROJECT_MEMBER_ROLES,
        )
        normalized_project_ids = _normalize_ids(values=project_ids, field_name="project_id")
        now = datetime.now(UTC)
        with self.session_factory() as session:
            statement = (
                select(ProjectInvitation)
                .options(
                    joinedload(ProjectInvitation.project),
                    joinedload(ProjectInvitation.invited_by),
                    joinedload(ProjectInvitation.invited_user),
                )
                .where(ProjectInvitation.invited_user_id == normalized_actor_id)
            )
            if normalized_statuses:
                statement = statement.where(
                    or_(*_invitation_status_predicates(statuses=normalized_statuses, now=now))
                )
            if normalized_roles:
                statement = statement.where(ProjectInvitation.role.in_(normalized_roles))
            if normalized_project_ids:
                statement = statement.where(
                    ProjectInvitation.project_id.in_(normalized_project_ids)
                )
            invitations = session.execute(
                statement.order_by(
                    ProjectInvitation.invited_at.desc(),
                    ProjectInvitation.id.desc(),
                )
            ).scalars()
            return tuple(
                _to_invitation_record(
                    invitation=invitation,
                    project_name=invitation.project.name,
                    inviter_username=invitation.invited_by.username,
                    now=now,
                )
                for invitation in invitations
            )

    def list_invitation_candidates(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        query: str,
        limit: int = 10,
    ) -> tuple[ProjectInvitationCandidateRecord, ...]:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_query = _normalize_invitation_candidate_query(value=query)
        normalized_limit = _require_invitation_candidate_limit(value=limit)
        now = datetime.now(UTC)
        escaped_query = _escape_like_prefix(value=normalized_query)
        with self.session_factory() as session:
            project = _load_project(session=session, project_id=project_id)
            _require_supervisor(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
            )
            current_member = exists().where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == User.id,
            )
            active_invitation = exists().where(
                ProjectInvitation.project_id == project.id,
                ProjectInvitation.invited_user_id == User.id,
                ProjectInvitation.status == "pending",
                ProjectInvitation.expires_at > now,
            )
            normalized_username = func.lower(User.username)
            candidates = session.execute(
                select(User)
                .where(
                    User.id != normalized_actor_id,
                    ~current_member,
                    ~active_invitation,
                    normalized_username.like(
                        f"{escaped_query}%",
                        escape="\\",
                    ),
                )
                .order_by(
                    case((normalized_username == normalized_query, 0), else_=1),
                    normalized_username,
                    User.id,
                )
                .limit(normalized_limit)
            ).scalars()
            return tuple(
                ProjectInvitationCandidateRecord(user_id=user.id, username=user.username)
                for user in candidates
            )

    def accept_invitation(
        self,
        *,
        actor_user_id: str,
        invitation_id: str,
    ) -> ProjectInvitationRecord:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_invitation_id = _require_id(
            value=invitation_id,
            field_name="invitation_id",
        )
        with self.session_factory() as session, session.begin():
            visible_invitation = _load_invitation(
                session=session,
                invitation_id=normalized_invitation_id,
                invited_user_id=normalized_actor_id,
            )
            project = _load_project(
                session=session,
                project_id=visible_invitation.project_id,
                lock=True,
            )
            invitation = _load_invitation(
                session=session,
                invitation_id=normalized_invitation_id,
                project_id=project.id,
                invited_user_id=normalized_actor_id,
                lock=True,
            )
            now = datetime.now(UTC)
            _require_pending_invitation(invitation=invitation, now=now)
            if project.status == "frozen":
                raise ProjectConflictError(
                    code="project_frozen",
                    message="invitations cannot be accepted while the project is frozen",
                )
            if (
                _load_membership(
                    session=session,
                    project_id=project.id,
                    user_id=normalized_actor_id,
                    lock=True,
                )
                is not None
            ):
                raise ProjectConflictError(
                    code="project_invitation_target_already_member",
                    message="invited user is already a project member",
                )
            session.add(
                ProjectMember(
                    project_id=project.id,
                    user_id=normalized_actor_id,
                    role=invitation.role,
                    joined_at=now,
                )
            )
            invitation.status = "accepted"
            invitation.resolved_at = now
            _add_history_event(
                session=session,
                project_id=project.id,
                actor_user_id=normalized_actor_id,
                subject_user_id=normalized_actor_id,
                event_type="member_joined",
                occurred_at=now,
                data={
                    "source": "invitation",
                    "invitation_id": invitation.id,
                    "role": invitation.role,
                },
            )
            session.flush()
            inviter = _load_user(session=session, user_id=invitation.invited_by_user_id)
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=(invitation.invited_by_user_id,),
                event_id="project.invitation.accepted",
                source_type="project_invitation",
                source_id=invitation.id,
                actor_user_id=normalized_actor_id,
                payload={
                    "invitation_id": invitation.id,
                    "project_id": project.id,
                    "project_name": project.name,
                    "invited_username": invitation.invited_user.username,
                    "role": invitation.role,
                },
            )
            return _to_invitation_record(
                invitation=invitation,
                project_name=project.name,
                inviter_username=inviter.username,
                now=now,
            )

    def decline_invitation(
        self,
        *,
        actor_user_id: str,
        invitation_id: str,
    ) -> ProjectInvitationRecord:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_invitation_id = _require_id(
            value=invitation_id,
            field_name="invitation_id",
        )
        with self.session_factory() as session, session.begin():
            invitation = _load_invitation(
                session=session,
                invitation_id=normalized_invitation_id,
                invited_user_id=normalized_actor_id,
                lock=True,
            )
            now = datetime.now(UTC)
            _require_pending_invitation(invitation=invitation, now=now)
            project = _load_project(session=session, project_id=invitation.project_id)
            inviter = _load_user(session=session, user_id=invitation.invited_by_user_id)
            invitation.status = "declined"
            invitation.resolved_at = now
            session.flush()
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=(invitation.invited_by_user_id,),
                event_id="project.invitation.declined",
                source_type="project_invitation",
                source_id=invitation.id,
                actor_user_id=normalized_actor_id,
                payload={
                    "invitation_id": invitation.id,
                    "project_id": project.id,
                    "project_name": project.name,
                    "invited_username": invitation.invited_user.username,
                    "role": invitation.role,
                },
            )
            return _to_invitation_record(
                invitation=invitation,
                project_name=project.name,
                inviter_username=inviter.username,
                now=now,
            )

    def revoke_invitation(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        invitation_id: str,
    ) -> ProjectInvitationRecord:
        normalized_invitation_id = _require_id(
            value=invitation_id,
            field_name="invitation_id",
        )
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            _require_supervisor(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            invitation = _load_invitation(
                session=session,
                invitation_id=normalized_invitation_id,
                project_id=project.id,
                lock=True,
            )
            now = datetime.now(UTC)
            _require_pending_invitation(invitation=invitation, now=now)
            inviter = _load_user(session=session, user_id=invitation.invited_by_user_id)
            invitation.status = "revoked"
            invitation.resolved_at = now
            session.flush()
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=(invitation.invited_user_id,),
                event_id="project.invitation.revoked",
                source_type="project_invitation",
                source_id=invitation.id,
                actor_user_id=actor_user_id,
                payload={
                    "invitation_id": invitation.id,
                    "project_id": project.id,
                    "project_name": project.name,
                    "inviter_username": inviter.username,
                    "role": invitation.role,
                },
            )
            return _to_invitation_record(
                invitation=invitation,
                project_name=project.name,
                inviter_username=inviter.username,
                now=now,
            )

    def create_comment(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        body: str,
    ) -> ProjectCommentRecord:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_body = _normalize_comment_body(value=body)
        now = datetime.now(UTC)
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id)
            member = _require_membership(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
            )
            _require_active_comment_project(project=project)
            _require_commenting_role(member=member)
            author = session.get(User, normalized_actor_id)
            if author is None:
                raise ProjectPermissionError(
                    code="project_membership_required",
                    message="project membership is required",
                )
            comment = ProjectComment(
                project_id=project.id,
                author_user_id=normalized_actor_id,
                body=normalized_body,
                created_at=now,
                edited_at=None,
            )
            session.add(comment)
            session.flush()
            usernames = parse_mention_usernames(normalized_body)
            resolved_users = resolve_project_member_usernames(
                session=session,
                project_id=project.id,
                usernames=usernames,
            )
            for user in resolved_users.values():
                if user.id != normalized_actor_id:
                    session.add(
                        ProjectCommentMention(
                            comment_id=comment.id,
                            mentioned_user_id=user.id,
                            created_at=now,
                        )
                    )
            session.flush()
            hydrated = _load_comment_with_mentions(
                session=session,
                project_id=project.id,
                comment_id=comment.id,
            )
            notification_payload = {
                "project_id": project.id,
                "project_name": project.name,
                "comment_id": comment.id,
                "actor_username": author.username,
            }
            producer = NotificationProducer(self.notification_service)
            producer.enqueue(
                session=session,
                recipient_user_ids=producer.project_member_user_ids(
                    session=session,
                    project_id=project.id,
                ),
                event_id="project_discussion.comment.created",
                source_type="project_comment",
                source_id=comment.id,
                actor_user_id=normalized_actor_id,
                payload=notification_payload,
            )
            producer.enqueue(
                session=session,
                recipient_user_ids=(mention.mentioned_user_id for mention in hydrated.mentions),
                event_id="project_discussion.comment.mentioned",
                source_type="project_comment",
                source_id=comment.id,
                actor_user_id=normalized_actor_id,
                payload=notification_payload,
            )
            return _to_comment_record(
                comment=hydrated,
                author_username=author.username,
            )

    def list_comments(
        self,
        *,
        actor_user_id: str,
        project_id: str,
    ) -> tuple[ProjectCommentListRecord, ...]:
        with self.session_factory() as session:
            project = _load_project(session=session, project_id=project_id)
            _require_membership(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            comments = tuple(
                session.execute(
                    select(ProjectComment)
                    .options(
                        joinedload(ProjectComment.author),
                        selectinload(ProjectComment.mentions).joinedload(
                            ProjectCommentMention.mentioned_user
                        ),
                    )
                    .where(ProjectComment.project_id == project.id)
                    .order_by(ProjectComment.created_at, ProjectComment.id)
                ).scalars()
            )
            comment_ids = tuple(comment.id for comment in comments)
            counts: dict[str, dict[str, int]] = {}
            current_reactions: dict[str, str] = {}
            if comment_ids:
                for comment_id, reaction, count in session.execute(
                    select(
                        ProjectCommentReaction.comment_id,
                        ProjectCommentReaction.reaction,
                        func.count(),
                    )
                    .where(ProjectCommentReaction.comment_id.in_(comment_ids))
                    .group_by(
                        ProjectCommentReaction.comment_id,
                        ProjectCommentReaction.reaction,
                    )
                ):
                    counts.setdefault(comment_id, {})[reaction] = int(count)
                current_reactions = dict(
                    session.execute(
                        select(
                            ProjectCommentReaction.comment_id,
                            ProjectCommentReaction.reaction,
                        ).where(
                            ProjectCommentReaction.comment_id.in_(comment_ids),
                            ProjectCommentReaction.user_id == actor_user_id,
                        )
                    ).all()
                )
            return tuple(
                ProjectCommentListRecord(
                    comment=_to_comment_record(
                        comment=comment,
                        author_username=comment.author.username,
                    ),
                    reaction_summary=ProjectCommentReactionSummaryRecord(
                        support=counts.get(comment.id, {}).get("support", 0),
                        oppose=counts.get(comment.id, {}).get("oppose", 0),
                        current_user_reaction=current_reactions.get(comment.id),
                    ),
                )
                for comment in comments
            )

    def edit_comment(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        comment_id: str,
        body: str,
    ) -> ProjectCommentRecord:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_body = _normalize_comment_body(value=body)
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id)
            member = _require_membership(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
            )
            comment = _load_comment(
                session=session,
                project_id=project.id,
                comment_id=comment_id,
            )
            _require_active_comment_project(project=project)
            if comment.author_user_id != normalized_actor_id:
                raise ProjectPermissionError(
                    code="project_comment_author_required",
                    message="only the comment author may edit its text",
                )
            _require_commenting_role(member=member)
            comment.body = normalized_body
            edited_at = datetime.now(UTC)
            comment.edited_at = edited_at
            new_mentioned_user_ids = _synchronize_comment_mentions(
                session=session,
                comment=comment,
                project_id=project.id,
                body=normalized_body,
                author_user_id=normalized_actor_id,
            )
            session.flush()
            hydrated = _load_comment_with_mentions(
                session=session,
                project_id=project.id,
                comment_id=comment.id,
            )
            author = session.get(User, hydrated.author_user_id)
            if author is None:
                raise ProjectCommentNotFoundError(
                    code="project_comment_not_found",
                    message="project comment was not found",
                )
            producer = NotificationProducer(self.notification_service)
            for mentioned_user_id in new_mentioned_user_ids:
                producer.enqueue(
                    session=session,
                    recipient_user_ids=(mentioned_user_id,),
                    event_id="project_discussion.comment.mentioned",
                    source_type="project_comment",
                    source_id=edited_mention_source_id(
                        comment_id=comment.id,
                        mentioned_user_id=mentioned_user_id,
                        edited_at_iso=edited_at.isoformat(),
                    ),
                    actor_user_id=normalized_actor_id,
                    payload={
                        "project_id": project.id,
                        "project_name": project.name,
                        "comment_id": comment.id,
                        "actor_username": author.username,
                    },
                )
            return _to_comment_record(comment=hydrated, author_username=author.username)

    def delete_comment(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        comment_id: str,
    ) -> None:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id)
            member = _require_membership(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
            )
            comment = _load_comment(
                session=session,
                project_id=project.id,
                comment_id=comment_id,
            )
            _require_active_comment_project(project=project)
            can_moderate = member.role == "supervisor"
            can_delete_own = (
                comment.author_user_id == normalized_actor_id
                and member.role in PROJECT_COMMENTING_ROLES
            )
            if not can_moderate and not can_delete_own:
                raise ProjectPermissionError(
                    code="project_comment_delete_forbidden",
                    message="comment author or project supervisor access is required",
                )
            if comment.author_user_id != normalized_actor_id:
                actor = session.get(User, normalized_actor_id)
                NotificationProducer(self.notification_service).enqueue(
                    session=session,
                    recipient_user_ids=(comment.author_user_id,),
                    event_id="project_discussion.comment.removed_by_admin",
                    source_type="project_comment",
                    source_id=comment.id,
                    actor_user_id=normalized_actor_id,
                    payload={
                        "project_id": project.id,
                        "project_name": project.name,
                        "comment_id": comment.id,
                        "actor_username": None if actor is None else actor.username,
                    },
                )
            session.delete(comment)
            session.flush()

    def set_comment_reaction(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        comment_id: str,
        reaction: str,
    ) -> ProjectCommentReactionSummaryRecord:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        normalized_reaction = _require_comment_reaction(value=reaction)
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id)
            member = _require_membership(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
            )
            comment = _load_comment(
                session=session,
                project_id=project.id,
                comment_id=comment_id,
            )
            _require_comment_reaction_mutation(
                project=project,
                member=member,
                comment=comment,
                actor_user_id=normalized_actor_id,
            )
            persisted = session.get(
                ProjectCommentReaction,
                (comment.id, normalized_actor_id),
            )
            previous_reaction = None if persisted is None else persisted.reaction
            changed = persisted is None or persisted.reaction != normalized_reaction
            if persisted is None:
                session.add(
                    ProjectCommentReaction(
                        comment_id=comment.id,
                        user_id=normalized_actor_id,
                        reaction=normalized_reaction,
                    )
                )
            elif persisted.reaction != normalized_reaction:
                persisted.reaction = normalized_reaction
            session.flush()
            if changed:
                actor = session.get(User, normalized_actor_id)
                occurred_at = datetime.now(UTC)
                NotificationProducer(self.notification_service).enqueue(
                    session=session,
                    recipient_user_ids=(comment.author_user_id,),
                    event_id="project_discussion.comment.reacted",
                    source_type="project_comment_reaction",
                    source_id=reaction_source_id(
                        comment_id=comment.id,
                        actor_user_id=normalized_actor_id,
                        previous_reaction=previous_reaction,
                        reaction=normalized_reaction,
                        occurred_at_iso=occurred_at.isoformat(),
                    ),
                    actor_user_id=normalized_actor_id,
                    payload={
                        "project_id": project.id,
                        "project_name": project.name,
                        "comment_id": comment.id,
                        "actor_username": None if actor is None else actor.username,
                        "reaction": normalized_reaction,
                    },
                )
            return _to_comment_reaction_summary_record(
                session=session,
                comment_id=comment.id,
                actor_user_id=normalized_actor_id,
            )

    def delete_comment_reaction(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        comment_id: str,
    ) -> ProjectCommentReactionSummaryRecord:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id)
            member = _require_membership(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
            )
            comment = _load_comment(
                session=session,
                project_id=project.id,
                comment_id=comment_id,
            )
            _require_comment_reaction_mutation(
                project=project,
                member=member,
                comment=comment,
                actor_user_id=normalized_actor_id,
            )
            persisted = session.get(
                ProjectCommentReaction,
                (comment.id, normalized_actor_id),
            )
            if persisted is not None:
                session.delete(persisted)
                session.flush()
            return _to_comment_reaction_summary_record(
                session=session,
                comment_id=comment.id,
                actor_user_id=normalized_actor_id,
            )

    def get_comment_reactions(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        comment_id: str,
    ) -> ProjectCommentReactionSummaryRecord:
        normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
        with self.session_factory() as session:
            project = _load_project(session=session, project_id=project_id)
            _require_membership(
                session=session,
                project=project,
                actor_user_id=normalized_actor_id,
            )
            comment = _load_comment(
                session=session,
                project_id=project.id,
                comment_id=comment_id,
            )
            return _to_comment_reaction_summary_record(
                session=session,
                comment_id=comment.id,
                actor_user_id=normalized_actor_id,
            )

    def list_tasks(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        owner_user_ids: tuple[str, ...] = (),
        states: tuple[str, ...] = (),
    ) -> tuple[ProjectTaskRecord, ...]:
        normalized_owner_ids = _normalize_ids(values=owner_user_ids, field_name="owner_user_id")
        normalized_states = _normalize_text_filters(values=states, field_name="state")
        with self.session_factory() as session:
            project = _load_project(session=session, project_id=project_id)
            _require_membership(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            statement = select(WebTask).where(WebTask.project_id == project.id)
            if normalized_owner_ids:
                statement = statement.where(WebTask.owner_user_id.in_(normalized_owner_ids))
            if normalized_states:
                statement = statement.where(WebTask.status.in_(normalized_states))
            tasks = session.execute(
                statement.order_by(
                    WebTask.updated_at.desc(),
                    WebTask.created_at.desc(),
                    WebTask.core_task_id.desc(),
                )
            ).scalars()
            return tuple(_to_task_record(task=task) for task in tasks)

    def attach_task(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        task_id: str,
    ) -> ProjectTaskRecord:
        normalized_task_id = _require_id(value=task_id, field_name="task_id")
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            actor_member = _require_task_operator(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            task = _load_task(session=session, task_id=normalized_task_id, lock=True)
            if task.owner_user_id is None:
                raise ProjectValidationError(
                    code="guest_task_cannot_join_project",
                    message="a guest task cannot be attached to a project",
                )
            owner_member = _load_membership(
                session=session,
                project_id=project.id,
                user_id=task.owner_user_id,
            )
            if owner_member is None:
                raise ProjectValidationError(
                    code="project_task_owner_not_member",
                    message="task owner must be a current project member",
                )
            if actor_member.user_id != task.owner_user_id and actor_member.role != "supervisor":
                raise ProjectPermissionError(
                    code="project_task_action_forbidden",
                    message="members may only attach their own tasks",
                )
            if session.get(TaskDiscussion, task.id) is None:
                session.add(TaskDiscussion(task_id=task.id, created_at=datetime.now(UTC)))
            if task.project_id == project.id:
                return _to_task_record(task=task)

            previous_project_id = task.project_id
            if previous_project_id is not None and actor_member.user_id != task.owner_user_id:
                source_member = _load_membership(
                    session=session,
                    project_id=previous_project_id,
                    user_id=actor_member.user_id,
                )
                if source_member is None or source_member.role != "supervisor":
                    raise ProjectPermissionError(
                        code="source_project_supervisor_required",
                        message=(
                            "source project supervisor access is required to move another "
                            "member's task"
                        ),
                    )
            if previous_project_id is not None:
                detached_event = _add_history_event(
                    session=session,
                    project_id=previous_project_id,
                    actor_user_id=actor_user_id,
                    subject_user_id=task.owner_user_id,
                    event_type="task_detached",
                    data={"task_id": task.core_task_id, "to_project_id": project.id},
                )
                source_project = session.get(Project, previous_project_id)
                NotificationProducer(self.notification_service).enqueue(
                    session=session,
                    recipient_user_ids=NotificationProducer.project_member_user_ids(
                        session=session,
                        project_id=previous_project_id,
                    ),
                    event_id="project.task.detached",
                    source_type="project_history",
                    source_id=_history_event_source_id(detached_event),
                    actor_user_id=actor_user_id,
                    payload={
                        "project_id": previous_project_id,
                        "project_name": (None if source_project is None else source_project.name),
                        "task_id": task.core_task_id,
                        "task_name": task.name,
                        "to_project_id": project.id,
                    },
                )
            task.project_id = project.id
            attached_event = _add_history_event(
                session=session,
                project_id=project.id,
                actor_user_id=actor_user_id,
                subject_user_id=task.owner_user_id,
                event_type="task_attached",
                data={
                    "task_id": task.core_task_id,
                    "from_project_id": previous_project_id,
                },
            )
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=NotificationProducer.project_member_user_ids(
                    session=session,
                    project_id=project.id,
                ),
                event_id="project.task.attached",
                source_type="project_history",
                source_id=_history_event_source_id(attached_event),
                actor_user_id=actor_user_id,
                payload={
                    "project_id": project.id,
                    "project_name": project.name,
                    "task_id": task.core_task_id,
                    "task_name": task.name,
                    "from_project_id": previous_project_id,
                },
            )
            session.flush()
            return _to_task_record(task=task)

    def detach_task(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        task_id: str,
    ) -> ProjectTaskRecord:
        normalized_task_id = _require_id(value=task_id, field_name="task_id")
        with self.session_factory() as session, session.begin():
            project = _load_project(session=session, project_id=project_id, lock=True)
            actor_member = _require_task_operator(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            task = _load_task(session=session, task_id=normalized_task_id, lock=True)
            if task.project_id != project.id:
                raise ProjectValidationError(
                    code="project_task_not_attached",
                    message="task is not attached to this project",
                )
            if actor_member.user_id != task.owner_user_id and actor_member.role != "supervisor":
                raise ProjectPermissionError(
                    code="project_task_action_forbidden",
                    message="members may only detach their own tasks",
                )

            task.project_id = None
            detached_event = _add_history_event(
                session=session,
                project_id=project.id,
                actor_user_id=actor_user_id,
                subject_user_id=task.owner_user_id,
                event_type="task_detached",
                data={"task_id": task.core_task_id},
            )
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=NotificationProducer.project_member_user_ids(
                    session=session,
                    project_id=project.id,
                ),
                event_id="project.task.detached",
                source_type="project_history",
                source_id=_history_event_source_id(detached_event),
                actor_user_id=actor_user_id,
                payload={
                    "project_id": project.id,
                    "project_name": project.name,
                    "task_id": task.core_task_id,
                    "task_name": task.name,
                },
            )
            session.flush()
            return _to_task_record(task=task)

    def list_history(
        self,
        *,
        actor_user_id: str,
        project_id: str,
        event_types: tuple[str, ...] = (),
    ) -> tuple[ProjectHistoryRecord, ...]:
        normalized_event_types = _normalize_text_filters(
            values=event_types,
            field_name="event_type",
        )
        with self.session_factory() as session:
            project = _load_project(session=session, project_id=project_id)
            _require_membership(
                session=session,
                project=project,
                actor_user_id=actor_user_id,
            )
            statement = select(ProjectHistoryEvent).where(
                ProjectHistoryEvent.project_id == project.id
            )
            if normalized_event_types:
                statement = statement.where(
                    ProjectHistoryEvent.event_type.in_(normalized_event_types)
                )
            events = session.execute(
                statement.order_by(
                    ProjectHistoryEvent.occurred_at,
                    ProjectHistoryEvent.id,
                )
            ).scalars()
            return tuple(_to_history_record(event=event) for event in events)

    def user_can_read_project(self, *, actor_user_id: str, project_id: str) -> bool:
        try:
            self.get_project(actor_user_id=actor_user_id, project_id=project_id)
        except (ProjectNotFoundError, ProjectPermissionError):
            return False
        return True

    def _remove_member_in_session(
        self,
        *,
        session: Session,
        project: Project,
        actor_user_id: str,
        removed_user_id: str,
    ) -> None:
        if removed_user_id == project.owner_user_id:
            raise ProjectConflictError(
                code="project_owner_membership_protected",
                message="transfer ownership before removing or leaving as the project owner",
            )
        member = _load_membership(
            session=session,
            project_id=project.id,
            user_id=removed_user_id,
            lock=True,
        )
        if member is None:
            raise ProjectMemberNotFoundError(
                code="project_member_not_found",
                message="project member was not found",
            )

        tasks = session.execute(
            select(WebTask)
            .where(
                WebTask.project_id == project.id,
                WebTask.owner_user_id == removed_user_id,
            )
            .order_by(WebTask.core_task_id)
            .with_for_update()
        ).scalars()
        for task in tasks:
            task.project_id = None
            detached_event = _add_history_event(
                session=session,
                project_id=project.id,
                actor_user_id=actor_user_id,
                subject_user_id=removed_user_id,
                event_type="task_detached",
                data={"task_id": task.core_task_id},
            )
            NotificationProducer(self.notification_service).enqueue(
                session=session,
                recipient_user_ids=NotificationProducer.project_member_user_ids(
                    session=session,
                    project_id=project.id,
                ),
                event_id="project.task.detached",
                source_type="project_history",
                source_id=_history_event_source_id(detached_event),
                actor_user_id=actor_user_id,
                payload={
                    "project_id": project.id,
                    "project_name": project.name,
                    "task_id": task.core_task_id,
                    "task_name": task.name,
                },
            )
        session.delete(member)
        member_event = _add_history_event(
            session=session,
            project_id=project.id,
            actor_user_id=actor_user_id,
            subject_user_id=removed_user_id,
            event_type="member_removed",
        )
        NotificationProducer(self.notification_service).enqueue(
            session=session,
            recipient_user_ids=(removed_user_id,),
            event_id="project.member.removed",
            source_type="project_history",
            source_id=_history_event_source_id(member_event),
            actor_user_id=actor_user_id,
            payload={"project_id": project.id, "project_name": project.name},
        )


def _load_project(
    *,
    session: Session,
    project_id: str,
    lock: bool = False,
) -> Project:
    normalized_project_id = _require_id(value=project_id, field_name="project_id")
    statement = select(Project).where(Project.id == normalized_project_id)
    if lock:
        statement = statement.with_for_update()
    project = session.execute(statement).scalar_one_or_none()
    if project is None:
        raise ProjectNotFoundError(
            code="project_not_found",
            message="project was not found",
        )
    return project


def _load_invitation(
    *,
    session: Session,
    invitation_id: str,
    project_id: str | None = None,
    invited_user_id: str | None = None,
    lock: bool = False,
) -> ProjectInvitation:
    statement = select(ProjectInvitation).where(ProjectInvitation.id == invitation_id)
    if project_id is not None:
        statement = statement.where(ProjectInvitation.project_id == project_id)
    if invited_user_id is not None:
        statement = statement.where(ProjectInvitation.invited_user_id == invited_user_id)
    if lock:
        statement = statement.with_for_update()
    invitation = session.execute(statement).scalar_one_or_none()
    if invitation is None:
        raise ProjectInvitationNotFoundError(
            code="project_invitation_not_found",
            message="project invitation was not found",
        )
    return invitation


def _load_comment(
    *,
    session: Session,
    project_id: str,
    comment_id: str,
) -> ProjectComment:
    normalized_comment_id = _require_id(value=comment_id, field_name="comment_id")
    comment = session.execute(
        select(ProjectComment).where(
            ProjectComment.id == normalized_comment_id,
            ProjectComment.project_id == project_id,
        )
    ).scalar_one_or_none()
    if comment is None:
        raise ProjectCommentNotFoundError(
            code="project_comment_not_found",
            message="project comment was not found",
        )
    return comment


def _load_comment_with_mentions(
    *,
    session: Session,
    project_id: str,
    comment_id: str,
) -> ProjectComment:
    normalized_comment_id = _require_id(value=comment_id, field_name="comment_id")
    comment = session.execute(
        select(ProjectComment)
        .options(
            joinedload(ProjectComment.author),
            selectinload(ProjectComment.mentions).joinedload(ProjectCommentMention.mentioned_user),
        )
        .where(
            ProjectComment.id == normalized_comment_id,
            ProjectComment.project_id == project_id,
        )
    ).scalar_one_or_none()
    if comment is None:
        raise ProjectCommentNotFoundError(
            code="project_comment_not_found",
            message="project comment was not found",
        )
    return comment


def _synchronize_comment_mentions(
    *,
    session: Session,
    comment: ProjectComment,
    project_id: str,
    body: str,
    author_user_id: str,
) -> tuple[str, ...]:
    existing_mentions = (
        session.execute(
            select(ProjectCommentMention).where(
                ProjectCommentMention.comment_id == comment.id,
            )
        )
        .scalars()
        .all()
    )
    existing_by_user_id = {mention.mentioned_user_id: mention for mention in existing_mentions}
    resolved_users = resolve_project_member_usernames(
        session=session,
        project_id=project_id,
        usernames=parse_mention_usernames(body),
    )
    desired_user_ids = {user.id for user in resolved_users.values() if user.id != author_user_id}
    for user_id, mention in existing_by_user_id.items():
        if user_id not in desired_user_ids:
            session.delete(mention)
    created_at = datetime.now(UTC)
    new_user_ids = desired_user_ids - existing_by_user_id.keys()
    for user_id in new_user_ids:
        session.add(
            ProjectCommentMention(
                comment_id=comment.id,
                mentioned_user_id=user_id,
                created_at=created_at,
            )
        )
    return tuple(sorted(new_user_ids))


def _load_user(*, session: Session, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ProjectInvitationTargetNotFoundError(
            code="project_invitation_target_not_found",
            message="invited user was not found",
        )
    return user


def _load_membership(
    *,
    session: Session,
    project_id: str,
    user_id: str,
    lock: bool = False,
) -> ProjectMember | None:
    statement = select(ProjectMember).where(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id == user_id,
    )
    if lock:
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def _require_membership(
    *,
    session: Session,
    project: Project,
    actor_user_id: str,
) -> ProjectMember:
    normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
    member = _load_membership(
        session=session,
        project_id=project.id,
        user_id=normalized_actor_id,
    )
    if member is None:
        raise ProjectPermissionError(
            code="project_membership_required",
            message="project membership is required",
        )
    return member


def _require_supervisor(
    *,
    session: Session,
    project: Project,
    actor_user_id: str,
) -> ProjectMember:
    member = _require_membership(
        session=session,
        project=project,
        actor_user_id=actor_user_id,
    )
    if member.role != "supervisor":
        raise ProjectPermissionError(
            code="project_supervisor_required",
            message="project supervisor access is required",
        )
    return member


def _require_owner(*, project: Project, actor_user_id: str) -> None:
    normalized_actor_id = _require_id(value=actor_user_id, field_name="actor_user_id")
    if project.owner_user_id != normalized_actor_id:
        raise ProjectPermissionError(
            code="project_owner_required",
            message="project owner access is required",
        )


def _require_task_operator(
    *,
    session: Session,
    project: Project,
    actor_user_id: str,
) -> ProjectMember:
    member = _require_membership(
        session=session,
        project=project,
        actor_user_id=actor_user_id,
    )
    if member.role not in {"member", "supervisor"}:
        raise ProjectPermissionError(
            code="project_task_role_required",
            message="member or supervisor access is required for project task operations",
        )
    return member


def _require_commenting_role(*, member: ProjectMember) -> None:
    if member.role not in PROJECT_COMMENTING_ROLES:
        raise ProjectPermissionError(
            code="project_comment_role_required",
            message="commenter, member, or supervisor access is required",
        )


def _require_active_comment_project(*, project: Project) -> None:
    if project.status == "frozen":
        raise ProjectConflictError(
            code="project_frozen",
            message="project comments are read-only while the project is frozen",
        )


def _require_comment_reaction_mutation(
    *,
    project: Project,
    member: ProjectMember,
    comment: ProjectComment,
    actor_user_id: str,
) -> None:
    _require_active_comment_project(project=project)
    _require_commenting_role(member=member)
    if comment.author_user_id == actor_user_id:
        raise ProjectPermissionError(
            code="project_comment_self_reaction_forbidden",
            message="users cannot react to their own project comments",
        )


def _load_task(*, session: Session, task_id: str, lock: bool) -> WebTask:
    statement = select(WebTask).where(WebTask.core_task_id == task_id)
    if lock:
        statement = statement.with_for_update()
    task = session.execute(statement).scalar_one_or_none()
    if task is None:
        raise ProjectTaskNotFoundError(
            code="project_task_not_found",
            message="task was not found",
        )
    return task


def _add_history_event(
    *,
    session: Session,
    project_id: str,
    event_type: str,
    actor_user_id: str | None,
    subject_user_id: str | None = None,
    data: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> ProjectHistoryEvent:
    event = ProjectHistoryEvent(
        id=str(uuid4()),
        project_id=project_id,
        actor_user_id=actor_user_id,
        subject_user_id=subject_user_id,
        event_type=event_type,
        data=data,
        occurred_at=datetime.now(UTC) if occurred_at is None else occurred_at,
    )
    session.add(event)
    return event


def _history_event_source_id(event: ProjectHistoryEvent | None) -> str:
    return event.id if event is not None else "notification-disabled"


def _to_project_record(*, project: Project, current_user_role: str | None = None) -> ProjectRecord:
    return ProjectRecord(
        project_id=project.id,
        name=project.name,
        description=project.description,
        status=project.status,
        created_by_user_id=project.created_by_user_id,
        owner_user_id=project.owner_user_id,
        current_user_role=current_user_role,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def _to_member_record(*, member: ProjectMember) -> ProjectMemberRecord:
    return ProjectMemberRecord(
        project_id=member.project_id,
        user_id=member.user_id,
        username=member.user.username,
        email=member.user.email,
        role=member.role,
        joined_at=member.joined_at,
    )


def _to_task_record(*, task: WebTask) -> ProjectTaskRecord:
    return ProjectTaskRecord(
        task_id=task.core_task_id,
        name=task.name,
        state=task.status,
        owner_user_id=task.owner_user_id,
        project_id=task.project_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _to_history_record(*, event: ProjectHistoryEvent) -> ProjectHistoryRecord:
    raw_data = event.data
    return ProjectHistoryRecord(
        event_id=event.id,
        project_id=event.project_id,
        actor_user_id=event.actor_user_id,
        subject_user_id=event.subject_user_id,
        event_type=event.event_type,
        data=None if raw_data is None else dict(raw_data),
        occurred_at=event.occurred_at,
    )


def _to_invitation_record(
    *,
    invitation: ProjectInvitation,
    project_name: str,
    inviter_username: str,
    now: datetime,
) -> ProjectInvitationRecord:
    return ProjectInvitationRecord(
        invitation_id=invitation.id,
        project_id=invitation.project_id,
        project_name=project_name,
        invited_user_id=invitation.invited_user_id,
        invited_username=invitation.invited_user.username,
        invited_by_user_id=invitation.invited_by_user_id,
        inviter_username=inviter_username,
        role=invitation.role,
        status=_effective_invitation_status(invitation=invitation, now=now),
        invited_at=invitation.invited_at,
        expires_at=invitation.expires_at,
        resolved_at=invitation.resolved_at,
    )


def _to_comment_record(
    *,
    comment: ProjectComment,
    author_username: str,
) -> ProjectCommentRecord:
    return ProjectCommentRecord(
        comment_id=comment.id,
        project_id=comment.project_id,
        author_user_id=comment.author_user_id,
        author_username=author_username,
        body=comment.body,
        created_at=comment.created_at,
        edited_at=comment.edited_at,
        mentions=tuple(
            _to_comment_mention_record(mention=mention)
            for mention in sorted(
                comment.mentions,
                key=lambda mention: (
                    mention.mentioned_user.username,
                    mention.mentioned_user_id,
                ),
            )
        ),
    )


def _to_comment_mention_record(
    *,
    mention: ProjectCommentMention,
) -> ProjectCommentMentionRecord:
    return ProjectCommentMentionRecord(
        user_id=mention.mentioned_user_id,
        username=mention.mentioned_user.username,
    )


def _to_comment_reaction_summary_record(
    *,
    session: Session,
    comment_id: str,
    actor_user_id: str,
) -> ProjectCommentReactionSummaryRecord:
    counts = dict(
        session.execute(
            select(ProjectCommentReaction.reaction, func.count())
            .where(ProjectCommentReaction.comment_id == comment_id)
            .group_by(ProjectCommentReaction.reaction)
        ).all()
    )
    current_reaction = session.get(
        ProjectCommentReaction,
        (comment_id, actor_user_id),
    )
    return ProjectCommentReactionSummaryRecord(
        support=int(counts.get("support", 0)),
        oppose=int(counts.get("oppose", 0)),
        current_user_reaction=(None if current_reaction is None else current_reaction.reaction),
    )


def _effective_invitation_status(
    *,
    invitation: ProjectInvitation,
    now: datetime,
) -> str:
    if invitation.status == "pending" and _as_utc(invitation.expires_at) <= _as_utc(now):
        return "expired"
    return invitation.status


def _require_pending_invitation(
    *,
    invitation: ProjectInvitation,
    now: datetime,
) -> None:
    effective_status = _effective_invitation_status(invitation=invitation, now=now)
    if effective_status == "pending":
        return
    if effective_status == "expired":
        raise ProjectConflictError(
            code="project_invitation_expired",
            message="project invitation has expired",
        )
    raise ProjectConflictError(
        code="project_invitation_not_pending",
        message=f"project invitation is already {effective_status}",
    )


def _invitation_status_predicates(
    *,
    statuses: tuple[str, ...],
    now: datetime,
) -> tuple[Any, ...]:
    predicates: list[Any] = []
    terminal_statuses = tuple(
        status
        for status in statuses
        if status in PROJECT_INVITATION_STORED_STATUSES and status != "pending"
    )
    if terminal_statuses:
        predicates.append(ProjectInvitation.status.in_(terminal_statuses))
    if "pending" in statuses:
        predicates.append(
            and_(
                ProjectInvitation.status == "pending",
                ProjectInvitation.expires_at > now,
            )
        )
    if "expired" in statuses:
        predicates.append(
            and_(
                ProjectInvitation.status == "pending",
                ProjectInvitation.expires_at <= now,
            )
        )
    return tuple(predicates)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_project_name(*, value: object) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(
            code="project_name_invalid",
            message="project name must be text",
        )
    normalized = value.strip()
    if normalized == "":
        raise ProjectValidationError(
            code="project_name_invalid",
            message="project name must not be empty",
        )
    if len(normalized) > 200:
        raise ProjectValidationError(
            code="project_name_invalid",
            message="project name must be at most 200 characters",
        )
    return normalized


def _normalize_description(*, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectValidationError(
            code="project_description_invalid",
            message="project description must be text or null",
        )
    normalized = value.strip()
    return normalized or None


def _normalize_comment_body(*, value: object) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(
            code="project_comment_body_invalid",
            message="comment body must be text",
        )
    normalized = value.strip()
    if normalized == "":
        raise ProjectValidationError(
            code="project_comment_body_invalid",
            message="comment body must not be empty",
        )
    if len(normalized) > PROJECT_COMMENT_MAX_LENGTH:
        raise ProjectValidationError(
            code="project_comment_body_invalid",
            message=f"comment body must be at most {PROJECT_COMMENT_MAX_LENGTH} characters",
        )
    return normalized


def _normalize_invitation_candidate_query(*, value: object) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(
            code="project_invitation_candidate_query_invalid",
            message="candidate query must be text",
        )
    normalized = value.strip().lower()
    if normalized == "":
        raise ProjectValidationError(
            code="project_invitation_candidate_query_invalid",
            message="candidate query must not be empty",
        )
    if len(normalized) > 64:
        raise ProjectValidationError(
            code="project_invitation_candidate_query_invalid",
            message="candidate query must be at most 64 characters",
        )
    return normalized


def _require_invitation_candidate_limit(*, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 20:
        raise ProjectValidationError(
            code="project_invitation_candidate_limit_invalid",
            message="candidate limit must be between 1 and 20",
        )
    return value


def _escape_like_prefix(*, value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _require_comment_reaction(*, value: object) -> str:
    return _require_allowed_text(
        value=value,
        field_name="comment_reaction",
        allowed=PROJECT_COMMENT_REACTIONS,
    )


def _require_project_status(*, value: object) -> str:
    return _require_allowed_text(
        value=value,
        field_name="status",
        allowed=PROJECT_STATUSES,
    )


def _require_member_role(*, value: object) -> str:
    return _require_allowed_text(
        value=value,
        field_name="role",
        allowed=PROJECT_MEMBER_ROLES,
    )


def _require_allowed_text(
    *,
    value: object,
    field_name: str,
    allowed: frozenset[str],
) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(
            code=f"project_{field_name}_invalid",
            message=f"{field_name} must be text",
        )
    normalized = value.strip()
    if normalized not in allowed:
        raise ProjectValidationError(
            code=f"project_{field_name}_invalid",
            message=f"{field_name} must be one of: {', '.join(sorted(allowed))}",
        )
    return normalized


def _require_id(*, value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(
            code="project_identifier_invalid",
            message=f"{field_name} must be text",
        )
    normalized = value.strip()
    if normalized == "":
        raise ProjectValidationError(
            code="project_identifier_invalid",
            message=f"{field_name} must not be empty",
        )
    return normalized


def _normalize_filter_values(
    *,
    values: tuple[str, ...],
    field_name: str,
    allowed: frozenset[str],
) -> tuple[str, ...]:
    normalized = tuple(
        _require_allowed_text(value=value, field_name=field_name, allowed=allowed)
        for value in values
    )
    return tuple(dict.fromkeys(normalized))


def _normalize_ids(*, values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_require_id(value=value, field_name=field_name) for value in values)
    return tuple(dict.fromkeys(normalized))


def _normalize_text_filters(*, values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_require_id(value=value, field_name=field_name) for value in values)
    return tuple(dict.fromkeys(normalized))


__all__ = [
    "PROJECT_COMMENT_REACTIONS",
    "PROJECT_COMMENTING_ROLES",
    "PROJECT_COMMENT_MAX_LENGTH",
    "PROJECT_INVITATION_EFFECTIVE_STATUSES",
    "PROJECT_INVITATION_EXPIRATION",
    "PROJECT_INVITATION_STORED_STATUSES",
    "PROJECT_HISTORY_EVENT_TYPES",
    "PROJECT_MEMBER_ROLES",
    "PROJECT_RELATIONS",
    "PROJECT_STATUSES",
    "ProjectConflictError",
    "ProjectCommentNotFoundError",
    "ProjectCommentMentionRecord",
    "ProjectCommentReactionSummaryRecord",
    "ProjectCommentRecord",
    "ProjectCommentListRecord",
    "ProjectDomainError",
    "ProjectHistoryRecord",
    "ProjectInvitationNotFoundError",
    "ProjectInvitationCandidateRecord",
    "ProjectInvitationRecord",
    "ProjectInvitationTargetNotFoundError",
    "ProjectMemberNotFoundError",
    "ProjectMemberRecord",
    "ProjectNotFoundError",
    "ProjectPermissionError",
    "ProjectRecord",
    "ProjectService",
    "ProjectTaskNotFoundError",
    "ProjectTaskRecord",
    "ProjectValidationError",
]
