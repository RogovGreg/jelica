from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from jelica_contracts import EventType
from jelica_core.events import (
    CORE_EVENT_CATALOG,
    CoreEventFactory,
    EventService,
    InMemoryEventSink,
    JsonlFileEventSink,
    MandatoryEventSinkWriteError,
    current_command_id,
    reset_command_id,
    run_initialize_analysis_task_from_inputs,
    set_command_id,
)
from jelica_core.events.context import CoreExecutionContext
from jelica_core.events.definitions import (
    CORE_ANALYTICAL_TASK_REGISTERED,
    CORE_ANALYZE_REQUEST_STARTED,
    CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED,
    CORE_CLADE_DETECTION_FAILED,
    CORE_COMPARATIVE_ANALYSIS_FAILED,
    CORE_DISTANCE_MATRIX_FAILED,
    CORE_PHYLOGENETIC_TREE_FAILED,
)
from jelica_core.events.operations import (
    _FAILED_JOB_REASON_DEFINITIONS,
    CoreOperationRuntime,
    _build_runtime_event_callback,
)
from jelica_core.events.sinks import EventSink, EventSinkError
from jelica_core.events.translator import CoreExceptionTranslator
from jelica_core.system_config import CoreConfigService


class _FailingSink(EventSink):
    def __init__(self, *, required: bool) -> None:
        super().__init__(minimum_level=EventType.DEBUG, required=required)

    def _emit(self, event):  # type: ignore[no-untyped-def]
        raise EventSinkError(sink_name="FailingSink", detail="simulated failure")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_jsonl_sink_writes_single_json_line(tmp_path: Path) -> None:
    log_path = tmp_path / "system-events.jsonl"
    sink = JsonlFileEventSink(path=log_path, minimum_level=EventType.INFO, required=True)
    service = EventService(factory=CoreEventFactory(), sinks=[sink])

    service.emit(CORE_ANALYZE_REQUEST_STARTED, execution_context=CoreExecutionContext())

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["name"] == "CORE_ANALYZE_REQUEST_STARTED"


def test_jsonl_sink_writes_multiple_events_sequentially(tmp_path: Path) -> None:
    log_path = tmp_path / "system-events.jsonl"
    sink = JsonlFileEventSink(path=log_path, minimum_level=EventType.INFO, required=True)
    service = EventService(factory=CoreEventFactory(), sinks=[sink])

    service.emit(CORE_ANALYZE_REQUEST_STARTED, execution_context=CoreExecutionContext())
    service.emit(
        CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED,
        execution_context=CoreExecutionContext(),
        message_params={"parameter": "alpha"},
    )

    lines = _read_jsonl(log_path)
    assert len(lines) == 2
    assert lines[0]["name"] == "CORE_ANALYZE_REQUEST_STARTED"
    assert lines[1]["name"] == "CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED"


def test_jsonl_sink_applies_level_filter(tmp_path: Path) -> None:
    log_path = tmp_path / "filtered-events.jsonl"
    sink = JsonlFileEventSink(path=log_path, minimum_level=EventType.WARNING, required=True)
    service = EventService(factory=CoreEventFactory(), sinks=[sink])

    service.emit(CORE_ANALYZE_REQUEST_STARTED, execution_context=CoreExecutionContext())
    service.emit(
        CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED,
        execution_context=CoreExecutionContext(),
        message_params={"parameter": "beta"},
    )

    lines = _read_jsonl(log_path)
    assert len(lines) == 1
    assert lines[0]["name"] == "CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED"


def test_jsonl_sink_stores_utf8_payload(tmp_path: Path) -> None:
    log_path = tmp_path / "utf8-events.jsonl"
    sink = JsonlFileEventSink(path=log_path, minimum_level=EventType.INFO, required=True)
    service = EventService(factory=CoreEventFactory(), sinks=[sink])

    service.emit(
        CORE_ANALYZE_UNKNOWN_PARAMETER_IGNORED,
        execution_context=CoreExecutionContext(),
        message_params={"parameter": "параметр"},
    )

    raw = log_path.read_bytes()
    text = raw.decode("utf-8")
    assert "параметр" in text
    assert len(_read_jsonl(log_path)) == 1


