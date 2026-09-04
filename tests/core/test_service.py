from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from jelica_core.config import AnalysisConfigInput, resolve_analysis_config
from jelica_core.events import (
    run_resume_analytical_task,
    run_service_runtime,
    run_start_analytical_task,
)
from jelica_core.runtime import (
    ServiceRunningTasksError,
    WorkerPipelineControl,
    get_service_status,
    read_service_logs,
    restart_service,
    start_service,
    stop_service,
)
from jelica_core.runtime.models import RuntimeShutdownMode
from jelica_core.runtime.service import (
    ServiceControlRequest,
    ServiceMetadata,
    ServiceRuntimeControlMonitor,
    ServiceState,
    ServiceStateStore,
    initial_service_metadata,
)
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import (
    AnalyticalTaskMutationResultType,
    AnalyticalTaskRegistryService,
    AnalyticalTaskState,
    LocalTaskStorage,
)
from jelica_core.tasks.timestamps import utc_now

_WAIT_TIMEOUT_SECONDS = 15.0


def _initialize_core(jelica_home: Path) -> CoreConfigService:
    service = CoreConfigService(jelica_home=jelica_home)
    service.initialize_system_config(force=True)
    return service


def _create_task(
    *,
    service: CoreConfigService,
    task_id: str,
    sample_path: Path,
    priority: int = 1,
) -> None:
    sample_path.write_text(f">{task_id}\nACGT\n", encoding="utf-8")
    resolved = service.load_resolved_config()
    config = resolve_analysis_config(
        AnalysisConfigInput(samples=[str(sample_path)], priority=priority)
    ).config
    workspace = LocalTaskStorage(tasks_dir=resolved.tasks_dir).create_task_workspace(
        task_id=task_id,
        config=config,
    )
    AnalyticalTaskRegistryService(database_path=resolved.database_path).register_task(
        task_id=task_id,
        task_dir_relative_path=task_id,
        default_priority=priority,
        current_config_revision=workspace.current_config_revision,
        current_config_relative_path=workspace.current_config_relative_path,
        current_config_hash=workspace.current_config_hash,
    )


def _wait_until(
    predicate: Any,
    *,
    description: str,
    timeout: float = _WAIT_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bool(predicate()):
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}")


def _wait_for_job_state(
    registry: AnalyticalTaskRegistryService,
    job_id: str,
    state: AnalyticalTaskState,
) -> None:
    _wait_until(
        lambda: registry.get_job(job_id=job_id).state is state,
        description=f"job {job_id} to reach {state.value}",
    )


def _service_process_entry(
    *,
    jelica_home: str,
    pipeline_name: str,
    pipeline_control: WorkerPipelineControl | None,
    result_queue: Any,
) -> None:
    result = run_service_runtime(
        core_config_service=CoreConfigService(jelica_home=Path(jelica_home)),
        pipeline_name=pipeline_name,
        pipeline_control=pipeline_control,
    )
    result_queue.put({"ok": result.ok})


def _task_start_client_process_entry(
    *,
    jelica_home: str,
    task_id: str,
    result_queue: Any,
) -> None:
    result = run_start_analytical_task(
        task_id=task_id,
        detached=True,
        core_config_service=CoreConfigService(jelica_home=Path(jelica_home)),
    )
    result_queue.put(
        {
            "ok": result.ok,
            "used_existing_runtime": (
                result.value.used_existing_runtime if result.value is not None else None
            ),
        }
    )


def _start_test_service(
    *,
    service: CoreConfigService,
    pipeline_name: str = "quick_success",
    pipeline_control: WorkerPipelineControl | None = None,
) -> tuple[Any, Any]:
    context = mp.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_service_process_entry,
        kwargs={
            "jelica_home": str(service.get_jelica_home()),
            "pipeline_name": pipeline_name,
            "pipeline_control": pipeline_control,
            "result_queue": result_queue,
        },
        daemon=False,
    )
    process.start()
    _wait_until(
        lambda: get_service_status(core_config_service=service).running,
        description="test Service to acquire its lease",
    )
    return process, result_queue


