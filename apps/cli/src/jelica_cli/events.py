from __future__ import annotations

from jelica_contracts import CodeNamespace, EventDefinition, EventType

CLI_ANALYZE_ARGUMENT_INVALID = EventDefinition(
    code=3000,
    name="CLI_ANALYZE_ARGUMENT_INVALID",
    namespace=CodeNamespace.CLI,
    default_type=EventType.ERROR,
    title="Invalid analyze arguments",
    message_template="{detail}",
    category="cli_arguments",
)

CLI_CONFIG_FILE_READ_FAILED = EventDefinition(
    code=3001,
    name="CLI_CONFIG_FILE_READ_FAILED",
    namespace=CodeNamespace.CLI,
    default_type=EventType.ERROR,
    title="Config file read error",
    message_template="{detail}",
    category="cli_filesystem",
)

CLI_OUTPUT_FORMAT_INVALID = EventDefinition(
    code=3002,
    name="CLI_OUTPUT_FORMAT_INVALID",
    namespace=CodeNamespace.CLI,
    default_type=EventType.ERROR,
    title="Invalid output format",
    message_template="Unsupported output format '{format}'.",
    category="cli_output",
)

CLI_INTERNAL_ERROR = EventDefinition(
    code=3003,
    name="CLI_INTERNAL_ERROR",
    namespace=CodeNamespace.CLI,
    default_type=EventType.CRITICAL,
    title="Unexpected CLI error",
    message_template="Unexpected CLI error: {detail}",
    category="cli_internal",
)
CLI_INLINE_SEQUENCE_TOO_LONG = EventDefinition(
    code=3004,
    name="CLI_INLINE_SEQUENCE_TOO_LONG",
    namespace=CodeNamespace.CLI,
    default_type=EventType.ERROR,
    title="Inline sequence too long",
    message_template="{detail}",
    category="cli_arguments",
)

CLI_COMMAND_INTERRUPTED = EventDefinition(
    code=3005,
    name="CLI_COMMAND_INTERRUPTED",
    namespace=CodeNamespace.CLI,
    default_type=EventType.ERROR,
    title="Command interrupted",
    message_template=(
        "Command observation was interrupted by the user; Service-owned task execution "
        "continues."
    ),
    category="cli_lifecycle",
)

CLI_USAGE_ERROR = EventDefinition(
    code=3006,
    name="CLI_USAGE_ERROR",
    namespace=CodeNamespace.CLI,
    default_type=EventType.ERROR,
    title="Invalid CLI invocation",
    message_template="{detail}",
    category="cli_arguments",
)

CLI_EVENT_CURSOR_NOT_FOUND = EventDefinition(
    code=3007,
    name="CLI_EVENT_CURSOR_NOT_FOUND",
    namespace=CodeNamespace.CLI,
    default_type=EventType.ERROR,
    title="Event cursor not found",
    message_template=(
        "Event cursor '{event_id}' was not found in the persisted system event log."
    ),
    category="cli_lifecycle",
)

CLI_RESULT_PACKAGE_RESOLUTION_FAILED = EventDefinition(
    code=3008,
    name="CLI_RESULT_PACKAGE_RESOLUTION_FAILED",
    namespace=CodeNamespace.CLI,
    default_type=EventType.ERROR,
    title="Result package resolution failed",
    message_template="{detail}",
    category="cli_lifecycle",
)

CLI_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    CLI_ANALYZE_ARGUMENT_INVALID,
    CLI_CONFIG_FILE_READ_FAILED,
    CLI_OUTPUT_FORMAT_INVALID,
    CLI_INTERNAL_ERROR,
    CLI_INLINE_SEQUENCE_TOO_LONG,
    CLI_COMMAND_INTERRUPTED,
    CLI_USAGE_ERROR,
    CLI_EVENT_CURSOR_NOT_FOUND,
    CLI_RESULT_PACKAGE_RESOLUTION_FAILED,
)

CLI_EVENTS_BY_NAME: dict[str, EventDefinition] = {}
CLI_EVENTS_BY_CODE: dict[int, EventDefinition] = {}

for _definition in CLI_EVENT_DEFINITIONS:
    if _definition.code in CLI_EVENTS_BY_CODE:
        raise ValueError(f"Duplicate CLI event code {_definition.code}.")
    if _definition.name in CLI_EVENTS_BY_NAME:
        raise ValueError(f"Duplicate CLI event name {_definition.name}.")
    CLI_EVENTS_BY_CODE[_definition.code] = _definition
    CLI_EVENTS_BY_NAME[_definition.name] = _definition
