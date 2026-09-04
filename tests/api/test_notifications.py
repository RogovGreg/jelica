from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from jelica_api.models import Base, Notification, NotificationDelivery, User
from jelica_api.notifications import (
    WEB_NOTIFICATION_RETENTION_DAYS,
    NotificationPreferenceError,
    NotificationService,
)


def test_notification_preferences_are_sparse_and_outbox_is_deduplicated() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        user = User(username="notify-user", email="notify@example.test", password_hash="x")
        session.add(user)
        session.commit()
        service = NotificationService(session_factory=sessions)
        service.patch(
            session=session,
            user_id=user.id,
            channels={"device": True},
            events=(("task.completed", "device", False),),
        )
        session.commit()
        notification = service.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="project.invitation.received",
            source_type="invitation",
            source_id="invitation-1",
            payload={"invitation_id": "invitation-1"},
        )
        duplicate = service.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="project.invitation.received",
            source_type="invitation",
            source_id="invitation-1",
            payload={},
        )
        session.commit()
        assert notification is not None
        assert duplicate is not None and duplicate.id == notification.id
        assert len(session.scalars(select(Notification)).all()) == 1
        assert len(session.scalars(select(NotificationDelivery)).all()) == 1


def test_invalid_preference_patch_is_atomic_and_mentions_supersede_generic_comment() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        user = User(username="atomic-user", email="atomic@example.test", password_hash="x")
        session.add(user)
        session.commit()
        service = NotificationService(session_factory=sessions)
        try:
            service.patch(session=session, user_id=user.id, channels={"not-a-channel": True})
        except NotificationPreferenceError:
            session.rollback()
        assert service.snapshot(session=session, user_id=user.id).enabled is True
        generic = service.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="project_discussion.comment.created",
            source_type="comment",
            source_id="comment-2",
            payload={"comment_id": "comment-2"},
        )
        mention = service.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="project_discussion.comment.mentioned",
            source_type="comment",
            source_id="comment-2",
            payload={"comment_id": "comment-2", "mentioned_user_id": user.id},
        )
        session.commit()
        assert generic is not None and mention is not None and generic.id != mention.id
        assert (
            session.scalar(
                select(Notification).where(
                    Notification.event_id == "project_discussion.comment.created"
                )
            )
            is None
        )


def test_whatsapp_is_not_exposed_or_configurable_in_web_preferences() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        user = User(username="no-whatsapp", email="no-whatsapp@example.test", password_hash="x")
        session.add(user)
        session.commit()
        service = NotificationService(session_factory=sessions)

        snapshot = service.snapshot(session=session, user_id=user.id)
        assert "whatsapp" not in snapshot.channels
        assert all("whatsapp" not in configured for _, configured, _ in snapshot.events)
        assert all("whatsapp" not in effective for _, _, effective in snapshot.events)

        with pytest.raises(NotificationPreferenceError, match="unknown notification channel"):
            service.patch(session=session, user_id=user.id, channels={"whatsapp": True})


def test_notification_rows_are_user_isolated_and_cascade_on_user_delete() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        owner = User(username="owner", email="owner@example.test", password_hash="x")
        other = User(username="other", email="other@example.test", password_hash="x")
        session.add_all([owner, other])
        session.commit()
        service = NotificationService(session_factory=sessions)
        service.patch(session=session, user_id=owner.id, channels={"device": True})
        notification = service.enqueue(
            session=session,
            recipient_user_id=owner.id,
            event_id="project.invitation.received",
            source_type="invitation",
            source_id="isolated-1",
            payload={"safe_id": "isolated-1"},
        )
        session.commit()
        assert service.snapshot(session=session, user_id=other.id).enabled is True
        assert notification is not None and notification.payload == {"safe_id": "isolated-1"}
        notification_id = notification.id
        session.delete(owner)
        session.commit()
        assert session.get(Notification, notification_id) is None


