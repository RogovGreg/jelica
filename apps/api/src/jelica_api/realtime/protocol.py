from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jelica_api.contracts.comments import ProjectCommentReaction

ProjectCommandName = Literal[
    "comment.create",
    "comment.update",
    "comment.delete",
    "reaction.set",
    "reaction.delete",
    "discussion.clear",
]


class CommentCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    body: str = Field(min_length=1, max_length=10_000)


class CommentUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    comment_id: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=10_000)


class CommentDeletePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    comment_id: str = Field(min_length=1)


class ReactionSetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    comment_id: str = Field(min_length=1)
    reaction: ProjectCommentReaction


class ReactionDeletePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    comment_id: str = Field(min_length=1)


class DiscussionClearPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["command"]
    id: str = Field(min_length=1, max_length=128)
    command: ProjectCommandName
    payload: dict[str, Any]


class TypingEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["typing.start", "typing.stop"]


class ProtocolMessageError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        command_id: str | None = None,
        command: str | None = None,
    ) -> None:
        self.code = code
        self.command_id = command_id
        self.command = command
        super().__init__(message)


_COMMAND_PAYLOADS: dict[str, type[BaseModel]] = {
    "comment.create": CommentCreatePayload,
    "comment.update": CommentUpdatePayload,
    "comment.delete": CommentDeletePayload,
    "reaction.set": ReactionSetPayload,
    "reaction.delete": ReactionDeletePayload,
    "discussion.clear": DiscussionClearPayload,
}


def parse_client_message(raw_text: str) -> CommandEnvelope | TypingEnvelope:
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ProtocolMessageError(
            code="malformed_command",
            message="Message must be valid JSON.",
        ) from error
    if not isinstance(raw, dict):
        raise ProtocolMessageError(
            code="malformed_command",
            message="Message must be a JSON object.",
        )

    message_type = raw.get("type")
    if message_type in {"typing.start", "typing.stop"}:
        try:
            return TypingEnvelope.model_validate(raw)
        except ValidationError as error:
            raise ProtocolMessageError(
                code="malformed_command",
                message="Typing message is malformed.",
            ) from error
    if message_type != "command":
        raise ProtocolMessageError(
            code="unsupported_command",
            message="Message type is not supported.",
        )

    command_id = raw.get("id") if isinstance(raw.get("id"), str) else None
    command = raw.get("command") if isinstance(raw.get("command"), str) else None
    if command not in _COMMAND_PAYLOADS:
        raise ProtocolMessageError(
            code="unsupported_command",
            message="Command is not supported.",
            command_id=command_id,
            command=command,
        )
    try:
        envelope = CommandEnvelope.model_validate(raw)
        payload = _COMMAND_PAYLOADS[command].model_validate(envelope.payload)
    except ValidationError as error:
        raise ProtocolMessageError(
            code="malformed_command",
            message="Command payload is malformed.",
            command_id=command_id,
            command=command,
        ) from error
    return envelope.model_copy(update={"payload": payload.model_dump()})


def command_ack(
    *,
    command_id: str,
    command: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "command.ack",
        "id": command_id,
        "command": command,
    }
    if result is not None:
        message["result"] = result
    return message


def command_error(
    *,
    command_id: str,
    command: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "type": "command.error",
        "id": command_id,
        "command": command,
        "error": {"code": code, "message": message},
    }


def protocol_error(*, code: str, message: str) -> dict[str, Any]:
    return {
        "type": "protocol.error",
        "error": {"code": code, "message": message},
    }


__all__ = [
    "CommandEnvelope",
    "ProtocolMessageError",
    "TypingEnvelope",
    "command_ack",
    "command_error",
    "parse_client_message",
    "protocol_error",
]
