from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jelica_core.config import ConfigOverride
from jelica_core.tasks.names import validate_task_name


class InitializeAnalysisTaskRequest(BaseModel):
    """Input payload for initializing an analysis task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    trace_id: UUID | None = None
    config_json: str | None = None
    overrides: tuple[ConfigOverride, ...] = Field(default_factory=tuple)
    positional_sources: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_task_name(value)
