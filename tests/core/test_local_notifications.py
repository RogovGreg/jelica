import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import jelica_core.notifications.local as local_notifications
from jelica_cli.system_config import CliSystemConfigService
from jelica_contracts import Event, EventComponent, EventType
from jelica_core.events.definitions import (
    CORE_ANALYTICAL_TASK_START_APPLIED,
    CORE_RUNTIME_JOB_COMPLETED,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PAUSE,
    CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION,
    CORE_RUNTIME_WORKER_STARTED,
)
from jelica_core.events.operations import run_service_runtime
from jelica_core.notifications import (
    BestEffortDeviceNotifier,
    BestEffortSoundPlayer,
    LocalNotificationCoordinator,
    occurrence_from_event,
    read_local_notification_preferences,
)
from jelica_core.runtime import (
    RUNTIME_EVENT_JOB_COMPLETED,
    RUNTIME_EVENT_JOB_FAILED,
    RUNTIME_EVENT_WORKER_STARTED,
    RuntimeContinueResult,
)
from jelica_core.system_config import CoreConfigService


def _event(name: str, *, task_id: str = "task-1") -> Event:
    definitions = (
        CORE_ANALYTICAL_TASK_START_APPLIED,
        CORE_RUNTIME_JOB_COMPLETED,
        CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PAUSE,
        CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION,
        CORE_RUNTIME_WORKER_STARTED,
    )
    definition = next(item for item in definitions if item.name == name)
    return Event(
        code=definition.code,
        name=name,
        type=EventType.INFO,
        title="x",
        message="x",
        component=EventComponent.CORE,
        task_id=task_id,
        timestamp=datetime.now(UTC),
    )


def test_local_occurrence_maps_only_authoritative_events() -> None:
    assert (
        occurrence_from_event(_event(CORE_ANALYTICAL_TASK_START_APPLIED.name)).event_id
        == "task.started"
    )
    assert (
        occurrence_from_event(_event(CORE_RUNTIME_JOB_COMPLETED.name)).event_id == "task.completed"
    )
    assert (
        occurrence_from_event(
            _event(CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PREEMPTION.name)
        ).event_id
        == "task.scheduler_paused"
    )
    assert occurrence_from_event(_event(CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_PAUSE.name)) is None
    started = _event(CORE_RUNTIME_WORKER_STARTED.name)
    started = started.model_copy(update={"context": {"initial_start": True}})
    assert occurrence_from_event(started).event_id == "task.started"
    resumed = started.model_copy(update={"context": {"initial_start": False}})
    assert occurrence_from_event(resumed) is None


def test_local_preferences_use_defaults_and_sound_roundtrip(tmp_path: Path) -> None:
    service = CoreConfigService(jelica_home=tmp_path)
    service.initialize_system_config()
    preferences = read_local_notification_preferences(service)
    assert preferences.sound_enabled is True
    assert preferences.enabled_for("task.completed", "device") is True
    assert preferences.enabled_for("task.started", "device") is False
    service.set_parameter(parameter="notifications.sound.enabled", value="false")
    assert read_local_notification_preferences(service).sound_enabled is False


def test_cli_composed_service_preserves_quoted_dotted_event_preferences(tmp_path: Path) -> None:
    service = CliSystemConfigService(jelica_home=tmp_path / "home")
    service.core_service.initialize_system_config()
    service.core_service.set_parameter(
        parameter="notifications.device.events.task.completed", value="false"
    )
    preferences = read_local_notification_preferences(service.core_service)
    assert preferences.enabled_for("task.completed", "device") is False


def test_macos_uses_owned_helper_and_fixed_afplay(tmp_path: Path) -> None:
    helper = tmp_path / "JELICA Notification Helper"
    helper.touch()
    helper.chmod(0o755)
    calls: list[list[str]] = []

    def capture(args: list[str], **_kwargs: object) -> None:
        calls.append(args)

    with (
        patch.object(local_notifications.platform, "system", return_value="Darwin"),
        patch.object(local_notifications, "macos_notification_helper_path", return_value=helper),
        patch.object(local_notifications.subprocess, "Popen", side_effect=capture),
        patch.object(local_notifications.Path, "is_file", return_value=True),
    ):
        BestEffortDeviceNotifier().notify(
            local_notifications.LocalNotificationOccurrence(
                occurrence_id="o",
                event_id="task.completed",
                task_id="t",
                task_name="Task",
                timestamp=datetime.now(UTC),
                state="completed",
            )
        )
        BestEffortSoundPlayer(resource=tmp_path / "notification.wav").play()
    assert calls[0][0] == str(helper)
    assert calls[0][1:3] == ["--title", "Analysis completed"]
    assert calls[0][-2:] == ["--occurrence-id", "o"]
    assert calls[1][0] == "/usr/bin/afplay"


