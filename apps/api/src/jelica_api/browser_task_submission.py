from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from jelica_api.actor_identity import WebActorIdentity
from jelica_api.analysis_uploads import (
    AnalysisUploadService,
    SubmissionReservation,
    UploadConflictError,
    UploadUnavailableError,
)
from jelica_api.contracts import (
    BrowserTaskSubmissionRequest,
    TaskSubmissionRequest,
    TaskSubmissionResult,
)
from jelica_api.contracts.analysis_overrides import cli_override_arguments
from jelica_api.task_orchestration import TaskOrchestrator

logger = logging.getLogger(__name__)
_SUBMISSION_RESERVATION_STALE_AFTER = timedelta(minutes=5)


class _RetryableSubmissionValidationError(UploadConflictError):
    """Internal marker for validation failures whose reservation is already released."""


@dataclass(frozen=True, slots=True)
class BrowserTaskSubmissionService:
    uploads: AnalysisUploadService
    orchestrator: TaskOrchestrator

    def submit(
        self, *, payload: BrowserTaskSubmissionRequest, actor: WebActorIdentity
    ) -> TaskSubmissionResult:
        trace_id = payload.trace_id or str(uuid4())
        reservation = self.uploads.reserve_submission(
            actor=actor, session_id=payload.upload_session_id, trace_id=trace_id
        )
        if reservation.status == "consumed":
            if reservation.task_id is None:
                raise UploadUnavailableError("Submitted task is unavailable.")
            return TaskSubmissionResult(
                task_id=reservation.task_id,
                final_state="submitted",
                trace_id=reservation.trace_id,
                command_id="recovered",
            )
        if reservation.status == "submitting":
            recovered = self._recover_safely(
                reservation=reservation, actor=actor, name=payload.name
            )
            if recovered is not None:
                return recovered
            if reservation.trace_id is not None and self.uploads.reset_stale_submission(
                actor=actor,
                session_id=reservation.session_id,
                trace_id=reservation.trace_id,
                stale_after=_SUBMISSION_RESERVATION_STALE_AFTER,
            ):
                logger.warning(
                    "Resetting stale browser task submission reservation",
                    extra={
                        "session_id": reservation.session_id,
                        "trace_id": reservation.trace_id,
                    },
                )
                return self.submit(payload=payload, actor=actor)
            raise UploadConflictError("This upload session already has a submission in progress.")

        try:
            session = self.uploads.get_session(actor=actor, session_id=payload.upload_session_id)
            local_items = [
                item for item in session.items if item.kind in {"input_file", "input_directory"}
            ]
            config_items = [item for item in session.items if item.kind == "config_file"]
            if not local_items and not payload.ncbi_sources:
                self.uploads.mark_submission_retryable(
                    actor=actor, session_id=session.id, trace_id=trace_id
                )
                raise _RetryableSubmissionValidationError(
                    "Add at least one uploaded input or NCBI source before submitting."
                )
            materialized = tuple(
                self.uploads.resolve_item(actor=actor, session_id=session.id, item_id=item.id)
                for item in local_items
            )
            config_path = None
            if config_items:
                config_path = str(
                    self.uploads.resolve_item(
                        actor=actor, session_id=session.id, item_id=config_items[0].id
                    ).path
                )
            internal = TaskSubmissionRequest(
                sources=tuple(str(item.path) for item in materialized) + payload.ncbi_sources,
                config_path=config_path,
                name=payload.name,
                trace_id=trace_id,
                overrides=cli_override_arguments(payload.analysis_overrides),
            )
            result = self.orchestrator.submit_task(
                request=internal,
                owner_user_id=actor.user_id,
                guest_session_hash=actor.guest_session_hash,
            )
            self.uploads.bind_submission(
                actor=actor, session_id=session.id, trace_id=trace_id, core_task_id=result.task_id
            )
            return result
        except _RetryableSubmissionValidationError:
            raise
        except Exception:
            logger.exception(
                "Browser task submission failed; attempting recovery",
                extra={"session_id": payload.upload_session_id, "trace_id": trace_id},
            )
            recovered = self._recover_safely(
                reservation=SubmissionReservation(
                    payload.upload_session_id, "submitting", trace_id, None
                ),
                actor=actor,
                name=payload.name,
            )
            if recovered is not None:
                return recovered
            self._mark_submission_retryable_safely(
                actor=actor, session_id=payload.upload_session_id, trace_id=trace_id
            )
            raise

    def _recover_safely(
        self, *, reservation: SubmissionReservation, actor: WebActorIdentity, name: str | None
    ) -> TaskSubmissionResult | None:
        try:
            return self._recover(reservation=reservation, actor=actor, name=name)
        except Exception:
            logger.exception(
                "Browser task submission recovery failed",
                extra={
                    "session_id": reservation.session_id,
                    "trace_id": reservation.trace_id,
                },
            )
            return None

    def _mark_submission_retryable_safely(
        self, *, actor: WebActorIdentity, session_id: str, trace_id: str
    ) -> None:
        try:
            self.uploads.mark_submission_retryable(
                actor=actor, session_id=session_id, trace_id=trace_id
            )
        except Exception:
            logger.exception(
                "Browser task submission reservation could not be released",
                extra={"session_id": session_id, "trace_id": trace_id},
            )

    def _recover(
        self, *, reservation: SubmissionReservation, actor: WebActorIdentity, name: str | None
    ) -> TaskSubmissionResult | None:
        if reservation.trace_id is None:
            return None
        snapshot = self.orchestrator.cli_client.find_task_by_trace_id(
            trace_id=reservation.trace_id, require_active_job=False
        )
        if snapshot is None:
            return None
        self.orchestrator.projection_store.upsert_task(
            core_task_id=snapshot.task_id,
            name=name,
            status=snapshot.state or "submitted",
            owner_user_id=actor.user_id,
            guest_session_hash=actor.guest_session_hash,
        )
        self.uploads.bind_submission(
            actor=actor,
            session_id=reservation.session_id,
            trace_id=reservation.trace_id,
            core_task_id=snapshot.task_id,
        )
        return TaskSubmissionResult(
            task_id=snapshot.task_id,
            final_state=snapshot.state or "submitted",
            trace_id=reservation.trace_id,
            command_id=snapshot.command_id or "recovered",
        )


__all__ = ["BrowserTaskSubmissionService"]
