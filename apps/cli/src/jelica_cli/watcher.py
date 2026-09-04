from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Callable
from uuid import UUID

from jelica_contracts import Event, EventType, event_type_rank
from jelica_core.events import SYSTEM_EVENTS_LOG_FILENAME, TASK_EVENTS_LOG_FILENAME
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import (
    ACTIVE_ANALYTICAL_TASK_JOB_STATES,
    TERMINAL_ANALYTICAL_TASK_STATES,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryService,
    AnalyticalTaskSnapshot,
)

WATCH_POLL_INTERVAL_SECONDS = 0.5
_TERMINAL_EVENT_NAMES_BY_STATE: dict[str, frozenset[str]] = {
    "completed": frozenset({"CORE_RUNTIME_JOB_COMPLETED"}),
    "failed": frozenset({"CORE_RUNTIME_JOB_FAILED", "CORE_RUNTIME_RECOVERY_FAILED"}),
    "cancelled": frozenset(
        {
            "CORE_ANALYTICAL_TASK_CANCEL_APPLIED",
            "CORE_RUNTIME_WORKER_SAFELY_STOPPED_FOR_CANCEL",
        }
    ),
}
_TERMINAL_EVENT_TYPE_BY_STATE: dict[str, EventType] = {
    "completed": EventType.SUCCESS,
    "failed": EventType.ERROR,
    "cancelled": EventType.SUCCESS,
}


class EventWatchCursorNotFoundError(ValueError):
    """Raised when a requested event cursor is absent from the persisted stream."""

    def __init__(self, *, event_id: UUID, path: Path) -> None:
        self.event_id = event_id
        self.path = path
        super().__init__(f"Event cursor '{event_id}' was not found in '{path}'.")


class EventWatchService:
    """Tail persisted system events in their physical JSONL order."""

    def __init__(
        self,
        *,
        poll_interval_seconds: float = WATCH_POLL_INTERVAL_SECONDS,
        task_id: str | None = None,
        core_config_service: CoreConfigService | None = None,
    ) -> None:
        config_service = core_config_service or CoreConfigService()
        resolved_config = config_service.require_initialized_config()
        normalized_task_id = task_id.strip() if task_id is not None else ""
        self._task_id = normalized_task_id or None
        self._path = resolved_config.logs_dir / SYSTEM_EVENTS_LOG_FILENAME
        self._poll_interval_seconds = poll_interval_seconds
        self._offset = 0
        self._pending = b""
        self._seen_event_ids: set[UUID] = set()

    def prepare(self, *, after_event_id: UUID | None = None) -> tuple[Event, ...]:
        self._reset()
        if after_event_id is None:
            try:
                self._offset = self._path.stat().st_size
            except FileNotFoundError:
                self._offset = 0
            return tuple()

        events = self._read_complete_events()
        cursor_index = next(
            (
                index
                for index, event in enumerate(events)
                if event.event_id == after_event_id
            ),
            None,
        )
        if cursor_index is None:
            raise EventWatchCursorNotFoundError(event_id=after_event_id, path=self._path)

        self._seen_event_ids.add(after_event_id)
        return self._select_visible_events(events[cursor_index + 1 :])

    def poll(self) -> tuple[Event, ...]:
        return self._select_visible_events(self._read_complete_events())

    def watch(
        self,
        callback: Callable[[tuple[Event, ...]], None],
        *,
        stop_condition: Callable[[], bool] | None = None,
    ) -> None:
        while stop_condition is None or not stop_condition():
            sleep(self._poll_interval_seconds)
            events = self.poll()
            if len(events) > 0:
                callback(events)

    def _read_complete_events(self) -> tuple[Event, ...]:
        if not self._path.is_file():
            return tuple()
        try:
            size = self._path.stat().st_size
            if size < self._offset:
                self._reset()
            with self._path.open("rb") as stream:
                stream.seek(self._offset)
                appended = stream.read()
        except OSError:
            return tuple()

        self._offset += len(appended)
        payload = self._pending + appended
        lines = payload.split(b"\n")
        self._pending = lines.pop()
        events: list[Event] = []
        for raw_line in lines:
            if raw_line.strip() == b"":
                continue
            try:
                event = Event.model_validate_json(raw_line)
            except ValueError:
                continue
            events.append(event)
        return tuple(events)

    def _select_visible_events(self, events: tuple[Event, ...]) -> tuple[Event, ...]:
        visible: list[Event] = []
        for event in events:
            if event.event_id in self._seen_event_ids:
                continue
            self._seen_event_ids.add(event.event_id)
            if self._task_id is not None and event.task_id != self._task_id:
                continue
            visible.append(event)
        return tuple(visible)

    def _reset(self) -> None:
        self._offset = 0
        self._pending = b""
        self._seen_event_ids.clear()


