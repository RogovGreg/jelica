from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

import pytest
import tomli_w

import jelica_core.system_config.resources as system_config_resources
from jelica_core.config import AnalysisConfigInput, resolve_analysis_config
from jelica_core.runtime import RuntimeConfig, WorkerLaunchSpec
from jelica_core.system_config import (
    CoreConfigInput,
    CoreConfigInvalidRootTypeError,
    CoreConfigInvalidTomlError,
    CoreConfigInvalidValueError,
    CoreConfigLoader,
    CoreConfigMissingError,
    CoreConfigMissingFieldError,
    CoreConfigParameterAlreadyUnsetError,
    CoreConfigParameterNotMutableError,
    CoreConfigParameterNotRemovableError,
    CoreConfigPathResolutionError,
    CoreConfigService,
    CoreConfigUnknownFieldError,
    CoreConfigUnknownParameterError,
    CoreConfigValidationError,
    CoreConfigWriteError,
    CoreWorkingDirectoryCreationError,
    UnsupportedCoreConfigSchemaVersionError,
    build_default_core_config_document,
    core_config_field_paths,
    core_config_top_level_keys,
)
from jelica_core.system_config.writer import CoreConfigWriter
from jelica_core.tasks import (
    TASK_REGISTRY_APPLICATION_ID,
    TASK_REGISTRY_SCHEMA_VERSION,
    AnalyticalTaskRegistryDatabaseUnavailableError,
    AnalyticalTaskRegistryService,
    LocalTaskStorage,
)


def _service_with_home(jelica_home: Path) -> CoreConfigService:
    return CoreConfigService(jelica_home=jelica_home)


@pytest.fixture(autouse=True)
def _stable_available_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jelica_core.system_config.resolver.detect_available_logical_cpu_count",
        lambda: 8,
    )


def _write_config_text(jelica_home: Path, text: str) -> Path:
    config_path = _service_with_home(jelica_home).get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
    return config_path


def _write_config_document(jelica_home: Path, document: dict[str, object]) -> Path:
    return _write_config_text(jelica_home, tomli_w.dumps(document))


def _set_document_value(
    document: dict[str, object],
    *,
    field_path: str,
    value: object,
) -> None:
    components = field_path.split(".")
    current = document
    for component in components[:-1]:
        nested = current[component]
        assert isinstance(nested, dict)
        current = nested
    current[components[-1]] = value


def _delete_document_value(document: dict[str, object], *, field_path: str) -> None:
    components = field_path.split(".")
    current = document
    for component in components[:-1]:
        nested = current[component]
        assert isinstance(nested, dict)
        current = nested
    del current[components[-1]]


