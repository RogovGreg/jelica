from __future__ import annotations

from contextvars import ContextVar, Token
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jelica_contracts import JSONObject, JSONValue

_CURRENT_COMMAND_ID: ContextVar[UUID | None] = ContextVar(
    "jelica_current_command_id",
    default=None,
)


def current_command_id() -> UUID | None:
    return _CURRENT_COMMAND_ID.get()


def set_command_id(command_id: UUID) -> Token[UUID | None]:
    return _CURRENT_COMMAND_ID.set(command_id)


def reset_command_id(token: Token[UUID | None]) -> None:
    _CURRENT_COMMAND_ID.reset(token)


class CoreExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: UUID | None = None
    command_id: UUID | None = Field(default_factory=current_command_id)
    task_id: str | None = None
    run_id: str | None = None
    stage: str | None = None
    worker_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    operation_id: str | None = None

    def merged_context(self, *, context: JSONObject | None = None) -> JSONObject | None:
        merged: dict[str, JSONValue] = dict(context or {})
        execution_block: dict[str, JSONValue] = {}
        if self.attempt is not None:
            execution_block["attempt"] = self.attempt
        if self.operation_id is not None:
            execution_block["operation_id"] = self.operation_id

        if len(execution_block) == 0:
            return merged or None

        existing_execution = merged.get("execution")
        if isinstance(existing_execution, dict):
            merged_execution = dict(existing_execution)
            merged_execution.update(execution_block)
        else:
            merged_execution = execution_block
        merged["execution"] = merged_execution
        return merged
