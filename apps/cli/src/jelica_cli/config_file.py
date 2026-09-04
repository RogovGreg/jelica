from __future__ import annotations

import stat
from pathlib import Path


class ConfigFileReadError(RuntimeError):
    """Base CLI error for config-file loading failures."""


class ConfigFileNotFoundError(ConfigFileReadError):
    """Raised when config file does not exist."""

    def __init__(self, *, path: Path) -> None:
        self.path = path
        super().__init__(f"Config file not found: '{path}'.")


class ConfigFileNotRegularFileError(ConfigFileReadError):
    """Raised when config path is not a regular file."""

    def __init__(self, *, path: Path) -> None:
        self.path = path
        super().__init__(f"Config path is not a regular file: '{path}'.")


class ConfigFileNotReadableError(ConfigFileReadError):
    """Raised when config file cannot be read."""

    def __init__(self, *, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"Cannot read config file '{path}': {detail}.")


class ConfigFileEncodingError(ConfigFileReadError):
    """Raised when config file is not valid UTF-8."""

    def __init__(self, *, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"Config file '{path}' is not valid UTF-8: {detail}.")


def read_config_file_text(path: Path) -> str:
    try:
        stat_result = path.stat()
    except FileNotFoundError as error:
        raise ConfigFileNotFoundError(path=path) from error
    except OSError as error:
        raise ConfigFileNotReadableError(path=path, detail=str(error)) from error

    if not stat.S_ISREG(stat_result.st_mode):
        raise ConfigFileNotRegularFileError(path=path)

    try:
        raw_content = path.read_bytes()
    except OSError as error:
        raise ConfigFileNotReadableError(path=path, detail=str(error)) from error

    try:
        return raw_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigFileEncodingError(path=path, detail=str(error)) from error
