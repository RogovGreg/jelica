from __future__ import annotations

from collections.abc import Mapping

from jelica_contracts import (
    Event,
    EventComponent,
    EventDefinition,
    EventDiagnostics,
    EventType,
    JSONObject,
    JSONValue,
)

from .context import CoreExecutionContext


class CoreEventFactory:
    """Factory that creates fully-populated Event instances for Core operations."""

    def __init__(self, *, component: EventComponent = EventComponent.CORE) -> None:
        self._component = component

    def create(
        self,
        definition: EventDefinition,
        *,
        execution_context: CoreExecutionContext | None = None,
        message_params: Mapping[str, JSONValue] | None = None,
        context: JSONObject | None = None,
        diagnostics: EventDiagnostics | None = None,
        event_type: EventType | None = None,
        component: EventComponent | None = None,
    ) -> Event:
        selected_execution_context = execution_context or CoreExecutionContext()
        selected_component = component or self._component
        selected_context = selected_execution_context.merged_context(context=context)
        return Event(
            code=definition.code,
            name=definition.name,
            type=event_type or definition.default_type,
            title=definition.title,
            message=definition.render_message(params=message_params),
            component=selected_component,
            trace_id=selected_execution_context.trace_id,
            command_id=selected_execution_context.command_id,
            task_id=selected_execution_context.task_id,
            run_id=selected_execution_context.run_id,
            stage=selected_execution_context.stage,
            worker_id=selected_execution_context.worker_id,
            attempt=selected_execution_context.attempt,
            operation_id=selected_execution_context.operation_id,
            context=selected_context,
            diagnostics=diagnostics,
        )
