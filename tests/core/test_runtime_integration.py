from __future__ import annotations

import multiprocessing as mp
import os
import signal
import threading
import time
from multiprocessing.process import BaseProcess
from pathlib import Path
from queue import Empty
from typing import Any
from uuid import uuid4

import pytest

from jelica_core.config import AnalysisConfigInput, resolve_analysis_config
from jelica_core.events import (
    run_cancel_analytical_task,
    run_delete_analytical_tasks,
    run_pause_analytical_task,
    run_runtime_continue,
)
from jelica_core.events.definitions import CORE_RUNTIME_LEASE_CONFLICT
from jelica_core.runtime import (
    DEFAULT_PIPELINE_VERSION,
    RUNTIME_EVENT_JOB_CLAIMED,
    RUNTIME_EVENT_JOB_COMPLETED,
    RUNTIME_EVENT_PREEMPTED_JOB_RETURNED_TO_WAITING,
    RUNTIME_EVENT_PREEMPTION_REQUESTED,
    RUNTIME_EVENT_PREEMPTION_SELECTED,
    RUNTIME_EVENT_SCHEDULER_STARTED,
    RUNTIME_EVENT_STAGE_STARTED,
    RUNTIME_EVENT_STALE_MESSAGE_REJECTED,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_CANCEL,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_DELETION,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PAUSE,
    RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PREEMPTION,
    RUNTIME_EVENT_WORKER_STARTED,
    ExecutionRuntime,
    RuntimeConfig,
    RuntimeContinueResult,
    RuntimeShutdownMode,
    WorkerPipelineControl,
)
from jelica_core.runtime.messages import ProgressUpdatedMessage
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import (
    AnalyticalTaskJobNotFoundError,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryService,
    AnalyticalTaskState,
    LocalTaskStorage,
)

_PIPELINE_NAME = "test_controlled_multi_stage"
_WAIT_TIMEOUT_SECONDS = 15.0
_PROCESS_JOIN_TIMEOUT_SECONDS = 20.0
_QUEUE_RESULT_TIMEOUT_SECONDS = 20.0


@pytest.fixture(autouse=True)
def _stable_available_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jelica_core.system_config.resolver.detect_available_logical_cpu_count",
        lambda: 8,
    )


def _initialize_core(jelica_home: Path) -> CoreConfigService:
    service = CoreConfigService(jelica_home=jelica_home)
    service.initialize_system_config(force=True)
    return service


def _write_sample(path: Path, *, sample_id: str) -> None:
    path.write_text(f">{sample_id}\nACGT\n", encoding="utf-8")


def _create_task(
    *,
    service: CoreConfigService,
    task_id: str,
    sample_path: Path,
    priority: int = 1,
) -> None:
    resolved = service.load_resolved_config()
    config = resolve_analysis_config(
        AnalysisConfigInput(samples=[str(sample_path)], priority=priority)
    ).config
    storage = LocalTaskStorage(tasks_dir=resolved.tasks_dir)
    workspace = storage.create_task_workspace(task_id=task_id, config=config)
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    registry.register_task(
        task_id=task_id,
        task_dir_relative_path=task_id,
        default_priority=priority,
        current_config_revision=workspace.current_config_revision,
        current_config_relative_path=workspace.current_config_relative_path,
        current_config_hash=workspace.current_config_hash,
    )


def _queue_job(
    *,
    registry: AnalyticalTaskRegistryService,
    task_id: str,
    priority: int | None = None,
) -> str:
    mutation = registry.start(task_id=task_id, priority=priority)
    assert mutation.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert mutation.job is not None
    return mutation.job.job_id


