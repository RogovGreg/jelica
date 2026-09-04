from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jelica_api.auth import hash_opaque_token
from jelica_api.cli import JelicaCliCommandError, MachineErrorPayload, MachineResponseEnvelope
from jelica_api.contracts import TaskStatusSnapshot, TaskSubmissionRequest, TaskSubmissionResult
from jelica_api.models import Base, Notification, Project, ProjectMember, User, WebTask
from jelica_api.notifications import NotificationService
from jelica_api.task_access import WebTaskActor
from jelica_api.task_orchestration import TaskOrchestrator
from jelica_api.task_reconciliation import WebTaskProjectionReconciler
from jelica_api.web_tasks import WebTaskProjectionStore


def test_task_orchestrator_submit_starts_without_completion_monitor() -> None:
    calls: list[bool] = []

    class StubCli:
        def create_and_start_task(
            self,
            *,
            request: TaskSubmissionRequest,
            wait_for_completion: bool = True,
            timeout_seconds: float | None = None,
        ) -> TaskSubmissionResult:
            _ = timeout_seconds
            calls.append(wait_for_completion)
            assert request.trace_id is not None
            return TaskSubmissionResult(
                task_id="task-1",
                final_state="running",
                trace_id=request.trace_id,
                command_id="cmd-start",
            )

        def find_task_by_trace_id(
            self,
            *,
            trace_id: str,
            require_active_job: bool,
            timeout_seconds: float | None = None,
            page_limit: int = 200,
        ) -> TaskStatusSnapshot | None:
            _ = (trace_id, require_active_job, timeout_seconds, page_limit)
            return None

    projection_store = _ProjectionStoreSpy()
    orchestrator = TaskOrchestrator(cli_client=StubCli(), projection_store=projection_store)
    guest_session_hash = hash_opaque_token("guest-session")
    result = orchestrator.submit_task(
        request=TaskSubmissionRequest(sources=("sample.fasta",)),
        guest_session_hash=guest_session_hash,
    )

    assert calls == [False]
    assert result.task_id == "task-1"
    assert result.final_state == "running"
    assert result.trace_id is not None
    assert projection_store.calls == [
        {
            "core_task_id": "task-1",
            "name": None,
            "status": "running",
            "owner_user_id": None,
            "guest_session_hash": guest_session_hash,
        }
    ]


def test_task_orchestrator_assigns_authenticated_owner_on_projection_insert() -> None:
    class StubCli:
        def create_and_start_task(
            self,
            *,
            request: TaskSubmissionRequest,
            wait_for_completion: bool = True,
            timeout_seconds: float | None = None,
        ) -> TaskSubmissionResult:
            _ = (wait_for_completion, timeout_seconds)
            return TaskSubmissionResult(
                task_id="task-owned",
                final_state="running",
                trace_id=request.trace_id,
                command_id="cmd-owned",
            )

        def find_task_by_trace_id(
            self,
            *,
            trace_id: str,
            require_active_job: bool,
            timeout_seconds: float | None = None,
            page_limit: int = 200,
        ) -> TaskStatusSnapshot | None:
            _ = (trace_id, require_active_job, timeout_seconds, page_limit)
            return None

    projection_store = _ProjectionStoreSpy()
    orchestrator = TaskOrchestrator(cli_client=StubCli(), projection_store=projection_store)

    orchestrator.submit_task(
        request=TaskSubmissionRequest(sources=("sample.fasta",)),
        owner_user_id="user-1",
    )

    assert projection_store.calls == [
        {
            "core_task_id": "task-owned",
            "name": None,
            "status": "running",
            "owner_user_id": "user-1",
            "guest_session_hash": None,
        }
    ]


def test_task_orchestrator_submit_syncs_projection_on_command_error() -> None:
    class StubCli:
        def create_and_start_task(
            self,
            *,
            request: TaskSubmissionRequest,
            wait_for_completion: bool = True,
            timeout_seconds: float | None = None,
        ) -> TaskSubmissionResult:
            _ = (request, wait_for_completion, timeout_seconds)
            raise _command_error(name="CLI_COMMAND_INTERRUPTED")

        def find_task_by_trace_id(
            self,
            *,
            trace_id: str,
            require_active_job: bool,
            timeout_seconds: float | None = None,
            page_limit: int = 200,
        ) -> TaskStatusSnapshot | None:
            _ = (trace_id, require_active_job, timeout_seconds, page_limit)
            return TaskStatusSnapshot(
                task_id="task-2",
                trace_id=trace_id,
                state="cancel_requested",
                active_job_state="cancel_requested",
                current_stage=None,
                progress=None,
                command_id="cmd-list",
            )

    projection_store = _ProjectionStoreSpy()
    orchestrator = TaskOrchestrator(cli_client=StubCli(), projection_store=projection_store)

    with pytest.raises(JelicaCliCommandError):
        orchestrator.submit_task(
            request=TaskSubmissionRequest(sources=("sample.fasta",)),
            guest_session_hash=hash_opaque_token("guest-error-session"),
        )
    assert projection_store.calls == [
        {
            "core_task_id": "task-2",
            "name": None,
            "status": "cancel_requested",
            "owner_user_id": None,
            "guest_session_hash": hash_opaque_token("guest-error-session"),
        }
    ]


