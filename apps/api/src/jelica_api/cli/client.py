from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from pydantic import ValidationError

from jelica_api.contracts import (
    TaskResultPackageReference,
    TaskStatusSnapshot,
    TaskSubmissionRequest,
    TaskSubmissionResult,
)

from .models import (
    AnalyzeMachineDataPayload,
    MachineResponseEnvelope,
    ResultPathMachineDataPayload,
    TaskMachinePayload,
    TasksShowMachineDataPayload,
)


class JelicaCliClientError(RuntimeError):
    """Base error for Web backend -> CLI machine adapter failures."""


class JelicaCliInvocationError(JelicaCliClientError):
    """Raised when subprocess invocation itself fails."""


class JelicaCliProtocolError(JelicaCliClientError):
    """Raised when CLI output is not a valid machine envelope."""


class JelicaCliCommandError(JelicaCliClientError):
    """Raised when machine envelope reports ok=false."""

    def __init__(self, *, envelope: MachineResponseEnvelope) -> None:
        self.envelope = envelope
        if envelope.error is None:
            super().__init__("CLI machine command failed with unspecified error.")
            return
        super().__init__(
            f"CLI machine command failed: [{envelope.error.name}] {envelope.error.message}"
        )


@dataclass(frozen=True, slots=True)
class JelicaCliClient:
    command_prefix: tuple[str, ...]
    default_timeout_seconds: float

    def run_machine_command(
        self,
        *,
        args: Sequence[str],
        timeout_seconds: float | None = None,
    ) -> MachineResponseEnvelope:
        if len(args) == 0:
            raise JelicaCliInvocationError("CLI machine command arguments must not be empty.")
        command_args = tuple(args)
        if "--machine" not in command_args:
            command_args = (*command_args, "--machine")
        command = [*self.command_prefix, *command_args]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._resolve_timeout(timeout_seconds=timeout_seconds),
            )
        except FileNotFoundError as error:
            raise JelicaCliInvocationError(
                f"CLI executable was not found for command prefix: {self.command_prefix!r}"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise JelicaCliInvocationError(
                f"CLI machine command timed out after {error.timeout} seconds."
            ) from error
        except OSError as error:
            raise JelicaCliInvocationError(
                f"CLI machine command failed to start: {error}"
            ) from error

        envelope = _parse_machine_envelope(stdout=completed.stdout)
        if envelope.ok:
            return envelope
        raise JelicaCliCommandError(envelope=envelope)

    def create_and_start_task(
        self,
        *,
        request: TaskSubmissionRequest,
        wait_for_completion: bool = True,
        timeout_seconds: float | None = None,
    ) -> TaskSubmissionResult:
        args: list[str] = ["analyze"]
        if not wait_for_completion:
            args.append("--no-watch")
        if request.name is not None:
            args.extend(["--name", request.name])
        if request.trace_id is not None:
            args.extend(["--trace-id", request.trace_id])
        if request.config_path is not None:
            args.append(request.config_path)
        args.extend(request.overrides)
        args.extend(request.sources)

        envelope = self.run_machine_command(args=args, timeout_seconds=timeout_seconds)
        payload = _parse_machine_data(
            envelope=envelope,
            payload_type=AnalyzeMachineDataPayload,
            payload_name="analyze payload",
        )

        return TaskSubmissionResult(
            task_id=payload.task.task_id.strip(),
            final_state=payload.final_state.strip(),
            trace_id=envelope.trace_id,
            command_id=envelope.command_id,
        )

    def get_task_status(
        self,
        *,
        task_reference: str,
        timeout_seconds: float | None = None,
    ) -> TaskStatusSnapshot:
        envelope = self.run_machine_command(
            args=["tasks", "show", task_reference],
            timeout_seconds=timeout_seconds,
        )
        payload = _parse_machine_data(
            envelope=envelope,
            payload_type=TasksShowMachineDataPayload,
            payload_name="tasks show payload",
        )
        if len(payload.tasks) == 0:
            raise JelicaCliProtocolError("tasks show machine payload returned no task records.")

        return _task_machine_payload_to_status_snapshot(
            payload.tasks[0],
            command_id=envelope.command_id,
        )

    def start_task(
        self, *, task_id: str, timeout_seconds: float | None = None
    ) -> TaskStatusSnapshot:
        return self._run_lifecycle_action(
            action="start", task_id=task_id, timeout_seconds=timeout_seconds
        )

    def pause_task(
        self, *, task_id: str, timeout_seconds: float | None = None
    ) -> TaskStatusSnapshot:
        return self._run_lifecycle_action(
            action="pause", task_id=task_id, timeout_seconds=timeout_seconds
        )

    def resume_task(
        self, *, task_id: str, timeout_seconds: float | None = None
    ) -> TaskStatusSnapshot:
        return self._run_lifecycle_action(
            action="resume", task_id=task_id, timeout_seconds=timeout_seconds
        )

    def _run_lifecycle_action(
        self, *, action: str, task_id: str, timeout_seconds: float | None
    ) -> TaskStatusSnapshot:
        self.run_machine_command(args=["tasks", action, task_id], timeout_seconds=timeout_seconds)
        return self.get_task_status(task_reference=task_id, timeout_seconds=timeout_seconds)

    def list_tasks(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        timeout_seconds: float | None = None,
    ) -> tuple[TaskStatusSnapshot, ...]:
        if limit <= 0:
            raise JelicaCliInvocationError("limit must be > 0.")
        if offset < 0:
            raise JelicaCliInvocationError("offset must be >= 0.")
        envelope = self.run_machine_command(
            args=["tasks", "list", "--limit", str(limit), "--offset", str(offset)],
            timeout_seconds=timeout_seconds,
        )
        payload = _parse_machine_data(
            envelope=envelope,
            payload_type=TasksShowMachineDataPayload,
            payload_name="tasks list payload",
        )
        return tuple(
            _task_machine_payload_to_status_snapshot(
                task_payload,
                command_id=envelope.command_id,
            )
            for task_payload in payload.tasks
        )

    def find_task_by_trace_id(
        self,
        *,
        trace_id: str,
        require_active_job: bool,
        timeout_seconds: float | None = None,
        page_limit: int = 200,
    ) -> TaskStatusSnapshot | None:
        normalized_trace_id = trace_id.strip()
        if normalized_trace_id == "":
            raise JelicaCliInvocationError("trace_id must not be empty.")
        if page_limit <= 0:
            raise JelicaCliInvocationError("page_limit must be > 0.")

        offset = 0
        while True:
            page = self.list_tasks(
                limit=page_limit,
                offset=offset,
                timeout_seconds=timeout_seconds,
            )
            for task in page:
                if task.trace_id != normalized_trace_id:
                    continue
                if require_active_job and task.active_job_state is None:
                    continue
                return task

            if len(page) < page_limit:
                return None
            offset += page_limit

    def resolve_result_package_reference(
        self,
        *,
        task_reference: str,
        timeout_seconds: float | None = None,
    ) -> TaskResultPackageReference:
        envelope = self.run_machine_command(
            args=["results", "path", task_reference],
            timeout_seconds=timeout_seconds,
        )
        payload = _parse_machine_data(
            envelope=envelope,
            payload_type=ResultPathMachineDataPayload,
            payload_name="results path payload",
        )
        return TaskResultPackageReference(
            content_id=payload.content_id,
            package_path=payload.path,
            command_id=envelope.command_id,
        )

    def _resolve_timeout(self, *, timeout_seconds: float | None) -> float:
        if timeout_seconds is None:
            return self.default_timeout_seconds
        if timeout_seconds <= 0:
            raise JelicaCliInvocationError("timeout_seconds must be > 0.")
        return timeout_seconds


def _parse_machine_envelope(*, stdout: str) -> MachineResponseEnvelope:
    raw_line = _last_non_empty_line(stdout)
    if raw_line is None:
        raise JelicaCliProtocolError("CLI machine command produced no JSON payload.")
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError as error:
        raise JelicaCliProtocolError(
            f"CLI machine command returned malformed JSON payload: {error}"
        ) from error
    try:
        return MachineResponseEnvelope.model_validate(payload)
    except ValidationError as error:
        raise JelicaCliProtocolError(
            f"CLI machine command returned invalid envelope shape: {error}"
        ) from error


def _last_non_empty_line(raw_text: str) -> str | None:
    for line in reversed(raw_text.splitlines()):
        normalized = line.strip()
        if normalized != "":
            return normalized
    return None


_MachinePayloadT = TypeVar("_MachinePayloadT")


def _parse_machine_data(
    *,
    envelope: MachineResponseEnvelope,
    payload_type: type[_MachinePayloadT],
    payload_name: str,
) -> _MachinePayloadT:
    data = envelope.data
    if data is None:
        raise JelicaCliProtocolError("CLI success envelope has no data payload.")
    try:
        return payload_type.model_validate(data)
    except ValidationError as error:
        raise JelicaCliProtocolError(
            f"CLI success envelope has invalid {payload_name}: {error}"
        ) from error


def _task_machine_payload_to_status_snapshot(
    task_payload: TaskMachinePayload,
    *,
    command_id: str,
) -> TaskStatusSnapshot:
    active_job = task_payload.active_or_latest_job
    return TaskStatusSnapshot(
        task_id=task_payload.task_id,
        trace_id=task_payload.trace_id,
        state=task_payload.state,
        active_job_state=active_job.state if active_job is not None else None,
        current_stage=active_job.current_stage if active_job is not None else None,
        progress=active_job.progress if active_job is not None else None,
        command_id=command_id,
    )


__all__ = [
    "JelicaCliClient",
    "JelicaCliClientError",
    "JelicaCliCommandError",
    "JelicaCliInvocationError",
    "JelicaCliProtocolError",
]