def _stop_process(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
        process.join(timeout=_PROCESS_JOIN_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(timeout=_PROCESS_JOIN_TIMEOUT_SECONDS)


def _await_runtime_process_result(
    *,
    process: BaseProcess,
    result_queue: Any,
) -> dict[str, Any]:
    process.join(timeout=_PROCESS_JOIN_TIMEOUT_SECONDS)
    if process.is_alive():
        raise AssertionError("runtime process did not exit within timeout")
    try:
        payload = result_queue.get(timeout=_QUEUE_RESULT_TIMEOUT_SECONDS)
    except Empty as error:
        raise AssertionError("runtime process did not return a result payload") from error
    if not isinstance(payload, dict):
        raise AssertionError("runtime process returned an invalid payload")
    return payload


def _drain_runtime_events(event_queue: Any) -> list[tuple[str, dict[str, Any] | None]]:
    events: list[tuple[str, dict[str, Any] | None]] = []
    while True:
        try:
            raw_event = event_queue.get_nowait()
        except Empty:
            break
        if not isinstance(raw_event, tuple) or len(raw_event) != 2:
            continue
        event_name_raw, context_raw = raw_event
        if not isinstance(event_name_raw, str):
            continue
        if context_raw is not None and not isinstance(context_raw, dict):
            continue
        event_name = event_name_raw
        context = context_raw if isinstance(context_raw, dict) else None
        events.append((event_name, context))
    return events


def _wait_until(
    predicate: Any,
    *,
    timeout_seconds: float = _WAIT_TIMEOUT_SECONDS,
    interval_seconds: float = 0.05,
    description: str,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if bool(predicate()):
            return
        time.sleep(interval_seconds)
    raise AssertionError(f"timed out waiting for {description}")


def _wait_for_job_state(
    *,
    registry: AnalyticalTaskRegistryService,
    job_id: str,
    state: AnalyticalTaskState,
    timeout_seconds: float = _WAIT_TIMEOUT_SECONDS,
) -> None:
    _wait_until(
        lambda: registry.get_job(job_id=job_id).state is state,
        timeout_seconds=timeout_seconds,
        description=f"job {job_id} to reach {state.value}",
    )


def _wait_for_started_job_id(
    *,
    started_queue: Any,
    description: str,
    timeout_seconds: float = _WAIT_TIMEOUT_SECONDS,
) -> str:
    try:
        started_job_id = started_queue.get(timeout=timeout_seconds)
    except Empty as error:
        raise AssertionError(f"timed out waiting for {description}") from error
    if not isinstance(started_job_id, str) or started_job_id.strip() == "":
        raise AssertionError("received invalid started-job notification")
    return started_job_id


def _assert_job_state_not_reached(
    *,
    registry: AnalyticalTaskRegistryService,
    job_id: str,
    state: AnalyticalTaskState,
    duration_seconds: float,
    interval_seconds: float = 0.05,
) -> None:
    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        if registry.get_job(job_id=job_id).state is state:
            raise AssertionError(f"job {job_id} unexpectedly reached {state.value}")
        time.sleep(interval_seconds)


def _job_deleted_or_in_state(
    *,
    registry: AnalyticalTaskRegistryService,
    job_id: str,
    state: AnalyticalTaskState,
) -> bool:
    try:
        return registry.get_job(job_id=job_id).state is state
    except AnalyticalTaskJobNotFoundError:
        return True


def _runtime_process_entry(
    *,
    database_path: str,
    tasks_dir: str,
    runtime_config: RuntimeConfig,
    runtime_instance_id: str,
    runtime_lease_token: str,
    pipeline_name: str,
    pipeline_version: str,
    auto_queue_waiting_jobs: bool,
    pipeline_control: WorkerPipelineControl | None,
    acquire_lease: bool,
    event_queue: Any,
    result_queue: Any,
) -> None:
    try:
        registry = AnalyticalTaskRegistryService(database_path=Path(database_path))
        if acquire_lease:
            acquired, _ = registry.acquire_execution_runtime_lease(
                runtime_instance_id=runtime_instance_id,
                owner_pid=os.getpid(),
                lease_token=runtime_lease_token,
                lease_timeout_seconds=runtime_config.lease_timeout_seconds,
            )
            if acquired is None:
                result_queue.put(
                    {
                        "ok": False,
                        "error_type": "LeaseConflict",
                        "error": "runtime lease conflict",
                    }
                )
                return

        def _event_callback(event_name: str, context: dict[str, Any] | None) -> None:
            event_queue.put((event_name, context))

        runtime = ExecutionRuntime(
            registry_service=registry,
            tasks_dir=Path(tasks_dir),
            runtime_config=runtime_config,
            runtime_instance_id=runtime_instance_id,
            runtime_lease_token=runtime_lease_token,
            pipeline_name=pipeline_name,
            pipeline_version=pipeline_version,
            pipeline_control=pipeline_control,
            event_callback=_event_callback,
        )
        runtime_result = runtime.run(auto_queue_waiting_jobs=auto_queue_waiting_jobs)
        result_queue.put({"ok": True, "result": runtime_result.model_dump(mode="json")})
    except Exception as error:  # pragma: no cover - defensive transfer from subprocess
        result_queue.put(
            {
                "ok": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )


def test_runtime_graceful_stop_and_continue_resumes_from_first_unconfirmed_stage(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="sample")
    task_id = "task-stop-and-continue"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)

    runtime_instance_id = f"runtime-{uuid4()}"
    runtime_lease_token = str(uuid4())
    acquired, conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id=runtime_instance_id,
        owner_pid=os.getpid(),
        lease_token=runtime_lease_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None
    assert conflict is None

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    pipeline_control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    interrupted_after_stage_start = threading.Event()
    runtime_events: list[tuple[str, dict[str, Any] | None]] = []

    def _event_callback(event_name: str, context: dict[str, Any] | None) -> None:
        runtime_events.append((event_name, context))

    runtime = ExecutionRuntime(
        registry_service=registry,
        tasks_dir=resolved.tasks_dir,
        runtime_config=RuntimeConfig.from_resolved_config(resolved),
        runtime_instance_id=runtime_instance_id,
        runtime_lease_token=runtime_lease_token,
        pipeline_name=_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        pipeline_control=pipeline_control,
        event_callback=_event_callback,
    )

    def _interrupt_runtime_after_stage_start() -> None:
        if not stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS):
            return
        interrupted_after_stage_start.set()
        runtime._handle_sigint(signal.SIGINT, None)
        stage_release_event.set()

    interrupter = threading.Thread(target=_interrupt_runtime_after_stage_start, daemon=True)
    interrupter.start()
    try:
        first_run = runtime.run(auto_queue_waiting_jobs=False)
    finally:
        stage_release_event.set()
        interrupter.join(timeout=_WAIT_TIMEOUT_SECONDS)

    assert interrupted_after_stage_start.is_set()
    assert first_run.interrupted is True

    waiting_snapshot = registry.get_task_snapshot(task_id=task_id)
    assert waiting_snapshot.task.state is AnalyticalTaskState.WAITING
    assert waiting_snapshot.task.active_job_id == job_id
    waiting_job = registry.get_job(job_id=job_id)
    assert waiting_job.state is AnalyticalTaskState.WAITING
    assert waiting_job.worker_instance_id is None
    assert waiting_job.worker_pid is None
    assert waiting_job.lease_token is None

    job_dir = resolved.tasks_dir / task_id / "jobs" / job_id
    staging_dir = job_dir / "staging"
    if staging_dir.exists():
        assert list(staging_dir.iterdir()) == []
    assert (job_dir / "stages" / "initialize_job" / "stage_manifest.json").is_file()
    assert (job_dir / "stages" / "controlled_slow" / "stage_manifest.json").is_file()
    assert not (job_dir / "stages" / "finalize").exists()

    continue_instance_id = f"runtime-{uuid4()}"
    continue_lease_token = str(uuid4())
    second_acquired, second_conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id=continue_instance_id,
        owner_pid=os.getpid(),
        lease_token=continue_lease_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert second_acquired is not None
    assert second_conflict is None

    continue_stage_started_event = spawn_context.Event()
    continue_stage_release_event = spawn_context.Event()
    continue_stage_release_event.set()
    continue_control = WorkerPipelineControl(
        stage_started_event=continue_stage_started_event,
        stage_release_event=continue_stage_release_event,
    )
    continue_events: list[tuple[str, dict[str, Any] | None]] = []

    def _continue_event_callback(event_name: str, context: dict[str, Any] | None) -> None:
        continue_events.append((event_name, context))

    continue_runtime = ExecutionRuntime(
        registry_service=registry,
        tasks_dir=resolved.tasks_dir,
        runtime_config=RuntimeConfig.from_resolved_config(resolved),
        runtime_instance_id=continue_instance_id,
        runtime_lease_token=continue_lease_token,
        pipeline_name=_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        pipeline_control=continue_control,
        event_callback=_continue_event_callback,
    )
    second_run = continue_runtime.run(auto_queue_waiting_jobs=True)

    assert second_run.completed_jobs == 1
    assert continue_stage_started_event.is_set() is False

    completed_snapshot = registry.get_task_snapshot(task_id=task_id)
    assert completed_snapshot.task.state is AnalyticalTaskState.COMPLETED
    completed_job = registry.get_job(job_id=job_id)
    assert completed_job.state is AnalyticalTaskState.COMPLETED
    assert completed_job.recovery_count == 0

    stage_started_ids = [
        str(context["stage_id"])
        for event_name, context in continue_events
        if event_name == RUNTIME_EVENT_STAGE_STARTED and context is not None
    ]
    assert stage_started_ids == ["finalize"]
    assert (job_dir / "stages" / "finalize" / "stage_manifest.json").is_file()

    first_run_stage_started_ids = [
        str(context["stage_id"])
        for event_name, context in runtime_events
        if event_name == RUNTIME_EVENT_STAGE_STARTED and context is not None
    ]
    assert first_run_stage_started_ids == ["initialize_job", "controlled_slow"]


def test_persistent_runtime_stays_alive_after_drain_and_claims_later_job(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    runtime_instance_id = f"runtime-{uuid4()}"
    runtime_lease_token = str(uuid4())
    acquired, conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id=runtime_instance_id,
        owner_pid=os.getpid(),
        lease_token=runtime_lease_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None
    assert conflict is None

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    stage_release_event.set()
    shutdown_requested = threading.Event()
    scheduler_started = threading.Event()
    runtime_results: list[RuntimeContinueResult] = []
    runtime_errors: list[BaseException] = []

    def _event_callback(event_name: str, _context: dict[str, Any] | None) -> None:
        if event_name == RUNTIME_EVENT_SCHEDULER_STARTED:
            scheduler_started.set()

    runtime = ExecutionRuntime(
        registry_service=registry,
        tasks_dir=resolved.tasks_dir,
        runtime_config=RuntimeConfig.from_resolved_config(resolved),
        runtime_instance_id=runtime_instance_id,
        runtime_lease_token=runtime_lease_token,
        pipeline_name=_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        pipeline_control=WorkerPipelineControl(
            stage_started_event=stage_started_event,
            stage_release_event=stage_release_event,
        ),
        event_callback=_event_callback,
        shutdown_poll=lambda: RuntimeShutdownMode.GRACEFUL if shutdown_requested.is_set() else None,
    )

    def _run_runtime() -> None:
        try:
            runtime_results.append(runtime.run(auto_queue_waiting_jobs=False, persistent=True))
        except BaseException as error:  # pragma: no cover - defensive thread transfer
            runtime_errors.append(error)

    runtime_thread = threading.Thread(target=_run_runtime, daemon=True)
    runtime_thread.start()
    try:
        assert scheduler_started.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        runtime_thread.join(timeout=0.2)
        assert runtime_thread.is_alive()

        sample = tmp_path / "later-sample.fasta"
        _write_sample(sample, sample_id="later")
        task_id = "task-queued-after-drain"
        _create_task(service=service, task_id=task_id, sample_path=sample)
        job_id = _queue_job(registry=registry, task_id=task_id)

        _wait_for_job_state(
            registry=registry,
            job_id=job_id,
            state=AnalyticalTaskState.COMPLETED,
        )
        runtime_thread.join(timeout=0.2)
        assert runtime_thread.is_alive()
    finally:
        shutdown_requested.set()
        stage_release_event.set()
        runtime_thread.join(timeout=_WAIT_TIMEOUT_SECONDS)

    assert runtime_thread.is_alive() is False
    assert runtime_errors == []
    assert len(runtime_results) == 1
    assert runtime_results[0].claimed_jobs == 1
    assert runtime_results[0].completed_jobs == 1
    assert runtime_results[0].interrupted is True
    assert registry.get_execution_runtime_lease() is None


def test_shutdown_poll_exits_with_queued_job_untouched(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "queued-sample.fasta"
    _write_sample(sample, sample_id="queued")
    task_id = "task-queued-during-shutdown"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)

    runtime_instance_id = f"runtime-{uuid4()}"
    runtime_lease_token = str(uuid4())
    acquired, conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id=runtime_instance_id,
        owner_pid=os.getpid(),
        lease_token=runtime_lease_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None
    assert conflict is None

    runtime = ExecutionRuntime(
        registry_service=registry,
        tasks_dir=resolved.tasks_dir,
        runtime_config=RuntimeConfig.from_resolved_config(resolved),
        runtime_instance_id=runtime_instance_id,
        runtime_lease_token=runtime_lease_token,
        shutdown_poll=lambda: RuntimeShutdownMode.GRACEFUL,
    )
    result = runtime.run(auto_queue_waiting_jobs=False, persistent=True)

    assert result.claimed_jobs == 0
    assert result.interrupted is True
    assert registry.get_job(job_id=job_id).state is AnalyticalTaskState.QUEUED
    assert registry.get_execution_runtime_lease() is None


def test_second_runtime_reports_lease_conflict_without_starting_second_scheduler(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="single-runtime")
    task_id = "task-single-runtime"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )

    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)

        conflict_result = run_runtime_continue(
            core_config_service=service,
            pipeline_name=_PIPELINE_NAME,
            pipeline_version=DEFAULT_PIPELINE_VERSION,
        )
        assert conflict_result.ok is False
        assert conflict_result.error is not None
        assert conflict_result.error.event.code == CORE_RUNTIME_LEASE_CONFLICT.code
        assert conflict_result.error.event.diagnostics is None

        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert payload["ok"] is True
    runtime_result = RuntimeContinueResult.model_validate(payload["result"])
    assert runtime_result.claimed_jobs == 1
    assert runtime_result.completed_jobs == 1
    assert runtime_result.failed_jobs == 0

    events = _drain_runtime_events(event_queue)
    assert sum(1 for event_name, _ in events if event_name == RUNTIME_EVENT_JOB_CLAIMED) == 1
    assert sum(1 for event_name, _ in events if event_name == RUNTIME_EVENT_WORKER_STARTED) == 1

    stage_started_ids = [
        str(context["stage_id"])
        for event_name, context in events
        if event_name == RUNTIME_EVENT_STAGE_STARTED and context is not None
    ]
    assert stage_started_ids == ["initialize_job", "controlled_slow", "finalize"]

    snapshot = registry.get_task_snapshot(task_id=task_id)
    assert snapshot.task.state is AnalyticalTaskState.COMPLETED
    assert snapshot.task.latest_job_id == job_id

    stages_root = resolved.tasks_dir / task_id / "jobs" / job_id / "stages"
    committed_stage_ids = sorted(path.name for path in stages_root.iterdir() if path.is_dir())
    assert committed_stage_ids == ["controlled_slow", "finalize", "initialize_job"]


def test_scheduler_respects_max_parallel_tasks_and_priority_ordering(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    service.set_parameter(parameter="execution.max_parallel_tasks", value="1")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    sample_high = tmp_path / "high.fasta"
    sample_mid_old = tmp_path / "mid-old.fasta"
    sample_mid_new = tmp_path / "mid-new.fasta"
    _write_sample(sample_high, sample_id="high")
    _write_sample(sample_mid_old, sample_id="mid-old")
    _write_sample(sample_mid_new, sample_id="mid-new")

    task_high = "task-high-priority"
    task_mid_old = "task-mid-priority-old"
    task_mid_new = "task-mid-priority-new"
    _create_task(service=service, task_id=task_mid_old, sample_path=sample_mid_old)
    _create_task(service=service, task_id=task_high, sample_path=sample_high)
    _create_task(service=service, task_id=task_mid_new, sample_path=sample_mid_new)

    job_mid_old = _queue_job(registry=registry, task_id=task_mid_old, priority=4)
    job_high = _queue_job(registry=registry, task_id=task_high, priority=9)
    job_mid_new = _queue_job(registry=registry, task_id=task_mid_new, priority=4)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )

    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)

        running_snapshots = registry.list_task_snapshots(
            states=(AnalyticalTaskState.RUNNING,),
            limit=None,
        )
        queued_snapshots = registry.list_task_snapshots(
            states=(AnalyticalTaskState.QUEUED,),
            limit=None,
        )
        assert len(running_snapshots) == 1
        assert len(queued_snapshots) == 2
        assert {snapshot.task.task_id for snapshot in running_snapshots} == {task_high}
        assert {snapshot.task.task_id for snapshot in queued_snapshots} == {
            task_mid_old,
            task_mid_new,
        }

        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert payload["ok"] is True
    runtime_result = RuntimeContinueResult.model_validate(payload["result"])
    assert runtime_result.claimed_jobs == 3
    assert runtime_result.completed_jobs == 3
    assert runtime_result.failed_jobs == 0

    events = _drain_runtime_events(event_queue)
    claim_events = [
        (index, context)
        for index, (event_name, context) in enumerate(events)
        if event_name == RUNTIME_EVENT_JOB_CLAIMED and context is not None
    ]
    claim_order = [str(context["job_id"]) for _, context in claim_events]
    assert claim_order == [job_high, job_mid_old, job_mid_new]

    completed_indices = {
        str(context["job_id"]): index
        for index, (event_name, context) in enumerate(events)
        if event_name == RUNTIME_EVENT_JOB_COMPLETED and context is not None
    }
    assert claim_events[1][0] > completed_indices[job_high]
    assert claim_events[2][0] > completed_indices[job_mid_old]

    assert registry.get_job(job_id=job_high).state is AnalyticalTaskState.COMPLETED
    assert registry.get_job(job_id=job_mid_old).state is AnalyticalTaskState.COMPLETED
    assert registry.get_job(job_id=job_mid_new).state is AnalyticalTaskState.COMPLETED


