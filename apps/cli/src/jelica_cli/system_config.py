from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from jelica_core.system_config import (
    CoreConfigError,
    CoreConfigInput,
    CoreConfigInvalidRootTypeError,
    CoreConfigInvalidTomlError,
    CoreConfigInvalidValueError,
    CoreConfigLoader,
    CoreConfigMissingError,
    CoreConfigMissingFieldError,
    CoreConfigParameterAlreadyUnsetError,
    CoreConfigReadError,
    CoreConfigResolver,
    CoreConfigService,
    CoreConfigUnknownFieldError,
    CoreConfigUnknownParameterError,
    CoreConfigValidationError,
    CoreConfigWriteError,
    CoreConfigWriter,
    ResolvedCoreConfig,
    core_config_top_level_keys,
    to_toml_document,
)

CLI_SECTION_NAME: Final = "cli"
DEFAULT_CLI_COLOR: Final = True
DEFAULT_CLI_EMOJI: Final = True
_CLI_PARAMETERS: Final = frozenset({"cli.color", "cli.emoji"})


class CliConfig(BaseModel):
    """Required CLI-owned fragment of persisted config.toml."""

    model_config = ConfigDict(extra="forbid")

    color: StrictBool
    emoji: StrictBool


@dataclass(frozen=True, slots=True)
class LoadedSystemConfig:
    """One validated snapshot of the shared system configuration document."""

    document: dict[str, object]
    core_input: CoreConfigInput
    resolved_core: ResolvedCoreConfig
    cli: CliConfig


def build_default_cli_config_document() -> dict[str, object]:
    """Build the complete CLI-owned fragment used only for generation/reset."""

    return {
        "color": DEFAULT_CLI_COLOR,
        "emoji": DEFAULT_CLI_EMOJI,
    }