def test_optional_failing_sink_does_not_break_dispatch(tmp_path: Path) -> None:
    log_path = tmp_path / "system-events.jsonl"
    file_sink = JsonlFileEventSink(path=log_path, minimum_level=EventType.INFO, required=True)
    optional_failing_sink = _FailingSink(required=False)
    service = EventService(factory=CoreEventFactory(), sinks=[file_sink, optional_failing_sink])

    service.emit(CORE_ANALYZE_REQUEST_STARTED, execution_context=CoreExecutionContext())

    lines = _read_jsonl(log_path)
    assert len(lines) == 1


def test_mandatory_failing_sink_raises_controlled_error() -> None:
    service = EventService(factory=CoreEventFactory(), sinks=[_FailingSink(required=True)])

    with pytest.raises(MandatoryEventSinkWriteError):
        service.emit(CORE_ANALYZE_REQUEST_STARTED, execution_context=CoreExecutionContext())


def test_run_initialize_analysis_task_creates_system_and_task_logs(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    sample_a = tmp_path / "sample_a.fasta"
    sample_b = tmp_path / "sample_b.fasta"
    sample_a.write_text(">a\nACGT\n", encoding="utf-8")
    sample_b.write_text(">b\nACGG\n", encoding="utf-8")

    service = CoreConfigService(jelica_home=jelica_home)
    service.initialize_system_config()

    result = run_initialize_analysis_task_from_inputs(
        config_json=None,
        raw_overrides=(),
        positional_sources=(str(sample_a), str(sample_b)),
        core_config_service=service,
    )

    assert result.ok is True
    assert result.value is not None
    assert result.system_log_path is not None
    assert result.task_log_path is not None
    assert result.system_log_path.is_file()
    assert result.task_log_path.is_file()

    system_events = _read_jsonl(result.system_log_path)
    task_events = _read_jsonl(result.task_log_path)

    assert any(event["name"] == "CORE_SYSTEM_CONFIG_LOADED" for event in system_events)
    assert any(event["name"] == CORE_ANALYTICAL_TASK_REGISTERED.name for event in task_events)
    assert any(event["name"] == "CORE_ANALYZE_TASK_INITIALIZED" for event in task_events)
    assert all("schema_version" in event for event in system_events)
    assert result.value.config.trace_id is not None
    assert result.event is not None
    assert result.event.trace_id == result.value.config.trace_id
    assert all(
        event.get("trace_id") == str(result.value.config.trace_id)
        for event in task_events
    )

    registration_events = [
        event for event in task_events if event["name"] == CORE_ANALYTICAL_TASK_REGISTERED.name
    ]
    assert len(registration_events) == 1
    registration_event = registration_events[0]
    context = registration_event.get("context")
    assert isinstance(context, dict)
    assert registration_event["task_id"] == result.value.task_id
    assert context["state"] == "waiting"
    assert context["default_priority"] == 1
    assert context["task_dir_relative_path"] == result.value.task_id
    assert context["record_version"] == 1


def test_in_memory_sink_collects_events() -> None:
    sink = InMemoryEventSink(minimum_level=EventType.DEBUG, required=False)
    service = EventService(factory=CoreEventFactory(), sinks=[sink])

    service.emit(CORE_ANALYZE_REQUEST_STARTED, execution_context=CoreExecutionContext())

    assert len(sink.events) == 1


def test_command_id_context_is_propagated_and_reset() -> None:
    command_id = UUID("e292680b-3fb2-4494-a33f-47629da96f84")
    sink = InMemoryEventSink(minimum_level=EventType.DEBUG, required=False)
    service = EventService(factory=CoreEventFactory(), sinks=[sink])

    token = set_command_id(command_id)
    try:
        assert current_command_id() == command_id
        service.emit(CORE_ANALYZE_REQUEST_STARTED)
    finally:
        reset_command_id(token)

    service.emit(CORE_ANALYZE_REQUEST_STARTED)

    assert sink.events[0].command_id == command_id
    assert sink.events[1].command_id is None
    assert current_command_id() is None


def test_runtime_callback_persists_registered_alignment_events(tmp_path: Path) -> None:
    alignment_events = (
        ("ALIGNMENT_STARTED", "CORE_ALIGNMENT_STARTED"),
        ("ALIGNMENT_SKIPPED", "CORE_ALIGNMENT_SKIPPED"),
        (
            "ALIGNMENT_PREALIGNED_VALIDATION_STARTED",
            "CORE_ALIGNMENT_PREALIGNED_VALIDATION_STARTED",
        ),
        ("ALIGNMENT_MAFFT_PROBED", "CORE_ALIGNMENT_MAFFT_AVAILABILITY_CONFIRMED"),
        ("ALIGNMENT_MAFFT_LAUNCHED", "CORE_ALIGNMENT_MAFFT_PROCESS_STARTED"),
        ("ALIGNMENT_MAFFT_COMPLETED", "CORE_ALIGNMENT_MAFFT_PROCESS_COMPLETED"),
        ("ALIGNMENT_MAFFT_FAILED", "CORE_ALIGNMENT_MAFFT_PROCESS_FAILED"),
        ("ALIGNMENT_MAFFT_STOPPED_PAUSE", "CORE_ALIGNMENT_MAFFT_STOPPED_FOR_PAUSE"),
        ("ALIGNMENT_MAFFT_STOPPED_CANCEL", "CORE_ALIGNMENT_MAFFT_STOPPED_FOR_CANCEL"),
        (
            "ALIGNMENT_MAFFT_STOPPED_SHUTDOWN",
            "CORE_ALIGNMENT_MAFFT_STOPPED_FOR_SHUTDOWN",
        ),
        ("ALIGNMENT_RESULT_INVALID", "CORE_ALIGNMENT_RESULT_VALIDATION_FAILED"),
        ("ALIGNMENT_RESULT_PUBLISHED", "CORE_ALIGNMENT_RESULT_PUBLISHED"),
        ("ALIGNMENT_COMPLETED", "CORE_ALIGNMENT_COMPLETED"),
    )
    log_path = tmp_path / "alignment-events.jsonl"
    event_service = EventService(
        factory=CoreEventFactory(),
        sinks=[
            JsonlFileEventSink(
                path=log_path,
                minimum_level=EventType.DEBUG,
                required=True,
            )
        ],
    )
    runtime = CoreOperationRuntime(
        event_service=event_service,
        translator=CoreExceptionTranslator(
            include_diagnostics=False,
            diagnostic_field_limit=256,
        ),
        system_log_path=log_path,
    )
    callback = _build_runtime_event_callback(
        runtime=runtime,
        execution_context=CoreExecutionContext(
            stage="alignment",
            operation_id="test.alignment_events",
        ),
    )

    for runtime_name, core_name in alignment_events:
        CORE_EVENT_CATALOG.get(core_name)
        callback(
            runtime_name,
            {
                "task_id": "task-test",
                "job_id": "job-test",
                "stage_id": "alignment",
                "detail": "Safe alignment lifecycle status.",
            },
        )

    records = _read_jsonl(log_path)
    assert [record["name"] for record in records] == [
        core_name for _, core_name in alignment_events
    ]
    assert all(record["task_id"] == "task-test" for record in records)
    assert all(record["stage"] == "alignment" for record in records)
    assert all(
        isinstance(record.get("context"), dict)
        and record["context"]["stage_id"] == "alignment"  # type: ignore[index]
        for record in records
    )


def test_runtime_callback_persists_registered_comparative_analysis_events(
    tmp_path: Path,
) -> None:
    comparative_events = (
        ("COMPARATIVE_ANALYSIS_STARTED", "CORE_COMPARATIVE_ANALYSIS_STARTED"),
        ("COMPARATIVE_ANALYSIS_SKIPPED", "CORE_COMPARATIVE_ANALYSIS_SKIPPED"),
        (
            "COMPARATIVE_ANALYSIS_PHASE_STARTED",
            "CORE_COMPARATIVE_ANALYSIS_PHASE_STARTED",
        ),
        ("COMPARATIVE_ANALYSIS_PROGRESS", "CORE_COMPARATIVE_ANALYSIS_PROGRESS"),
        (
            "COMPARATIVE_ANALYSIS_OPERATION_FAILED",
            "CORE_COMPARATIVE_ANALYSIS_OPERATION_FAILED",
        ),
        (
            "COMPARATIVE_ANALYSIS_RESULT_PUBLISHED",
            "CORE_COMPARATIVE_ANALYSIS_RESULT_PUBLISHED",
        ),
        ("COMPARATIVE_ANALYSIS_COMPLETED", "CORE_COMPARATIVE_ANALYSIS_COMPLETED"),
        (
            "COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS",
            "CORE_COMPARATIVE_ANALYSIS_PARTIAL_SUCCESS",
        ),
        ("COMPARATIVE_ANALYSIS_FAILED", "CORE_COMPARATIVE_ANALYSIS_FAILED"),
    )
    log_path = tmp_path / "comparative-events.jsonl"
    event_service = EventService(
        factory=CoreEventFactory(),
        sinks=[
            JsonlFileEventSink(
                path=log_path,
                minimum_level=EventType.DEBUG,
                required=True,
            )
        ],
    )
    runtime = CoreOperationRuntime(
        event_service=event_service,
        translator=CoreExceptionTranslator(
            include_diagnostics=False,
            diagnostic_field_limit=256,
        ),
        system_log_path=log_path,
    )
    callback = _build_runtime_event_callback(
        runtime=runtime,
        execution_context=CoreExecutionContext(
            stage="comparative_analysis",
            operation_id="test.comparative_events",
        ),
    )

    for runtime_name, core_name in comparative_events:
        CORE_EVENT_CATALOG.get(core_name)
        callback(
            runtime_name,
            {
                "task_id": "task-test",
                "job_id": "job-test",
                "stage_id": "comparative_analysis",
                "detail": "Safe comparative lifecycle status.",
            },
        )

    records = _read_jsonl(log_path)
    assert [record["name"] for record in records] == [
        core_name for _, core_name in comparative_events
    ]
    assert all(record["task_id"] == "task-test" for record in records)


def test_comparative_failure_event_name_resolves_structured_definition() -> None:
    assert (
        _FAILED_JOB_REASON_DEFINITIONS["COMPARATIVE_ANALYSIS_FAILED"]
        is CORE_COMPARATIVE_ANALYSIS_FAILED
    )


def test_runtime_callback_persists_registered_distance_matrix_events(
    tmp_path: Path,
) -> None:
    distance_events = (
        ("DISTANCE_MATRIX_STARTED", "CORE_DISTANCE_MATRIX_STARTED"),
        ("DISTANCE_MATRIX_SKIPPED", "CORE_DISTANCE_MATRIX_SKIPPED"),
        ("DISTANCE_MATRIX_PROGRESS", "CORE_DISTANCE_MATRIX_PROGRESS"),
        (
            "DISTANCE_MATRIX_RESULT_PUBLISHED",
            "CORE_DISTANCE_MATRIX_RESULT_PUBLISHED",
        ),
        ("DISTANCE_MATRIX_COMPLETED", "CORE_DISTANCE_MATRIX_COMPLETED"),
        (
            "DISTANCE_MATRIX_PARTIAL_SUCCESS",
            "CORE_DISTANCE_MATRIX_PARTIAL_SUCCESS",
        ),
        ("DISTANCE_MATRIX_FAILED", "CORE_DISTANCE_MATRIX_FAILED"),
    )
    log_path = tmp_path / "distance-events.jsonl"
    event_service = EventService(
        factory=CoreEventFactory(),
        sinks=[
            JsonlFileEventSink(
                path=log_path,
                minimum_level=EventType.DEBUG,
                required=True,
            )
        ],
    )
    runtime = CoreOperationRuntime(
        event_service=event_service,
        translator=CoreExceptionTranslator(
            include_diagnostics=False,
            diagnostic_field_limit=256,
        ),
        system_log_path=log_path,
    )
    callback = _build_runtime_event_callback(
        runtime=runtime,
        execution_context=CoreExecutionContext(
            stage="distance_matrix",
            operation_id="test.distance_events",
        ),
    )

    for runtime_name, core_name in distance_events:
        CORE_EVENT_CATALOG.get(core_name)
        callback(
            runtime_name,
            {
                "task_id": "task-test",
                "job_id": "job-test",
                "stage_id": "distance_matrix",
                "detail": "Safe distance-matrix lifecycle status.",
            },
        )

    records = _read_jsonl(log_path)
    assert [record["name"] for record in records] == [
        core_name for _, core_name in distance_events
    ]
    assert all(record["task_id"] == "task-test" for record in records)


def test_distance_failure_event_name_resolves_structured_definition() -> None:
    assert _FAILED_JOB_REASON_DEFINITIONS["DISTANCE_MATRIX_FAILED"] is CORE_DISTANCE_MATRIX_FAILED


def test_runtime_callback_persists_registered_phylogenetic_tree_events(
    tmp_path: Path,
) -> None:
    tree_events = (
        ("PHYLOGENETIC_TREE_STARTED", "CORE_PHYLOGENETIC_TREE_STARTED"),
        ("PHYLOGENETIC_TREE_SKIPPED", "CORE_PHYLOGENETIC_TREE_SKIPPED"),
        ("PHYLOGENETIC_TREE_PROGRESS", "CORE_PHYLOGENETIC_TREE_PROGRESS"),
        (
            "PHYLOGENETIC_TREE_RESULT_PUBLISHED",
            "CORE_PHYLOGENETIC_TREE_RESULT_PUBLISHED",
        ),
        ("PHYLOGENETIC_TREE_COMPLETED", "CORE_PHYLOGENETIC_TREE_COMPLETED"),
        ("PHYLOGENETIC_TREE_FAILED", "CORE_PHYLOGENETIC_TREE_FAILED"),
    )
    log_path = tmp_path / "phylogenetic-events.jsonl"
    event_service = EventService(
        factory=CoreEventFactory(),
        sinks=[
            JsonlFileEventSink(
                path=log_path,
                minimum_level=EventType.DEBUG,
                required=True,
            )
        ],
    )
    runtime = CoreOperationRuntime(
        event_service=event_service,
        translator=CoreExceptionTranslator(
            include_diagnostics=False,
            diagnostic_field_limit=256,
        ),
        system_log_path=log_path,
    )
    callback = _build_runtime_event_callback(
        runtime=runtime,
        execution_context=CoreExecutionContext(
            stage="phylogenetic_tree",
            operation_id="test.phylogenetic_tree_events",
        ),
    )

    for runtime_name, core_name in tree_events:
        CORE_EVENT_CATALOG.get(core_name)
        callback(
            runtime_name,
            {
                "task_id": "task-test",
                "job_id": "job-test",
                "stage_id": "phylogenetic_tree",
                "detail": "Safe phylogenetic-tree lifecycle status.",
            },
        )

    records = _read_jsonl(log_path)
    assert [record["name"] for record in records] == [core_name for _, core_name in tree_events]
    assert all(record["task_id"] == "task-test" for record in records)


def test_phylogenetic_tree_failure_event_name_resolves_structured_definition() -> None:
    assert (
        _FAILED_JOB_REASON_DEFINITIONS["PHYLOGENETIC_TREE_FAILED"]
        is CORE_PHYLOGENETIC_TREE_FAILED
    )


def test_runtime_callback_persists_registered_clade_detection_events(
    tmp_path: Path,
) -> None:
    clade_events = (
        ("CLADE_DETECTION_STARTED", "CORE_CLADE_DETECTION_STARTED"),
        ("CLADE_DETECTION_SKIPPED", "CORE_CLADE_DETECTION_SKIPPED"),
        ("CLADE_DETECTION_PROGRESS", "CORE_CLADE_DETECTION_PROGRESS"),
        (
            "CLADE_DETECTION_RESULT_PUBLISHED",
            "CORE_CLADE_DETECTION_RESULT_PUBLISHED",
        ),
        ("CLADE_DETECTION_COMPLETED", "CORE_CLADE_DETECTION_COMPLETED"),
        ("CLADE_DETECTION_FAILED", "CORE_CLADE_DETECTION_FAILED"),
    )
    log_path = tmp_path / "clade-events.jsonl"
    event_service = EventService(
        factory=CoreEventFactory(),
        sinks=[
            JsonlFileEventSink(
                path=log_path,
                minimum_level=EventType.DEBUG,
                required=True,
            )
        ],
    )
    runtime = CoreOperationRuntime(
        event_service=event_service,
        translator=CoreExceptionTranslator(
            include_diagnostics=False,
            diagnostic_field_limit=256,
        ),
        system_log_path=log_path,
    )
    callback = _build_runtime_event_callback(
        runtime=runtime,
        execution_context=CoreExecutionContext(
            stage="clade_detection",
            operation_id="test.clade_detection_events",
        ),
    )

    for runtime_name, core_name in clade_events:
        CORE_EVENT_CATALOG.get(core_name)
        callback(
            runtime_name,
            {
                "task_id": "task-test",
                "job_id": "job-test",
                "stage_id": "clade_detection",
                "detail": "Safe clade-detection lifecycle status.",
            },
        )

    records = _read_jsonl(log_path)
    assert [record["name"] for record in records] == [core_name for _, core_name in clade_events]
    assert all(record["task_id"] == "task-test" for record in records)


def test_clade_detection_failure_event_name_resolves_structured_definition() -> None:
    assert (
        _FAILED_JOB_REASON_DEFINITIONS["CLADE_DETECTION_FAILED"]
        is CORE_CLADE_DETECTION_FAILED
    )