def test_scheduler_uses_reprioritized_priority_for_next_claim(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    sample_a = tmp_path / "a.fasta"
    sample_b = tmp_path / "b.fasta"
    _write_sample(sample_a, sample_id="a")
    _write_sample(sample_b, sample_id="b")

    task_a = "task-a"
    task_b = "task-b"
    _create_task(service=service, task_id=task_a, sample_path=sample_a)
    _create_task(service=service, task_id=task_b, sample_path=sample_b)

    job_a = _queue_job(registry=registry, task_id=task_a, priority=2)
    job_b = _queue_job(registry=registry, task_id=task_b, priority=4)

    reprioritized = registry.reprioritize_active_job(task_id=task_a, priority=6)
    assert reprioritized.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert reprioritized.job is not None
    assert reprioritized.job.job_id == job_a

    spawn_context = mp.get_context("spawn")
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": None,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )
    process.start()
    try:
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        _stop_process(process)

    assert payload["ok"] is True
    events = _drain_runtime_events(event_queue)
    claim_order = [
        str(context["job_id"])
        for event_name, context in events
        if event_name == RUNTIME_EVENT_JOB_CLAIMED and context is not None
    ]
    assert claim_order[:2] == [job_a, job_b]


def test_pause_transitions_queued_job_to_paused_without_worker(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="queued-pause")
    task_id = "task-queued-pause"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    _queue_job(registry=registry, task_id=task_id)

    pause_result = run_pause_analytical_task(task_id=task_id, core_config_service=service)

    assert pause_result.ok is True
    assert pause_result.value is not None
    assert pause_result.value.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert pause_result.value.job is not None
    assert pause_result.value.job.state is AnalyticalTaskState.PAUSED
    assert registry.get_execution_runtime_lease() is None


def test_running_job_pause_request_stops_worker_and_preserves_committed_stages(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="running-pause")
    task_id = "task-running-pause"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )
    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        pause_result = run_pause_analytical_task(task_id=task_id, core_config_service=service)
        assert pause_result.ok is True
        assert pause_result.value is not None
        assert pause_result.value.job is not None
        assert pause_result.value.job.state is AnalyticalTaskState.PAUSE_REQUESTED

        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert payload["ok"] is True
    paused_job = registry.get_job(job_id=job_id)
    assert paused_job.state is AnalyticalTaskState.PAUSED
    assert paused_job.worker_instance_id is None
    assert paused_job.worker_pid is None
    assert paused_job.lease_token is None
    job_dir = resolved.tasks_dir / task_id / "jobs" / job_id
    assert (job_dir / "stages" / "initialize_job" / "stage_manifest.json").is_file()
    assert (job_dir / "stages" / "controlled_slow" / "stage_manifest.json").is_file()
    assert not (job_dir / "stages" / "finalize").exists()

    runtime_events = _drain_runtime_events(event_queue)
    assert (
        sum(
            1
            for event_name, _ in runtime_events
            if event_name == RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PAUSE
        )
        == 1
    )


