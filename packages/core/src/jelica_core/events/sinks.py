from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from jelica_contracts import Event, EventType, event_type_rank

SYSTEM_EVENTS_LOG_FILENAME = "system-events.jsonl"
TASK_EVENTS_LOG_FILENAME = "task-events.jsonl"


class EventSinkError(RuntimeError):
    def __init__(self, *, sink_name: str, detail: str, path: Path | None = None) -> None:
        self.sink_name = sink_name
        self.detail = detail
        self.path = path
        if path is None:
            message = f"Event sink '{sink_name}' failed: {detail}"
        else:
            message = f"Event sink '{sink_name}' failed for '{path}': {detail}"
        super().__init__(message)


class EventSink(ABC):
    def __init__(self, *, minimum_level: EventType, required: bool) -> None:
        self._minimum_level = minimum_level
        self._required = required

    @property
    def required(self) -> bool:
        return self._required

    def emit(self, event: Event) -> None:
        if event_type_rank(event.type) < event_type_rank(self._minimum_level):
            return
        self._emit(event)

    @abstractmethod
    def _emit(self, event: Event) -> None:
        """Write one event into this sink."""


class JsonlFileEventSink(EventSink):
    def __init__(
        self,
        *,
        path: Path,
        minimum_level: EventType,
        required: bool,
        task_id: str | None = None,
    ) -> None:
        super().__init__(minimum_level=minimum_level, required=required)
        self._path = path
        normalized_task_id = task_id.strip() if task_id is not None else ""
        self._task_id = normalized_task_id if normalized_task_id != "" else None

    @property
    def path(self) -> Path:
        return self._path

    def _emit(self, event: Event) -> None:
        if self._task_id is not None and event.task_id != self._task_id:
            return
        payload = json.dumps(
            event.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open(mode="a", encoding="utf-8", newline="\n") as file:
                file.write(payload)
                file.write("\n")
        except OSError as error:
            raise EventSinkError(
                sink_name=self.__class__.__name__,
                detail=str(error),
                path=self._path,
            ) from error


class InMemoryEventSink(EventSink):
    def __init__(
        self,
        *,
        minimum_level: EventType = EventType.DEBUG,
        required: bool = False,
    ) -> None:
        super().__init__(minimum_level=minimum_level, required=required)
        self.events: list[Event] = []

    def _emit(self, event: Event) -> None:
        self.events.append(event)
