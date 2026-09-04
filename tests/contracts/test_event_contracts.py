from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from jelica_contracts import (
    CONTRACT_SCHEMA_VERSION,
    Event,
    EventComponent,
    EventDiagnostics,
    EventType,
    PublicError,
    event_json_schema,
    public_error_json_schema,
)


def _build_event(**kwargs: object) -> Event:
    payload: dict[str, object] = {
        "code": 2000,
        "name": "CORE_TEST_EVENT",
        "type": EventType.INFO,
        "title": "Test event",
        "message": "Event message",
        "component": EventComponent.CORE,
    }
    payload.update(kwargs)
    return Event.model_validate(payload)


def test_event_model_creates_valid_instance() -> None:
    event = _build_event()

    assert event.schema_version == CONTRACT_SCHEMA_VERSION
    assert event.code == 2000
    assert event.name == "CORE_TEST_EVENT"


def test_event_timestamp_is_utc_iso8601_with_microseconds() -> None:
    event = _build_event(timestamp=datetime(2026, 7, 26, 20, 15, 31, 381527, tzinfo=UTC))

    serialized_timestamp = event.model_dump(mode="json")["timestamp"]
    assert serialized_timestamp == "2026-07-26T20:15:31.381527Z"


def test_event_json_roundtrip_preserves_schema_version() -> None:
    event = _build_event(context={"source": "sample.fasta"})

    payload = json.dumps(event.model_dump(mode="json"))
    restored = Event.model_validate_json(payload)

    assert restored.schema_version == CONTRACT_SCHEMA_VERSION
    assert restored.context == {"source": "sample.fasta"}


def test_event_context_allows_structured_json_values() -> None:
    event = _build_event(
        context={
            "stats": {"count": 2, "names": ["a", "b"]},
            "nullable": None,
            "flag": True,
        }
    )

    assert event.context is not None
    assert event.context["stats"] == {"count": 2, "names": ["a", "b"]}


def test_event_context_rejects_non_serializable_objects() -> None:
    with pytest.raises(ValidationError):
        _build_event(context={"bad": object()})


def test_public_error_excludes_traceback_by_default() -> None:
    event = _build_event(
        type=EventType.ERROR,
        diagnostics=EventDiagnostics(
            diagnostic_message="detail",
            traceback="traceback details",
        ),
    )
    public_error = PublicError(event=event, expected=False)

    assert "diagnostics" not in public_error.to_dict()["event"]
    assert "diagnostics" in public_error.to_dict(include_diagnostics=True)["event"]


def test_public_error_requires_error_event_type() -> None:
    event = _build_event(type=EventType.INFO)

    with pytest.raises(ValidationError):
        PublicError(event=event)


def test_event_json_schema_is_available() -> None:
    schema = event_json_schema()

    assert schema["type"] == "object"
    assert "schema_version" in schema["properties"]
    assert "event_id" in schema["properties"]


def test_public_error_json_schema_is_available() -> None:
    schema = public_error_json_schema()

    assert schema["type"] == "object"
    assert "event" in schema["properties"]
    assert "expected" in schema["properties"]


def test_generated_schema_files_exist_and_are_valid_json() -> None:
    schemas_dir = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "schemas"
    event_schema = json.loads((schemas_dir / "event.schema.json").read_text(encoding="utf-8"))
    error_schema = json.loads(
        (schemas_dir / "public_error.schema.json").read_text(encoding="utf-8")
    )
    result_package_schema = json.loads(
        (schemas_dir / "jelica_result_package_manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert event_schema["type"] == "object"
    assert error_schema["type"] == "object"
    assert result_package_schema["type"] == "object"
    assert (
        result_package_schema["properties"]["format"]["const"] == "jelica-result-package"
    )
    assert result_package_schema["properties"]["format_version"]["const"] == "1.0"