def test_resume_reuses_same_job_and_skips_committed_stages(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="resume")
    task_id = "task-resume"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )
    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        pause_result = run_pause_analytical_task(task_id=task_id, core_config_service=service)
        assert pause_result.ok is True
        stage_release_event.set()
        _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    paused_job = registry.get_job(job_id=job_id)
    assert paused_job.state is AnalyticalTaskState.PAUSED
    resume_transition = registry.resume(task_id=task_id)
    assert resume_transition.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert resume_transition.job is not None
    assert resume_transition.job.job_id == job_id
    assert resume_transition.job.state is AnalyticalTaskState.QUEUED

    runtime_instance_id = f"runtime-{uuid4()}"
    runtime_lease_token = str(uuid4())
    acquired, conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id=runtime_instance_id,
        owner_pid=os.getpid(),
        lease_token=runtime_lease_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None
    assert conflict is None

    resume_control = WorkerPipelineControl(
        stage_started_event=spawn_context.Event(),
        stage_release_event=spawn_context.Event(),
    )
    assert resume_control.stage_release_event is not None
    resume_control.stage_release_event.set()
    resume_events: list[tuple[str, dict[str, Any] | None]] = []

    def _resume_event_callback(event_name: str, context: dict[str, Any] | None) -> None:
        resume_events.append((event_name, context))

    runtime = ExecutionRuntime(
        registry_service=registry,
        tasks_dir=resolved.tasks_dir,
        runtime_config=RuntimeConfig.from_resolved_config(resolved),
        runtime_instance_id=runtime_instance_id,
        runtime_lease_token=runtime_lease_token,
        pipeline_name=_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        pipeline_control=resume_control,
        event_callback=_resume_event_callback,
    )
    resume_run_result = runtime.run(auto_queue_waiting_jobs=False)

    assert resume_run_result.completed_jobs == 1
    assert resume_control.stage_started_event is not None
    assert resume_control.stage_started_event.is_set() is False
    resumed_job = registry.get_job(job_id=job_id)
    assert resumed_job.state is AnalyticalTaskState.COMPLETED
    stage_ids = [
        str(context["stage_id"])
        for event_name, context in resume_events
        if event_name == RUNTIME_EVENT_STAGE_STARTED and context is not None
    ]
    assert stage_ids == ["finalize"]


def test_cancel_transitions_queued_and_paused_jobs_to_cancelled_immediately(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    queued_sample = tmp_path / "queued.fasta"
    _write_sample(queued_sample, sample_id="queued")
    queued_task = "task-cancel-queued"
    _create_task(service=service, task_id=queued_task, sample_path=queued_sample)
    _queue_job(registry=registry, task_id=queued_task)
    queued_cancel = run_cancel_analytical_task(task_id=queued_task, core_config_service=service)
    assert queued_cancel.ok is True
    assert queued_cancel.value is not None
    assert queued_cancel.value.job is not None
    assert queued_cancel.value.job.state is AnalyticalTaskState.CANCELLED

    paused_sample = tmp_path / "paused.fasta"
    _write_sample(paused_sample, sample_id="paused")
    paused_task = "task-cancel-paused"
    _create_task(service=service, task_id=paused_task, sample_path=paused_sample)
    _queue_job(registry=registry, task_id=paused_task)
    pause_result = run_pause_analytical_task(task_id=paused_task, core_config_service=service)
    assert pause_result.ok is True
    paused_cancel = run_cancel_analytical_task(task_id=paused_task, core_config_service=service)
    assert paused_cancel.ok is True
    assert paused_cancel.value is not None
    assert paused_cancel.value.job is not None
    assert paused_cancel.value.job.state is AnalyticalTaskState.CANCELLED


def test_running_job_cancel_request_stops_worker_and_not_completed(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="running-cancel")
    task_id = "task-running-cancel"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )
    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        cancel_result = run_cancel_analytical_task(task_id=task_id, core_config_service=service)
        assert cancel_result.ok is True
        assert cancel_result.value is not None
        assert cancel_result.value.job is not None
        assert cancel_result.value.job.state is AnalyticalTaskState.CANCEL_REQUESTED

        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert payload["ok"] is True
    cancelled_job = registry.get_job(job_id=job_id)
    assert cancelled_job.state is AnalyticalTaskState.CANCELLED
    assert cancelled_job.worker_instance_id is None
    assert cancelled_job.worker_pid is None
    assert cancelled_job.lease_token is None
    assert cancelled_job.state is not AnalyticalTaskState.COMPLETED
    assert cancelled_job.state is not AnalyticalTaskState.FAILED
    runtime_events = _drain_runtime_events(event_queue)
    assert (
        sum(
            1
            for event_name, _ in runtime_events
            if event_name == RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_CANCEL
        )
        == 1
    )


