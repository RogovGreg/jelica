from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from jelica_api import __version__
from jelica_api.analysis_uploads import AnalysisUploadService
from jelica_api.api import (
    analysis_uploads_router,
    auth_router,
    comments_router,
    health_router,
    internal_reconciliation_router,
    invitations_router,
    notification_push_router,
    notification_realtime_router,
    notifications_router,
    projects_router,
    realtime_router,
    support_router,
    task_discussions_router,
    task_realtime_router,
    tasks_router,
    telegram_router,
    telegram_webhook_router,
)
from jelica_api.app_state import ApiAppState
from jelica_api.auth import AuthService, AuthStore, DevelopmentEmailSender, SmtpEmailSender
from jelica_api.cli import JelicaCliClient
from jelica_api.database import create_database_engine, create_session_factory
from jelica_api.email_notifications import (
    EMAIL_OUTBOX_POLL_INTERVAL_SECONDS,
    EmailNotificationWorker,
)
from jelica_api.notifications import NotificationService
from jelica_api.projects import ProjectService
from jelica_api.realtime import (
    NotificationRealtimeHub,
    ProjectRealtimeHub,
    ProjectRealtimePublisher,
    TaskRealtimeHub,
    TaskRealtimePublisher,
)
from jelica_api.request_security import FixedWindowRateLimiter, request_origin_is_allowed
from jelica_api.settings import ApiSettings, load_api_settings
from jelica_api.support_requests import SupportRequestStore
from jelica_api.task_discussions import TaskDiscussionService
from jelica_api.task_lifecycle import TaskLifecycleService
from jelica_api.task_orchestration import TaskOrchestrator
from jelica_api.task_reconciliation import WebTaskProjectionReconciler
from jelica_api.telegram import TelegramIntegration
from jelica_api.telegram_client import TelegramBotApiClient
from jelica_api.telegram_notifications import (
    TELEGRAM_OUTBOX_POLL_INTERVAL_SECONDS,
    TelegramNotificationWorker,
)
from jelica_api.web_push import (
    WEB_PUSH_OUTBOX_POLL_INTERVAL_SECONDS,
    PyWebPushSender,
    WebPushDeliveryWorker,
    WebPushSubscriptionService,
    add_web_push_delivery_targets,
)
from jelica_api.web_tasks import WebTaskProjectionStore


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    resolved_settings = load_api_settings() if settings is None else settings
    engine = create_database_engine(database_url=resolved_settings.database_url)
    session_factory = create_session_factory(engine=engine)
    cli_client = JelicaCliClient(
        command_prefix=resolved_settings.cli_command_prefix,
        default_timeout_seconds=resolved_settings.cli_timeout_seconds,
    )
    notification_realtime_hub = NotificationRealtimeHub()
    web_push_subscription_service = WebPushSubscriptionService(session_factory=session_factory)
    notification_service = NotificationService(
        session_factory=session_factory,
        device_available=resolved_settings.web_push_configured,
        email_available=resolved_settings.email_delivery_mode == "smtp",
        telegram_available=resolved_settings.telegram_configured,
        device_target_writer=(
            lambda session, delivery, user_id: add_web_push_delivery_targets(
                session=session,
                delivery_id=delivery.id,
                subscriptions=web_push_subscription_service.active_subscriptions(
                    session=session, user_id=user_id
                ),
            )
        ),
        realtime_publisher=lambda user_id, message: notification_realtime_hub.run_from_sync(
            notification_realtime_hub.send_to_user(user_id=user_id, message=message)
        ),
    )
    projection_store = WebTaskProjectionStore(
        session_factory=session_factory,
        notification_service=notification_service,
    )
    support_request_store = SupportRequestStore(session_factory=session_factory)
    auth_store = AuthStore(session_factory=session_factory)
    if resolved_settings.email_delivery_mode == "smtp":
        email_sender = SmtpEmailSender(
            host=resolved_settings.smtp_host,
            port=resolved_settings.smtp_port,
            username=resolved_settings.smtp_username,
            password=resolved_settings.smtp_password,
            from_email=resolved_settings.smtp_from_email,
            from_name=resolved_settings.smtp_from_name,
            public_web_base_url=resolved_settings.public_web_base_url,
            tls_mode=resolved_settings.smtp_tls_mode,
            timeout_seconds=resolved_settings.smtp_timeout_seconds,
        )
    else:
        email_sender = DevelopmentEmailSender(
            expose_verification_tokens=resolved_settings.auth_expose_dev_tokens,
            public_web_base_url=resolved_settings.public_web_base_url,
        )
    auth_service = AuthService(
        store=auth_store,
        email_sender=email_sender,
        password_reset_ttl=timedelta(seconds=resolved_settings.password_reset_ttl_seconds),
    )
    project_service = ProjectService(
        session_factory=session_factory,
        notification_service=notification_service,
    )
    realtime_hub = ProjectRealtimeHub()
    realtime_publisher = ProjectRealtimePublisher(hub=realtime_hub)
    projection_reconciler = WebTaskProjectionReconciler(
        cli_client=cli_client,
        projection_store=projection_store,
    )
    task_orchestrator = TaskOrchestrator(
        cli_client=cli_client,
        projection_store=projection_store,
    )
    task_discussion_service = TaskDiscussionService(
        session_factory=session_factory,
        notification_service=notification_service,
    )
    task_realtime_hub = TaskRealtimeHub()
    task_realtime_publisher = TaskRealtimePublisher(hub=task_realtime_hub)
    telegram_client = (
        TelegramBotApiClient(
            bot_token=resolved_settings.telegram_bot_token,
            timeout_seconds=resolved_settings.telegram_timeout_seconds,
        )
        if resolved_settings.telegram_configured
        else None
    )
    telegram_integration = TelegramIntegration(
        session_factory=session_factory,
        bot_username=resolved_settings.telegram_bot_username,
        public_web_base_url=resolved_settings.public_web_base_url,
        configured=resolved_settings.telegram_configured,
        client=telegram_client,
        notification_service=notification_service,
        project_service=project_service,
        task_discussion_service=task_discussion_service,
        project_realtime_publisher=realtime_publisher,
        task_realtime_publisher=task_realtime_publisher,
    )
    analysis_upload_service = AnalysisUploadService(
        session_factory=session_factory,
        settings=resolved_settings,
    )
    task_lifecycle_service = TaskLifecycleService(
        cli_client=cli_client, projection_store=projection_store
    )
    web_push_worker = (
        WebPushDeliveryWorker(
            session_factory=session_factory,
            sender=PyWebPushSender(
                vapid_private_key=resolved_settings.web_push_vapid_private_key,
                vapid_subject=resolved_settings.web_push_vapid_subject,
            ),
        )
        if resolved_settings.web_push_configured
        else None
    )
    email_notification_worker = (
        EmailNotificationWorker(
            session_factory=session_factory,
            sender=email_sender,
            public_web_base_url=resolved_settings.public_web_base_url,
        )
        if resolved_settings.email_delivery_mode == "smtp"
        else None
    )
    telegram_notification_worker = (
        TelegramNotificationWorker(
            session_factory=session_factory,
            sender=telegram_client,
            public_web_base_url=resolved_settings.public_web_base_url,
        )
        if telegram_client is not None
        else None
    )
    auth_rate_limiter = FixedWindowRateLimiter(
        window_seconds=resolved_settings.auth_rate_limit_window_seconds
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        projection_reconciler.reconcile()
        stop_event = asyncio.Event()
        background_tasks = [
            asyncio.create_task(projection_reconciler.run_periodically(stop_event=stop_event))
        ]
        if web_push_worker is not None:
            background_tasks.append(
                asyncio.create_task(
                    _run_web_push_worker(
                        worker=web_push_worker,
                        stop_event=stop_event,
                    )
                )
            )
        if email_notification_worker is not None:
            background_tasks.append(
                asyncio.create_task(
                    _run_email_notification_worker(
                        worker=email_notification_worker,
                        stop_event=stop_event,
                    )
                )
            )
        if telegram_notification_worker is not None:
            background_tasks.append(
                asyncio.create_task(
                    _run_telegram_notification_worker(
                        worker=telegram_notification_worker,
                        stop_event=stop_event,
                    )
                )
            )
        try:
            yield
        finally:
            stop_event.set()
            await asyncio.gather(*background_tasks, return_exceptions=True)
            await realtime_hub.shutdown()
            await task_realtime_hub.shutdown()
            await notification_realtime_hub.shutdown()
            task_orchestrator.shutdown()
            engine.dispose()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    application.state.jelica_api_state = ApiAppState(
        settings=resolved_settings,
        engine=engine,
        session_factory=session_factory,
        cli_client=cli_client,
        web_task_projection_store=projection_store,
        support_request_store=support_request_store,
        auth_store=auth_store,
        auth_service=auth_service,
        email_sender=email_sender,
        project_service=project_service,
        realtime_hub=realtime_hub,
        realtime_publisher=realtime_publisher,
        web_task_reconciler=projection_reconciler,
        task_orchestrator=task_orchestrator,
        task_discussion_service=task_discussion_service,
        task_realtime_hub=task_realtime_hub,
        task_realtime_publisher=task_realtime_publisher,
        analysis_upload_service=analysis_upload_service,
        task_lifecycle_service=task_lifecycle_service,
        auth_rate_limiter=auth_rate_limiter,
        notification_service=notification_service,
        notification_realtime_hub=notification_realtime_hub,
        web_push_subscription_service=web_push_subscription_service,
        web_push_worker=web_push_worker,
        email_notification_worker=email_notification_worker,
        telegram_integration=telegram_integration,
        telegram_notification_worker=telegram_notification_worker,
    )

    @application.middleware("http")
    async def browser_mutation_origin_guard(request, call_next):
        if not request_origin_is_allowed(
            request=request,
            public_web_base_url=resolved_settings.public_web_base_url,
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "error": "origin_not_allowed",
                        "message": "Request origin is not allowed.",
                    }
                },
            )
        return await call_next(request)

    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(tasks_router)
    application.include_router(analysis_uploads_router)
    application.include_router(task_discussions_router)
    application.include_router(task_realtime_router)
    application.include_router(projects_router)
    application.include_router(invitations_router)
    application.include_router(comments_router)
    application.include_router(realtime_router)
    application.include_router(support_router)
    application.include_router(internal_reconciliation_router)
    application.include_router(notifications_router)
    application.include_router(notification_push_router)
    application.include_router(notification_realtime_router)
    application.include_router(telegram_router)
    application.include_router(telegram_webhook_router)

    @application.get("/", tags=["system"])
    def root() -> dict[str, str]:
        return {
            "service": "web-backend",
            "status": "ok",
            "version": __version__,
        }

    return application


async def _run_web_push_worker(*, worker: WebPushDeliveryWorker, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await asyncio.to_thread(worker.run_once)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=WEB_PUSH_OUTBOX_POLL_INTERVAL_SECONDS)
        except TimeoutError:
            continue


async def _run_email_notification_worker(
    *, worker: EmailNotificationWorker, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        await asyncio.to_thread(worker.run_once)
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=EMAIL_OUTBOX_POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue


async def _run_telegram_notification_worker(
    *, worker: TelegramNotificationWorker, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        await asyncio.to_thread(worker.run_once)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TELEGRAM_OUTBOX_POLL_INTERVAL_SECONDS)
        except TimeoutError:
            continue


__all__ = ["create_app"]