@dataclass(frozen=True, slots=True)
class WatchTaskRow:
    task_id: str
    job_id: str | None
    state: str
    stage: str | None
    progress: int
    warning_count: int
    task_name: str | None = None
    trace_id: UUID | None = None

    @property
    def terminal(self) -> bool:
        return self.state in {state.value for state in TERMINAL_ANALYTICAL_TASK_STATES} | {
            "deleted"
        }


@dataclass(frozen=True, slots=True)
class InactiveTask:
    task_id: str
    state: str
    task_name: str | None = None
    trace_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class WatchPreparation:
    explicit: bool
    rows: tuple[WatchTaskRow, ...]
    missing_task_ids: tuple[str, ...]
    inactive_tasks: tuple[InactiveTask, ...]
    events: tuple[Event, ...]


@dataclass(frozen=True, slots=True)
class WatchUpdate:
    rows: tuple[WatchTaskRow, ...]
    events: tuple[Event, ...]


@dataclass(slots=True)
class _ObservedTask:
    row: WatchTaskRow
    tail: _TaskEventTail | None
    terminal_settled: bool = False
    comparative_progress_stage: str | None = None


class _TaskEventTail:
    def __init__(
        self,
        *,
        path: Path,
        task_id: str,
        job_id: str,
        event_since: datetime,
    ) -> None:
        self._path = path
        self._task_id = task_id
        self._job_id = job_id
        self._event_since = event_since
        self._offset = 0
        self._pending = b""
        self._task_warning_count = 0
        self._job_warning_count = 0
        self._seen_event_ids: set[str] = set()
        self._seen_job_event_names: set[str] = set()

    @property
    def warning_count(self) -> int:
        return self._task_warning_count + self._job_warning_count

    def has_terminal_event(self, state: str) -> bool:
        expected_names = _TERMINAL_EVENT_NAMES_BY_STATE.get(state, frozenset())
        return bool(expected_names & self._seen_job_event_names)

    def read(self) -> tuple[Event, ...]:
        if not self._path.is_file():
            return tuple()
        try:
            size = self._path.stat().st_size
            if size < self._offset:
                self._reset()
            with self._path.open("rb") as stream:
                stream.seek(self._offset)
                appended = stream.read()
        except OSError:
            return tuple()

        self._offset += len(appended)
        payload = self._pending + appended
        lines = payload.split(b"\n")
        self._pending = lines.pop()
        visible_events: list[Event] = []
        for raw_line in lines:
            if raw_line.strip() == b"":
                continue
            event = self._parse_event(raw_line)
            if event is None or event.task_id != self._task_id:
                continue
            event_id = str(event.event_id)
            if event_id in self._seen_event_ids:
                continue
            self._seen_event_ids.add(event_id)
            self._count_warning(event)
            event_job_id = _event_job_id(event)
            if event_job_id == self._job_id:
                self._seen_job_event_names.add(event.name)
            if event_job_id == self._job_id or (
                event_job_id is None and event.timestamp >= self._event_since
            ):
                visible_events.append(event)
        return tuple(visible_events)

    def _parse_event(self, raw_line: bytes) -> Event | None:
        try:
            payload = json.loads(raw_line.decode("utf-8"))
            return Event.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None

    def _count_warning(self, event: Event) -> None:
        if event.type is not EventType.WARNING:
            return
        context_job_id = _event_job_id(event)
        if context_job_id is None:
            self._task_warning_count += 1
        elif context_job_id == self._job_id:
            self._job_warning_count += 1

    def _reset(self) -> None:
        self._offset = 0
        self._pending = b""
        self._task_warning_count = 0
        self._job_warning_count = 0
        self._seen_event_ids.clear()
        self._seen_job_event_names.clear()