def test_cancel_after_pause_requested_has_priority(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="cancel-priority")
    task_id = "task-cancel-priority"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    result_queue = spawn_context.Queue()
    event_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )
    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        pause_result = run_pause_analytical_task(task_id=task_id, core_config_service=service)
        assert pause_result.ok is True
        assert pause_result.value is not None
        assert pause_result.value.job is not None
        assert pause_result.value.job.state is AnalyticalTaskState.PAUSE_REQUESTED

        cancel_result = run_cancel_analytical_task(task_id=task_id, core_config_service=service)
        assert cancel_result.ok is True
        assert cancel_result.value is not None
        assert cancel_result.value.job is not None
        assert cancel_result.value.job.state is AnalyticalTaskState.CANCEL_REQUESTED

        stage_release_event.set()
        _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    final_job = registry.get_job(job_id=job_id)
    assert final_job.state is AnalyticalTaskState.CANCELLED


def test_running_job_deletion_request_stops_worker_and_removes_task(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="running-delete")
    task_id = "task-running-delete"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)
    task_dir = resolved.tasks_dir / task_id

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    result_queue = spawn_context.Queue()
    event_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )
    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        delete_result = run_delete_analytical_tasks(
            task_ids=(task_id,),
            core_config_service=service,
        )
        assert delete_result.ok is True
        assert delete_result.value is not None
        assert delete_result.value.items[0].result.value == "deletion_requested"

        _wait_until(
            lambda: _job_deleted_or_in_state(
                registry=registry,
                job_id=job_id,
                state=AnalyticalTaskState.DELETION_REQUESTED,
            ),
            description=f"job {job_id} to become deletion_requested or deleted",
        )
        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert payload["ok"] is True
    with pytest.raises(AnalyticalTaskNotFoundError):
        registry.get_task(task_id=task_id)
    with pytest.raises(AnalyticalTaskJobNotFoundError):
        registry.get_job(job_id=job_id)
    assert not task_dir.exists()

    runtime_events = _drain_runtime_events(event_queue)
    assert (
        sum(
            1
            for event_name, _ in runtime_events
            if event_name == RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_DELETION
        )
        == 1
    )


def test_stale_message_from_deleted_worker_is_rejected(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="stale-deleted")
    task_id = "task-stale-deleted-worker"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)

    runtime_instance_id = f"runtime-{uuid4()}"
    runtime_lease_token = str(uuid4())
    acquired, conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id=runtime_instance_id,
        owner_pid=os.getpid(),
        lease_token=runtime_lease_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None
    assert conflict is None

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    runtime_events: list[tuple[str, dict[str, Any] | None]] = []

    def _event_callback(event_name: str, context: dict[str, Any] | None) -> None:
        runtime_events.append((event_name, context))

    runtime = ExecutionRuntime(
        registry_service=registry,
        tasks_dir=resolved.tasks_dir,
        runtime_config=RuntimeConfig.from_resolved_config(resolved),
        runtime_instance_id=runtime_instance_id,
        runtime_lease_token=runtime_lease_token,
        pipeline_name=_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        pipeline_control=control,
        event_callback=_event_callback,
    )

    stale_worker_instance_id: str | None = None
    stale_lease_token: str | None = None

    def _request_deletion_and_release() -> None:
        nonlocal stale_worker_instance_id, stale_lease_token
        if not stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS):
            return
        delete_result = run_delete_analytical_tasks(
            task_ids=(task_id,),
            core_config_service=service,
        )
        assert delete_result.ok is True
        assert delete_result.value is not None
        assert delete_result.value.items[0].result.value == "deletion_requested"
        deleting_job = registry.get_job(job_id=job_id)
        stale_worker_instance_id = deleting_job.worker_instance_id
        stale_lease_token = deleting_job.lease_token
        stage_release_event.set()

    controller = threading.Thread(target=_request_deletion_and_release, daemon=True)
    controller.start()
    try:
        runtime.run(auto_queue_waiting_jobs=False)
    finally:
        stage_release_event.set()
        controller.join(timeout=_WAIT_TIMEOUT_SECONDS)

    assert stale_worker_instance_id is not None
    assert stale_lease_token is not None
    runtime._handle_worker_message(
        ProgressUpdatedMessage(
            task_id=task_id,
            job_id=job_id,
            worker_instance_id=stale_worker_instance_id,
            lease_token=stale_lease_token,
            stage_id="controlled_slow",
            stage_progress=1.0,
        ),
    )
    stale_reasons = [
        str(context["reason"])
        for event_name, context in runtime_events
        if event_name == RUNTIME_EVENT_STALE_MESSAGE_REJECTED and context is not None
    ]
    assert "task_not_found" in stale_reasons or "job_not_running" in stale_reasons


@pytest.mark.parametrize(
    ("control_operation", "requested_state", "final_state"),
    [
        ("pause", AnalyticalTaskState.PAUSE_REQUESTED, AnalyticalTaskState.PAUSED),
        ("cancel", AnalyticalTaskState.CANCEL_REQUESTED, AnalyticalTaskState.CANCELLED),
    ],
)
def test_stale_worker_message_after_control_request_is_rejected(
    tmp_path: Path,
    control_operation: str,
    requested_state: AnalyticalTaskState,
    final_state: AnalyticalTaskState,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    sample = tmp_path / "sample.fasta"
    _write_sample(sample, sample_id="stale")
    task_id = f"task-stale-{control_operation}"
    _create_task(service=service, task_id=task_id, sample_path=sample)
    job_id = _queue_job(registry=registry, task_id=task_id)

    runtime_instance_id = f"runtime-{uuid4()}"
    runtime_lease_token = str(uuid4())
    acquired, conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id=runtime_instance_id,
        owner_pid=os.getpid(),
        lease_token=runtime_lease_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None
    assert conflict is None

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    runtime_events: list[tuple[str, dict[str, Any] | None]] = []

    def _event_callback(event_name: str, context: dict[str, Any] | None) -> None:
        runtime_events.append((event_name, context))

    runtime = ExecutionRuntime(
        registry_service=registry,
        tasks_dir=resolved.tasks_dir,
        runtime_config=RuntimeConfig.from_resolved_config(resolved),
        runtime_instance_id=runtime_instance_id,
        runtime_lease_token=runtime_lease_token,
        pipeline_name=_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        pipeline_control=control,
        event_callback=_event_callback,
    )

    stale_worker_instance_id: str | None = None
    stale_lease_token: str | None = None

    def _request_control_and_release() -> None:
        nonlocal stale_worker_instance_id, stale_lease_token
        if not stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS):
            return
        if control_operation == "pause":
            control_result = run_pause_analytical_task(
                task_id=task_id,
                core_config_service=service,
            )
        else:
            control_result = run_cancel_analytical_task(
                task_id=task_id,
                core_config_service=service,
            )
        assert control_result.ok is True
        assert control_result.value is not None
        assert control_result.value.job is not None
        assert control_result.value.job.state is requested_state
        stale_worker_instance_id = control_result.value.job.worker_instance_id
        stale_lease_token = control_result.value.job.lease_token
        stage_release_event.set()

    controller = threading.Thread(target=_request_control_and_release, daemon=True)
    controller.start()
    try:
        runtime.run(auto_queue_waiting_jobs=False)
    finally:
        stage_release_event.set()
        controller.join(timeout=_WAIT_TIMEOUT_SECONDS)
    assert registry.get_job(job_id=job_id).state is final_state

    assert stale_worker_instance_id is not None
    assert stale_lease_token is not None
    runtime._handle_worker_message(
        ProgressUpdatedMessage(
            task_id=task_id,
            job_id=job_id,
            worker_instance_id=stale_worker_instance_id,
            lease_token=stale_lease_token,
            stage_id="controlled_slow",
            stage_progress=1.0,
        ),
    )

    stale_events = [
        context
        for event_name, context in runtime_events
        if event_name == RUNTIME_EVENT_STALE_MESSAGE_REJECTED
    ]
    assert len(stale_events) > 0
    assert any(
        context is not None and context.get("reason") == "job_not_running"
        for context in stale_events
    )


