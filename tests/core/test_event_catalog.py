from __future__ import annotations

import pytest

from jelica_contracts import CodeNamespace, EventDefinition, EventType
from jelica_core.events import (
    CORE_EVENT_CATALOG,
    DuplicateEventCodeError,
    DuplicateEventNameError,
    EventCatalog,
    EventDefinitionNotFoundError,
)


def _definition(*, code: int, name: str) -> EventDefinition:
    return EventDefinition(
        code=code,
        name=name,
        namespace=CodeNamespace.CORE,
        default_type=EventType.INFO,
        title="Core event",
        message_template="message",
    )


def test_catalog_rejects_duplicate_codes() -> None:
    first = _definition(code=2100, name="CORE_FIRST")
    second = _definition(code=2100, name="CORE_SECOND")
    catalog = EventCatalog([first])

    with pytest.raises(DuplicateEventCodeError):
        catalog.register(second)


def test_catalog_rejects_duplicate_names() -> None:
    first = _definition(code=2100, name="CORE_DUPLICATE")
    second = _definition(code=2101, name="CORE_DUPLICATE")
    catalog = EventCatalog([first])

    with pytest.raises(DuplicateEventNameError):
        catalog.register(second)


def test_definition_rejects_code_outside_namespace_range() -> None:
    with pytest.raises(ValueError):
        EventDefinition(
            code=1500,
            name="CORE_INVALID_RANGE",
            namespace=CodeNamespace.CORE,
            default_type=EventType.INFO,
            title="Invalid",
            message_template="invalid",
        )


def test_definition_rejects_name_with_wrong_prefix() -> None:
    with pytest.raises(ValueError):
        EventDefinition(
            code=2100,
            name="SYSTEM_WRONG_PREFIX",
            namespace=CodeNamespace.CORE,
            default_type=EventType.INFO,
            title="Invalid",
            message_template="invalid",
        )


def test_catalog_lookup_raises_for_unknown_name() -> None:
    with pytest.raises(EventDefinitionNotFoundError):
        CORE_EVENT_CATALOG.get("CORE_UNKNOWN_EVENT")