class TaskWatchService:
    def __init__(
        self,
        *,
        poll_interval_seconds: float = WATCH_POLL_INTERVAL_SECONDS,
        event_since: datetime | None = None,
        core_config_service: CoreConfigService | None = None,
    ) -> None:
        config_service = core_config_service or CoreConfigService()
        resolved_config = config_service.require_initialized_config()
        self._registry = AnalyticalTaskRegistryService(database_path=resolved_config.database_path)
        self._tasks_dir = resolved_config.tasks_dir
        self._task_log_minimum_type = EventType(resolved_config.task_log_level.upper())
        self._poll_interval_seconds = poll_interval_seconds
        self._event_since = event_since or datetime.now(UTC)
        self._explicit = False
        self._explicit_task_ids: tuple[str, ...] = tuple()
        self._observed: dict[str, _ObservedTask] = {}

    def prepare(
        self,
        task_ids: tuple[str, ...],
        *,
        include_explicit_inactive: bool = False,
    ) -> WatchPreparation:
        normalized_ids = tuple(
            dict.fromkeys(task_id.strip() for task_id in task_ids if task_id.strip() != "")
        )
        self._explicit = len(normalized_ids) > 0
        self._explicit_task_ids = normalized_ids
        missing: list[str] = []
        inactive: list[InactiveTask] = []
        events: list[Event] = []

        if self._explicit:
            for task_id in normalized_ids:
                try:
                    snapshot = self._registry.get_task_snapshot(task_id=task_id)
                except AnalyticalTaskNotFoundError:
                    missing.append(task_id)
                    continue
                if not _snapshot_is_active(snapshot) and not (
                    include_explicit_inactive and snapshot.active_or_latest_job is not None
                ):
                    inactive.append(
                        InactiveTask(
                            task_id=task_id,
                            state=snapshot.task.state.value,
                            task_name=snapshot.task.name,
                            trace_id=self._registry.get_task_trace_id(task_id=task_id),
                        )
                    )
                    continue
                events.extend(self._observe_snapshot(snapshot))
        else:
            snapshots = self._registry.list_task_snapshots(
                states=tuple(ACTIVE_ANALYTICAL_TASK_JOB_STATES),
                limit=None,
            )
            for snapshot in snapshots:
                events.extend(self._observe_snapshot(snapshot))

        return WatchPreparation(
            explicit=self._explicit,
            rows=self.rows,
            missing_task_ids=tuple(missing),
            inactive_tasks=tuple(inactive),
            events=tuple(events),
        )

    @property
    def rows(self) -> tuple[WatchTaskRow, ...]:
        return tuple(observed.row for observed in self._observed.values())

    @property
    def complete(self) -> bool:
        if not self._explicit:
            return False
        if len(self._observed) == 0:
            return True
        return all(
            observed.row.terminal and observed.terminal_settled
            for observed in self._observed.values()
        )

    def watch(
        self,
        callback: Callable[[WatchUpdate], None],
        *,
        stop_condition: Callable[[], bool] | None = None,
        wait_for_observed_rows: bool = False,
    ) -> WatchUpdate:
        latest = WatchUpdate(rows=self.rows, events=tuple())
        waiting_for_observed_rows = (
            wait_for_observed_rows and self._explicit and len(self._observed) == 0
        )
        if self.complete and not waiting_for_observed_rows:
            return latest

        while True:
            if stop_condition is not None and stop_condition():
                return latest
            sleep(self._poll_interval_seconds)
            latest = self.poll()
            callback(latest)
            waiting_for_observed_rows = (
                wait_for_observed_rows and self._explicit and len(self._observed) == 0
            )
            if self.complete and not waiting_for_observed_rows:
                return latest

    def poll(self) -> WatchUpdate:
        events: list[Event] = []
        if self._explicit:
            if len(self._observed) == 0:
                for task_id in self._explicit_task_ids:
                    try:
                        snapshot = self._registry.get_task_snapshot(task_id=task_id)
                    except AnalyticalTaskNotFoundError:
                        continue
                    if snapshot.active_or_latest_job is None:
                        continue
                    events.extend(self._observe_snapshot(snapshot))
            for task_id in tuple(self._observed):
                events.extend(self._refresh_explicit_task(task_id))
        else:
            active_snapshots = self._registry.list_task_snapshots(
                states=tuple(ACTIVE_ANALYTICAL_TASK_JOB_STATES),
                limit=None,
            )
            active_ids: set[str] = set()
            for snapshot in active_snapshots:
                active_ids.add(snapshot.task.task_id)
                events.extend(self._observe_snapshot(snapshot))
            for task_id, observed in tuple(self._observed.items()):
                if task_id in active_ids:
                    continue
                if observed.row.terminal:
                    events.extend(self._settle_terminal(observed))
                    continue
                events.extend(self._refresh_explicit_task(task_id))
        return WatchUpdate(rows=self.rows, events=tuple(events))

    def _refresh_explicit_task(self, task_id: str) -> tuple[Event, ...]:
        observed = self._observed[task_id]
        if observed.row.terminal:
            return self._settle_terminal(observed)
        try:
            snapshot = self._registry.get_task_snapshot(task_id=task_id)
        except AnalyticalTaskNotFoundError:
            observed.row = WatchTaskRow(
                task_id=observed.row.task_id,
                job_id=observed.row.job_id,
                state="deleted",
                stage=observed.row.stage,
                progress=observed.row.progress,
                warning_count=observed.row.warning_count,
                task_name=observed.row.task_name,
                trace_id=observed.row.trace_id,
            )
            return tuple() if observed.tail is None else observed.tail.read()
        return self._observe_snapshot(snapshot)

    def _observe_snapshot(self, snapshot: AnalyticalTaskSnapshot) -> tuple[Event, ...]:
        job = snapshot.active_or_latest_job
        task_id = snapshot.task.task_id
        observed = self._observed.get(task_id)
        trace_id = (
            observed.row.trace_id
            if observed is not None
            else self._registry.get_task_trace_id(task_id=task_id)
        )
        if job is None:
            self._observed[task_id] = _ObservedTask(
                row=WatchTaskRow(
                    task_id=task_id,
                    job_id=None,
                    state=snapshot.task.state.value,
                    stage=None,
                    progress=0,
                    warning_count=0,
                    task_name=snapshot.task.name,
                    trace_id=trace_id,
                ),
                tail=None,
            )
            return tuple()
        task_log_path = (
            self._tasks_dir / snapshot.task.task_dir_relative_path / TASK_EVENTS_LOG_FILENAME
        )
        if observed is None or observed.row.job_id != job.job_id or observed.tail is None:
            tail = _TaskEventTail(
                path=task_log_path,
                task_id=task_id,
                job_id=job.job_id,
                event_since=self._event_since,
            )
            observed = _ObservedTask(
                row=WatchTaskRow(
                    task_id=task_id,
                    job_id=job.job_id,
                    state=job.state.value,
                    stage=job.current_stage,
                    progress=job.progress,
                    warning_count=0,
                    task_name=snapshot.task.name,
                    trace_id=trace_id,
                ),
                tail=tail,
            )
            self._observed[task_id] = observed
        active_tail = observed.tail
        if active_tail is None:  # pragma: no cover - guarded by the branch above
            raise RuntimeError(f"Task {task_id} has a job without an event tail.")
        events = active_tail.read()
        if job.current_stage == "comparative_analysis":
            latest_progress_stage = _comparative_progress_stage(events)
            if latest_progress_stage is not None:
                observed.comparative_progress_stage = latest_progress_stage
        else:
            observed.comparative_progress_stage = None
        was_terminal = observed.row.terminal
        observed.row = WatchTaskRow(
            task_id=task_id,
            job_id=job.job_id,
            state=job.state.value,
            stage=observed.comparative_progress_stage or job.current_stage,
            progress=job.progress,
            warning_count=active_tail.warning_count,
            task_name=snapshot.task.name,
            trace_id=trace_id,
        )
        if observed.row.terminal and not was_terminal:
            observed.terminal_settled = False
        elif not observed.row.terminal:
            observed.terminal_settled = False
        return events

    def _settle_terminal(self, observed: _ObservedTask) -> tuple[Event, ...]:
        if observed.tail is None:
            observed.terminal_settled = True
            return tuple()
        events = observed.tail.read()
        observed.row = WatchTaskRow(
            task_id=observed.row.task_id,
            job_id=observed.row.job_id,
            state=observed.row.state,
            stage=observed.row.stage,
            progress=observed.row.progress,
            warning_count=observed.tail.warning_count,
            task_name=observed.row.task_name,
            trace_id=observed.row.trace_id,
        )
        expected_type = _TERMINAL_EVENT_TYPE_BY_STATE.get(observed.row.state)
        terminal_event_is_logged = expected_type is not None and event_type_rank(
            expected_type
        ) >= event_type_rank(self._task_log_minimum_type)
        observed.terminal_settled = (
            not terminal_event_is_logged or observed.tail.has_terminal_event(observed.row.state)
        )
        return events