def test_submit_restart_reconcile_without_in_process_monitor() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    projection_store = WebTaskProjectionStore(session_factory=session_factory)

    class SubmitCli:
        def create_and_start_task(
            self,
            *,
            request: TaskSubmissionRequest,
            wait_for_completion: bool = True,
            timeout_seconds: float | None = None,
        ) -> TaskSubmissionResult:
            _ = (wait_for_completion, timeout_seconds)
            assert request.trace_id is not None
            return TaskSubmissionResult(
                task_id="task-restart",
                final_state="running",
                trace_id=request.trace_id,
                command_id="cmd-submit",
            )

        def find_task_by_trace_id(
            self,
            *,
            trace_id: str,
            require_active_job: bool,
            timeout_seconds: float | None = None,
            page_limit: int = 200,
        ) -> TaskStatusSnapshot | None:
            _ = (trace_id, require_active_job, timeout_seconds, page_limit)
            return None

    # Submit phase
    submit_orchestrator = TaskOrchestrator(
        cli_client=SubmitCli(),
        projection_store=projection_store,
    )
    submit_result = submit_orchestrator.submit_task(
        request=TaskSubmissionRequest(sources=("sample.fasta",)),
        guest_session_hash=hash_opaque_token("guest-restart-session"),
    )
    assert submit_result.final_state == "running"
    projection_before = projection_store.get_task(core_task_id="task-restart")
    assert projection_before is not None
    assert projection_before.status == "running"
    assert projection_before.owner_user_id is None
    assert projection_before.guest_session_hash == hash_opaque_token("guest-restart-session")
    assert projection_before.project_id is None

    # Restart phase: new reconciler instance updates terminal state from CLI/Core snapshot.
    class ReconcileCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            assert task_reference == "task-restart"
            return TaskStatusSnapshot(
                task_id="task-restart",
                trace_id="trace-restart",
                state="completed",
                active_job_state="completed",
                current_stage="completed",
                progress=100,
                command_id="cmd-status",
            )

    reconciler = WebTaskProjectionReconciler(
        cli_client=ReconcileCli(),
        projection_store=projection_store,
    )
    report = reconciler.reconcile()

    assert report.scanned == 1
    assert report.updated == 1
    projection_after = projection_store.get_task(core_task_id="task-restart")
    assert projection_after is not None
    assert projection_after.status == "completed"
    assert projection_after.owner_user_id is None
    assert projection_after.guest_session_hash == hash_opaque_token("guest-restart-session")
    assert projection_after.project_id is None
    engine.dispose()


def test_reconciler_exposes_diagnostics_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    projection_store = WebTaskProjectionStore(session_factory=session_factory)
    projection_store.upsert_task(core_task_id="task-3", name="Task 3", status="running")

    class ReconcileCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            assert task_reference == "task-3"
            return TaskStatusSnapshot(
                task_id="task-3",
                trace_id="trace-3",
                state="running",
                active_job_state="running",
                current_stage="align",
                progress=10,
                command_id="cmd-3",
            )

    reconciler = WebTaskProjectionReconciler(
        cli_client=ReconcileCli(),
        projection_store=projection_store,
    )
    initial = reconciler.get_diagnostics()
    assert initial.scanned == 0
    assert initial.updated == 0
    assert initial.errors == 0
    assert initial.last_run_at is None

    report = reconciler.reconcile()
    diagnostics = reconciler.get_diagnostics()
    assert diagnostics.scanned == report.scanned
    assert diagnostics.updated == report.updated
    assert diagnostics.errors == report.errors
    assert diagnostics.last_run_at is not None
    engine.dispose()


def test_web_task_projection_store_upserts_status() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    store = WebTaskProjectionStore(session_factory=session_factory)

    store.upsert_task(core_task_id="task-1", name="Task One", status="running")
    store.upsert_task(core_task_id="task-1", name=None, status="completed")

    with session_factory() as session:
        row = session.execute(select(WebTask).where(WebTask.core_task_id == "task-1")).scalar_one()
        assert row.name == "Task One"
        assert row.status == "completed"
    engine.dispose()


