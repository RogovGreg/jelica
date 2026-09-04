from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jelica_api.models import Base, Notification, ProjectMember, User, WebTask
from jelica_api.notifications import NotificationService
from jelica_api.projects import ProjectService
from jelica_api.task_discussions import TaskDiscussionService


def _services():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    notifications = NotificationService(session_factory=sessions)
    projects = ProjectService(
        session_factory=sessions,
        notification_service=notifications,
    )
    return engine, sessions, notifications, projects


def _user(sessions, label: str) -> User:
    with sessions() as session:
        user = User(
            username=label,
            email=f"{label}@example.test",
            password_hash="x",
        )
        session.add(user)
        session.commit()
        return user


def _add_member(sessions, *, project_id: str, user_id: str, role: str = "member") -> None:
    with sessions() as session:
        session.add(ProjectMember(project_id=project_id, user_id=user_id, role=role))
        session.commit()


def _events(sessions, *, recipient: str) -> list[str]:
    with sessions() as session:
        return [
            row.event_id
            for row in session.scalars(
                select(Notification)
                .where(Notification.recipient_user_id == recipient)
                .order_by(Notification.created_at, Notification.id)
            )
        ]


def test_invitation_notifications_follow_direct_recipient_rules() -> None:
    engine, sessions, _, projects = _services()
    owner = _user(sessions, "invitation-owner")
    invited = _user(sessions, "invitation-target")
    project = projects.create_project(
        actor_user_id=owner.id,
        name="Invitation notifications",
        description=None,
    )
    invitation = projects.create_invitation(
        actor_user_id=owner.id,
        project_id=project.project_id,
        invited_user_id=invited.id,
        role="member",
    )
    assert _events(sessions, recipient=invited.id) == ["project.invitation.received"]
    assert _events(sessions, recipient=owner.id) == []
    projects.accept_invitation(
        actor_user_id=invited.id,
        invitation_id=invitation.invitation_id,
    )
    assert _events(sessions, recipient=owner.id) == ["project.invitation.accepted"]
    engine.dispose()


def test_project_freeze_delete_and_attach_suppress_actor() -> None:
    engine, sessions, notifications, projects = _services()
    owner = _user(sessions, "project-owner")
    member = _user(sessions, "project-member")
    project = projects.create_project(
        actor_user_id=owner.id,
        name="Project events",
        description=None,
    )
    _add_member(sessions, project_id=project.project_id, user_id=member.id)
    with sessions() as session:
        notifications.patch(
            session=session,
            user_id=member.id,
            events=(("project.frozen", "in_app", True),),
        )
        notifications.patch(
            session=session,
            user_id=owner.id,
            events=(("project.task.attached", "in_app", True),),
        )
        session.add(
            WebTask(
                core_task_id="attach-producer-task",
                name="Attach producer task",
                status="running",
                owner_user_id=member.id,
            )
        )
        session.commit()
    projects.update_project(
        actor_user_id=owner.id,
        project_id=project.project_id,
        changes={"status": "frozen"},
    )
    assert "project.frozen" in _events(sessions, recipient=member.id)
    assert "project.frozen" not in _events(sessions, recipient=owner.id)

    projects.update_project(
        actor_user_id=owner.id,
        project_id=project.project_id,
        changes={"status": "active"},
    )
    projects.attach_task(
        actor_user_id=member.id,
        project_id=project.project_id,
        task_id="attach-producer-task",
    )
    assert "project.task.attached" in _events(sessions, recipient=owner.id)
    assert "project.task.attached" not in _events(sessions, recipient=member.id)

    projects.delete_project(actor_user_id=owner.id, project_id=project.project_id)
    assert "project.deleted" in _events(sessions, recipient=member.id)
    assert "project.deleted" not in _events(sessions, recipient=owner.id)
    engine.dispose()


