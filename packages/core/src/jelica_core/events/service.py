from __future__ import annotations

from collections.abc import Mapping, Sequence

from jelica_contracts import (
    Event,
    EventDefinition,
    EventDiagnostics,
    EventType,
    JSONObject,
    JSONValue,
)

from .context import CoreExecutionContext
from .factory import CoreEventFactory
from .sinks import EventSink, EventSinkError


class EventServiceError(RuntimeError):
    """Base service error for event dispatch failures."""


class MandatoryEventSinkWriteError(EventServiceError):
    def __init__(self, *, event: Event, sink_error: EventSinkError) -> None:
        self.event = event
        self.sink_error = sink_error
        super().__init__(f"Mandatory event sink failed for '{event.name}': {sink_error}")


class EventService:
    """Dispatch Core events to registered sinks."""

    def __init__(
        self,
        *,
        factory: CoreEventFactory | None = None,
        sinks: Sequence[EventSink] = (),
    ) -> None:
        self._factory = factory or CoreEventFactory()
        self._sinks: list[EventSink] = list(sinks)

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def emit(
        self,
        definition: EventDefinition,
        *,
        execution_context: CoreExecutionContext | None = None,
        message_params: Mapping[str, JSONValue] | None = None,
        context: JSONObject | None = None,
        diagnostics: EventDiagnostics | None = None,
        event_type: EventType | None = None,
    ) -> Event:
        event = self._factory.create(
            definition,
            execution_context=execution_context,
            message_params=message_params,
            context=context,
            diagnostics=diagnostics,
            event_type=event_type,
        )
        self.emit_event(event)
        return event

    def emit_event(self, event: Event) -> tuple[EventSinkError, ...]:
        optional_errors: list[EventSinkError] = []
        for sink in self._sinks:
            try:
                sink.emit(event)
            except EventSinkError as error:
                if sink.required:
                    raise MandatoryEventSinkWriteError(event=event, sink_error=error) from error
                optional_errors.append(error)
        return tuple(optional_errors)