def _snapshot_is_active(snapshot: AnalyticalTaskSnapshot) -> bool:
    job = snapshot.active_or_latest_job
    return (
        job is not None
        and snapshot.task.state in ACTIVE_ANALYTICAL_TASK_JOB_STATES
        and job.state in ACTIVE_ANALYTICAL_TASK_JOB_STATES
    )


def _comparative_progress_stage(events: tuple[Event, ...]) -> str | None:
    labels = {
        "preparation": "preparation",
        "statistics_metric": "statistics",
        "reference_comparison": "reference",
        "pairwise_comparison": "pairwise",
        "publication": "publication",
    }
    for event in reversed(events):
        if event.name != "CORE_COMPARATIVE_ANALYSIS_PROGRESS":
            continue
        context = event.context or {}
        operation_kind = context.get("operation_kind")
        completed = context.get("completed")
        total = context.get("total")
        if (
            not isinstance(operation_kind, str)
            or operation_kind not in labels
            or isinstance(completed, bool)
            or not isinstance(completed, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or completed < 0
            or total < 0
        ):
            return None
        return f"comparative_analysis · {labels[operation_kind]} {completed}/{total}"
    return None


def _event_job_id(event: Event) -> str | None:
    if event.context is None:
        return None
    raw_job_id = event.context.get("job_id")
    return raw_job_id if isinstance(raw_job_id, str) else None
