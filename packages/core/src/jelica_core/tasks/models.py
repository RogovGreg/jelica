from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from jelica_core.config import ResolvedAnalysisConfig


class TaskWorkspacePaths(BaseModel):
    """File-system paths created for a task workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_dir: Path
    config_path: Path
    configs_dir: Path
    jobs_dir: Path
    current_config_revision: int = Field(ge=1)
    current_config_relative_path: str = Field(min_length=1)
    current_config_hash: str = Field(min_length=1)


class InitializedAnalysisTask(BaseModel):
    """Initialized analysis task state for the first pipeline stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    name: str | None = None
    task_dir: Path
    config_path: Path
    config: ResolvedAnalysisConfig
    current_config_revision: int = Field(ge=1)
    current_config_relative_path: str = Field(min_length=1)
    current_config_hash: str = Field(min_length=1)
    warnings: tuple[str, ...] = Field(default_factory=tuple)
