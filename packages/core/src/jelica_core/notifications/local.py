from __future__ import annotations

import base64
import logging
import os
import platform
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from jelica_contracts import Event, EventType
from jelica_core.events.definitions import (
    CORE_ANALYTICAL_TASK_START_APPLIED,
    CORE_RUNTIME_JOB_COMPLETED,
    CORE_RUNTIME_JOB_FAILED,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION,
    CORE_RUNTIME_WORKER_STARTED,
)
from jelica_core.events.sinks import EventSink
from jelica_core.system_config import CoreConfigService

_LOGGER = logging.getLogger(__name__)

LOCAL_NOTIFICATION_EVENT_IDS = (
    "task.started",
    "task.scheduler_paused",
    "task.completed",
    "task.failed",
)
DEFAULT_LOCAL_EVENT_ENABLED = {
    "task.started": False,
    "task.scheduler_paused": True,
    "task.completed": True,
    "task.failed": True,
}


@dataclass(frozen=True, slots=True)
class LocalNotificationOccurrence:
    occurrence_id: str
    event_id: str
    task_id: str
    task_name: str
    timestamp: datetime
    state: str
    play_sound: bool = False


DiagnosticCallback = Callable[[str, str, LocalNotificationOccurrence | None], None]


@dataclass(frozen=True, slots=True)
class LocalNotificationPreferences:
    device_enabled: bool
    in_app_enabled: bool
    sound_enabled: bool
    device_events: dict[str, bool]
    in_app_events: dict[str, bool]

    def enabled_for(self, event_id: str, channel: str) -> bool:
        if channel == "device":
            return self.device_enabled and self.device_events.get(
                event_id, DEFAULT_LOCAL_EVENT_ENABLED[event_id]
            )
        if channel == "in_app":
            return self.in_app_enabled and self.in_app_events.get(
                event_id, DEFAULT_LOCAL_EVENT_ENABLED[event_id]
            )
        raise ValueError(f"unknown local notification channel: {channel}")


def read_local_notification_preferences(
    service: CoreConfigService,
) -> LocalNotificationPreferences:
    """Resolve sparse local preferences from the canonical system TOML document."""

    config = service._loader.load(config_path=service.get_config_path())  # noqa: SLF001
    document = getattr(config, "_notification_document", {})
    notifications = document.get("notifications", {})
    desktop = document.get("desktop", {})
    device = notifications.get("device", {}) if isinstance(notifications, dict) else {}
    sound = notifications.get("sound", {}) if isinstance(notifications, dict) else {}
    desktop_notifications = desktop.get("notifications", {}) if isinstance(desktop, dict) else {}
    in_app = (
        desktop_notifications.get("in_app", {}) if isinstance(desktop_notifications, dict) else {}
    )
    return LocalNotificationPreferences(
        device_enabled=bool(device.get("enabled", True)) if isinstance(device, dict) else True,
        in_app_enabled=bool(in_app.get("enabled", True)) if isinstance(in_app, dict) else True,
        sound_enabled=bool(sound.get("enabled", True)) if isinstance(sound, dict) else True,
        device_events=_event_overrides(device),
        in_app_events=_event_overrides(in_app),
    )


def occurrence_from_event(event: Event) -> LocalNotificationOccurrence | None:
    """Map only authoritative lifecycle events to the safe local DTO."""

    mapping = {
        CORE_ANALYTICAL_TASK_START_APPLIED.name: ("task.started", "started"),
        CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION.name: (
            "task.scheduler_paused",
            "paused",
        ),
        CORE_RUNTIME_JOB_COMPLETED.name: ("task.completed", "completed"),
        CORE_RUNTIME_JOB_FAILED.name: ("task.failed", "failed"),
    }
    if (
        event.name == CORE_RUNTIME_WORKER_STARTED.name
        and isinstance(event.context, dict)
        and event.context.get("initial_start") is True
    ):
        mapping[event.name] = ("task.started", "started")
    mapped = mapping.get(event.name)
    if mapped is None or event.task_id is None:
        return None
    task_name = ""
    if isinstance(event.context, dict) and isinstance(event.context.get("name"), str):
        task_name = event.context["name"].strip()
    return LocalNotificationOccurrence(
        occurrence_id=str(event.event_id),
        event_id=mapped[0],
        task_id=event.task_id,
        task_name=task_name or event.task_id,
        timestamp=event.timestamp,
        state=mapped[1],
    )


