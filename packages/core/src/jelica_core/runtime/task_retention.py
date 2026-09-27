from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Callable

from jelica_core.system_config import ResolvedCoreConfig
from jelica_core.tasks import (
    TERMINAL_ANALYTICAL_TASK_STATES,
    AnalyticalTaskRegistryService,
    AnalyticalTaskSortOrder,
)
from jelica_core.tasks.timestamps import utc_now

TASK_RETENTION_MAINTENANCE_INTERVAL = timedelta(hours=24)
TASK_RETENTION_FAILURE_RETRY_INTERVAL = timedelta(hours=1)


class RetentionDeletionResult(StrEnum):
    DELETED = "deleted"
    ALREADY_SATISFIED = "already_satisfied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RetentionDeletionOutcome:
    result: RetentionDeletionResult
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRetentionResult:
    enabled: bool
    attempted: int
    candidate_count: int
    deleted_count: int
    already_satisfied_count: int
    failed_count: int
    started_at: datetime
    completed_at: datetime
    success: bool
    failed_task_ids: tuple[str, ...] = tuple()
    errors: tuple[str, ...] = tuple()


RetentionDeletionCallback = Callable[[str], RetentionDeletionOutcome]


class TaskRetentionMaintenance:
    """Run one deterministic, testable automatic task-retention pass."""

    def __init__(
        self,
        *,
        resolved_config: ResolvedCoreConfig,
        registry_service: AnalyticalTaskRegistryService,
        delete_task: RetentionDeletionCallback,
        clock: Callable[[], datetime] = utc_now,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._resolved_config = resolved_config
        self._registry_service = registry_service
        self._delete_task = delete_task
        self._clock = clock
        self._stop_requested = stop_requested or (lambda: False)

    def run_once(self) -> TaskRetentionResult:
        started_at = _as_utc(self._clock())
        retention_days = self._resolved_config.tasks_retention_days
        if retention_days is None:
            return TaskRetentionResult(
                enabled=False,
                attempted=0,
                candidate_count=0,
                deleted_count=0,
                already_satisfied_count=0,
                failed_count=0,
                started_at=started_at,
                completed_at=started_at,
                success=True,
            )

        cutoff = started_at - timedelta(days=retention_days)
        failed_task_ids: list[str] = []
        errors: list[str] = []
        attempted = 0
        deleted_count = 0
        already_satisfied_count = 0
        failed_count = 0
        candidate_count = 0
        interrupted = False
        try:
            candidates = self._registry_service.list_task_snapshots(
                states=tuple(TERMINAL_ANALYTICAL_TASK_STATES),
                updated_before=cutoff,
                limit=None,
                order=AnalyticalTaskSortOrder.UPDATED_AT_ASC,
            )
            candidate_count = len(candidates)
        except Exception as error:
            completed_at = _as_utc(self._clock())
            return TaskRetentionResult(
                enabled=True,
                attempted=0,
                candidate_count=0,
                deleted_count=0,
                already_satisfied_count=0,
                failed_count=1,
                started_at=started_at,
                completed_at=completed_at,
                success=False,
                errors=(f"candidate scan failed: {error}",),
            )

        for candidate in candidates:
            if self._stop_requested():
                interrupted = True
                errors.append("cleanup interrupted by shutdown request")
                break
            task_id = candidate.task.task_id
            attempted += 1
            try:
                outcome = self._delete_task(task_id)
            except Exception as error:
                outcome = RetentionDeletionOutcome(
                    result=RetentionDeletionResult.FAILED,
                    detail=str(error),
                )
            if outcome.result is RetentionDeletionResult.DELETED:
                deleted_count += 1
            elif outcome.result is RetentionDeletionResult.ALREADY_SATISFIED:
                already_satisfied_count += 1
            else:
                failed_count += 1
                failed_task_ids.append(task_id)
                errors.append(f"{task_id}: {outcome.detail or 'deletion failed'}")

        if not interrupted and self._stop_requested():
            interrupted = True
            errors.append("cleanup interrupted by shutdown request")

        completed_at = _as_utc(self._clock())
        success = not interrupted and failed_count == 0
        if success:
            try:
                self._registry_service.set_last_tasks_cleanup_at(completed_at)
            except Exception as error:
                success = False
                failed_count += 1
                errors.append(f"maintenance state write failed: {error}")

        return TaskRetentionResult(
            enabled=True,
            attempted=attempted,
            candidate_count=candidate_count,
            deleted_count=deleted_count,
            already_satisfied_count=already_satisfied_count,
            failed_count=failed_count,
            started_at=started_at,
            completed_at=completed_at,
            success=success,
            failed_task_ids=tuple(failed_task_ids),
            errors=tuple(errors),
        )


class TaskRetentionWorker:
    """One interruptible maintenance worker owned by the active Service."""

    def __init__(
        self,
        *,
        maintenance: TaskRetentionMaintenance,
        registry_service: AnalyticalTaskRegistryService,
        stop_event: threading.Event | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._maintenance = maintenance
        self._registry_service = registry_service
        self._stop_event = stop_event or threading.Event()
        self._logger = logger or logging.getLogger(__name__)
        self._clock = clock
        self._thread = threading.Thread(
            target=self._run,
            name="jelica-task-retention",
            daemon=False,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self) -> None:
        self._thread.join()

    def _run(self) -> None:
        delay = self._initial_delay_seconds()
        while not self._stop_event.wait(delay):
            try:
                result = self._maintenance.run_once()
            except Exception as error:
                self._logger.warning(
                    "automatic task cleanup crashed; retry_in_seconds=%d error=%s",
                    int(TASK_RETENTION_FAILURE_RETRY_INTERVAL.total_seconds()),
                    error,
                )
                delay = TASK_RETENTION_FAILURE_RETRY_INTERVAL.total_seconds()
                continue
            if result.success:
                if result.candidate_count == 0:
                    self._logger.info("automatic task cleanup completed with zero candidates")
                else:
                    self._logger.info(
                        "automatic task cleanup completed candidates=%d deleted=%d",
                        result.candidate_count,
                        result.deleted_count,
                    )
                delay = TASK_RETENTION_MAINTENANCE_INTERVAL.total_seconds()
            else:
                self._logger.warning(
                    "automatic task cleanup failed failed=%d retry_in_seconds=%d errors=%s",
                    result.failed_count,
                    int(TASK_RETENTION_FAILURE_RETRY_INTERVAL.total_seconds()),
                    "; ".join(result.errors),
                )
                delay = TASK_RETENTION_FAILURE_RETRY_INTERVAL.total_seconds()

    def _initial_delay_seconds(self) -> float:
        try:
            last_success = self._registry_service.get_last_tasks_cleanup_at()
        except Exception as error:
            self._logger.warning(
                "cannot read last task cleanup timestamp; retrying maintenance in one hour: %s",
                error,
            )
            return TASK_RETENTION_FAILURE_RETRY_INTERVAL.total_seconds()
        if last_success is None:
            return 0.0
        elapsed = (_as_utc(self._clock()) - last_success).total_seconds()
        return max(0.0, TASK_RETENTION_MAINTENANCE_INTERVAL.total_seconds() - elapsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("retention clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


__all__ = [
    "TASK_RETENTION_FAILURE_RETRY_INTERVAL",
    "TASK_RETENTION_MAINTENANCE_INTERVAL",
    "RetentionDeletionCallback",
    "RetentionDeletionOutcome",
    "RetentionDeletionResult",
    "TaskRetentionMaintenance",
    "TaskRetentionResult",
    "TaskRetentionWorker",
]