def test_scheduler_preemption_stops_running_job_and_resumes_after_candidate(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    service.set_parameter(parameter="execution.max_parallel_tasks", value="1")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    victim_task_id = "task-preemption-victim"
    victim_sample = tmp_path / "victim.fasta"
    _write_sample(victim_sample, sample_id="victim")
    _create_task(service=service, task_id=victim_task_id, sample_path=victim_sample)
    victim_job_id = _queue_job(registry=registry, task_id=victim_task_id, priority=2)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )

    candidate_task_id = "task-preemption-candidate"
    candidate_job_id: str | None = None
    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        _wait_for_job_state(
            registry=registry,
            job_id=victim_job_id,
            state=AnalyticalTaskState.RUNNING,
        )

        candidate_sample = tmp_path / "candidate.fasta"
        _write_sample(candidate_sample, sample_id="candidate")
        _create_task(service=service, task_id=candidate_task_id, sample_path=candidate_sample)
        candidate_job_id = _queue_job(
            registry=registry,
            task_id=candidate_task_id,
            priority=9,
        )

        _wait_for_job_state(
            registry=registry,
            job_id=victim_job_id,
            state=AnalyticalTaskState.PREEMPTION_REQUESTED,
        )
        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert candidate_job_id is not None
    assert payload["ok"] is True
    assert registry.get_job(job_id=victim_job_id).state is AnalyticalTaskState.COMPLETED
    assert registry.get_job(job_id=victim_job_id).recovery_count == 0
    assert registry.get_job(job_id=candidate_job_id).state is AnalyticalTaskState.COMPLETED

    runtime_events = _drain_runtime_events(event_queue)
    claim_order = [
        str(context["job_id"])
        for event_name, context in runtime_events
        if event_name == RUNTIME_EVENT_JOB_CLAIMED and context is not None
    ]
    assert claim_order == [victim_job_id, candidate_job_id, victim_job_id]
    assert (
        sum(
            1 for event_name, _ in runtime_events if event_name == RUNTIME_EVENT_PREEMPTION_SELECTED
        )
        == 1
    )
    assert (
        sum(
            1
            for event_name, _ in runtime_events
            if event_name == RUNTIME_EVENT_PREEMPTION_REQUESTED
        )
        == 1
    )
    assert (
        sum(
            1
            for event_name, _ in runtime_events
            if event_name == RUNTIME_EVENT_WORKER_SAFELY_STOPPED_FOR_PREEMPTION
        )
        == 1
    )
    assert (
        sum(
            1
            for event_name, _ in runtime_events
            if event_name == RUNTIME_EVENT_PREEMPTED_JOB_RETURNED_TO_WAITING
        )
        == 1
    )
    victim_stage_ids = [
        str(context["stage_id"])
        for event_name, context in runtime_events
        if event_name == RUNTIME_EVENT_STAGE_STARTED
        and context is not None
        and str(context["job_id"]) == victim_job_id
    ]
    assert victim_stage_ids == ["initialize_job", "controlled_slow", "finalize"]

    staging_dir = resolved.tasks_dir / victim_task_id / "jobs" / victim_job_id / "staging"
    if staging_dir.exists():
        assert list(staging_dir.iterdir()) == []


@pytest.mark.parametrize("candidate_priority", [5, 4])
def test_scheduler_skips_preemption_for_equal_or_lower_priority_candidates(
    tmp_path: Path,
    candidate_priority: int,
) -> None:
    service = _initialize_core(tmp_path / "home")
    service.set_parameter(parameter="execution.max_parallel_tasks", value="1")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    victim_task_id = "task-no-preemption-victim"
    victim_sample = tmp_path / "victim.fasta"
    _write_sample(victim_sample, sample_id="victim")
    _create_task(service=service, task_id=victim_task_id, sample_path=victim_sample)
    victim_job_id = _queue_job(registry=registry, task_id=victim_task_id, priority=5)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )

    candidate_task_id = "task-no-preemption-candidate"
    candidate_job_id: str | None = None
    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        candidate_sample = tmp_path / "candidate.fasta"
        _write_sample(candidate_sample, sample_id="candidate")
        _create_task(service=service, task_id=candidate_task_id, sample_path=candidate_sample)
        candidate_job_id = _queue_job(
            registry=registry,
            task_id=candidate_task_id,
            priority=candidate_priority,
        )
        _assert_job_state_not_reached(
            registry=registry,
            job_id=victim_job_id,
            state=AnalyticalTaskState.PREEMPTION_REQUESTED,
            duration_seconds=1.5,
        )
        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert candidate_job_id is not None
    assert payload["ok"] is True
    runtime_events = _drain_runtime_events(event_queue)
    assert not any(
        event_name == RUNTIME_EVENT_PREEMPTION_REQUESTED for event_name, _ in runtime_events
    )
    claim_order = [
        str(context["job_id"])
        for event_name, context in runtime_events
        if event_name == RUNTIME_EVENT_JOB_CLAIMED and context is not None
    ]
    assert claim_order[:2] == [victim_job_id, candidate_job_id]


def test_scheduler_does_not_preempt_when_free_slot_exists(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    service.set_parameter(parameter="execution.max_parallel_tasks", value="2")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    victim_task_id = "task-free-slot-victim"
    victim_sample = tmp_path / "victim.fasta"
    _write_sample(victim_sample, sample_id="victim")
    _create_task(service=service, task_id=victim_task_id, sample_path=victim_sample)
    victim_job_id = _queue_job(registry=registry, task_id=victim_task_id, priority=2)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )

    candidate_task_id = "task-free-slot-candidate"
    candidate_job_id: str | None = None
    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        _wait_for_job_state(
            registry=registry,
            job_id=victim_job_id,
            state=AnalyticalTaskState.RUNNING,
        )
        candidate_sample = tmp_path / "candidate.fasta"
        _write_sample(candidate_sample, sample_id="candidate")
        _create_task(service=service, task_id=candidate_task_id, sample_path=candidate_sample)
        candidate_job_id = _queue_job(
            registry=registry,
            task_id=candidate_task_id,
            priority=9,
        )
        _wait_for_job_state(
            registry=registry,
            job_id=candidate_job_id,
            state=AnalyticalTaskState.RUNNING,
        )
        assert registry.get_job(job_id=victim_job_id).state is AnalyticalTaskState.RUNNING
        _assert_job_state_not_reached(
            registry=registry,
            job_id=victim_job_id,
            state=AnalyticalTaskState.PREEMPTION_REQUESTED,
            duration_seconds=1.0,
        )
        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert candidate_job_id is not None
    assert payload["ok"] is True
    runtime_events = _drain_runtime_events(event_queue)
    assert not any(
        event_name == RUNTIME_EVENT_PREEMPTION_REQUESTED for event_name, _ in runtime_events
    )


