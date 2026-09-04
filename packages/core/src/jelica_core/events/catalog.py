from __future__ import annotations

from collections.abc import Iterable

from jelica_contracts import EventDefinition


class EventCatalogError(ValueError):
    """Base error for invalid event catalog state."""


class DuplicateEventCodeError(EventCatalogError):
    def __init__(self, *, code: int, existing_name: str, new_name: str) -> None:
        self.code = code
        self.existing_name = existing_name
        self.new_name = new_name
        super().__init__(
            f"Duplicate event code {code}: '{existing_name}' conflicts with '{new_name}'."
        )


class DuplicateEventNameError(EventCatalogError):
    def __init__(self, *, name: str) -> None:
        self.name = name
        super().__init__(f"Duplicate event name '{name}'.")


class EventDefinitionNotFoundError(EventCatalogError):
    def __init__(self, *, name: str) -> None:
        self.name = name
        super().__init__(f"Event definition '{name}' is not registered.")


class EventCatalog:
    """Component-owned collection of stable event definitions."""

    def __init__(self, definitions: Iterable[EventDefinition] = ()) -> None:
        self._by_code: dict[int, EventDefinition] = {}
        self._by_name: dict[str, EventDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: EventDefinition) -> None:
        existing_by_code = self._by_code.get(definition.code)
        if existing_by_code is not None:
            raise DuplicateEventCodeError(
                code=definition.code,
                existing_name=existing_by_code.name,
                new_name=definition.name,
            )

        if definition.name in self._by_name:
            raise DuplicateEventNameError(name=definition.name)

        self._by_code[definition.code] = definition
        self._by_name[definition.name] = definition

    def get(self, name: str) -> EventDefinition:
        definition = self._by_name.get(name)
        if definition is None:
            raise EventDefinitionNotFoundError(name=name)
        return definition

    def get_by_code(self, code: int) -> EventDefinition | None:
        return self._by_code.get(code)

    def all(self) -> tuple[EventDefinition, ...]:
        return tuple(sorted(self._by_code.values(), key=lambda item: item.code))
