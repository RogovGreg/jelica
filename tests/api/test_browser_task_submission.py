from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jelica_api.actor_identity import WebActorIdentity
from jelica_api.analysis_uploads import SubmissionReservation
from jelica_api.browser_task_submission import BrowserTaskSubmissionService
from jelica_api.contracts import BrowserTaskSubmissionRequest


class _Uploads:
    def __init__(self) -> None:
        self.marked_retryable: list[tuple[str, str]] = []

    def reserve_submission(
        self, *, actor: WebActorIdentity, session_id: str, trace_id: str
    ) -> SubmissionReservation:
        _ = actor
        return SubmissionReservation(session_id, "open", trace_id, None)

    def get_session(self, *, actor: WebActorIdentity, session_id: str) -> SimpleNamespace:
        _ = actor
        return SimpleNamespace(
            id=session_id,
            items=(SimpleNamespace(id="item-1", kind="input_file"),),
        )

    def resolve_item(
        self, *, actor: WebActorIdentity, session_id: str, item_id: str
    ) -> SimpleNamespace:
        _ = (actor, session_id, item_id)
        return SimpleNamespace(path=Path("/tmp/input.fasta"))

    def mark_submission_retryable(
        self, *, actor: WebActorIdentity, session_id: str, trace_id: str
    ) -> None:
        _ = actor
        self.marked_retryable.append((session_id, trace_id))


class _Cli:
    def find_task_by_trace_id(self, *, trace_id: str, require_active_job: bool) -> None:
        _ = (trace_id, require_active_job)
        return None


class _Orchestrator:
    cli_client = _Cli()
    projection_store = SimpleNamespace()

    def submit_task(self, **kwargs: object) -> None:
        _ = kwargs
        raise RuntimeError("unexpected submission failure")


def test_unexpected_submission_failure_releases_reservation() -> None:
    uploads = _Uploads()
    service = BrowserTaskSubmissionService(uploads=uploads, orchestrator=_Orchestrator())
    payload = BrowserTaskSubmissionRequest(upload_session_id="session-1", trace_id="trace-1")

    with pytest.raises(RuntimeError, match="unexpected submission failure"):
        service.submit(payload=payload, actor=WebActorIdentity(user_id="user-1"))

    assert uploads.marked_retryable == [("session-1", "trace-1")]