class CliSystemConfigService:
    """CLI composition root for the Core-owned and CLI-owned TOML fragments."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        jelica_home: Path | None = None,
        on_loaded: Callable[[CliConfig], None] | None = None,
    ) -> None:
        self._on_loaded = on_loaded
        self._loaded: LoadedSystemConfig | None = None
        self._core_loader = CoreConfigLoader()
        self._core_resolver = CoreConfigResolver()
        self._core_service = _ComposedCoreConfigService(
            owner=self,
            environment=environment,
            jelica_home=jelica_home,
        )

    @property
    def core_service(self) -> CoreConfigService:
        return self._core_service

    def get_config_path(self) -> Path:
        return self._core_service.get_config_path()

    def load(self) -> LoadedSystemConfig:
        if self._loaded is not None:
            return self._loaded

        config_path = self.get_config_path()
        document = _read_toml_document(config_path=config_path)
        loaded = self._validate_document(document=document, config_path=config_path)
        self._set_loaded(loaded)
        return loaded

    def load_resolved_core_config(self) -> ResolvedCoreConfig:
        return self.load().resolved_core

    def show_document(self) -> dict[str, object]:
        document = deepcopy(self.load().document)
        raw_api_key = document.get("ncbi_api_key")
        if isinstance(raw_api_key, str):
            document["ncbi_api_key"] = (
                "<configured>" if raw_api_key.strip() != "" else "<not configured>"
            )
        return document

    def _load_core_fragment(self, *, config_path: Path) -> CoreConfigInput:
        expected_path = self.get_config_path()
        if config_path != expected_path:
            raise CoreConfigReadError(
                path=config_path,
                detail=f"unexpected config path; expected '{expected_path}'",
            )
        return self.load().core_input

    def _write_core_fragment(
        self,
        *,
        config_path: Path,
        config_input: CoreConfigInput,
    ) -> None:
        cli_config = (
            self._loaded.cli
            if self._loaded is not None
            else CliConfig.model_validate(build_default_cli_config_document())
        )
        self._write_combined(
            config_path=config_path,
            config_input=config_input,
            cli_config=cli_config,
        )

    def _set_cli_parameter(self, *, parameter: str, value: str) -> None:
        normalized_parameter = parameter.strip()
        if normalized_parameter not in _CLI_PARAMETERS:
            raise CoreConfigUnknownParameterError(parameter=parameter)
        parsed_value = _parse_cli_bool(parameter=normalized_parameter, value=value)
        loaded = self.load()
        field_name = normalized_parameter.removeprefix("cli.")
        updated_cli = loaded.cli.model_copy(update={field_name: parsed_value})
        self._write_combined(
            config_path=self.get_config_path(),
            config_input=loaded.core_input,
            cli_config=updated_cli,
        )

    def _unset_cli_parameter(self, *, parameter: str) -> None:
        normalized_parameter = parameter.strip()
        if normalized_parameter not in _CLI_PARAMETERS:
            raise CoreConfigUnknownParameterError(parameter=parameter)
        loaded = self.load()
        field_name = normalized_parameter.removeprefix("cli.")
        default_value = build_default_cli_config_document()[field_name]
        if getattr(loaded.cli, field_name) == default_value:
            raise CoreConfigParameterAlreadyUnsetError(parameter=normalized_parameter)
        updated_cli = loaded.cli.model_copy(update={field_name: default_value})
        self._write_combined(
            config_path=self.get_config_path(),
            config_input=loaded.core_input,
            cli_config=updated_cli,
        )

    def _write_combined(
        self,
        *,
        config_path: Path,
        config_input: CoreConfigInput,
        cli_config: CliConfig,
    ) -> LoadedSystemConfig:
        document = to_toml_document(config_input)
        document[CLI_SECTION_NAME] = cli_config.model_dump(mode="python")
        loaded = self._validate_document(document=document, config_path=config_path)
        loaded.core_input._notification_document = {
            key: deepcopy(document[key])
            for key in ("notifications", "desktop")
            if key in document
        }
        payload = _serialize_combined_document(
            core_input=loaded.core_input,
            cli_config=loaded.cli,
        )
        _write_text_atomically(config_path=config_path, payload=payload)
        self._set_loaded(loaded)
        return loaded

    def _validate_document(
        self,
        *,
        document: dict[str, object],
        config_path: Path,
    ) -> LoadedSystemConfig:
        core_keys = set(core_config_top_level_keys())
        allowed_keys = core_keys | {CLI_SECTION_NAME, "notifications", "desktop"}
        unknown_keys = sorted(set(document) - allowed_keys)
        if unknown_keys:
            raise CoreConfigUnknownFieldError(field_path=unknown_keys[0])

        core_document = {key: document[key] for key in core_keys if key in document}
        core_input = self._core_loader.load_from_mapping(data=core_document)
        # Preserve the complete notification/desktop fragments for Core-side
        # presentation adapters.  These sections are intentionally outside
        # the validated CoreConfigInput model, but remain part of the shared
        # canonical TOML document consumed by the local notification service.
        core_input._notification_document = {
            key: deepcopy(document[key])
            for key in ("notifications", "desktop")
            if key in document
        }
        resolved_core = self._core_resolver.resolve(
            config_input=core_input,
            config_path=config_path,
        )

        if CLI_SECTION_NAME not in document:
            raise CoreConfigMissingFieldError(field_path=CLI_SECTION_NAME)
        raw_cli = document[CLI_SECTION_NAME]
        if not isinstance(raw_cli, dict):
            raise CoreConfigValidationError(detail="cli: value must be a TOML table")
        cli_config = _validate_cli_mapping(raw_cli)
        return LoadedSystemConfig(
            document=deepcopy(document),
            core_input=core_input,
            resolved_core=resolved_core,
            cli=cli_config,
        )

    def _set_loaded(self, loaded: LoadedSystemConfig) -> None:
        self._loaded = loaded
        if self._on_loaded is not None:
            self._on_loaded(loaded.cli)


class _CoreFragmentLoader(CoreConfigLoader):
    def __init__(self, *, owner: CliSystemConfigService) -> None:
        self._owner = owner

    def load(self, *, config_path: Path) -> CoreConfigInput:
        return self._owner._load_core_fragment(config_path=config_path)


class _CombinedCoreConfigWriter(CoreConfigWriter):
    def __init__(self, *, owner: CliSystemConfigService) -> None:
        self._owner = owner

    def write(self, *, config_path: Path, config_input: CoreConfigInput) -> None:
        self._owner._write_core_fragment(
            config_path=config_path,
            config_input=config_input,
        )


class _ComposedCoreConfigService(CoreConfigService):
    def __init__(
        self,
        *,
        owner: CliSystemConfigService,
        environment: Mapping[str, str] | None,
        jelica_home: Path | None,
    ) -> None:
        self._owner = owner
        super().__init__(
            environment=environment,
            jelica_home=jelica_home,
            loader=_CoreFragmentLoader(owner=owner),
            writer=_CombinedCoreConfigWriter(owner=owner),
        )

    def set_parameter(self, *, parameter: str, value: str) -> ResolvedCoreConfig:
        if parameter.strip().startswith("cli."):
            self._owner._set_cli_parameter(parameter=parameter, value=value)
            return self.load_resolved_config()
        return super().set_parameter(parameter=parameter, value=value)

    def initialize_system_config(
        self,
        *,
        data_directory: str | None = None,
        max_parallel_tasks: int | None = None,
        max_workers: int | None = None,
        log_level: str | None = None,
        force: bool = False,
    ) -> ResolvedCoreConfig:
        config_path = self.get_config_path()
        _ = force
        self._ensure_directory(config_path.parent)

        effective_max_parallel_tasks = (
            max_parallel_tasks if max_parallel_tasks is not None else max_workers
        )
        candidate_core_input = self._build_initialized_input(
            data_directory=data_directory,
            max_parallel_tasks=effective_max_parallel_tasks,
            log_level=log_level,
        )

        if config_path.exists():
            try:
                existing_resolved = self.load_resolved_config()
            except CoreConfigError:
                pass
            else:
                self._ensure_runtime_directories(existing_resolved)
                self._initialize_task_registry_database(
                    database_path=existing_resolved.database_path
                )
                return existing_resolved

        candidate_cli_config = CliConfig.model_validate(build_default_cli_config_document())
        loaded = self._owner._write_combined(
            config_path=config_path,
            config_input=candidate_core_input,
            cli_config=candidate_cli_config,
        )
        resolved_candidate = loaded.resolved_core
        self._ensure_runtime_directories(resolved_candidate)
        self._initialize_task_registry_database(database_path=resolved_candidate.database_path)
        return resolved_candidate

    def unset_parameter(self, *, parameter: str) -> ResolvedCoreConfig:
        if parameter.strip().startswith("cli."):
            self._owner._unset_cli_parameter(parameter=parameter)
            return self.load_resolved_config()
        return super().unset_parameter(parameter=parameter)


def _read_toml_document(*, config_path: Path) -> dict[str, object]:
    try:
        raw_bytes = config_path.read_bytes()
    except FileNotFoundError as error:
        raise CoreConfigMissingError(path=config_path) from error
    except OSError as error:
        raise CoreConfigReadError(path=config_path, detail=str(error)) from error

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CoreConfigReadError(
            path=config_path,
            detail=f"invalid UTF-8: {error}",
        ) from error

    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise CoreConfigInvalidTomlError(
            path=config_path,
            line=getattr(error, "lineno", None),
            column=getattr(error, "colno", None),
            detail=str(error),
        ) from error
    if not isinstance(document, dict):
        raise CoreConfigInvalidRootTypeError(root_type=type(document).__name__)
    return document


def _validate_cli_mapping(data: dict[str, object]) -> CliConfig:
    try:
        return CliConfig.model_validate(data)
    except ValidationError as error:
        details = error.errors(include_url=False)
        if not details:
            raise CoreConfigValidationError(detail="cli: unknown validation error") from error
        first_detail = details[0]
        location_parts = (CLI_SECTION_NAME, *first_detail.get("loc", ()))
        location = ".".join(str(part) for part in location_parts)
        error_type = str(first_detail.get("type", ""))
        if error_type == "missing":
            raise CoreConfigMissingFieldError(field_path=location) from error
        if error_type == "extra_forbidden":
            raise CoreConfigUnknownFieldError(field_path=location) from error
        message = str(first_detail.get("msg", "invalid value"))
        raise CoreConfigValidationError(detail=f"{location}: {message}") from error


def _parse_cli_bool(*, parameter: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise CoreConfigInvalidValueError(
        parameter=parameter,
        detail="value must be a boolean (true/false)",
    )


def _serialize_combined_document(
    *,
    core_input: CoreConfigInput,
    cli_config: CliConfig,
) -> str:
    core_payload = CoreConfigWriter().serialize(config_input=core_input).rstrip("\n")
    color = str(cli_config.color).lower()
    emoji = str(cli_config.emoji).lower()
    return f"{core_payload}\n\n[cli]\ncolor = {color}\nemoji = {emoji}\n"


def _write_text_atomically(*, config_path: Path, payload: str) -> None:
    temp_file_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=config_path.parent,
            prefix="config.",
            suffix=".tmp",
        ) as temp_file:
            temp_file.write(payload)
            temp_file_path = Path(temp_file.name)
        os.replace(temp_file_path, config_path)
    except OSError as error:
        if temp_file_path is not None:
            temp_file_path.unlink(missing_ok=True)
        raise CoreConfigWriteError(path=config_path, detail=str(error)) from error


__all__ = [
    "CLI_SECTION_NAME",
    "DEFAULT_CLI_COLOR",
    "DEFAULT_CLI_EMOJI",
    "CliConfig",
    "CliSystemConfigService",
    "LoadedSystemConfig",
    "build_default_cli_config_document",
]
