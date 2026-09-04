from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from jelica_cli.watcher import EventWatchCursorNotFoundError, EventWatchService
from jelica_contracts import Event, EventComponent, EventType
from jelica_core.events import SYSTEM_EVENTS_LOG_FILENAME
from jelica_core.system_config import CoreConfigService


def _event(*, event_id: str, task_id: str | None = None) -> Event:
    return Event(
        event_id=UUID(event_id),
        code=2000,
        name="CORE_TEST_EVENT",
        type=EventType.INFO,
        title="Test event",
        message="Ready",
        component=EventComponent.CORE,
        task_id=task_id,
    )


def _configured_service(tmp_path: Path) -> tuple[CoreConfigService, Path]:
    service = CoreConfigService(jelica_home=tmp_path / "home")
    resolved = service.initialize_system_config(force=True)
    return service, resolved.logs_dir / SYSTEM_EVENTS_LOG_FILENAME


def _append_event(path: Path, event: Event) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(event.model_dump_json(exclude_none=True).encode("utf-8"))
        stream.write(b"\n")


def test_prepare_without_cursor_starts_at_current_end_of_file(tmp_path: Path) -> None:
    config_service, log_path = _configured_service(tmp_path)
    historical = _event(event_id="00000000-0000-4000-8000-000000000001")
    appended = _event(event_id="00000000-0000-4000-8000-000000000002")
    _append_event(log_path, historical)
    watcher = EventWatchService(core_config_service=config_service)

    assert watcher.prepare() == tuple()
    _append_event(log_path, appended)

    assert watcher.poll() == (appended,)


def test_prepare_after_cursor_preserves_physical_order_then_filters_task(
    tmp_path: Path,
) -> None:
    config_service, log_path = _configured_service(tmp_path)
    before = _event(
        event_id="00000000-0000-4000-8000-000000000001",
        task_id="task-a",
    )
    cursor = _event(
        event_id="00000000-0000-4000-8000-000000000002",
        task_id="task-b",
    )
    expected_first = _event(
        event_id="00000000-0000-4000-8000-000000000003",
        task_id="task-a",
    )
    filtered_out = _event(
        event_id="00000000-0000-4000-8000-000000000004",
        task_id="task-b",
    )
    expected_second = _event(
        event_id="00000000-0000-4000-8000-000000000005",
        task_id="task-a",
    )
    for event in (before, cursor, expected_first, filtered_out, expected_second):
        _append_event(log_path, event)

    watcher = EventWatchService(task_id="task-a", core_config_service=config_service)

    assert watcher.prepare(after_event_id=cursor.event_id) == (
        expected_first,
        expected_second,
    )


def test_prepare_rejects_unknown_cursor(tmp_path: Path) -> None:
    config_service, log_path = _configured_service(tmp_path)
    _append_event(
        log_path,
        _event(event_id="00000000-0000-4000-8000-000000000001"),
    )
    watcher = EventWatchService(core_config_service=config_service)
    missing = UUID("00000000-0000-4000-8000-000000000099")

    with pytest.raises(EventWatchCursorNotFoundError, match=str(missing)):
        watcher.prepare(after_event_id=missing)


def test_poll_buffers_partial_jsonl_records_until_newline(tmp_path: Path) -> None:
    config_service, log_path = _configured_service(tmp_path)
    watcher = EventWatchService(core_config_service=config_service)
    watcher.prepare()
    event = _event(event_id="00000000-0000-4000-8000-000000000001")
    payload = event.model_dump_json(exclude_none=True).encode("utf-8")
    midpoint = len(payload) // 2
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("ab") as stream:
        stream.write(payload[:midpoint])
    assert watcher.poll() == tuple()

    with log_path.open("ab") as stream:
        stream.write(payload[midpoint:])
        stream.write(b"\n")
    assert watcher.poll() == (event,)
