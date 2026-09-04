from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from jelica_contracts import Event, PublicError

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CoreOperationResult(Generic[T]):
    ok: bool
    event: Event
    value: T | None
    error: PublicError | None
    system_log_path: Path | None
    task_log_path: Path | None

    @classmethod
    def success(
        cls,
        *,
        event: Event,
        value: T,
        system_log_path: Path | None,
        task_log_path: Path | None = None,
    ) -> CoreOperationResult[T]:
        return cls(
            ok=True,
            event=event,
            value=value,
            error=None,
            system_log_path=system_log_path,
            task_log_path=task_log_path,
        )

    @classmethod
    def failure(
        cls,
        *,
        error: PublicError,
        system_log_path: Path | None,
        task_log_path: Path | None = None,
    ) -> CoreOperationResult[T]:
        return cls(
            ok=False,
            event=error.event,
            value=None,
            error=error,
            system_log_path=system_log_path,
            task_log_path=task_log_path,
        )