def test_sound_and_raw_matrix_preferences_survive_disabled_masters() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        user = User(username="matrix-user", email="matrix@example.test", password_hash="x")
        session.add(user)
        session.commit()
        service = NotificationService(session_factory=sessions)
        service.patch(
            session=session,
            user_id=user.id,
            enabled=False,
            sound_enabled=False,
            channels={"device": True, "email": True},
            events=(
                ("task.completed", "device", True),
                ("task.completed", "email", True),
            ),
        )
        session.commit()
        snapshot = service.snapshot(session=session, user_id=user.id)
        completed = next(item for item in snapshot.events if item[0].event_id == "task.completed")
        assert snapshot.enabled is False
        assert snapshot.sound_enabled is False
        assert snapshot.channels["device"] == (True, False)
        assert snapshot.channels["email"] == (True, False)
        assert completed[1]["device"] is True
        assert completed[1]["email"] is True
        assert completed[2]["device"] is False
        assert completed[2]["email"] is False


def test_unavailable_channels_never_create_pending_delivery() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        user = User(username="unavailable", email="unavailable@example.test", password_hash="x")
        session.add(user)
        session.commit()
        service = NotificationService(session_factory=sessions)
        service.patch(
            session=session,
            user_id=user.id,
            channels={"in_app": False, "email": True, "telegram": True},
            events=(("task.completed", "email", True),),
        )
        service.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="task.completed",
            source_type="task",
            source_id="no-unavailable-outbox",
            payload={},
        )
        session.commit()
        assert session.scalars(select(NotificationDelivery)).all() == []


def test_inbox_retention_order_read_lifecycle_and_cleanup() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions() as session:
        owner = User(username="inbox-owner", email="inbox-owner@example.test", password_hash="x")
        other = User(username="inbox-other", email="inbox-other@example.test", password_hash="x")
        session.add_all([owner, other])
        session.commit()
        service = NotificationService(session_factory=sessions)
        old = service.enqueue(
            session=session,
            recipient_user_id=owner.id,
            event_id="project.invitation.received",
            source_type="invitation",
            source_id="old",
            payload={},
        )
        first = service.enqueue(
            session=session,
            recipient_user_id=owner.id,
            event_id="project.invitation.received",
            source_type="invitation",
            source_id="first",
            payload={"target_path": "/app/projects"},
        )
        second = service.enqueue(
            session=session,
            recipient_user_id=owner.id,
            event_id="project.invitation.received",
            source_type="invitation",
            source_id="second",
            payload={"target_path": "https://attacker.invalid"},
        )
        assert old is not None and first is not None and second is not None
        old_id = old.id
        old.created_at = now - timedelta(days=WEB_NOTIFICATION_RETENTION_DAYS, seconds=1)
        first.created_at = now - timedelta(minutes=2)
        second.created_at = now - timedelta(minutes=1)
        session.commit()

        inbox = service.list_inbox(session=session, user_id=owner.id)
        assert [item.id for item in inbox] == [second.id, first.id]
        assert inbox[0].target_path is None
        assert service.unread_count(session=session, user_id=owner.id) == 2
        assert (
            service.set_read(
                session=session,
                user_id=other.id,
                notification_id=first.id,
                read=True,
            )
            is None
        )
        updated = service.set_read(
            session=session,
            user_id=owner.id,
            notification_id=first.id,
            read=True,
        )
        assert updated is not None and updated.read_at is not None
        changed, _ = service.mark_all_read(session=session, user_id=owner.id)
        assert changed == 1
        assert service.unread_count(session=session, user_id=owner.id) == 0
        assert service.cleanup_expired(session=session) == 1
        session.commit()
        assert session.get(Notification, old_id) is None


def test_realtime_publication_occurs_only_after_commit_and_not_after_rollback() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    published: list[tuple[str, dict[str, object]]] = []
    service = NotificationService(
        session_factory=sessions,
        realtime_publisher=lambda user_id, message: published.append((user_id, message)),
    )
    with sessions() as session:
        user = User(username="realtime-user", email="realtime@example.test", password_hash="x")
        session.add(user)
        session.commit()
        published.clear()
        service.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="project.invitation.received",
            source_type="invitation",
            source_id="committed",
            payload={},
        )
        assert published == []
        session.commit()
        assert published[0][0] == user.id
        assert published[0][1]["type"] == "notification.created"

        service.enqueue(
            session=session,
            recipient_user_id=user.id,
            event_id="project.invitation.received",
            source_type="invitation",
            source_id="rolled-back",
            payload={},
        )
        session.rollback()
        assert len(published) == 1