def test_web_task_projection_store_preserves_immutable_owner_and_project() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    store = WebTaskProjectionStore(session_factory=session_factory)

    with session_factory() as session:
        session.add_all(
            [
                User(
                    id="user-1",
                    username="immutable-owner",
                    email="immutable-owner@example.org",
                    password_hash="test-password-hash",
                    email_verified=True,
                    language="en",
                ),
                User(
                    id="user-2",
                    username="replacement-owner",
                    email="replacement-owner@example.org",
                    password_hash="test-password-hash",
                    email_verified=True,
                    language="en",
                ),
            ]
        )
        session.flush()
        session.add(
            Project(
                id="project-1",
                name="Immutable task project",
                description=None,
                status="active",
                created_by_user_id="user-1",
                owner_user_id="user-1",
            )
        )
        session.flush()
        session.add(
            ProjectMember(
                project_id="project-1",
                user_id="user-1",
                role="supervisor",
            )
        )
        session.commit()

    store.upsert_task(
        core_task_id="task-owned",
        name="Owned task",
        status="running",
        owner_user_id="user-1",
    )
    with session_factory() as session:
        row = session.execute(
            select(WebTask).where(WebTask.core_task_id == "task-owned")
        ).scalar_one()
        row.project_id = "project-1"
        session.commit()

    store.upsert_task(
        core_task_id="task-owned",
        name=None,
        status="completed",
        owner_user_id="user-2",
    )

    with session_factory() as session:
        row = session.execute(
            select(WebTask).where(WebTask.core_task_id == "task-owned")
        ).scalar_one()
        assert row.owner_user_id == "user-1"
        assert row.guest_session_hash is None
        assert row.project_id == "project-1"
        assert row.status == "completed"
    engine.dispose()