def test_project_discussion_mentions_reactions_and_admin_removal() -> None:
    engine, sessions, notifications, projects = _services()
    owner = _user(sessions, "discussion-owner")
    mentioned = _user(sessions, "discussion-mentioned")
    other = _user(sessions, "discussion-other")
    project = projects.create_project(
        actor_user_id=owner.id,
        name="Discussion notifications",
        description=None,
    )
    _add_member(sessions, project_id=project.project_id, user_id=mentioned.id)
    _add_member(sessions, project_id=project.project_id, user_id=other.id)
    with sessions() as session:
        for user_id in (owner.id, mentioned.id, other.id):
            notifications.patch(
                session=session,
                user_id=user_id,
                events=(
                    ("project_discussion.comment.created", "in_app", True),
                    ("project_discussion.comment.reacted", "in_app", True),
                ),
            )
        session.commit()

    comment = projects.create_comment(
        actor_user_id=owner.id,
        project_id=project.project_id,
        body=f"Hello @{mentioned.username}",
    )
    assert _events(sessions, recipient=owner.id) == []
    assert _events(sessions, recipient=mentioned.id) == ["project_discussion.comment.mentioned"]
    assert _events(sessions, recipient=other.id) == ["project_discussion.comment.created"]

    projects.edit_comment(
        actor_user_id=owner.id,
        project_id=project.project_id,
        comment_id=comment.comment_id,
        body=f"Hello @{mentioned.username} and @{other.username}",
    )
    assert (
        _events(sessions, recipient=mentioned.id).count("project_discussion.comment.mentioned") == 1
    )
    assert "project_discussion.comment.mentioned" in _events(sessions, recipient=other.id)

    projects.set_comment_reaction(
        actor_user_id=mentioned.id,
        project_id=project.project_id,
        comment_id=comment.comment_id,
        reaction="support",
    )
    assert _events(sessions, recipient=owner.id) == ["project_discussion.comment.reacted"]

    member_comment = projects.create_comment(
        actor_user_id=mentioned.id,
        project_id=project.project_id,
        body="A removable comment",
    )
    projects.delete_comment(
        actor_user_id=owner.id,
        project_id=project.project_id,
        comment_id=member_comment.comment_id,
    )
    assert "project_discussion.comment.removed_by_admin" in _events(
        sessions, recipient=mentioned.id
    )
    engine.dispose()


def test_task_discussion_uses_same_recipient_and_supersession_rules() -> None:
    engine, sessions, notifications, projects = _services()
    owner = _user(sessions, "task-discussion-owner")
    mentioned = _user(sessions, "task-discussion-mentioned")
    other = _user(sessions, "task-discussion-other")
    project = projects.create_project(
        actor_user_id=owner.id,
        name="Task Discussion notifications",
        description=None,
    )
    _add_member(sessions, project_id=project.project_id, user_id=mentioned.id)
    _add_member(sessions, project_id=project.project_id, user_id=other.id)
    with sessions() as session:
        session.add(
            WebTask(
                core_task_id="discussion-producer-task",
                name="Discussion producer task",
                status="running",
                owner_user_id=owner.id,
            )
        )
        for user_id in (owner.id, mentioned.id, other.id):
            notifications.patch(
                session=session,
                user_id=user_id,
                events=(
                    ("task_discussion.comment.created", "in_app", True),
                    ("task_discussion.comment.reacted", "in_app", True),
                ),
            )
        session.commit()
    projects.attach_task(
        actor_user_id=owner.id,
        project_id=project.project_id,
        task_id="discussion-producer-task",
    )
    discussions = TaskDiscussionService(
        session_factory=sessions,
        notification_service=notifications,
    )
    comment = discussions.create_comment(
        actor_user_id=owner.id,
        task_id="discussion-producer-task",
        body=f"Hello @{mentioned.username}",
    )
    assert "task_discussion.comment.mentioned" in _events(sessions, recipient=mentioned.id)
    assert "task_discussion.comment.created" not in _events(sessions, recipient=mentioned.id)
    assert "task_discussion.comment.created" in _events(sessions, recipient=other.id)
    discussions.set_reaction(
        actor_user_id=mentioned.id,
        task_id="discussion-producer-task",
        comment_id=comment.comment_id,
        reaction="support",
    )
    assert "task_discussion.comment.reacted" in _events(sessions, recipient=owner.id)
    member_comment = discussions.create_comment(
        actor_user_id=mentioned.id,
        task_id="discussion-producer-task",
        body="Remove this task comment",
    )
    discussions.delete_comment(
        actor_user_id=owner.id,
        task_id="discussion-producer-task",
        comment_id=member_comment.comment_id,
    )
    assert "task_discussion.comment.removed_by_admin" in _events(sessions, recipient=mentioned.id)
    engine.dispose()