def test_package_resources_are_discovered_from_core_resources_root() -> None:
    helper = local_notifications.macos_notification_helper_path()
    sound = local_notifications.bundled_sound_path()
    assert helper is not None
    assert helper.name == "JELICA Notification Helper"
    assert helper.is_file()
    assert os.access(helper, os.X_OK)
    assert sound is not None
    assert sound.parent.name == "notifications"
    assert sound.parent.parent.name == "resources"
    assert sound.is_file()


def test_macos_helper_uses_modern_user_notifications_lifecycle() -> None:
    source = Path(__file__).parents[2] / "packages/core/native/macos/JelicaNotificationHelper.swift"
    text = source.read_text(encoding="utf-8")
    assert "UNUserNotificationCenter" in text
    assert "requestAuthorization" in text
    assert "center.add(request)" in text
    assert "} == 0" in text
    assert "osascript" not in text
    assert "NSUserNotificationAlertStyle" not in text


def test_macos_helper_build_signs_and_verifies_final_bundle() -> None:
    script = Path(__file__).parents[2] / "packages/core/scripts/build-macos-notification-helper.py"
    text = script.read_text(encoding="utf-8")
    assert 'codesign = Path("/usr/bin/codesign")' in text
    assert '"--sign",' in text
    assert '"--verify",' in text
    assert '"--strict",' in text


@pytest.mark.parametrize(
    ("terminal_event", "expected_event_id"),
    (
        (RUNTIME_EVENT_JOB_COMPLETED, "task.completed"),
        (RUNTIME_EVENT_JOB_FAILED, "task.failed"),
    ),
)
def test_service_runtime_owns_terminal_local_notification_wiring(
    tmp_path: Path,
    terminal_event: str,
    expected_event_id: str,
) -> None:
    service = CoreConfigService(jelica_home=tmp_path / "home")
    service.initialize_system_config()
    service.set_parameter(parameter="notifications.device.events.task.started", value="true")
    delivered: list[local_notifications.LocalNotificationOccurrence] = []

    class RecordingDevice:
        def notify(
            self,
            occurrence: local_notifications.LocalNotificationOccurrence,
            **_: object,
        ) -> bool:
            delivered.append(occurrence)
            return True

    class RecordingSound:
        def play(self) -> None:
            pass

    class RecordingCoordinator(LocalNotificationCoordinator):
        def __init__(
            self,
            *,
            config_service: CoreConfigService,
            diagnostic_callback: local_notifications.DiagnosticCallback | None = None,
        ) -> None:
            super().__init__(
                config_service=config_service,
                device_notifier=RecordingDevice(),
                sound_player=RecordingSound(),
                diagnostic_callback=diagnostic_callback,
            )

    def fake_run(self: object, **_: object) -> RuntimeContinueResult:
        callback = getattr(self, "_event_callback")
        callback(
            RUNTIME_EVENT_WORKER_STARTED,
            {"task_id": "task-service", "event_type": "INFO", "initial_start": True},
        )
        callback(
            terminal_event,
            {"task_id": "task-service", "event_type": "SUCCESS"},
        )
        return RuntimeContinueResult(
            runtime_instance_id="runtime-test",
            recovered_jobs=0,
            claimed_jobs=1,
            completed_jobs=int(terminal_event == RUNTIME_EVENT_JOB_COMPLETED),
            failed_jobs=int(terminal_event == RUNTIME_EVENT_JOB_FAILED),
        )

    with (
        patch("jelica_core.notifications.local.LocalNotificationCoordinator", RecordingCoordinator),
        patch("jelica_core.runtime.ExecutionRuntime.run", fake_run),
    ):
        result = run_service_runtime(core_config_service=service)

    assert result.ok, result.error
    assert [item.event_id for item in delivered] == ["task.started", expected_event_id]
    assert all(item.task_id == "task-service" for item in delivered)
    log_text = (service.load_resolved_config().logs_dir / "system-events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "CORE_LOCAL_NOTIFICATION_DIAGNOSTIC" in log_text
    assert "local_notification" in log_text
    assert '"phase": "occurrence"' in log_text
    assert '"phase": "device"' in log_text


def test_failed_device_delivery_does_not_play_sound(tmp_path: Path) -> None:
    service = CoreConfigService(jelica_home=tmp_path / "home")
    service.initialize_system_config()
    played: list[bool] = []

    class RejectingDevice:
        def notify(
            self,
            occurrence: local_notifications.LocalNotificationOccurrence,
            **_: object,
        ) -> bool:
            _ = occurrence
            return False

    class RecordingSound:
        def play(self) -> None:
            played.append(True)

    coordinator = LocalNotificationCoordinator(
        config_service=service,
        device_notifier=RejectingDevice(),
        sound_player=RecordingSound(),
    )
    coordinator.emit(_event(CORE_RUNTIME_JOB_COMPLETED.name))
    assert played == []