def test_web_task_projection_store_applies_visibility_before_filters() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    store = WebTaskProjectionStore(session_factory=session_factory)
    with session_factory() as session:
        session.add_all(
            [
                User(
                    id=user_id,
                    username=user_id,
                    email=f"{user_id}@example.org",
                    password_hash="test-password-hash",
                    email_verified=True,
                    language="en",
                )
                for user_id in ("user-1", "user-2", "user-3")
            ]
        )
        session.flush()
        session.add_all(
            [
                Project(
                    id="project-1",
                    name="Project 1",
                    description=None,
                    status="active",
                    created_by_user_id="user-1",
                    owner_user_id="user-1",
                ),
                Project(
                    id="project-2",
                    name="Project 2",
                    description=None,
                    status="active",
                    created_by_user_id="user-2",
                    owner_user_id="user-2",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ProjectMember(project_id="project-1", user_id="user-1", role="supervisor"),
                ProjectMember(project_id="project-2", user_id="user-2", role="supervisor"),
                ProjectMember(project_id="project-2", user_id="user-1", role="member"),
            ]
        )
        session.commit()

    guest_a_hash = hash_opaque_token("guest-a")
    guest_b_hash = hash_opaque_token("guest-b")
    for task_id, owner_user_id, guest_session_hash, project_id, task_status in (
        ("task-1", "user-1", None, "project-1", "completed"),
        ("task-2", "user-2", None, "project-2", "failed"),
        ("task-3", "user-1", None, None, "running"),
        ("task-guest-a", None, guest_a_hash, None, "waiting"),
        ("task-guest-b", None, guest_b_hash, None, "waiting"),
        ("task-legacy", None, None, None, "waiting"),
    ):
        store.upsert_task(
            core_task_id=task_id,
            name=None,
            status=task_status,
            owner_user_id=owner_user_id,
            guest_session_hash=guest_session_hash,
        )
        if project_id is not None:
            with session_factory() as session:
                row = session.execute(
                    select(WebTask).where(WebTask.core_task_id == task_id)
                ).scalar_one()
                row.project_id = project_id
                session.commit()

    user_actor = WebTaskActor(user_id="user-1")
    accessible_rows = store.list_recent_tasks(actor=user_actor)
    project_rows = store.list_recent_tasks(actor=user_actor, project_ids=("project-2",))
    unassigned_rows = store.list_recent_tasks(actor=user_actor, project_none=True)
    owned_rows = store.list_recent_tasks(
        actor=user_actor,
        owner_user_id="user-1",
    )
    state_rows = store.list_recent_tasks(
        actor=user_actor,
        states=("completed", "failed"),
    )
    guest_rows = store.list_recent_tasks(actor=WebTaskActor(guest_session_hash=guest_a_hash))
    outsider_rows = store.list_recent_tasks(
        actor=WebTaskActor(user_id="user-3"),
        project_ids=("project-2",),
    )

    assert {row.core_task_id for row in accessible_rows} == {"task-1", "task-2", "task-3"}
    assert [row.core_task_id for row in project_rows] == ["task-2"]
    assert [row.core_task_id for row in unassigned_rows] == ["task-3"]
    assert {row.core_task_id for row in owned_rows} == {"task-1", "task-3"}
    assert {row.core_task_id for row in state_rows} == {"task-1", "task-2"}
    assert [row.core_task_id for row in guest_rows] == ["task-guest-a"]
    assert outsider_rows == ()
    engine.dispose()


def test_reconciler_makes_no_cli_calls_without_active_tasks() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    class NoCallCli:
        def get_task_status(self, *, task_reference: str) -> TaskStatusSnapshot:
            raise AssertionError(f"unexpected CLI call for {task_reference}")

    report = WebTaskProjectionReconciler(
        cli_client=NoCallCli(),
        projection_store=WebTaskProjectionStore(session_factory=sessions),
    ).reconcile()
    assert report.scanned == 0
    assert report.updated == 0
    engine.dispose()


def test_task_transition_notifications_are_once_and_owner_is_not_double_notified() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    notifications = NotificationService(session_factory=sessions)
    store = WebTaskProjectionStore(
        session_factory=sessions,
        notification_service=notifications,
    )
    with sessions() as session:
        owner = User(
            username="task-notify-owner",
            email="task-notify-owner@example.test",
            password_hash="x",
        )
        member = User(
            username="task-notify-member",
            email="task-notify-member@example.test",
            password_hash="x",
        )
        session.add_all([owner, member])
        session.flush()
        project = Project(
            name="Task notification project",
            description=None,
            status="active",
            created_by_user_id=owner.id,
            owner_user_id=owner.id,
        )
        session.add(project)
        session.flush()
        session.add_all(
            [
                ProjectMember(project_id=project.id, user_id=owner.id, role="supervisor"),
                ProjectMember(project_id=project.id, user_id=member.id, role="member"),
            ]
        )
        notifications.patch(
            session=session,
            user_id=owner.id,
            events=(("task.started", "in_app", True),),
        )
        notifications.patch(
            session=session,
            user_id=member.id,
            events=(("project.task.completed", "in_app", True),),
        )
        session.commit()

    store.upsert_task(
        core_task_id="task-transition",
        name="Transition task",
        status="running",
        owner_user_id=owner.id,
    )
    with sessions() as session:
        task = session.scalar(select(WebTask).where(WebTask.core_task_id == "task-transition"))
        assert task is not None
        task.project_id = project.id
        session.commit()
    store.upsert_task(core_task_id="task-transition", name=None, status="completed")
    store.upsert_task(core_task_id="task-transition", name=None, status="completed")

    with sessions() as session:
        rows = tuple(session.scalars(select(Notification)))
        owner_events = {row.event_id for row in rows if row.recipient_user_id == owner.id}
        member_events = {row.event_id for row in rows if row.recipient_user_id == member.id}
        assert owner_events == {"task.started", "task.completed"}
        assert member_events == {"project.task.completed"}
        assert len(rows) == 3
    engine.dispose()


def test_paused_to_running_resume_does_not_emit_initial_started() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    notifications = NotificationService(session_factory=sessions)
    store = WebTaskProjectionStore(
        session_factory=sessions,
        notification_service=notifications,
    )
    with sessions() as session:
        owner = User(
            username="resume-owner",
            email="resume-owner@example.test",
            password_hash="x",
        )
        session.add(owner)
        session.flush()
        notifications.patch(
            session=session,
            user_id=owner.id,
            events=(("task.started", "in_app", True),),
        )
        session.commit()
    store.upsert_task(
        core_task_id="resume-task",
        name="Resume task",
        status="paused",
        owner_user_id=owner.id,
    )
    store.upsert_task(core_task_id="resume-task", name=None, status="running")
    with sessions() as session:
        assert session.scalars(select(Notification)).all() == []
    engine.dispose()


@dataclass
class _ProjectionStoreSpy:
    calls: list[dict[str, str | None]] = field(default_factory=list)

    def upsert_task(
        self,
        *,
        core_task_id: str,
        name: str | None,
        status: str,
        owner_user_id: str | None = None,
        guest_session_hash: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "core_task_id": core_task_id,
                "name": name,
                "status": status,
                "owner_user_id": owner_user_id,
                "guest_session_hash": guest_session_hash,
            }
        )


def _command_error(*, name: str) -> JelicaCliCommandError:
    envelope = MachineResponseEnvelope(
        machine_protocol_version="1",
        jelica_version="0.1.0",
        trace_id=None,
        command_id="cmd-error",
        ok=False,
        error=MachineErrorPayload(
            code=1,
            name=name,
            message="failure",
            details={},
        ),
    )
    return JelicaCliCommandError(envelope=envelope)
