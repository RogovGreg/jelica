from __future__ import annotations

from typing import Protocol


class ProgressReporter(Protocol):
    def start(self, *, description: str, total: float | None = None) -> None: ...

    def update(
        self,
        *,
        description: str | None = None,
        progress: float | None = None,
    ) -> None: ...

    def complete(self, *, description: str | None = None) -> None: ...

    def __call__(self, progress: float) -> None: ...


class NullProgressReporter:
    def start(self, *, description: str, total: float | None = None) -> None:
        return None

    def update(
        self,
        *,
        description: str | None = None,
        progress: float | None = None,
    ) -> None:
        return None

    def complete(self, *, description: str | None = None) -> None:
        return None

    def __call__(self, progress: float) -> None:
        return None
