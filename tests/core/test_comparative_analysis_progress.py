from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from jelica_cli.terminal import TerminalMode, TerminalPresenter
from jelica_cli.watcher import _comparative_progress_stage
from jelica_contracts import Event, EventComponent, EventType
from jelica_core.comparative_analysis import COMPARATIVE_ANALYSIS_STAGE_ID
from jelica_core.runtime.comparative_analysis_stage import (
    COMPARATIVE_ANALYSIS_PROGRESS_EVENT,
    _Phase,
    _ProgressTracker,
)
from jelica_core.runtime.models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RuntimeStateCheckpoint,
    WorkerLaunchSpec,
)
from jelica_core.runtime.pipeline import StageContext


class _Reporter:
    def __init__(self) -> None:
        self.values: list[float] = []
        self.descriptions: list[str] = []

    def start(self, *, description: str, total: float | None = None) -> None:
        self.update(description=description, progress=0.0)

    def update(
        self,
        *,
        description: str | None = None,
        progress: float | None = None,
    ) -> None:
        if description is not None:
            self.descriptions.append(description)
        if progress is not None:
            self.values.append(progress)

    def complete(self, *, description: str | None = None) -> None:
        self.update(description=description, progress=1.0)

    def __call__(self, progress: float) -> None:
        self.update(progress=progress)


def _context(
    tmp_path: Path,
    *,
    events: list[tuple[str, dict[str, object]]],
) -> StageContext:
    task_dir = tmp_path / "task"
    job_dir = task_dir / "jobs" / "job-1"
    launch_spec = WorkerLaunchSpec(
        task_id="task-1",
        job_id="job-1",
        worker_instance_id="worker-1",
        lease_token="lease-1",
        database_path=tmp_path / "registry.sqlite3",
        task_dir=task_dir,
        job_dir=job_dir,
        config_revision_path=task_dir / "configs" / "000001.json",
        config_hash="0" * 64,
        runtime_state_json=RuntimeStateCheckpoint.new(
            pipeline_version=DEFAULT_PIPELINE_VERSION
        ).to_runtime_state_json(),
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
    )
    return StageContext(
        launch_spec=launch_spec,
        stage_index=4,
        stage_staging_directory=(
            job_dir / "staging" / COMPARATIVE_ANALYSIS_STAGE_ID / "worker-1"
        ),
        event_reporter=lambda name, payload: events.append((name, payload)),
    )


def test_large_operation_progress_is_aggregated_and_final_payload_is_complete(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    reporter = _Reporter()
    tracker = _ProgressTracker(
        context=_context(tmp_path, events=events),
        reporter=reporter,
        phases=(
            _Phase("Preparation", "preparation", 1),
            _Phase("Pairwise comparisons", "pairwise_comparison", 100),
            _Phase("Publication", "publication", 1),
        ),
    )
    tracker.begin(0)
    tracker.advance(successful=1)
    tracker.begin(1)
    for index in range(100):
        tracker.advance(successful=int(index % 7 != 0), failed=int(index % 7 == 0))

    progress_events = [
        payload for name, payload in events if name == COMPARATIVE_ANALYSIS_PROGRESS_EVENT
    ]
    pairwise_events = [
        payload
        for payload in progress_events
        if payload["operation_kind"] == "pairwise_comparison"
    ]
    final = pairwise_events[-1]
    assert len(pairwise_events) <= 21
    assert final == {
        "phase_index": 2,
        "phase_total": 3,
        "operation_kind": "pairwise_comparison",
        "completed": 100,
        "total": 100,
        "successful": 85,
        "failed": 15,
        "detail": "Pairwise comparisons: 100/100; successful: 85, failed: 15",
    }
    assert reporter.values == sorted(reporter.values)


def test_progress_payload_contains_only_aggregated_safe_fields(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    tracker = _ProgressTracker(
        context=_context(tmp_path, events=events),
        reporter=_Reporter(),
        phases=(_Phase("Statistical metrics", "statistics_metric", 2),),
    )
    tracker.begin(0)
    tracker.advance(successful=1)
    tracker.advance(failed=1)

    payloads = [
        payload for name, payload in events if name == COMPARATIVE_ANALYSIS_PROGRESS_EVENT
    ]
    allowed = {
        "phase_index",
        "phase_total",
        "operation_kind",
        "completed",
        "total",
        "successful",
        "failed",
        "detail",
    }
    assert payloads
    assert all(set(payload) == allowed for payload in payloads)


def test_standard_terminal_accumulates_local_failures_but_verbose_shows_them() -> None:
    local_failure = Event(
        code=2334,
        name="CORE_COMPARATIVE_ANALYSIS_OPERATION_FAILED",
        type=EventType.ERROR,
        title="Comparative-analysis operation failed",
        message="A local comparative operation failed.",
        component=EventComponent.CORE,
        task_id="task-1",
    )
    partial = Event(
        code=2337,
        name="CORE_COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS",
        type=EventType.WARNING,
        title="Comparative analysis partially completed",
        message="Comparative analysis completed with partial analytical errors.",
        component=EventComponent.CORE,
        task_id="task-1",
    )
    standard_output = StringIO()
    standard = TerminalPresenter(
        console=Console(file=standard_output, color_system=None, force_terminal=False)
    )
    standard.event(local_failure, mode=TerminalMode.STANDARD)
    standard.event(partial, mode=TerminalMode.STANDARD)
    verbose_output = StringIO()
    verbose = TerminalPresenter(
        console=Console(file=verbose_output, color_system=None, force_terminal=False)
    )
    verbose.event(local_failure, mode=TerminalMode.VERBOSE)

    assert "local comparative operation" not in standard_output.getvalue()
    assert "partial analytical errors" in standard_output.getvalue()
    assert "local comparative operation" in verbose_output.getvalue()


def test_tty_watch_stage_uses_aggregated_comparative_progress() -> None:
    progress_event = Event(
        code=2333,
        name="CORE_COMPARATIVE_ANALYSIS_PROGRESS",
        type=EventType.INFO,
        title="Comparative-analysis progress",
        message="Aggregated progress.",
        component=EventComponent.CORE,
        task_id="task-1",
        context={
            "operation_kind": "pairwise_comparison",
            "completed": 37,
            "total": 80,
            "successful": 35,
            "failed": 2,
        },
    )

    assert _comparative_progress_stage((progress_event,)) == (
        "comparative_analysis · pairwise 37/80"
    )
