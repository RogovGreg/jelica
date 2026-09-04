from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import HTTPConnection

from jelica_api.analysis_uploads import AnalysisUploadService
from jelica_api.auth import AuthService, AuthStore, EmailSender
from jelica_api.cli import JelicaCliClient
from jelica_api.email_notifications import EmailNotificationWorker
from jelica_api.notifications import NotificationService
from jelica_api.projects import ProjectService
from jelica_api.realtime import (
    NotificationRealtimeHub,
    ProjectRealtimeHub,
    ProjectRealtimePublisher,
    TaskRealtimeHub,
    TaskRealtimePublisher,
)
from jelica_api.request_security import FixedWindowRateLimiter
from jelica_api.settings import ApiSettings
from jelica_api.support_requests import SupportRequestStore
from jelica_api.task_discussions import TaskDiscussionService
from jelica_api.task_lifecycle import TaskLifecycleService
from jelica_api.task_orchestration import TaskOrchestrator
from jelica_api.task_reconciliation import WebTaskProjectionReconciler
from jelica_api.telegram import TelegramIntegration
from jelica_api.telegram_notifications import TelegramNotificationWorker
from jelica_api.web_push import WebPushDeliveryWorker, WebPushSubscriptionService
from jelica_api.web_tasks import WebTaskProjectionStore


@dataclass(frozen=True, slots=True)
class ApiAppState:
    settings: ApiSettings
    engine: Engine
    session_factory: sessionmaker[Session]
    cli_client: JelicaCliClient
    web_task_projection_store: WebTaskProjectionStore
    support_request_store: SupportRequestStore
    auth_store: AuthStore
    auth_service: AuthService
    email_sender: EmailSender
    project_service: ProjectService
    realtime_hub: ProjectRealtimeHub
    realtime_publisher: ProjectRealtimePublisher
    web_task_reconciler: WebTaskProjectionReconciler
    task_orchestrator: TaskOrchestrator
    task_discussion_service: TaskDiscussionService
    task_realtime_hub: TaskRealtimeHub
    task_realtime_publisher: TaskRealtimePublisher
    analysis_upload_service: AnalysisUploadService
    task_lifecycle_service: TaskLifecycleService
    auth_rate_limiter: FixedWindowRateLimiter
    notification_service: NotificationService
    notification_realtime_hub: NotificationRealtimeHub
    web_push_subscription_service: WebPushSubscriptionService
    web_push_worker: WebPushDeliveryWorker | None
    email_notification_worker: EmailNotificationWorker | None
    telegram_integration: TelegramIntegration
    telegram_notification_worker: TelegramNotificationWorker | None


def get_app_state(connection: HTTPConnection) -> ApiAppState:
    state = getattr(connection.app.state, "jelica_api_state", None)
    if not isinstance(state, ApiAppState):
        raise RuntimeError("API application state is not initialized.")
    return state


__all__ = ["ApiAppState", "get_app_state"]
