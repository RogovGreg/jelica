from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jelica_api.cli import (
    JelicaCliClient,
    JelicaCliCommandError,
    JelicaCliInvocationError,
    JelicaCliProtocolError,
)
from jelica_api.web_tasks import WebTaskProjectionStore

_LOGGER = logging.getLogger(__name__)
_TASK_NOT_FOUND_MACHINE_ERROR = "CORE_ANALYTICAL_TASK_NOT_FOUND"
WEB_TASK_RECONCILIATION_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    scanned: int
    updated: int
    unchanged: int
    errors: int


@dataclass(frozen=True, slots=True)
class ReconciliationDiagnostics:
    scanned: int
    updated: int
    errors: int
    last_run_at: datetime | None


@dataclass(slots=True)
class WebTaskProjectionReconciler:
    cli_client: JelicaCliClient
    projection_store: WebTaskProjectionStore
    _last_diagnostics: ReconciliationDiagnostics = field(
        default_factory=lambda: ReconciliationDiagnostics(
            scanned=0,
            updated=0,
            errors=0,
            last_run_at=None,
        ),
        init=False,
        repr=False,
    )

    def reconcile(self) -> ReconciliationReport:
        candidates = self.projection_store.list_potentially_active_tasks()
        updated = 0
        unchanged = 0
        errors = 0
        for projection in candidates:
            try:
                snapshot = self.cli_client.get_task_status(task_reference=projection.core_task_id)
            except JelicaCliCommandError as error:
                if _is_task_not_found(error):
                    unchanged += 1
                    continue
                errors += 1
                _LOGGER.warning(
                    "Projection reconciliation command error for task '%s': %s",
                    projection.core_task_id,
                    error,
                )
                continue
            except (JelicaCliInvocationError, JelicaCliProtocolError) as error:
                errors += 1
                _LOGGER.warning(
                    "Projection reconciliation infrastructure error for task '%s': %s",
                    projection.core_task_id,
                    error,
                )
                continue

            if snapshot.state == projection.status:
                unchanged += 1
                continue

            self.projection_store.upsert_task(
                core_task_id=projection.core_task_id,
                name=projection.name,
                status=snapshot.state,
            )
            updated += 1

        report = ReconciliationReport(
            scanned=len(candidates),
            updated=updated,
            unchanged=unchanged,
            errors=errors,
        )
        self._last_diagnostics = ReconciliationDiagnostics(
            scanned=report.scanned,
            updated=report.updated,
            errors=report.errors,
            last_run_at=datetime.now(UTC),
        )
        return report

    def get_diagnostics(self) -> ReconciliationDiagnostics:
        return self._last_diagnostics

    async def run_periodically(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=WEB_TASK_RECONCILIATION_INTERVAL_SECONDS,
                )
            except TimeoutError:
                await asyncio.to_thread(self.reconcile)


def _is_task_not_found(error: JelicaCliCommandError) -> bool:
    machine_error = error.envelope.error
    if machine_error is None:
        return False
    return machine_error.name == _TASK_NOT_FOUND_MACHINE_ERROR


__all__ = [
    "ReconciliationDiagnostics",
    "ReconciliationReport",
    "WebTaskProjectionReconciler",
    "WEB_TASK_RECONCILIATION_INTERVAL_SECONDS",
]