def test_scheduler_preempts_only_one_victim_when_slots_are_full(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    service.set_parameter(parameter="execution.max_parallel_tasks", value="2")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    low_task_1 = "task-full-low-1"
    low_task_2 = "task-full-low-2"
    high_task = "task-full-high"
    sample_low_1 = tmp_path / "low-1.fasta"
    sample_low_2 = tmp_path / "low-2.fasta"
    sample_high = tmp_path / "high.fasta"
    _write_sample(sample_low_1, sample_id="low-1")
    _write_sample(sample_low_2, sample_id="low-2")
    _write_sample(sample_high, sample_id="high")
    _create_task(service=service, task_id=low_task_1, sample_path=sample_low_1)
    _create_task(service=service, task_id=low_task_2, sample_path=sample_low_2)
    _create_task(service=service, task_id=high_task, sample_path=sample_high)

    low_job_1 = _queue_job(registry=registry, task_id=low_task_1, priority=1)
    low_job_2 = _queue_job(registry=registry, task_id=low_task_2, priority=2)

    spawn_context = mp.get_context("spawn")
    control_manager = spawn_context.Manager()
    stage_started_queue = spawn_context.Queue()
    stage_release_semaphores_by_job_id = control_manager.dict()
    stage_release_semaphores_by_job_id[low_job_1] = control_manager.Semaphore(0)
    stage_release_semaphores_by_job_id[low_job_2] = control_manager.Semaphore(0)
    control = WorkerPipelineControl(
        stage_started_queue=stage_started_queue,
        stage_release_semaphores_by_job_id=stage_release_semaphores_by_job_id,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )

    process.start()
    high_job_id: str | None = None
    try:
        started_counts_by_job_id: dict[str, int] = {
            low_job_1: 0,
            low_job_2: 0,
        }
        while started_counts_by_job_id[low_job_1] == 0 or started_counts_by_job_id[low_job_2] == 0:
            started_job_id = _wait_for_started_job_id(
                started_queue=stage_started_queue,
                description="both low-priority workers to enter controlled stage",
            )
            if started_job_id not in {low_job_1, low_job_2}:
                raise AssertionError(
                    "unexpected job entered controlled stage before preemption setup: "
                    f"'{started_job_id}'"
                )
            started_counts_by_job_id[started_job_id] = started_counts_by_job_id[started_job_id] + 1
            if started_counts_by_job_id[started_job_id] > 1:
                raise AssertionError(
                    "job entered controlled stage more than once before preemption setup: "
                    f"'{started_job_id}'"
                )

        _wait_until(
            lambda: (
                registry.get_job(job_id=low_job_1).state is AnalyticalTaskState.RUNNING
                and registry.get_job(job_id=low_job_2).state is AnalyticalTaskState.RUNNING
            ),
            description="both low-priority jobs to start running",
        )
        queued_high_job_id = _queue_job(
            registry=registry,
            task_id=high_task,
            priority=9,
        )
        high_job_id = queued_high_job_id
        _wait_for_job_state(
            registry=registry,
            job_id=low_job_1,
            state=AnalyticalTaskState.PREEMPTION_REQUESTED,
        )
        assert registry.get_job(job_id=low_job_2).state is AnalyticalTaskState.RUNNING
        _assert_job_state_not_reached(
            registry=registry,
            job_id=low_job_2,
            state=AnalyticalTaskState.PREEMPTION_REQUESTED,
            duration_seconds=1.0,
        )
        stage_release_semaphores_by_job_id[queued_high_job_id] = control_manager.Semaphore(0)
        started_counts_by_job_id[queued_high_job_id] = 0

        low_job_1_release = stage_release_semaphores_by_job_id.get(low_job_1)
        if low_job_1_release is None:
            raise AssertionError("missing release gate for low_job_1")
        low_job_1_release.release()

        post_preemption_started_job_id = _wait_for_started_job_id(
            started_queue=stage_started_queue,
            description="high-priority job to enter controlled stage after preemption",
        )
        if post_preemption_started_job_id != queued_high_job_id:
            if post_preemption_started_job_id == low_job_1:
                raise AssertionError(
                    "preempted victim re-entered controlled stage after commit: "
                    f"'{post_preemption_started_job_id}'"
                )
            raise AssertionError(
                "unexpected job entered controlled stage after preemption setup: "
                f"'{post_preemption_started_job_id}'"
            )
        started_counts_by_job_id[queued_high_job_id] = (
            started_counts_by_job_id[queued_high_job_id] + 1
        )
        high_job_release = stage_release_semaphores_by_job_id.get(queued_high_job_id)
        if high_job_release is None:
            raise AssertionError(f"missing release gate for job '{queued_high_job_id}'")
        high_job_release.release()
        low_job_2_release = stage_release_semaphores_by_job_id.get(low_job_2)
        if low_job_2_release is None:
            raise AssertionError("missing release gate for low_job_2")
        low_job_2_release.release()

        assert started_counts_by_job_id[low_job_1] == 1
        assert started_counts_by_job_id[low_job_2] == 1
        assert started_counts_by_job_id[queued_high_job_id] == 1
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        _stop_process(process)
        control_manager.shutdown()

    assert high_job_id is not None
    assert payload["ok"] is True
    assert registry.get_job(job_id=low_job_1).state is AnalyticalTaskState.COMPLETED
    assert registry.get_job(job_id=low_job_2).state is AnalyticalTaskState.COMPLETED
    assert registry.get_job(job_id=high_job_id).state is AnalyticalTaskState.COMPLETED
    runtime_events = _drain_runtime_events(event_queue)
    assert (
        sum(
            1 for event_name, _ in runtime_events if event_name == RUNTIME_EVENT_PREEMPTION_SELECTED
        )
        == 1
    )
    assert (
        sum(
            1
            for event_name, _ in runtime_events
            if event_name == RUNTIME_EVENT_PREEMPTION_REQUESTED
        )
        == 1
    )


@pytest.mark.parametrize(
    ("operation", "requested_state", "final_state"),
    [
        ("cancel", AnalyticalTaskState.CANCEL_REQUESTED, AnalyticalTaskState.CANCELLED),
        ("pause", AnalyticalTaskState.PAUSE_REQUESTED, AnalyticalTaskState.PAUSED),
    ],
)
def test_cancel_or_pause_after_preemption_requested_keeps_higher_priority_intent(
    tmp_path: Path,
    operation: str,
    requested_state: AnalyticalTaskState,
    final_state: AnalyticalTaskState,
) -> None:
    service = _initialize_core(tmp_path / "home")
    service.set_parameter(parameter="execution.max_parallel_tasks", value="1")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    victim_task_id = f"task-conflict-victim-{operation}"
    candidate_task_id = f"task-conflict-candidate-{operation}"
    victim_sample = tmp_path / f"victim-{operation}.fasta"
    candidate_sample = tmp_path / f"candidate-{operation}.fasta"
    _write_sample(victim_sample, sample_id=f"victim-{operation}")
    _write_sample(candidate_sample, sample_id=f"candidate-{operation}")
    _create_task(service=service, task_id=victim_task_id, sample_path=victim_sample)
    _create_task(service=service, task_id=candidate_task_id, sample_path=candidate_sample)
    victim_job_id = _queue_job(registry=registry, task_id=victim_task_id, priority=2)

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )

    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        _queue_job(registry=registry, task_id=candidate_task_id, priority=9)
        _wait_for_job_state(
            registry=registry,
            job_id=victim_job_id,
            state=AnalyticalTaskState.PREEMPTION_REQUESTED,
        )
        if operation == "cancel":
            control_result = run_cancel_analytical_task(
                task_id=victim_task_id,
                core_config_service=service,
            )
        else:
            control_result = run_pause_analytical_task(
                task_id=victim_task_id,
                core_config_service=service,
            )
        assert control_result.ok is True
        assert control_result.value is not None
        assert control_result.value.job is not None
        assert control_result.value.job.state is requested_state

        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert payload["ok"] is True
    assert registry.get_job(job_id=victim_job_id).state is final_state


