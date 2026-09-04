from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from jelica_core.analysis.errors import AnalysisTaskWorkspaceCompensationError
from jelica_core.events import CoreExceptionTranslator
from jelica_core.events.definitions import CORE_ANALYZE_TASK_CONFIG_INVALID
from jelica_core.events.structured_errors import CoreTaskConfigError
from jelica_core.tasks import (
    AnalyticalTaskAlreadyExistsError,
    AnalyticalTaskInvalidRecordDataError,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryDatabaseCorruptedError,
    AnalyticalTaskRegistryDatabaseUnavailableError,
    AnalyticalTaskRegistryForeignDatabaseError,
)


class _StrictPayload(BaseModel):
    count: int


def _build_translator(*, include_diagnostics: bool = False) -> CoreExceptionTranslator:
    return CoreExceptionTranslator(
        include_diagnostics=include_diagnostics,
        diagnostic_field_limit=4_096,
    )


def test_translator_maps_json_parse_error() -> None:
    translator = _build_translator()
    try:
        json.loads('{"samples": [}')
    except json.JSONDecodeError as error:
        public_error = translator.to_public_error(error)
    else:
        pytest.fail("Expected JSONDecodeError.")

    assert public_error.event.code == 2010
    assert public_error.event.name == "CORE_ANALYZE_TASK_CONFIG_INVALID"


def test_translator_maps_pydantic_validation_error() -> None:
    translator = _build_translator()
    try:
        _StrictPayload.model_validate({"count": "invalid"})
    except ValidationError as error:
        public_error = translator.to_public_error(error)
    else:
        pytest.fail("Expected ValidationError.")

    assert public_error.event.code == 2010
    assert public_error.expected is True


def test_translator_maps_file_not_found_error() -> None:
    translator = _build_translator()
    error = FileNotFoundError(errno.ENOENT, "missing", "sample.fasta")

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2006
    assert public_error.event.name == "CORE_ANALYZE_SOURCE_NOT_FOUND"
    assert public_error.safe_details == {"source": "sample.fasta"}


def test_translator_maps_permission_error() -> None:
    translator = _build_translator()
    error = PermissionError(errno.EACCES, "denied", "sample.fasta")

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2007
    assert public_error.event.name == "CORE_ANALYZE_SOURCE_UNAVAILABLE"


def test_translator_maps_internal_structured_error() -> None:
    translator = _build_translator()
    error = CoreTaskConfigError(
        definition=CORE_ANALYZE_TASK_CONFIG_INVALID,
        message="invalid",
        message_params={"detail": "invalid schema"},
    )

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2010
    assert public_error.event.message.endswith("invalid schema")


def test_translator_maps_unknown_exception_to_stable_internal_error() -> None:
    translator = _build_translator()
    try:
        raise RuntimeError("very sensitive detail")
    except RuntimeError as error:
        public_error = translator.to_public_error(error)
    else:
        pytest.fail("Expected RuntimeError.")

    assert public_error.event.code == 2011
    assert public_error.event.name == "CORE_INTERNAL_UNEXPECTED_ERROR"
    assert public_error.expected is False
    assert "very sensitive detail" not in public_error.event.message


def test_translator_keeps_traceback_for_unexpected_error() -> None:
    translator = _build_translator()
    try:
        raise RuntimeError("boom")
    except RuntimeError as error:
        public_error = translator.to_public_error(error)
    else:
        pytest.fail("Expected RuntimeError.")

    assert public_error.event.diagnostics is not None
    assert public_error.event.diagnostics.traceback is not None
    assert public_error.event.diagnostics.source_exception_type == "RuntimeError"


def test_public_error_default_view_hides_traceback() -> None:
    translator = _build_translator()
    try:
        raise RuntimeError("boom")
    except RuntimeError as error:
        public_error = translator.to_public_error(error)
    else:
        pytest.fail("Expected RuntimeError.")

    assert "diagnostics" not in public_error.to_dict()["event"]


def test_translator_maps_registry_database_unavailable_error() -> None:
    translator = _build_translator()
    error = AnalyticalTaskRegistryDatabaseUnavailableError(
        database_path=Path("/tmp/jelica.db"),
        detail="permission denied",
        sqlite_exception_type="OperationalError",
    )

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2202
    assert public_error.event.name == "CORE_TASK_REGISTRY_DATABASE_UNAVAILABLE"


def test_translator_maps_registry_database_corrupted_error() -> None:
    translator = _build_translator()
    error = AnalyticalTaskRegistryDatabaseCorruptedError(
        database_path=Path("/tmp/jelica.db"),
        detail="file is not a database",
        sqlite_exception_type="DatabaseError",
    )

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2203
    assert public_error.event.name == "CORE_TASK_REGISTRY_DATABASE_CORRUPTED"


def test_translator_maps_registry_foreign_database_error() -> None:
    translator = _build_translator()
    error = AnalyticalTaskRegistryForeignDatabaseError(
        database_path=Path("/tmp/jelica.db"),
        application_id=123,
    )

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2204
    assert public_error.event.name == "CORE_TASK_REGISTRY_FOREIGN_DATABASE"


def test_translator_maps_analytical_task_not_found_error() -> None:
    translator = _build_translator()
    error = AnalyticalTaskNotFoundError(task_id="task-42")

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2210
    assert public_error.event.name == "CORE_ANALYTICAL_TASK_NOT_FOUND"
    assert public_error.safe_details == {"task_id": "task-42"}


def test_translator_maps_analytical_task_already_exists_error() -> None:
    translator = _build_translator()
    error = AnalyticalTaskAlreadyExistsError(field_name="task_id", field_value="task-42")

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2211
    assert public_error.event.name == "CORE_ANALYTICAL_TASK_ALREADY_EXISTS"


def test_translator_maps_analytical_task_invalid_request_error() -> None:
    translator = _build_translator()
    error = AnalyticalTaskInvalidRecordDataError(detail="limit must be > 0")

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2212
    assert public_error.event.name == "CORE_ANALYTICAL_TASK_REQUEST_INVALID"


def test_translator_maps_workspace_compensation_error() -> None:
    translator = _build_translator()
    original_error = RuntimeError("registration failed")
    cleanup_error = OSError("cleanup failed")
    error = AnalysisTaskWorkspaceCompensationError(
        task_id="task-42",
        task_dir=Path("/tmp/task-42"),
        original_error=original_error,
        cleanup_error=cleanup_error,
    )

    public_error = translator.to_public_error(error)

    assert public_error.event.code == 2213
    assert public_error.event.name == "CORE_ANALYZE_TASK_WORKSPACE_COMPENSATION_FAILED"
    assert public_error.event.diagnostics is None
    assert public_error.safe_details is not None
    assert public_error.safe_details["task_id"] == "task-42"
    assert public_error.safe_details["original_exception_type"] == "RuntimeError"
    assert public_error.safe_details["cleanup_exception_type"] == "OSError"