def _join_test_service(process: Any, result_queue: Any) -> None:
    process.join(timeout=_WAIT_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(timeout=_WAIT_TIMEOUT_SECONDS)
    assert not process.is_alive()
    try:
        payload = result_queue.get(timeout=2)
    except Empty as error:
        raise AssertionError("Service process returned no result") from error
    assert payload == {"ok": True}


def test_service_start_and_stop_are_idempotent_and_lease_authoritative(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")

    started = start_service(core_config_service=service)
    try:
        assert started.already_running is False
        assert started.status.running is True
        assert started.status.service_id is not None
        assert started.status.pid is not None
        assert started.status.started_at is not None
        assert started.status.last_heartbeat is not None
        assert started.status.configured_workers == 1
        logs = read_service_logs(core_config_service=service)
        assert logs.path.name == "system-events.jsonl"
        assert len(logs.lines) > 0

        repeated = start_service(core_config_service=service)
        assert repeated.already_running is True
        assert repeated.status.service_id == started.status.service_id
    finally:
        stopped = stop_service(force=True, core_config_service=service)

    assert stopped.already_stopped is False
    assert stopped.status.running is False
    repeated_stop = stop_service(core_config_service=service)
    assert repeated_stop.already_stopped is True

    resolved = service.load_resolved_config()
    store = ServiceStateStore(data_dir=resolved.data_dir)
    stale = store.read_metadata()
    assert stale is not None
    store.write_metadata(stale.model_copy(update={"state": ServiceState.RUNNING, "pid": 999_999}))
    stale_status = get_service_status(core_config_service=service)
    assert stale_status.running is False
    assert stale_status.state is ServiceState.STOPPED

    store.write_metadata(
        stale.model_copy(
            update={
                "state": ServiceState.ERROR,
                "pid": 999_999,
                "error_detail": "simulated Service failure",
            }
        )
    )
    error_status = get_service_status(core_config_service=service)
    assert error_status.running is False
    assert error_status.state is ServiceState.ERROR


def test_stale_service_runner_exits_after_registry_lease_replacement(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    stale_service_id = "stale-service"
    current_service_id = "current-service"
    current_token = "current-service-token"
    acquired, _ = registry.acquire_execution_runtime_lease(
        runtime_instance_id=current_service_id,
        owner_pid=os.getpid(),
        lease_token=current_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None

    store = ServiceStateStore(data_dir=resolved.data_dir)
    metadata = initial_service_metadata(service_id=stale_service_id, pid=os.getpid())
    store.write_metadata(metadata)
    store.write_control_request(
        ServiceControlRequest(
            request_id="replacement-stop-request",
            service_id=current_service_id,
            force=False,
            requested_at=utc_now(),
        )
    )
    monitor = ServiceRuntimeControlMonitor(
        store=store,
        registry_service=registry,
        metadata=metadata,
    )

    try:
        assert monitor.poll() is RuntimeShutdownMode.GRACEFUL
        # The stale runner must not overwrite metadata belonging to the
        # current owner while it cooperatively exits.
        assert store.read_metadata() == metadata
    finally:
        registry.release_execution_runtime_lease(
            runtime_instance_id=current_service_id,
            lease_token=current_token,
        )


def test_service_runner_ignores_control_for_other_service_while_still_owner(
    tmp_path: Path,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    service_id = "current-service"
    token = "current-service-token"
    acquired, _ = registry.acquire_execution_runtime_lease(
        runtime_instance_id=service_id,
        owner_pid=os.getpid(),
        lease_token=token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None

    store = ServiceStateStore(data_dir=resolved.data_dir)
    metadata = initial_service_metadata(service_id=service_id, pid=os.getpid())
    store.write_metadata(metadata)
    store.write_control_request(
        ServiceControlRequest(
            request_id="unrelated-stop-request",
            service_id="other-service",
            force=False,
            requested_at=utc_now(),
        )
    )
    monitor = ServiceRuntimeControlMonitor(
        store=store,
        registry_service=registry,
        metadata=metadata,
    )

    try:
        assert monitor.poll() is None
        assert store.read_metadata() == metadata
    finally:
        registry.release_execution_runtime_lease(
            runtime_instance_id=service_id,
            lease_token=token,
        )


def test_persistent_service_survives_queue_drain_and_claims_later_task(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    process, result_queue = _start_test_service(service=service)
    service_id = get_service_status(core_config_service=service).service_id
    try:
        _create_task(
            service=service,
            task_id="first-task",
            sample_path=tmp_path / "first.fasta",
        )
        first = run_start_analytical_task(
            task_id="first-task",
            detached=True,
            core_config_service=service,
        )
        assert first.ok is True
        assert first.value is not None
        _wait_for_job_state(
            registry,
            first.value.job.job_id,
            AnalyticalTaskState.COMPLETED,
        )

        drained_status = get_service_status(core_config_service=service)
        assert drained_status.running is True
        assert drained_status.service_id == service_id
        assert drained_status.running_tasks == 0
        assert drained_status.queued_tasks == 0

        _create_task(
            service=service,
            task_id="later-task",
            sample_path=tmp_path / "later.fasta",
        )
        later = run_start_analytical_task(
            task_id="later-task",
            detached=True,
            core_config_service=service,
        )
        assert later.ok is True
        assert later.value is not None
        _wait_for_job_state(
            registry,
            later.value.job.job_id,
            AnalyticalTaskState.COMPLETED,
        )
        assert get_service_status(core_config_service=service).service_id == service_id

        _create_task(
            service=service,
            task_id="resumed-task",
            sample_path=tmp_path / "resumed.fasta",
        )
        paused_job = registry.start(task_id="resumed-task")
        assert paused_job.job is not None
        paused = registry.pause(task_id="resumed-task")
        assert paused.job is not None
        assert paused.job.state is AnalyticalTaskState.PAUSED
        resumed = run_resume_analytical_task(
            task_id="resumed-task",
            detached=True,
            core_config_service=service,
        )
        assert resumed.ok is True
        assert resumed.value is not None
        _wait_for_job_state(
            registry,
            resumed.value.job.job_id,
            AnalyticalTaskState.COMPLETED,
        )

        conflicting = run_service_runtime(
            core_config_service=service,
            pipeline_name="quick_success",
        )
        assert conflicting.ok is False
    finally:
        stop_service(force=True, core_config_service=service)
        _join_test_service(process, result_queue)


def test_client_process_can_exit_while_service_owned_task_continues(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    context = mp.get_context("spawn")
    stage_started = context.Event()
    stage_release = context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started,
        stage_release_event=stage_release,
    )
    service_process, service_result_queue = _start_test_service(
        service=service,
        pipeline_name="test_controlled_multi_stage",
        pipeline_control=control,
    )
    _create_task(
        service=service,
        task_id="client-exit-task",
        sample_path=tmp_path / "client-exit.fasta",
    )
    client_result_queue = context.Queue()
    client_process = context.Process(
        target=_task_start_client_process_entry,
        kwargs={
            "jelica_home": str(service.get_jelica_home()),
            "task_id": "client-exit-task",
            "result_queue": client_result_queue,
        },
        daemon=False,
    )

    try:
        client_process.start()
        assert stage_started.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        client_process.join(timeout=_WAIT_TIMEOUT_SECONDS)
        assert not client_process.is_alive()
        assert client_result_queue.get(timeout=2) == {
            "ok": True,
            "used_existing_runtime": True,
        }
        running = registry.get_task_snapshot(task_id="client-exit-task")
        assert running.task.state is AnalyticalTaskState.RUNNING
        assert get_service_status(core_config_service=service).running is True

        stage_release.set()
        assert running.active_or_latest_job is not None
        _wait_for_job_state(
            registry,
            running.active_or_latest_job.job_id,
            AnalyticalTaskState.COMPLETED,
        )
    finally:
        stage_release.set()
        if client_process.is_alive():
            client_process.terminate()
            client_process.join(timeout=_WAIT_TIMEOUT_SECONDS)
        stop_service(force=True, core_config_service=service)
        _join_test_service(service_process, service_result_queue)


def test_stop_without_force_rejects_running_task_without_mutation(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    _create_task(
        service=service,
        task_id="running-task",
        sample_path=tmp_path / "running.fasta",
    )
    queued = registry.start(task_id="running-task")
    assert queued.job is not None
    claimed = registry.claim_next_queued_job_for_worker(
        worker_instance_id="worker",
        lease_token="worker-lease",
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert claimed is not None

    service_id = "manual-service"
    service_token = "manual-service-lease"
    acquired, _ = registry.acquire_execution_runtime_lease(
        runtime_instance_id=service_id,
        owner_pid=os.getpid(),
        lease_token=service_token,
        lease_timeout_seconds=resolved.lease_timeout_seconds,
    )
    assert acquired is not None
    now = utc_now()
    ServiceStateStore(data_dir=resolved.data_dir).write_metadata(
        ServiceMetadata(
            service_id=service_id,
            pid=os.getpid(),
            started_at=now,
            last_heartbeat=now,
            state=ServiceState.RUNNING,
            jelica_version="0.1.0",
        )
    )
    try:
        with pytest.raises(ServiceRunningTasksError) as exc_info:
            stop_service(core_config_service=service)
        assert exc_info.value.task_ids == ("running-task",)
        assert registry.get_job(job_id=queued.job.job_id).state is AnalyticalTaskState.RUNNING
    finally:
        registry.release_execution_runtime_lease(
            runtime_instance_id=service_id,
            lease_token=service_token,
        )


def test_forced_stop_pauses_task_and_normal_start_does_not_resume_it(tmp_path: Path) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    context = mp.get_context("spawn")
    stage_started = context.Event()
    stage_release = context.Event()
    control = WorkerPipelineControl(
        stage_started_event=stage_started,
        stage_release_event=stage_release,
    )
    process, result_queue = _start_test_service(
        service=service,
        pipeline_name="test_controlled_multi_stage",
        pipeline_control=control,
    )
    _create_task(
        service=service,
        task_id="force-stopped-task",
        sample_path=tmp_path / "force-stopped.fasta",
    )
    queued = registry.start(task_id="force-stopped-task")
    assert queued.job is not None
    assert stage_started.wait(timeout=_WAIT_TIMEOUT_SECONDS)
    _create_task(
        service=service,
        task_id="queued-survivor",
        sample_path=tmp_path / "queued-survivor.fasta",
    )
    queued_survivor = registry.start(task_id="queued-survivor")
    assert queued_survivor.job is not None

    result_holder: dict[str, Any] = {}
    stop_thread = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result",
            stop_service(force=True, core_config_service=service),
        )
    )
    stop_thread.start()
    _wait_for_job_state(registry, queued.job.job_id, AnalyticalTaskState.PAUSE_REQUESTED)
    stage_release.set()
    stop_thread.join(timeout=_WAIT_TIMEOUT_SECONDS)
    assert not stop_thread.is_alive()
    _join_test_service(process, result_queue)

    stopped = result_holder["result"]
    assert stopped.interrupted_task_ids == ("force-stopped-task",)
    assert registry.get_job(job_id=queued.job.job_id).state is AnalyticalTaskState.PAUSED
    assert registry.get_job(job_id=queued_survivor.job.job_id).state is AnalyticalTaskState.QUEUED
    job_dir = resolved.tasks_dir / "force-stopped-task" / "jobs" / queued.job.job_id
    assert (job_dir / "stages" / "initialize_job" / "stage_manifest.json").is_file()

    restarted_normally = start_service(core_config_service=service)
    try:
        assert restarted_normally.status.running is True
        time.sleep(0.5)
        assert registry.get_job(job_id=queued.job.job_id).state is AnalyticalTaskState.PAUSED
    finally:
        stop_service(force=True, core_config_service=service)


def test_forced_restart_resumes_only_tasks_interrupted_by_that_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _initialize_core(tmp_path / "home")
    resolved = service.load_resolved_config()
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    context = mp.get_context("spawn")

    _create_task(
        service=service,
        task_id="previously-paused",
        sample_path=tmp_path / "previous.fasta",
    )
    previous = registry.start(task_id="previously-paused")
    assert previous.job is not None
    paused = registry.pause(task_id="previously-paused")
    assert paused.result_type is AnalyticalTaskMutationResultType.APPLIED
    assert paused.job is not None
    assert paused.job.state is AnalyticalTaskState.PAUSED

    stage_started = context.Event()
    stage_release = context.Event()
    first_control = WorkerPipelineControl(
        stage_started_event=stage_started,
        stage_release_event=stage_release,
    )
    first_process, first_result_queue = _start_test_service(
        service=service,
        pipeline_name="test_controlled_multi_stage",
        pipeline_control=first_control,
    )
    _create_task(
        service=service,
        task_id="restart-interrupted",
        sample_path=tmp_path / "restart.fasta",
    )
    interrupted = registry.start(task_id="restart-interrupted")
    assert interrupted.job is not None
    assert stage_started.wait(timeout=_WAIT_TIMEOUT_SECONDS)

    second_release = context.Event()
    second_release.set()
    second_control = WorkerPipelineControl(stage_release_event=second_release)
    launched_processes: list[tuple[Any, Any]] = []

    def _launch_replacement(
        *,
        jelica_home: Path | None = None,
        runner_module: str,
    ) -> int:
        assert jelica_home == service.get_jelica_home()
        assert runner_module == "jelica_core.runtime.background_runner"
        process, result_queue = _start_test_service(
            service=service,
            pipeline_name="test_controlled_multi_stage",
            pipeline_control=second_control,
        )
        launched_processes.append((process, result_queue))
        return int(process.pid)

    monkeypatch.setattr(
        "jelica_core.runtime.service.launch_background_runtime",
        _launch_replacement,
    )
    result_holder: dict[str, Any] = {}
    restart_thread = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result",
            restart_service(force=True, core_config_service=service),
        )
    )
    restart_thread.start()
    _wait_for_job_state(
        registry,
        interrupted.job.job_id,
        AnalyticalTaskState.PAUSE_REQUESTED,
    )
    stage_release.set()
    restart_thread.join(timeout=_WAIT_TIMEOUT_SECONDS)
    assert not restart_thread.is_alive()
    _join_test_service(first_process, first_result_queue)

    restarted = result_holder["result"]
    assert restarted.interrupted_task_ids == ("restart-interrupted",)
    assert restarted.resumed_task_ids == ("restart-interrupted",)
    assert registry.get_job(job_id=previous.job.job_id).state is AnalyticalTaskState.PAUSED
    _wait_for_job_state(
        registry,
        interrupted.job.job_id,
        AnalyticalTaskState.COMPLETED,
    )

    stop_service(force=True, core_config_service=service)
    for process, result_queue in launched_processes:
        _join_test_service(process, result_queue)