def test_stale_message_from_preempted_worker_is_rejected(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    service.set_parameter(parameter="execution.max_parallel_tasks", value="1")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    victim_task_id = "task-stale-preempt-victim"
    candidate_task_id = "task-stale-preempt-candidate"
    victim_sample = tmp_path / "victim.fasta"
    candidate_sample = tmp_path / "candidate.fasta"
    _write_sample(victim_sample, sample_id="victim")
    _write_sample(candidate_sample, sample_id="candidate")
    _create_task(service=service, task_id=victim_task_id, sample_path=victim_sample)
    _create_task(service=service, task_id=candidate_task_id, sample_path=candidate_sample)
    victim_job_id = _queue_job(registry=registry, task_id=victim_task_id, priority=2)

    runtime_instance_id = f"runtime-{uuid4()}"
    runtime_lease_token = str(uuid4())
    acquired, conflict = registry.acquire_execution_runtime_lease(
        runtime_instance_id=runtime_instance_id,
        owner_pid=os.getpid(),
        lease_token=runtime_lease_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None
    assert conflict is None

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    runtime_events: list[tuple[str, dict[str, Any] | None]] = []

    def _event_callback(event_name: str, context: dict[str, Any] | None) -> None:
        runtime_events.append((event_name, context))

    runtime = ExecutionRuntime(
        registry_service=registry,
        tasks_dir=resolved.tasks_dir,
        runtime_config=RuntimeConfig.from_resolved_config(resolved),
        runtime_instance_id=runtime_instance_id,
        runtime_lease_token=runtime_lease_token,
        pipeline_name=_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        pipeline_control=control,
        event_callback=_event_callback,
    )

    stale_worker_instance_id: str | None = None
    stale_lease_token: str | None = None

    def _trigger_preemption() -> None:
        nonlocal stale_worker_instance_id, stale_lease_token
        if not stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS):
            return
        _queue_job(registry=registry, task_id=candidate_task_id, priority=9)
        _wait_for_job_state(
            registry=registry,
            job_id=victim_job_id,
            state=AnalyticalTaskState.PREEMPTION_REQUESTED,
        )
        preempted = registry.get_job(job_id=victim_job_id)
        stale_worker_instance_id = preempted.worker_instance_id
        stale_lease_token = preempted.lease_token
        stage_release_event.set()

    controller = threading.Thread(target=_trigger_preemption, daemon=True)
    controller.start()
    try:
        runtime.run(auto_queue_waiting_jobs=False)
    finally:
        stage_release_event.set()
        controller.join(timeout=_WAIT_TIMEOUT_SECONDS)

    assert stale_worker_instance_id is not None
    assert stale_lease_token is not None
    runtime._handle_worker_message(
        ProgressUpdatedMessage(
            task_id=victim_task_id,
            job_id=victim_job_id,
            worker_instance_id=stale_worker_instance_id,
            lease_token=stale_lease_token,
            stage_id="controlled_slow",
            stage_progress=1.0,
        ),
    )
    stale_reasons = [
        str(context["reason"])
        for event_name, context in runtime_events
        if event_name == RUNTIME_EVENT_STALE_MESSAGE_REJECTED and context is not None
    ]
    assert "job_not_running" in stale_reasons or "worker_identity_mismatch" in stale_reasons


@pytest.mark.parametrize("reprioritize_target", ["queued", "running"])
def test_reprioritize_changes_preemption_decision_on_next_cycle(
    tmp_path: Path,
    reprioritize_target: str,
) -> None:
    service = _initialize_core(tmp_path / "home")
    service.set_parameter(parameter="execution.max_parallel_tasks", value="1")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)

    running_task_id = f"task-reprioritize-running-{reprioritize_target}"
    queued_task_id = f"task-reprioritize-queued-{reprioritize_target}"
    running_sample = tmp_path / f"running-{reprioritize_target}.fasta"
    queued_sample = tmp_path / f"queued-{reprioritize_target}.fasta"
    _write_sample(running_sample, sample_id=f"running-{reprioritize_target}")
    _write_sample(queued_sample, sample_id=f"queued-{reprioritize_target}")
    _create_task(service=service, task_id=running_task_id, sample_path=running_sample)
    _create_task(service=service, task_id=queued_task_id, sample_path=queued_sample)

    if reprioritize_target == "queued":
        running_priority = 6
        queued_priority = 4
    else:
        running_priority = 8
        queued_priority = 7

    running_job_id = _queue_job(
        registry=registry,
        task_id=running_task_id,
        priority=running_priority,
    )
    queued_job_id = _queue_job(
        registry=registry,
        task_id=queued_task_id,
        priority=queued_priority,
    )

    spawn_context = mp.get_context("spawn")
    stage_started_event = spawn_context.Event()
    stage_release_event = spawn_context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started_event,
        stage_release_event=stage_release_event,
    )
    event_queue = spawn_context.Queue()
    result_queue = spawn_context.Queue()
    process = spawn_context.Process(
        target=_runtime_process_entry,
        kwargs={
            "database_path": str(resolved.database_path),
            "tasks_dir": str(resolved.tasks_dir),
            "runtime_config": RuntimeConfig.from_resolved_config(resolved),
            "runtime_instance_id": f"runtime-{uuid4()}",
            "runtime_lease_token": str(uuid4()),
            "pipeline_name": _PIPELINE_NAME,
            "pipeline_version": DEFAULT_PIPELINE_VERSION,
            "auto_queue_waiting_jobs": False,
            "pipeline_control": control,
            "acquire_lease": True,
            "event_queue": event_queue,
            "result_queue": result_queue,
        },
        daemon=False,
    )

    process.start()
    try:
        assert stage_started_event.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        _assert_job_state_not_reached(
            registry=registry,
            job_id=running_job_id,
            state=AnalyticalTaskState.PREEMPTION_REQUESTED,
            duration_seconds=1.2,
        )
        if reprioritize_target == "queued":
            reprioritize_result = registry.reprioritize_active_job(
                task_id=queued_task_id,
                priority=9,
            )
        else:
            reprioritize_result = registry.reprioritize_active_job(
                task_id=running_task_id,
                priority=3,
            )
        assert reprioritize_result.result_type is AnalyticalTaskMutationResultType.APPLIED
        _wait_for_job_state(
            registry=registry,
            job_id=running_job_id,
            state=AnalyticalTaskState.PREEMPTION_REQUESTED,
        )
        stage_release_event.set()
        payload = _await_runtime_process_result(process=process, result_queue=result_queue)
    finally:
        stage_release_event.set()
        _stop_process(process)

    assert payload["ok"] is True
    runtime_events = _drain_runtime_events(event_queue)
    assert any(event_name == RUNTIME_EVENT_PREEMPTION_REQUESTED for event_name, _ in runtime_events)
    assert registry.get_job(job_id=queued_job_id).state is AnalyticalTaskState.COMPLETED
