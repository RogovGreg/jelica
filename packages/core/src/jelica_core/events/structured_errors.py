from __future__ import annotations

from enum import StrEnum
from typing import Any

from jelica_contracts import EventDefinition, JSONObject, JSONValue


class CoreErrorCategory(StrEnum):
    SYSTEM_CONFIG = "system_config"
    TASK_CONFIG = "task_config"
    INPUT_DATA = "input_data"
    FILESYSTEM = "filesystem"
    TASK_LIFECYCLE = "task_lifecycle"
    PIPELINE_STAGE = "pipeline_stage"
    EXTERNAL_TOOL = "external_tool"
    INTERNAL = "internal"


class CoreStructuredError(RuntimeError):
    def __init__(
        self,
        *,
        category: CoreErrorCategory,
        definition: EventDefinition,
        message: str,
        expected: bool = True,
        retryable: bool = False,
        can_continue: bool = False,
        safe_details: JSONObject | None = None,
        context: JSONObject | None = None,
        message_params: dict[str, JSONValue] | None = None,
        diagnostic_message: str | None = None,
    ) -> None:
        self.category = category
        self.definition = definition
        self.expected = expected
        self.retryable = retryable
        self.can_continue = can_continue
        self.safe_details = safe_details
        self.context = context
        self.message_params = message_params
        self.diagnostic_message = diagnostic_message
        super().__init__(message)


class CoreSystemConfigError(CoreStructuredError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(category=CoreErrorCategory.SYSTEM_CONFIG, **kwargs)


class CoreTaskConfigError(CoreStructuredError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(category=CoreErrorCategory.TASK_CONFIG, **kwargs)


class CoreInputDataError(CoreStructuredError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(category=CoreErrorCategory.INPUT_DATA, **kwargs)


class CoreFilesystemError(CoreStructuredError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(category=CoreErrorCategory.FILESYSTEM, **kwargs)


class CoreTaskLifecycleError(CoreStructuredError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(category=CoreErrorCategory.TASK_LIFECYCLE, **kwargs)


class CorePipelineStageError(CoreStructuredError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(category=CoreErrorCategory.PIPELINE_STAGE, **kwargs)


class CoreExternalToolError(CoreStructuredError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(category=CoreErrorCategory.EXTERNAL_TOOL, **kwargs)


class CoreInternalError(CoreStructuredError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(category=CoreErrorCategory.INTERNAL, **kwargs)
