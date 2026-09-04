from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jelica_api.models import (
    Base,
    ProjectMember,
    TaskDiscussion,
    TaskDiscussionComment,
    TaskDiscussionCommentMention,
    TaskDiscussionCommentReaction,
    User,
    WebTask,
)
from jelica_api.projects import ProjectConflictError, ProjectService
from jelica_api.task_discussions import TaskDiscussionService


@pytest.fixture
def discussion_harness():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as session:
        alice = User(
            username="alice",
            email="alice@example.org",
            password_hash="x",
            email_verified=True,
            language="en",
        )
        bob = User(
            username="bob",
            email="bob@example.org",
            password_hash="x",
            email_verified=True,
            language="en",
        )
        session.add_all([alice, bob])
        session.flush()
        task = WebTask(core_task_id="task-1", name="Task", status="running", owner_user_id=alice.id)
        session.add(task)
        session.commit()
        ids = alice.id, bob.id
    project_service = ProjectService(session_factory=factory)
    project = project_service.create_project(actor_user_id=ids[0], name="Project", description=None)
    with factory() as session:
        session.add(
            ProjectMember(
                project_id=project.project_id,
                user_id=ids[1],
                role="commenter",
                joined_at=datetime.now(UTC),
            )
        )
        session.commit()
    project_service.attach_task(
        actor_user_id=ids[0], project_id=project.project_id, task_id="task-1"
    )
    yield (
        factory,
        project_service,
        TaskDiscussionService(session_factory=factory),
        ids,
        project.project_id,
    )
    engine.dispose()


def test_attach_enables_one_persistent_discussion_and_detach_preserves_history(discussion_harness):
    factory, project_service, service, (alice_id, bob_id), project_id = discussion_harness
    root = service.get_discussion(actor_user_id=alice_id, task_id="task-1")
    assert root.available and root.project_id == project_id and root.mode == "collaborative"
    comment = service.create_comment(actor_user_id=alice_id, task_id="task-1", body="@bob review")
    service.set_reaction(
        actor_user_id=bob_id, task_id="task-1", comment_id=comment.comment_id, reaction="support"
    )
    project_service.attach_task(actor_user_id=alice_id, project_id=project_id, task_id="task-1")
    with factory() as session:
        assert len(session.scalars(select(TaskDiscussion)).all()) == 1
        assert len(session.scalars(select(TaskDiscussionComment)).all()) == 1
        assert len(session.scalars(select(TaskDiscussionCommentMention)).all()) == 1
        assert len(session.scalars(select(TaskDiscussionCommentReaction)).all()) == 1
    project_service.detach_task(actor_user_id=alice_id, project_id=project_id, task_id="task-1")
    detached = service.get_discussion(actor_user_id=alice_id, task_id="task-1")
    assert detached.available and detached.mode == "read_only" and detached.project_id is None
    assert len(service.list_comments(actor_user_id=alice_id, task_id="task-1")) == 1
    with pytest.raises(ProjectConflictError, match="read-only"):
        service.create_comment(actor_user_id=alice_id, task_id="task-1", body="blocked")


def test_task_discussion_visibility_requires_current_task_access(discussion_harness):
    _, _, service, (alice_id, bob_id), project_id = discussion_harness
    with pytest.raises(Exception):
        service.get_discussion(actor_user_id="outsider", task_id="task-1")
    assert service.get_discussion(actor_user_id=bob_id, task_id="task-1").available
    assert service.list_comments(actor_user_id=bob_id, task_id="task-1") == ()


def test_task_discussion_routes_are_registered(discussion_harness):
    from jelica_api.app import create_app
    from jelica_api.settings import ApiSettings

    app = create_app(
        ApiSettings(
            database_url="sqlite+pysqlite:///:memory:",
            cli_command_prefix=("jelica",),
            cli_timeout_seconds=1,
            api_host="127.0.0.1",
            api_port=8000,
            app_name="x",
        )
    )
    paths = app.openapi()["paths"]
    assert "/api/tasks/{task_id}/discussion" in paths
    assert "/api/tasks/{task_id}/discussion/comments" in paths
    app.state.jelica_api_state.engine.dispose()


def test_task_owner_can_delete_any_comment_and_clear_history_preserves_root(discussion_harness):
    factory, project_service, service, (alice_id, bob_id), project_id = discussion_harness
    first = service.create_comment(actor_user_id=alice_id, task_id="task-1", body="owner")
    foreign = service.create_comment(actor_user_id=bob_id, task_id="task-1", body="foreign")
    service.set_reaction(
        actor_user_id=bob_id,
        task_id="task-1",
        comment_id=first.comment_id,
        reaction="support",
    )
    service.delete_comment(actor_user_id=alice_id, task_id="task-1", comment_id=foreign.comment_id)
    with factory() as session:
        assert session.get(TaskDiscussionComment, foreign.comment_id) is None
        assert (
            session.get(
                TaskDiscussion,
                session.scalar(select(WebTask.id).where(WebTask.core_task_id == "task-1")),
            )
            is not None
        )

    project_service.update_project(
        actor_user_id=alice_id, project_id=project_id, changes={"status": "frozen"}
    )
    service.clear_discussion(actor_user_id=alice_id, task_id="task-1")
    service.clear_discussion(actor_user_id=alice_id, task_id="task-1")
    assert service.get_discussion(actor_user_id=alice_id, task_id="task-1").available
    assert service.list_comments(actor_user_id=alice_id, task_id="task-1") == ()
    with factory() as session:
        assert session.scalar(select(TaskDiscussion)) is not None
        assert session.scalar(select(TaskDiscussionCommentReaction)) is None


def test_owner_admin_clear_is_not_collaboration_permission_when_detached(discussion_harness):
    _, project_service, service, (alice_id, bob_id), project_id = discussion_harness
    comment = service.create_comment(actor_user_id=bob_id, task_id="task-1", body="history")
    project_service.detach_task(actor_user_id=alice_id, project_id=project_id, task_id="task-1")
    service.delete_comment(actor_user_id=alice_id, task_id="task-1", comment_id=comment.comment_id)
    with pytest.raises(ProjectConflictError):
        service.create_comment(actor_user_id=alice_id, task_id="task-1", body="blocked")