class DeviceNotifier(Protocol):
    def notify(
        self,
        occurrence: LocalNotificationOccurrence,
        *,
        on_delivered: Callable[[], None] | None = None,
    ) -> bool: ...


class SoundPlayer(Protocol):
    def play(self) -> None: ...


class BestEffortDeviceNotifier:
    """Small, shell-free native adapter; unavailable capabilities are ignored."""

    def __init__(self, diagnostic_callback: DiagnosticCallback | None = None) -> None:
        self._diagnostic_callback = diagnostic_callback

    def notify(
        self,
        occurrence: LocalNotificationOccurrence,
        *,
        on_delivered: Callable[[], None] | None = None,
    ) -> bool:
        title = {
            "task.started": "Analysis started",
            "task.scheduler_paused": "Analysis paused",
            "task.completed": "Analysis completed",
            "task.failed": "Analysis failed",
        }.get(occurrence.event_id, "JELICA notification")
        body = f"{occurrence.task_name} {occurrence.state}."
        system = platform.system()
        try:
            if system == "Darwin":
                helper = macos_notification_helper_path()
                if helper is None:
                    self._diagnose("device", "helper_executable_missing", occurrence)
                    return False
                if not os.access(helper, os.X_OK):
                    self._diagnose("device", "helper_not_executable", occurrence)
                    return False
                process = subprocess.Popen(
                    [
                        str(helper),
                        "--title",
                        title,
                        "--body",
                        body,
                        "--occurrence-id",
                        occurrence.occurrence_id,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _wait_for_delivery(process, occurrence, on_delivered, self._diagnose)
                return True
            elif system == "Linux" and _executable_available("notify-send"):
                process = subprocess.Popen(
                    ["notify-send", title, body],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _wait_for_delivery(process, occurrence, on_delivered, self._diagnose)
                return True
            elif system == "Windows" and _executable_available("powershell"):
                command = (
                    "$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
                    f"{_b64(title)}'));"
                    "$b=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
                    f"{_b64(body)}'));"
                    "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
                    "$x.LoadXml(\"<toast><visual><binding template='ToastGeneric'>"
                    '<text>$t</text><text>$b</text></binding></visual></toast>");'
                    "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
                    "ContentType=WindowsRuntime]::CreateToastNotifier('JELICA').Show("
                    "[Windows.UI.Notifications.ToastNotification]::new($x))"
                )
                process = subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _wait_for_delivery(process, occurrence, on_delivered, self._diagnose)
                return True
            self._diagnose("device", "backend_unavailable", occurrence)
            return False
        except (OSError, subprocess.SubprocessError):
            self._diagnose("device", "backend_unavailable", occurrence)
            return False

    def _diagnose(self, phase: str, reason: str, occurrence: LocalNotificationOccurrence) -> None:
        _log_diagnostic(reason, occurrence)
        if self._diagnostic_callback is not None:
            self._diagnostic_callback(phase, reason, occurrence)


class BestEffortSoundPlayer:
    def __init__(
        self,
        resource: Path | None = None,
        diagnostic_callback: DiagnosticCallback | None = None,
    ) -> None:
        self.resource = resource or bundled_sound_path()
        self._diagnostic_callback = diagnostic_callback

    def play(self) -> None:
        if self.resource is None or not self.resource.is_file():
            _LOGGER.info("local notification sound_resource_missing")
            self._diagnose("sound", "sound_resource_missing", None)
            return
        try:
            system = platform.system()
            if system == "Darwin" and Path("/usr/bin/afplay").is_file():
                subprocess.Popen(
                    ["/usr/bin/afplay", str(self.resource)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif system == "Linux" and _executable_available("aplay"):
                subprocess.Popen(
                    ["aplay", "-q", str(self.resource)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif system == "Windows":
                import winsound

                winsound.PlaySound(str(self.resource), winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                _LOGGER.info("local notification sound_backend_unavailable")
                self._diagnose("sound", "sound_backend_unavailable", None)
        except (OSError, subprocess.SubprocessError):
            _LOGGER.info("local notification sound_playback_failed")
            self._diagnose("sound", "sound_playback_failed", None)
            return

    def _diagnose(
        self,
        phase: str,
        reason: str,
        occurrence: LocalNotificationOccurrence | None,
    ) -> None:
        if self._diagnostic_callback is not None:
            self._diagnostic_callback(phase, reason, occurrence)


class LocalNotificationCoordinator(EventSink):
    """Optional EventService sink. Side effects are best-effort and never required."""

    def __init__(
        self,
        *,
        config_service: CoreConfigService,
        device_notifier: DeviceNotifier | None = None,
        sound_player: SoundPlayer | None = None,
        publish_in_app: Callable[[LocalNotificationOccurrence], None] | None = None,
        diagnostic_callback: DiagnosticCallback | None = None,
    ) -> None:
        super().__init__(minimum_level=_event_type_info(), required=False)
        self.config_service = config_service
        self._diagnostic_callback = diagnostic_callback
        self.device_notifier = device_notifier or BestEffortDeviceNotifier(diagnostic_callback)
        self.sound_player = sound_player or BestEffortSoundPlayer(
            diagnostic_callback=diagnostic_callback
        )
        self.publish_in_app = publish_in_app
        self._delivered: set[str] = set()
        self._diagnose("coordinator", "initialized", None)

    def _emit(self, event: Event) -> None:
        occurrence = occurrence_from_event(event)
        if occurrence is None or occurrence.occurrence_id in self._delivered:
            return
        self._delivered.add(occurrence.occurrence_id)
        if occurrence.event_id in {"task.completed", "task.failed"}:
            self._diagnose("occurrence", "received", occurrence)
        try:
            preferences = read_local_notification_preferences(self.config_service)
        except Exception:
            self._diagnose("config", "unavailable", occurrence)
            return
        device = preferences.enabled_for(occurrence.event_id, "device")
        in_app = preferences.enabled_for(occurrence.event_id, "in_app")
        if device:
            try:
                self._diagnose("device", "attempt", occurrence)
                on_delivered = self.sound_player.play if preferences.sound_enabled else None
                callback_called = False

                def deliver_sound() -> None:
                    nonlocal callback_called
                    callback_called = True
                    if on_delivered is not None:
                        on_delivered()

                try:
                    delivered = self.device_notifier.notify(
                        occurrence,
                        on_delivered=deliver_sound if on_delivered is not None else None,
                    )
                except TypeError:
                    # Keep compatibility with small third-party/test adapters
                    # implementing the original one-argument protocol.
                    delivered = self.device_notifier.notify(occurrence)  # type: ignore[call-arg]
                # Test/in-process adapters may report delivery synchronously;
                # the native adapter invokes the callback after its helper exits
                # successfully, without blocking the Service event loop.
                if delivered and preferences.sound_enabled and not callback_called:
                    deliver_sound()
                if not delivered:
                    self._diagnose("device", "failure", occurrence)
            except Exception:
                self._diagnose("device", "delivery_failed", occurrence)
        else:
            self._diagnose("suppression", "device_suppressed", occurrence)
        if in_app and self.publish_in_app is not None:
            try:
                self.publish_in_app(
                    LocalNotificationOccurrence(
                        occurrence_id=occurrence.occurrence_id,
                        event_id=occurrence.event_id,
                        task_id=occurrence.task_id,
                        task_name=occurrence.task_name,
                        timestamp=occurrence.timestamp,
                        state=occurrence.state,
                        play_sound=preferences.sound_enabled and not device,
                    )
                )
            except Exception:
                pass

    def _diagnose(
        self,
        phase: str,
        reason: str,
        occurrence: LocalNotificationOccurrence | None,
    ) -> None:
        if occurrence is None:
            _LOGGER.info("local_notification.%s.%s", phase, reason)
        else:
            _log_diagnostic(reason, occurrence)
        if self._diagnostic_callback is not None:
            self._diagnostic_callback(phase, reason, occurrence)


def bundled_sound_path() -> Path | None:
    candidate = _core_resources_root() / "notifications" / "notification.wav"
    return candidate if candidate.is_file() else None


def macos_notification_helper_path() -> Path | None:
    candidate = (
        _core_resources_root()
        / "macos"
        / "JELICA Notification Helper.app"
        / "Contents"
        / "MacOS"
        / "JELICA Notification Helper"
    )
    return candidate if candidate.is_file() else None


def _core_resources_root() -> Path:
    """Return the package-level resources directory for editable and wheel installs."""

    return Path(__file__).resolve().parents[1] / "resources"


def _executable_available(name: str) -> bool:
    import shutil

    return shutil.which(name) is not None


def _event_overrides(value: object) -> dict[str, bool]:
    if not isinstance(value, dict) or not isinstance(value.get("events"), dict):
        return {}
    return {
        str(key): bool(raw)
        for key, raw in value["events"].items()
        if str(key) in LOCAL_NOTIFICATION_EVENT_IDS
    }


def _event_type_info():
    return EventType.DEBUG


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _wait_for_delivery(
    process: subprocess.Popen[bytes] | object,
    occurrence: LocalNotificationOccurrence,
    on_delivered: Callable[[], None] | None,
    diagnostic_callback: DiagnosticCallback | None = None,
) -> None:
    """Observe helper completion off the Service event-dispatch thread."""

    def wait() -> None:
        if not hasattr(process, "wait"):
            _log_diagnostic("delivery_failed", occurrence)
            if diagnostic_callback is not None:
                diagnostic_callback("device", "delivery_failed", occurrence)
            return
        try:
            return_code = process.wait(timeout=12)  # type: ignore[union-attr]
        except subprocess.TimeoutExpired:
            _log_diagnostic("helper_timeout", occurrence)
            if diagnostic_callback is not None:
                diagnostic_callback("device", "helper_timeout", occurrence)
            return
        except (OSError, subprocess.SubprocessError):
            _log_diagnostic("delivery_failed", occurrence)
            if diagnostic_callback is not None:
                diagnostic_callback("device", "delivery_failed", occurrence)
            return
        if return_code == 3:
            _log_diagnostic("authorization_denied", occurrence)
            if diagnostic_callback is not None:
                diagnostic_callback("device", "authorization_denied", occurrence)
            return
        if return_code != 0:
            _log_diagnostic("helper_exited_nonzero", occurrence)
            if diagnostic_callback is not None:
                diagnostic_callback("device", "helper_exited_nonzero", occurrence)
            return
        _log_diagnostic("device_success", occurrence)
        if diagnostic_callback is not None:
            diagnostic_callback("device", "success", occurrence)
        if on_delivered is not None:
            try:
                on_delivered()
            except Exception:
                _log_diagnostic("sound_playback_failed", occurrence)

    threading.Thread(target=wait, name="jelica-local-notification", daemon=True).start()


def _log_diagnostic(reason: str, occurrence: LocalNotificationOccurrence) -> None:
    _LOGGER.info(
        "local notification %s event=%s occurrence=%s",
        reason,
        occurrence.event_id,
        occurrence.occurrence_id,
    )
