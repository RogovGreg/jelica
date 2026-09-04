from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from jelica_api.cli import (
    JelicaCliClient,
    JelicaCliCommandError,
    JelicaCliInvocationError,
    JelicaCliProtocolError,
)
from jelica_api.contracts import TaskSubmissionRequest, TaskSubmissionResult
from jelica_api.web_tasks import WebTaskProjectionStore


@dataclass(frozen=True, slots=True)
class TaskOrchestrator:
    cli_client: JelicaCliClient
    projection_store: WebTaskProjectionStore

    def shutdown(self) -> None:
        return None

    def submit_task(
        self,
        *,
        request: TaskSubmissionRequest,
        owner_user_id: str | None = None,
        guest_session_hash: str | None = None,
    ) -> TaskSubmissionResult:
        if owner_user_id is not None and guest_session_hash is not None:
            raise ValueError(
                "owner_user_id and guest_session_hash cannot both be assigned to a task"
            )
        request_with_trace = _with_trace_id(request=request)
        trace_id = request_with_trace.trace_id
        if trace_id is None:
            raise ValueError("TaskSubmissionRequest trace_id must be set before submission.")

        try:
            result = self.cli_client.create_and_start_task(
                request=request_with_trace,
                wait_for_completion=False,
            )
        except JelicaCliCommandError as error:
            self._sync_projection_for_trace_id(
                trace_id=trace_id,
                fallback_status=_fallback_status_for_command_error(error),
                task_name=request_with_trace.name,
                owner_user_id=owner_user_id,
                guest_session_hash=guest_session_hash,
            )
            raise
        except (JelicaCliInvocationError, JelicaCliProtocolError):
            self._sync_projection_for_trace_id(
                trace_id=trace_id,
                fallback_status="failed",
                task_name=request_with_trace.name,
                owner_user_id=owner_user_id,
                guest_session_hash=guest_session_hash,
            )
            raise

        self.projection_store.upsert_task(
            core_task_id=result.task_id,
            name=request_with_trace.name,
            status=result.final_state,
            owner_user_id=owner_user_id,
            guest_session_hash=guest_session_hash,
        )
        return result.model_copy(update={"trace_id": trace_id})

    def _sync_projection_for_trace_id(
        self,
        *,
        trace_id: str,
        fallback_status: str,
        task_name: str | None,
        owner_user_id: str | None,
        guest_session_hash: str | None,
    ) -> None:
        snapshot = self.cli_client.find_task_by_trace_id(
            trace_id=trace_id,
            require_active_job=False,
        )
        if snapshot is None:
            return

        normalized_snapshot_status = snapshot.state.strip()
        resolved_status = (
            normalized_snapshot_status if normalized_snapshot_status != "" else fallback_status
        )
        self.projection_store.upsert_task(
            core_task_id=snapshot.task_id,
            name=task_name,
            status=resolved_status,
            owner_user_id=owner_user_id,
            guest_session_hash=guest_session_hash,
        )


def _with_trace_id(*, request: TaskSubmissionRequest) -> TaskSubmissionRequest:
    if request.trace_id is not None:
        return request
    return request.model_copy(update={"trace_id": str(uuid4())})


def _fallback_status_for_command_error(error: JelicaCliCommandError) -> str:
    machine_error = error.envelope.error
    if machine_error is None:
        return "failed"
    if machine_error.name == "CLI_COMMAND_INTERRUPTED":
        return "interrupted"
    return "failed"


__all__ = ["TaskOrchestrator"]
