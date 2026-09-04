from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Callable, Final
from uuid import UUID, uuid4

from jelica_contracts import Event, JSONValue, PublicError
from jelica_core import __version__ as JELICA_VERSION

MACHINE_PROTOCOL_VERSION: Final = "1"


@dataclass(frozen=True, slots=True)
class MachineInvocation:
    """Correlation identifiers shared by one CLI invocation."""

    command_id: str
    trace_id: str | None = None

    def with_trace_id(self, trace_id: str | UUID | None) -> MachineInvocation:
        normalized_trace_id = None if trace_id is None else str(trace_id)
        return replace(self, trace_id=normalized_trace_id)


def create_machine_invocation(
    *,
    trace_id: str | UUID | None = None,
    command_id_factory: Callable[[], UUID] = uuid4,
) -> MachineInvocation:
    return MachineInvocation(
        command_id=str(command_id_factory()),
        trace_id=None if trace_id is None else str(trace_id),
    )


def machine_success_payload(
    *,
    invocation: MachineInvocation,
    data: dict[str, JSONValue],
) -> dict[str, JSONValue]:
    return {
        "machine_protocol_version": MACHINE_PROTOCOL_VERSION,
        "jelica_version": JELICA_VERSION,
        "trace_id": invocation.trace_id,
        "command_id": invocation.command_id,
        "ok": True,
        "data": data,
    }


def machine_error_payload(
    *,
    invocation: MachineInvocation,
    error: PublicError,
) -> dict[str, JSONValue]:
    return {
        "machine_protocol_version": MACHINE_PROTOCOL_VERSION,
        "jelica_version": JELICA_VERSION,
        "trace_id": invocation.trace_id,
        "command_id": invocation.command_id,
        "ok": False,
        "error": {
            "code": error.event.code,
            "name": error.event.name,
            "message": error.event.message,
            "details": error.safe_details or {},
        },
    }


def serialize_machine_payload(payload: dict[str, JSONValue]) -> str:
    """Serialize one machine response without terminal decoration."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def serialize_machine_event(
    *,
    event: Event,
) -> str:
    """Serialize one machine protocol event stream record as one JSONL line."""

    payload = event.model_dump(mode="json", exclude_none=True)
    payload["machine_protocol_version"] = MACHINE_PROTOCOL_VERSION
    return serialize_machine_payload(payload)