def _document_leaf_paths(document: dict[str, object], *, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for key, value in document.items():
        field_path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            paths.update(_document_leaf_paths(value, prefix=field_path))
        else:
            paths.add(field_path)
    return paths


def test_jelica_home_is_resolved_from_environment(tmp_path: Path) -> None:
    env_home = (tmp_path / "env-home").resolve()
    service = CoreConfigService(environment={"JELICA_HOME": str(env_home)})

    assert service.get_jelica_home() == env_home
    assert service.get_config_path() == env_home / "config.toml"


def test_jelica_home_falls_back_to_platform_directory(tmp_path: Path) -> None:
    platform_home = (tmp_path / "platform-home").resolve()
    service = CoreConfigService(
        environment={},
        platform_home_resolver=lambda: platform_home,
    )

    assert service.get_jelica_home() == platform_home


def test_jelica_home_path_resolution_is_independent_of_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jelica_home = (tmp_path / "home").resolve()
    service = CoreConfigService(environment={"JELICA_HOME": str(jelica_home)})
    different_cwd = tmp_path / "different-cwd"
    different_cwd.mkdir(parents=True)
    monkeypatch.chdir(different_cwd)

    assert service.get_jelica_home() == jelica_home


def test_config_path_is_available_before_initialization(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")

    config_path = service.get_config_path()

    assert config_path == (tmp_path / "home" / "config.toml")
    assert not config_path.exists()


def test_relative_jelica_home_env_path_is_rejected() -> None:
    service = CoreConfigService(environment={"JELICA_HOME": "relative/home"})

    with pytest.raises(CoreConfigPathResolutionError):
        service.get_jelica_home()


def test_initialize_system_config_with_defaults(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")

    resolved = service.initialize_system_config()
    config_path = service.get_config_path()
    config_text = config_path.read_text(encoding="utf-8")
    config_document = tomllib.loads(config_text)

    assert resolved.data_dir == tmp_path / "home" / "data"
    assert resolved.tasks_dir == tmp_path / "home" / "data" / "tasks"
    assert resolved.temp_dir == tmp_path / "home" / "data" / "temp"
    assert resolved.logs_dir == tmp_path / "home" / "data" / "logs"
    assert resolved.database_path == tmp_path / "home" / "data" / "jelica.db"
    assert resolved.max_workers == 1
    assert resolved.max_parallel_tasks == 1
    assert resolved.scheduler_poll_interval_seconds == 0.25
    assert resolved.heartbeat_interval_seconds == 1.0
    assert resolved.lease_timeout_seconds == 5.0
    assert resolved.progress_flush_interval_seconds == 1.0
    assert resolved.max_recovery_attempts == 3
    assert resolved.log_level == "INFO"
    assert resolved.system_log_level == "INFO"
    assert resolved.task_log_level == "INFO"
    assert resolved.include_diagnostics is False
    assert resolved.diagnostic_field_limit == 8_192
    assert resolved.input_directory_max_depth == 3
    assert resolved.ncbi_api_key == ""
    assert resolved.ncbi_max_retries == 3
    assert resolved.default_alignment_mode == "compute"
    assert resolved.mafft_executable is None
    assert resolved.data_dir.is_dir()
    assert resolved.tasks_dir.is_dir()
    assert resolved.temp_dir.is_dir()
    assert resolved.logs_dir.is_dir()
    assert resolved.database_path.is_file()
    assert config_text.endswith("\n")
    assert tuple(config_document) == core_config_top_level_keys()
    assert _document_leaf_paths(config_document) == set(core_config_field_paths())
    assert config_document["logging"] == {
        "level": "INFO",
        "system_level": "",
        "task_level": "",
        "include_diagnostics": False,
        "diagnostic_field_limit": 8_192,
    }
    assert config_document["tools"] == {"mafft": {"executable": ""}}


def test_initialize_creates_task_registry_database_with_expected_identity(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")

    resolved = service.initialize_system_config()
    connection = sqlite3.connect(resolved.database_path)
    try:
        app_id_row = connection.execute("PRAGMA application_id").fetchone()
        user_version_row = connection.execute("PRAGMA user_version").fetchone()
    finally:
        connection.close()

    assert app_id_row is not None
    assert user_version_row is not None
    assert int(app_id_row[0]) == TASK_REGISTRY_APPLICATION_ID
    assert int(user_version_row[0]) == TASK_REGISTRY_SCHEMA_VERSION


def test_initialize_system_config_with_explicit_values(tmp_path: Path) -> None:
    custom_data_dir = tmp_path / "external-data"
    service = _service_with_home(tmp_path / "home")

    resolved = service.initialize_system_config(
        data_directory=str(custom_data_dir),
        max_workers=4,
        log_level="warning",
    )

    assert resolved.data_dir == custom_data_dir
    assert resolved.max_workers == 4
    assert resolved.log_level == "WARNING"
    assert resolved.tasks_dir == custom_data_dir / "tasks"


def test_initialize_reuses_existing_valid_config_and_existing_database(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    initial = service.initialize_system_config(max_workers=2, log_level="warning")
    config_path = service.get_config_path()
    before = config_path.read_text(encoding="utf-8")

    repeated = service.initialize_system_config(
        data_directory="ignored-data",
        max_workers=9,
        log_level="error",
    )
    after = config_path.read_text(encoding="utf-8")

    assert repeated.max_workers == initial.max_workers
    assert repeated.log_level == initial.log_level
    assert repeated.data_dir == initial.data_dir
    assert repeated.database_path == initial.database_path
    assert repeated.database_path.is_file()
    assert before == after


def test_initialize_reuses_existing_valid_config_and_recreates_missing_database(
    tmp_path: Path,
) -> None:
    service = _service_with_home(tmp_path / "home")
    initial = service.initialize_system_config(max_workers=3)
    config_path = service.get_config_path()
    before = config_path.read_text(encoding="utf-8")

    initial.database_path.unlink()
    assert not initial.database_path.exists()

    recreated = service.initialize_system_config()
    after = config_path.read_text(encoding="utf-8")

    assert recreated.database_path == initial.database_path
    assert recreated.database_path.is_file()
    assert recreated.max_workers == initial.max_workers
    assert before == after

    connection = sqlite3.connect(recreated.database_path)
    try:
        app_id_row = connection.execute("PRAGMA application_id").fetchone()
        user_version_row = connection.execute("PRAGMA user_version").fetchone()
    finally:
        connection.close()

    assert app_id_row is not None
    assert user_version_row is not None
    assert int(app_id_row[0]) == TASK_REGISTRY_APPLICATION_ID
    assert int(user_version_row[0]) == TASK_REGISTRY_SCHEMA_VERSION


def test_initialize_rejects_existing_invalid_toml_and_keeps_filesystem_unchanged(
    tmp_path: Path,
) -> None:
    service = _service_with_home(tmp_path / "home")
    config_path = _write_config_text(tmp_path / "home", "schema_version =\n")
    before = config_path.read_text(encoding="utf-8")
    expected_database_path = tmp_path / "home" / "data" / "jelica.db"

    with pytest.raises(CoreConfigInvalidTomlError):
        service.initialize_system_config()

    after = config_path.read_text(encoding="utf-8")
    assert before == after
    assert not expected_database_path.exists()


def test_initialize_is_idempotent_on_repeated_calls(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    first = service.initialize_system_config()
    second = service.initialize_system_config()
    third = service.initialize_system_config()

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert second.model_dump(mode="json") == third.model_dump(mode="json")


def test_initialize_force_preserves_existing_registry_records(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()
    resolved = service.load_resolved_config()
    registry_service = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    config = resolve_analysis_config(AnalysisConfigInput(samples=["sample-a.fasta"])).config
    workspace = LocalTaskStorage(tasks_dir=resolved.tasks_dir).create_task_workspace(
        task_id="task-1",
        config=config,
    )
    created = registry_service.register_task(
        task_id="task-1",
        task_dir_relative_path="task-1",
        default_priority=3,
        current_config_revision=workspace.current_config_revision,
        current_config_relative_path=workspace.current_config_relative_path,
        current_config_hash=workspace.current_config_hash,
    )

    service.initialize_system_config(force=True, max_workers=2)
    reloaded_config = service.load_resolved_config()
    reloaded = AnalyticalTaskRegistryService(database_path=resolved.database_path).get_task(
        task_id="task-1"
    )

    assert reloaded.task_id == created.task_id
    assert reloaded.default_priority == 3
    assert reloaded_config.max_workers == 1


def test_initialize_does_not_delete_existing_user_data(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    existing_file = tmp_path / "home" / "data" / "tasks" / "existing-task" / "config.json"
    existing_file.parent.mkdir(parents=True, exist_ok=True)
    existing_file.write_text('{"schema_version":1}', encoding="utf-8")

    service.initialize_system_config()

    assert existing_file.exists()


def test_load_resolved_config_raises_missing_when_config_absent(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")

    with pytest.raises(CoreConfigMissingError):
        service.load_resolved_config()


def test_validate_rejects_missing_defaulted_field_in_manually_edited_config(
    tmp_path: Path,
) -> None:
    service = _service_with_home(tmp_path / "home")
    document = build_default_core_config_document()
    _delete_document_value(document, field_path="logging.include_diagnostics")
    _write_config_document(tmp_path / "home", document)

    with pytest.raises(CoreConfigMissingFieldError) as exc_info:
        service.validate_current_config()

    assert exc_info.value.field_path == "logging.include_diagnostics"


def test_validate_rejects_missing_task_registry_database(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()
    database_path = service.load_resolved_config().database_path
    database_path.unlink()

    with pytest.raises(AnalyticalTaskRegistryDatabaseUnavailableError):
        service.validate_current_config()


def test_validate_rejects_invalid_toml_syntax(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    _write_config_text(tmp_path / "home", "schema_version =\n")

    with pytest.raises(CoreConfigInvalidTomlError):
        service.validate_current_config()


def test_loader_rejects_non_object_root_data() -> None:
    loader = CoreConfigLoader()

    with pytest.raises(CoreConfigInvalidRootTypeError):
        loader.load_from_mapping(data=["not", "an", "object"])


def test_validate_rejects_unknown_top_level_section(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    document = build_default_core_config_document()
    document["unknown"] = {"flag": True}
    _write_config_document(tmp_path / "home", document)

    with pytest.raises(CoreConfigUnknownFieldError) as exc_info:
        service.validate_current_config()

    assert exc_info.value.field_path == "unknown"


def test_core_loader_rejects_cli_namespace_when_given_combined_document() -> None:
    document = build_default_core_config_document()
    document["cli"] = {"color": True, "emoji": True}

    with pytest.raises(CoreConfigUnknownFieldError) as exc_info:
        CoreConfigLoader().load_from_mapping(data=document)

    assert exc_info.value.field_path == "cli"


def test_validate_rejects_unknown_nested_field(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    document = build_default_core_config_document()
    execution = document["execution"]
    assert isinstance(execution, dict)
    execution["extra"] = True
    _write_config_document(tmp_path / "home", document)

    with pytest.raises(CoreConfigUnknownFieldError) as exc_info:
        service.validate_current_config()

    assert exc_info.value.field_path == "execution.extra"


def test_validate_rejects_missing_schema_version(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    document = build_default_core_config_document()
    del document["schema_version"]
    _write_config_document(tmp_path / "home", document)

    with pytest.raises(CoreConfigMissingFieldError) as exc_info:
        service.validate_current_config()

    assert exc_info.value.field_path == "schema_version"


@pytest.mark.parametrize(
    ("field_path", "expected_missing_path"),
    [
        ("data", "data"),
        ("ncbi_api_key", "ncbi_api_key"),
        ("execution.max_parallel_tasks", "execution.max_parallel_tasks"),
        (
            "execution.scheduler_poll_interval_seconds",
            "execution.scheduler_poll_interval_seconds",
        ),
        ("logging", "logging"),
        ("logging.include_diagnostics", "logging.include_diagnostics"),
        ("tools.mafft", "tools.mafft"),
        ("tools.mafft.executable", "tools.mafft.executable"),
    ],
)
def test_loader_rejects_missing_required_sections_and_scalars(
    field_path: str,
    expected_missing_path: str,
) -> None:
    document = build_default_core_config_document()
    _delete_document_value(document, field_path=field_path)

    with pytest.raises(CoreConfigMissingFieldError) as exc_info:
        CoreConfigLoader().load_from_mapping(data=document)

    assert exc_info.value.field_path == expected_missing_path


@pytest.mark.parametrize(
    ("field_path", "invalid_value"),
    [
        ("schema_version", "1"),
        ("input_directory_max_depth", 3.0),
        ("ncbi_api_key", 7),
        ("ncbi_max_retries", 3.0),
        ("default_alignment_mode", 1),
        ("data.directory", False),
        ("execution.max_parallel_tasks", 1.0),
        ("execution.scheduler_poll_interval_seconds", 1),
        ("logging.level", 1),
        ("logging.include_diagnostics", 0),
        ("logging.diagnostic_field_limit", 8_192.0),
        ("tools.mafft.executable", False),
    ],
)
def test_loader_rejects_non_strict_scalar_types(
    field_path: str,
    invalid_value: object,
) -> None:
    document = build_default_core_config_document()
    _set_document_value(document, field_path=field_path, value=invalid_value)

    with pytest.raises(CoreConfigValidationError) as exc_info:
        CoreConfigLoader().load_from_mapping(data=document)

    assert not isinstance(exc_info.value, CoreConfigMissingFieldError)


def test_validate_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    document = build_default_core_config_document()
    document["schema_version"] = 99
    _write_config_document(tmp_path / "home", document)

    with pytest.raises(UnsupportedCoreConfigSchemaVersionError):
        service.validate_current_config()


def test_validate_rejects_invalid_max_workers(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    document = build_default_core_config_document()
    _set_document_value(document, field_path="execution.max_parallel_tasks", value=0)
    _write_config_document(tmp_path / "home", document)

    with pytest.raises(CoreConfigInvalidValueError):
        service.validate_current_config()


def test_validate_rejects_workers_above_detected_cpu_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jelica_core.system_config.resolver.detect_available_logical_cpu_count",
        lambda: 2,
    )
    service = _service_with_home(tmp_path / "home")
    document = build_default_core_config_document(max_parallel_tasks=3)
    _write_config_document(tmp_path / "home", document)

    with pytest.raises(CoreConfigInvalidValueError) as exc_info:
        service.validate_current_config()

    assert exc_info.value.parameter == "execution.max_parallel_tasks"
    assert "detected available logical CPU count (2)" in exc_info.value.detail


def test_detect_available_logical_cpu_count_uses_most_restrictive_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_config_resources.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(
        system_config_resources,
        "_detect_process_affinity_cpu_count",
        lambda: 4,
    )
    monkeypatch.setattr(
        system_config_resources,
        "_detect_cgroup_cpu_quota_count",
        lambda: 2,
    )

    assert system_config_resources.detect_available_logical_cpu_count() == 2


def test_detect_available_logical_cpu_count_falls_back_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_config_resources.os, "cpu_count", lambda: None)
    monkeypatch.setattr(
        system_config_resources,
        "_detect_process_affinity_cpu_count",
        lambda: None,
    )
    monkeypatch.setattr(
        system_config_resources,
        "_detect_cgroup_cpu_quota_count",
        lambda: None,
    )

    assert system_config_resources.detect_available_logical_cpu_count() == 1


def test_validate_rejects_invalid_logging_level(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    document = build_default_core_config_document()
    _set_document_value(document, field_path="logging.level", value="TRACE")
    _write_config_document(tmp_path / "home", document)

    with pytest.raises(CoreConfigInvalidValueError):
        service.validate_current_config()


def test_validate_rejects_empty_data_directory(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    document = build_default_core_config_document()
    _set_document_value(document, field_path="data.directory", value="")
    _write_config_document(tmp_path / "home", document)

    with pytest.raises(CoreConfigInvalidValueError):
        service.validate_current_config()


def test_relative_data_directory_is_resolved_against_jelica_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config(data_directory="custom-data")

    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir(parents=True)
    monkeypatch.chdir(other_cwd)
    resolved = service.load_resolved_config()

    assert resolved.data_dir == tmp_path / "home" / "custom-data"


def test_absolute_data_directory_is_used_directly(tmp_path: Path) -> None:
    absolute_data_dir = tmp_path / "absolute-data"
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config(data_directory=str(absolute_data_dir))

    resolved = service.load_resolved_config()

    assert resolved.data_dir == absolute_data_dir
    assert resolved.tasks_dir == absolute_data_dir / "tasks"
    assert resolved.temp_dir == absolute_data_dir / "temp"
    assert resolved.logs_dir == absolute_data_dir / "logs"
    assert resolved.database_path == absolute_data_dir / "jelica.db"


def test_writer_serialize_produces_canonical_toml_order_and_trailing_newline() -> None:
    loader = CoreConfigLoader()
    writer = CoreConfigWriter()
    document = build_default_core_config_document()
    execution = document["execution"]
    assert isinstance(execution, dict)
    execution["max_workers"] = execution.pop("max_parallel_tasks")
    config_input = loader.load_from_mapping(data=document)

    serialized = writer.serialize(config_input=config_input)

    assert serialized.startswith("schema_version = 1\n")
    assert "[data]\n" in serialized
    assert "[execution]\n" in serialized
    assert "[logging]\n" in serialized
    assert serialized.endswith("\n")
    assert serialized.index("schema_version") < serialized.index("[data]")
    assert serialized.index("[data]") < serialized.index("[execution]")
    assert serialized.index("[execution]") < serialized.index("[logging]")
    assert "max_parallel_tasks = 1" in serialized
    assert "max_workers" not in serialized
    assert config_input.execution.max_workers == 1


def test_atomic_write_removes_temporary_file_on_success(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()
    config_dir = service.get_config_path().parent

    temp_files = list(config_dir.glob("config.*.tmp"))

    assert temp_files == []


def test_atomic_write_preserves_old_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()
    config_path = service.get_config_path()
    before = config_path.read_text(encoding="utf-8")

    def _failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("jelica_core.system_config.writer.os.replace", _failing_replace)

    with pytest.raises(CoreConfigWriteError):
        service.set_parameter(parameter="logging.level", value="ERROR")

    after = config_path.read_text(encoding="utf-8")
    temp_files = list(config_path.parent.glob("config.*.tmp"))
    assert before == after
    assert temp_files == []


def test_set_updates_each_supported_parameter(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    updated_data = service.set_parameter(parameter="data.directory", value="custom-data")
    updated_workers = service.set_parameter(parameter="execution.max_workers", value="4")
    updated_logging = service.set_parameter(parameter="logging.level", value="warning")
    updated_depth = service.set_parameter(parameter="input_directory_max_depth", value="1")
    updated_api_key = service.set_parameter(parameter="ncbi_api_key", value="secret-key")
    updated_retries = service.set_parameter(parameter="ncbi_max_retries", value="5")
    updated_alignment = service.set_parameter(parameter="default_alignment_mode", value="none")

    assert updated_data.data_dir == tmp_path / "home" / "custom-data"
    assert updated_workers.max_workers == 4
    assert updated_logging.log_level == "WARNING"
    assert updated_depth.input_directory_max_depth == 1
    assert updated_api_key.ncbi_api_key == "secret-key"
    assert updated_retries.ncbi_max_retries == 5
    assert updated_alignment.default_alignment_mode == "none"


def test_set_accepts_short_parameter_aliases(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    updated_data = service.set_parameter(parameter="data_dir", value="custom-data")
    updated_workers = service.set_parameter(parameter="max_workers", value="6")
    updated_logging = service.set_parameter(parameter="log_level", value="error")

    assert updated_data.data_dir == tmp_path / "home" / "custom-data"
    assert updated_workers.max_workers == 6
    assert updated_logging.log_level == "ERROR"


def test_set_rejects_unknown_parameter(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    with pytest.raises(CoreConfigUnknownParameterError):
        service.set_parameter(parameter="unknown.value", value="1")


def test_set_rejects_schema_version_change(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    with pytest.raises(CoreConfigParameterNotMutableError):
        service.set_parameter(parameter="schema_version", value="2")


def test_set_rejects_empty_value(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="data.directory", value=" ")


def test_set_rejects_invalid_max_workers_value(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="execution.max_workers", value="abc")


def test_set_rejects_invalid_runtime_intervals_and_recovery_values(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="execution.scheduler_poll_interval_seconds", value="0")
    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="execution.heartbeat_interval_seconds", value="-1")
    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="execution.lease_timeout_seconds", value="abc")
    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="execution.lease_timeout_seconds", value="0.5")
    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="execution.max_recovery_attempts", value="-1")
    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="input_directory_max_depth", value="-1")
    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="ncbi_max_retries", value="-1")
    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="default_alignment_mode", value="custom")


def test_set_preserves_old_file_on_invalid_value(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()
    config_path = service.get_config_path()
    before = config_path.read_text(encoding="utf-8")

    with pytest.raises(CoreConfigInvalidValueError):
        service.set_parameter(parameter="execution.max_workers", value="0")

    after = config_path.read_text(encoding="utf-8")
    assert before == after


def test_unset_reverts_parameter_to_physical_generation_default(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config(data_directory="custom-data", max_workers=3, log_level="ERROR")
    service.set_parameter(parameter="input_directory_max_depth", value="1")
    service.set_parameter(parameter="ncbi_api_key", value="secret-key")
    service.set_parameter(parameter="ncbi_max_retries", value="5")
    service.set_parameter(parameter="default_alignment_mode", value="none")

    after_data = service.unset_parameter(parameter="data.directory")
    after_workers = service.unset_parameter(parameter="execution.max_workers")
    after_logging = service.unset_parameter(parameter="logging.level")
    after_depth = service.unset_parameter(parameter="input_directory_max_depth")
    after_api_key = service.unset_parameter(parameter="ncbi_api_key")
    after_retries = service.unset_parameter(parameter="ncbi_max_retries")
    after_alignment = service.unset_parameter(parameter="default_alignment_mode")
    config_document = tomllib.loads(service.get_config_path().read_text(encoding="utf-8"))

    assert after_data.data_dir == tmp_path / "home" / "data"
    assert after_workers.max_workers == 1
    assert after_logging.log_level == "INFO"
    assert after_depth.input_directory_max_depth == 3
    assert after_api_key.ncbi_api_key == ""
    assert after_retries.ncbi_max_retries == 3
    assert after_alignment.default_alignment_mode == "compute"
    assert config_document["data"]["directory"] == "data"
    assert config_document["execution"]["max_parallel_tasks"] == 1
    assert config_document["logging"]["level"] == "INFO"
    assert config_document["input_directory_max_depth"] == 3
    assert config_document["ncbi_api_key"] == ""
    assert config_document["ncbi_max_retries"] == 3
    assert config_document["default_alignment_mode"] == "compute"
    assert _document_leaf_paths(config_document) == set(core_config_field_paths())


def test_unset_accepts_short_parameter_aliases(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config(data_directory="custom-data", max_workers=3, log_level="ERROR")

    after_data = service.unset_parameter(parameter="data_dir")
    after_workers = service.unset_parameter(parameter="max_workers")
    after_logging = service.unset_parameter(parameter="log_level")

    assert after_data.data_dir == tmp_path / "home" / "data"
    assert after_workers.max_workers == 1
    assert after_logging.log_level == "INFO"


def test_unset_rejects_schema_version(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    with pytest.raises(CoreConfigParameterNotRemovableError):
        service.unset_parameter(parameter="schema_version")


def test_unset_rejects_unknown_parameter(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    with pytest.raises(CoreConfigUnknownParameterError):
        service.unset_parameter(parameter="unknown.value")


def test_unset_rejects_parameter_already_at_generation_default(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()

    with pytest.raises(CoreConfigParameterAlreadyUnsetError) as exc_info:
        service.unset_parameter(parameter="logging.level")

    assert str(exc_info.value) == "Parameter 'logging.level' is already set to its default value."

    service.set_parameter(parameter="logging.level", value="ERROR")
    service.unset_parameter(parameter="logging.level")
    with pytest.raises(CoreConfigParameterAlreadyUnsetError):
        service.unset_parameter(parameter="logging.level")


def test_unset_does_not_delete_user_data_from_previous_data_directory(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    custom_data_dir = tmp_path / "custom-data"
    service.initialize_system_config(data_directory=str(custom_data_dir))
    user_file = custom_data_dir / "tasks" / "task-x" / "config.json"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("{}", encoding="utf-8")

    service.unset_parameter(parameter="data.directory")

    assert user_file.exists()


def test_initialize_raises_directory_creation_error_when_home_path_is_file(tmp_path: Path) -> None:
    fake_home = tmp_path / "home-file"
    fake_home.write_text("not-a-directory", encoding="utf-8")
    service = _service_with_home(fake_home)

    with pytest.raises(CoreWorkingDirectoryCreationError):
        service.initialize_system_config()


def test_core_config_input_has_strict_unknown_field_rejection() -> None:
    document = build_default_core_config_document()
    data = document["data"]
    assert isinstance(data, dict)
    data["extra"] = True

    with pytest.raises(CoreConfigValidationError):
        CoreConfigLoader().load_from_mapping(data=document)


def test_writer_serialization_is_utf8_compatible() -> None:
    writer = CoreConfigWriter()
    config_input = CoreConfigInput.model_validate(build_default_core_config_document())

    encoded = writer.serialize(config_input=config_input).encode("utf-8")

    assert isinstance(encoded, bytes)


def test_required_empty_string_defaults_resolve_to_effective_values(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")

    resolved = service.initialize_system_config()
    config_document = tomllib.loads(service.get_config_path().read_text(encoding="utf-8"))

    assert resolved.ncbi_api_key == ""
    assert resolved.system_log_level == "INFO"
    assert resolved.task_log_level == "INFO"
    assert resolved.mafft_executable is None
    assert config_document["ncbi_api_key"] == ""
    assert config_document["logging"]["system_level"] == ""
    assert config_document["logging"]["task_level"] == ""
    assert config_document["tools"]["mafft"]["executable"] == ""


def test_load_resolves_configured_mafft_executable_without_checking_it(tmp_path: Path) -> None:
    service = _service_with_home(tmp_path / "home")
    missing_executable = tmp_path / "does-not-exist" / "mafft"
    document = build_default_core_config_document()
    _set_document_value(
        document,
        field_path="tools.mafft.executable",
        value=f"  {missing_executable}  ",
    )
    _write_config_document(tmp_path / "home", document)

    resolved = service.load_resolved_config()

    assert resolved.mafft_executable == str(missing_executable)


def test_mafft_executable_serialization_and_strict_nested_validation() -> None:
    loader = CoreConfigLoader()
    writer = CoreConfigWriter()
    document = build_default_core_config_document()
    _set_document_value(
        document,
        field_path="tools.mafft.executable",
        value="/opt/mafft/bin/mafft",
    )
    config_input = loader.load_from_mapping(data=document)

    serialized = writer.serialize(config_input=config_input)

    assert '[tools.mafft]\nexecutable = "/opt/mafft/bin/mafft"\n' in serialized
    invalid_document = build_default_core_config_document()
    mafft = invalid_document["tools"]
    assert isinstance(mafft, dict)
    mafft = mafft["mafft"]
    assert isinstance(mafft, dict)
    mafft["unknown"] = "value"
    with pytest.raises(CoreConfigValidationError):
        loader.load_from_mapping(data=invalid_document)


@pytest.mark.parametrize(
    "parameter",
    [
        "tools.mafft.executable",
        "mafft.executable",
        "mafft_executable",
        "mafft-executable",
    ],
)
def test_set_and_unset_mafft_executable_aliases(tmp_path: Path, parameter: str) -> None:
    service = _service_with_home(tmp_path / parameter.replace(".", "-").replace("_", "-"))
    service.initialize_system_config()

    updated = service.set_parameter(parameter=parameter, value="custom-mafft")
    config_text = service.get_config_path().read_text(encoding="utf-8")

    assert updated.mafft_executable == "custom-mafft"
    assert '[tools.mafft]\nexecutable = "custom-mafft"\n' in config_text

    reverted = service.unset_parameter(parameter=parameter)

    assert reverted.mafft_executable is None
    assert '[tools.mafft]\nexecutable = ""\n' in service.get_config_path().read_text(
        encoding="utf-8"
    )


def test_mafft_executable_propagates_through_runtime_and_worker_launch_spec(
    tmp_path: Path,
) -> None:
    service = _service_with_home(tmp_path / "home")
    service.initialize_system_config()
    resolved = service.set_parameter(parameter="mafft_executable", value="custom-mafft")

    runtime_config = RuntimeConfig.from_resolved_config(resolved)
    launch_spec = WorkerLaunchSpec(
        task_id="task-1",
        job_id="job-1",
        worker_instance_id="worker-1",
        lease_token="lease-1",
        database_path=resolved.database_path,
        task_dir=resolved.tasks_dir / "task-1",
        job_dir=resolved.tasks_dir / "task-1" / "jobs" / "job-1",
        config_revision_path=resolved.tasks_dir / "task-1" / "config.json",
        config_hash="hash",
        runtime_state_json="{}",
        pipeline_name="initialize_only",
        pipeline_version="v1",
        mafft_executable=runtime_config.mafft_executable,
    )

    assert runtime_config.mafft_executable == "custom-mafft"
    assert launch_spec.mafft_executable == "custom-mafft"
