from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from jelica_api.api.authentication import require_current_user
from jelica_api.app_state import get_app_state
from jelica_api.contracts import (
    NotificationChannelPreference,
    NotificationEventPreference,
    NotificationListResponse,
    NotificationMarkAllReadResponse,
    NotificationPreferencesPatch,
    NotificationPreferencesResponse,
    NotificationReadPatch,
    NotificationResourceResponse,
    NotificationResponse,
    NotificationUnreadCountResponse,
)
from jelica_api.notifications import (
    NotificationItem,
    NotificationPreferenceError,
    NotificationPreferenceSnapshot,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
def list_notifications(request: Request) -> NotificationListResponse:
    user = require_current_user(request)
    state = get_app_state(request)
    with state.session_factory() as session, session.begin():
        state.notification_service.cleanup_expired(session=session)
        items = state.notification_service.list_inbox(session=session, user_id=user.user_id)
    return NotificationListResponse(items=tuple(_item_response(item) for item in items))


@router.get("/unread-count", response_model=NotificationUnreadCountResponse)
def unread_count(request: Request) -> NotificationUnreadCountResponse:
    user = require_current_user(request)
    state = get_app_state(request)
    with state.session_factory() as session:
        count = state.notification_service.unread_count(session=session, user_id=user.user_id)
    return NotificationUnreadCountResponse(unread_count=count)


@router.post("/mark-all-read", response_model=NotificationMarkAllReadResponse)
def mark_all_read(request: Request) -> NotificationMarkAllReadResponse:
    user = require_current_user(request)
    state = get_app_state(request)
    with state.session_factory() as session, session.begin():
        updated, read_at = state.notification_service.mark_all_read(
            session=session, user_id=user.user_id
        )
    return NotificationMarkAllReadResponse(updated=updated, read_at=read_at)


@router.get("/preferences", response_model=NotificationPreferencesResponse)
def get_preferences(request: Request) -> NotificationPreferencesResponse:
    user = require_current_user(request)
    state = get_app_state(request)
    with state.session_factory() as session:
        snapshot = state.notification_service.snapshot(session=session, user_id=user.user_id)
    return _preference_response(snapshot)


@router.patch("/preferences", response_model=NotificationPreferencesResponse)
def patch_preferences(
    payload: NotificationPreferencesPatch, request: Request
) -> NotificationPreferencesResponse:
    user = require_current_user(request)
    state = get_app_state(request)
    try:
        with state.session_factory() as session, session.begin():
            snapshot = state.notification_service.patch(
                session=session,
                user_id=user.user_id,
                enabled=payload.enabled,
                sound_enabled=payload.sound_enabled,
                channels=payload.channels,
                events=tuple(
                    (item.event_id, item.channel, item.enabled) for item in payload.events or ()
                ),
            )
    except NotificationPreferenceError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_notification_preference",
                "message": str(error),
            },
        ) from error
    return _preference_response(snapshot)


@router.patch("/{notification_id}", response_model=NotificationResponse)
def patch_notification(
    notification_id: str, payload: NotificationReadPatch, request: Request
) -> NotificationResponse:
    user = require_current_user(request)
    state = get_app_state(request)
    with state.session_factory() as session, session.begin():
        item = state.notification_service.set_read(
            session=session,
            user_id=user.user_id,
            notification_id=notification_id,
            read=payload.read,
        )
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "notification_not_found",
                    "message": "Notification not found.",
                },
            )
    return _item_response(item)


def _preference_response(
    snapshot: NotificationPreferenceSnapshot,
) -> NotificationPreferencesResponse:
    return NotificationPreferencesResponse(
        enabled=snapshot.enabled,
        sound_enabled=snapshot.sound_enabled,
        channels=tuple(
            NotificationChannelPreference(channel=key, enabled=value[0], available=value[1])
            for key, value in snapshot.channels.items()
        ),
        events=tuple(
            NotificationEventPreference(
                event_id=definition.event_id,
                category=definition.category,
                scope=definition.scope,
                default_enabled=definition.default_enabled,
                channels=tuple(
                    channel for channel in definition.channels if channel in snapshot.channels
                ),
                enabled=configured,
                effective=effective,
            )
            for definition, configured, effective in snapshot.events
        ),
    )


def _item_response(item: NotificationItem) -> NotificationResponse:
    resource = NotificationResourceResponse(**item.resource) if item.resource is not None else None
    return NotificationResponse(
        id=item.id,
        event_id=item.event_id,
        category=item.category,
        actor_username=item.actor_username,
        resource=resource,
        created_at=item.created_at,
        read_at=item.read_at,
        target_path=item.target_path,
    )


__all__ = ["router"]
