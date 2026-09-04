from __future__ import annotations

from datetime import UTC

from jelica_core.events.context import CoreExecutionContext
from jelica_core.events.definitions import (
    CORE_ANALYZE_TASK_INITIALIZED,
    CORE_SYSTEM_CONFIG_INITIALIZED,
)
from jelica_core.events.factory import CoreEventFactory


def test_factory_generates_event_id_and_timestamp() -> None:
    factory = CoreEventFactory()

    first = factory.create(
        CORE_SYSTEM_CONFIG_INITIALIZED,
        message_params={"config_path": "/tmp/config.toml"},
    )
    second = factory.create(
        CORE_SYSTEM_CONFIG_INITIALIZED,
        message_params={"config_path": "/tmp/config.toml"},
    )

    assert first.event_id != second.event_id
    assert first.timestamp.tzinfo == UTC
    assert first.message == "System config initialized at '/tmp/config.toml'."


def test_factory_applies_execution_context_fields() -> None:
    factory = CoreEventFactory()
    context = CoreExecutionContext(
        task_id="task-1",
        run_id="run-1",
        stage="initialization",
        worker_id="worker-1",
        attempt=2,
        operation_id="op-1",
    )

    event = factory.create(
        CORE_ANALYZE_TASK_INITIALIZED,
        execution_context=context,
        message_params={"task_id": "task-1"},
        context={"source": "sample.fasta"},
    )

    assert event.task_id == "task-1"
    assert event.run_id == "run-1"
    assert event.stage == "initialization"
    assert event.worker_id == "worker-1"
    assert event.attempt == 2
    assert event.operation_id == "op-1"
    assert event.context == {
        "source": "sample.fasta",
        "execution": {"attempt": 2, "operation_id": "op-1"},
    }


def test_factory_creates_event_without_task_context() -> None:
    factory = CoreEventFactory()

    event = factory.create(
        CORE_SYSTEM_CONFIG_INITIALIZED,
        execution_context=CoreExecutionContext(),
        message_params={"config_path": "/tmp/config.toml"},
    )

    assert event.task_id is None
    assert event.run_id is None
    assert event.context is None
