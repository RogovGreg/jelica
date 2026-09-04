from __future__ import annotations

from pathlib import Path


class CoreConfigError(ValueError):
    """Base domain error for JELICA Core system configuration."""


class CoreConfigMissingError(CoreConfigError):
    """Raised when the system configuration file does not exist."""

    def __init__(self, *, path: Path) -> None:
        self.path = path
        super().__init__(f"System config file is missing: '{path}'.")


class CoreNotInitializedError(CoreConfigError):
    """Raised when an operation requires initialized Core config."""

    def __init__(self, *, path: Path) -> None:
        self.path = path
        super().__init__(
            "JELICA Core is not initialized. "
            f"System config file was not found at '{path}'. "
            "Run 'jelica config init'."
        )


class CoreConfigAlreadyExistsError(CoreConfigError):
    """Raised when initialization is requested but config already exists."""

    def __init__(self, *, path: Path) -> None:
        self.path = path
        super().__init__(f"System config already exists: '{path}'. Use '--force' to overwrite it.")


class CoreConfigReadError(CoreConfigError):
    """Raised when reading the config file fails."""

    def __init__(self, *, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"Cannot read system config '{path}': {detail}.")


class CoreConfigInvalidTomlError(CoreConfigError):
    """Raised when TOML syntax is invalid."""

    def __init__(self, *, path: Path, line: int | None, column: int | None, detail: str) -> None:
        self.path = path
        self.line = line
        self.column = column
        self.detail = detail

        location_parts: list[str] = []
        if line is not None:
            location_parts.append(f"line {line}")
        if column is not None:
            location_parts.append(f"column {column}")
        location = ", ".join(location_parts)

        if location == "":
            message = f"Invalid TOML in system config '{path}': {detail}."
        else:
            message = f"Invalid TOML in system config '{path}' at {location}: {detail}."
        super().__init__(message)


class CoreConfigInvalidRootTypeError(CoreConfigError):
    """Raised when loaded TOML root is not an object/table."""

    def __init__(self, *, root_type: str) -> None:
        self.root_type = root_type
        super().__init__(f"System config root must be a TOML table, got {root_type}.")


class CoreConfigValidationError(CoreConfigError):
    """Raised when structure or field-level validation fails."""

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__(f"System config validation failed: {detail}.")


class CoreConfigUnknownFieldError(CoreConfigValidationError):
    """Raised when an unknown field or section is present."""

    def __init__(self, *, field_path: str) -> None:
        self.field_path = field_path
        super().__init__(detail=f"unknown field '{field_path}'")


class CoreConfigMissingFieldError(CoreConfigValidationError):
    """Raised when a required persisted field or section is absent."""

    def __init__(self, *, field_path: str) -> None:
        self.field_path = field_path
        super().__init__(detail=f"missing required field '{field_path}'")


class UnsupportedCoreConfigSchemaVersionError(CoreConfigError):
    """Raised when schema_version is unsupported."""

    def __init__(self, *, schema_version: int, supported_version: int) -> None:
        self.schema_version = schema_version
        self.supported_version = supported_version
        super().__init__(
            "Unsupported system config schema_version "
            f"{schema_version}. Supported schema_version is {supported_version}."
        )


class CoreConfigInvalidValueError(CoreConfigError):
    """Raised when a known parameter value is invalid."""

    def __init__(self, *, parameter: str, detail: str) -> None:
        self.parameter = parameter
        self.detail = detail
        super().__init__(f"Invalid value for '{parameter}': {detail}.")


class CoreConfigPathResolutionError(CoreConfigError):
    """Raised when path resolution fails."""

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Cannot resolve system config paths: {detail}.")


class CoreConfigUnknownParameterError(CoreConfigError):
    """Raised when config set/unset targets an unsupported parameter."""

    def __init__(self, *, parameter: str) -> None:
        self.parameter = parameter
        super().__init__(f"Unknown system config parameter '{parameter}'.")


class CoreConfigParameterNotMutableError(CoreConfigError):
    """Raised when attempting to modify an immutable parameter."""

    def __init__(self, *, parameter: str) -> None:
        self.parameter = parameter
        super().__init__(f"Parameter '{parameter}' cannot be changed with this operation.")


class CoreConfigParameterNotRemovableError(CoreConfigError):
    """Raised when attempting to unset a required parameter."""

    def __init__(self, *, parameter: str) -> None:
        self.parameter = parameter
        super().__init__(f"Parameter '{parameter}' cannot be removed.")


class CoreConfigParameterAlreadyUnsetError(CoreConfigError):
    """Raised when unsetting a parameter that already has its generated default."""

    def __init__(self, *, parameter: str) -> None:
        self.parameter = parameter
        super().__init__(f"Parameter '{parameter}' is already set to its default value.")


class CoreConfigWriteError(CoreConfigError):
    """Raised when config cannot be written atomically."""

    def __init__(self, *, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"Cannot write system config '{path}' atomically: {detail}.")


class CoreWorkingDirectoryCreationError(CoreConfigError):
    """Raised when required runtime directories cannot be created."""

    def __init__(self, *, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"Cannot create required directory '{path}': {detail}.")
